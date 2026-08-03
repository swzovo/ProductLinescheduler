from __future__ import annotations

import base64
import ctypes
import hashlib
import json
import os
import platform
import re
import secrets
import ssl
import tempfile
import urllib.request
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx
from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from fastapi import (
    APIRouter,
    File,
    Form,
    HTTPException,
    Query,
    Response,
    UploadFile,
)
from pydantic import BaseModel, Field

from .database import (
    connect,
    database_path,
    install_database_snapshot,
    temporary_database_backup,
)


ENV_ID = os.environ.get(
    "CLOUDBASE_ENV_ID",
    "production-schedule-test-d73e723",
)
REGION = os.environ.get("CLOUDBASE_REGION", "ap-shanghai")
PUBLISHABLE_KEY = os.environ.get("CLOUDBASE_PUBLISHABLE_KEY", "")
STORAGE_BUCKET_ID = os.environ.get(
    "CLOUDBASE_STORAGE_BUCKET_ID",
    "7072-production-schedule-test-d73e723-1460691865",
)
STORAGE_MODE = os.environ.get("CLOUDBASE_STORAGE_MODE", "classic")
MAX_SNAPSHOT_BYTES = 256 * 1024 * 1024
MAGIC = b"PLSYNC1\x00"
KEY_PREFIX = "PLS1."
# The final 3.5.8 package uses a fresh namespace because the previous
# ad-hoc-signed build bound its Keychain ACL to that exact binary. Cloud data
# remains recoverable with the user-saved recovery key.
KEYRING_SERVICE = "com.local.production-line-scheduler.cloud-v6"
USER_ID_PATTERN = re.compile(r"^[^\x00-\x1f]{1,256}$")
CLOUD_PATH_PATTERN = re.compile(r"^[^\x00-\x1f\\]{1,2048}$")
CLOUD_API_VERSION = "2020-01-10"

router = APIRouter(prefix="/api/cloud-sync", tags=["cloud-sync"])
_UPLOAD_STATUS: dict[str, Any] = {
    "stage": "not_called",
    "updated_at": None,
}


class UserPayload(BaseModel):
    user_id: str = Field(min_length=1, max_length=256)


class RecoveryKeyPayload(UserPayload):
    recovery_key: str = Field(min_length=20, max_length=200)


class SessionPayload(UserPayload):
    access_token: str = Field(min_length=20, max_length=8192)
    refresh_token: str = Field(min_length=20, max_length=8192)
    display_name: str | None = Field(default=None, max_length=200)


class SyncStatePayload(UserPayload):
    last_revision_id: str | None = Field(default=None, max_length=200)
    last_plain_sha256: str | None = Field(default=None, max_length=64)
    last_sync_at: str | None = Field(default=None, max_length=80)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _validate_user_id(user_id: str) -> str:
    value = user_id.strip()
    if not USER_ID_PATTERN.fullmatch(value):
        raise HTTPException(status_code=422, detail="云端用户标识无效")
    return value


def _state_path() -> Path:
    return database_path().parent / "cloud_sync_state.json"


def _load_state() -> dict[str, Any]:
    path = _state_path()
    if not path.exists():
        return {"version": 1, "users": {}}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"version": 1, "users": {}}
    if not isinstance(payload, dict) or not isinstance(payload.get("users"), dict):
        return {"version": 1, "users": {}}
    return payload


def _save_state(state: dict[str, Any]) -> None:
    path = _state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(
        json.dumps(state, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    temporary.replace(path)


def _device() -> dict[str, str]:
    state = _load_state()
    device = state.get("device")
    if not isinstance(device, dict) or not device.get("id"):
        device = {
            "id": str(uuid.uuid4()),
            "name": platform.node() or platform.system() or "本机",
            "platform": platform.system() or "Unknown",
        }
        state["device"] = device
        _save_state(state)
    return {
        "id": str(device["id"]),
        "name": str(device.get("name") or "本机"),
        "platform": str(device.get("platform") or platform.system()),
    }


def user_namespace(user_id: str) -> str:
    return hashlib.sha256(user_id.encode("utf-8")).hexdigest()[:32]


def _secret_account(kind: str, user_id: str) -> str:
    digest = hashlib.sha256(
        f"{ENV_ID}:{user_id}".encode("utf-8")
    ).hexdigest()
    return f"{kind}:{digest}"


def _test_secret_store_path() -> Path | None:
    value = os.environ.get("SCHEDULER_TEST_SECRET_STORE")
    return Path(value) if value else None


def _read_test_secrets() -> dict[str, str]:
    path = _test_secret_store_path()
    if path is None or not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _windows_dpapi_store_path() -> Path:
    return database_path().parent / "cloud_sync_credentials.dpapi.json"


def _windows_dpapi_protect(value: bytes) -> bytes:
    from ctypes import wintypes

    class DataBlob(ctypes.Structure):
        _fields_ = [
            ("size", wintypes.DWORD),
            ("data", ctypes.POINTER(ctypes.c_ubyte)),
        ]

    def blob(payload: bytes):
        buffer = (ctypes.c_ubyte * len(payload)).from_buffer_copy(payload)
        return DataBlob(len(payload), buffer), buffer

    crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    crypt32.CryptProtectData.argtypes = [
        ctypes.POINTER(DataBlob),
        wintypes.LPCWSTR,
        ctypes.POINTER(DataBlob),
        ctypes.c_void_p,
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.POINTER(DataBlob),
    ]
    crypt32.CryptProtectData.restype = wintypes.BOOL
    kernel32.LocalFree.argtypes = [ctypes.c_void_p]
    kernel32.LocalFree.restype = ctypes.c_void_p

    input_blob, input_buffer = blob(value)
    entropy_blob, entropy_buffer = blob(
        hashlib.sha256(KEYRING_SERVICE.encode("utf-8")).digest()
    )
    output_blob = DataBlob()
    if not crypt32.CryptProtectData(
        ctypes.byref(input_blob),
        "产线排班系统云同步密钥",
        ctypes.byref(entropy_blob),
        None,
        None,
        0x01,  # CRYPTPROTECT_UI_FORBIDDEN
        ctypes.byref(output_blob),
    ):
        code = ctypes.get_last_error()
        raise OSError(code, ctypes.FormatError(code))
    # Keep the input buffers alive until the native call has returned.
    del input_buffer, entropy_buffer
    try:
        return ctypes.string_at(output_blob.data, output_blob.size)
    finally:
        kernel32.LocalFree(ctypes.cast(output_blob.data, ctypes.c_void_p))


def _windows_dpapi_unprotect(value: bytes) -> bytes:
    from ctypes import wintypes

    class DataBlob(ctypes.Structure):
        _fields_ = [
            ("size", wintypes.DWORD),
            ("data", ctypes.POINTER(ctypes.c_ubyte)),
        ]

    def blob(payload: bytes):
        buffer = (ctypes.c_ubyte * len(payload)).from_buffer_copy(payload)
        return DataBlob(len(payload), buffer), buffer

    crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    crypt32.CryptUnprotectData.argtypes = [
        ctypes.POINTER(DataBlob),
        ctypes.c_void_p,
        ctypes.POINTER(DataBlob),
        ctypes.c_void_p,
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.POINTER(DataBlob),
    ]
    crypt32.CryptUnprotectData.restype = wintypes.BOOL
    kernel32.LocalFree.argtypes = [ctypes.c_void_p]
    kernel32.LocalFree.restype = ctypes.c_void_p

    input_blob, input_buffer = blob(value)
    entropy_blob, entropy_buffer = blob(
        hashlib.sha256(KEYRING_SERVICE.encode("utf-8")).digest()
    )
    output_blob = DataBlob()
    if not crypt32.CryptUnprotectData(
        ctypes.byref(input_blob),
        None,
        ctypes.byref(entropy_blob),
        None,
        None,
        0x01,  # CRYPTPROTECT_UI_FORBIDDEN
        ctypes.byref(output_blob),
    ):
        code = ctypes.get_last_error()
        raise OSError(code, ctypes.FormatError(code))
    # Keep the input buffers alive until the native call has returned.
    del input_buffer, entropy_buffer
    try:
        return ctypes.string_at(output_blob.data, output_blob.size)
    finally:
        kernel32.LocalFree(ctypes.cast(output_blob.data, ctypes.c_void_p))


class _WindowsDpapiStore:
    """Small keyring-compatible store protected by Windows DPAPI."""

    @staticmethod
    def _entry_key(service: str, account: str) -> str:
        return hashlib.sha256(
            f"{service}\0{account}".encode("utf-8")
        ).hexdigest()

    @staticmethod
    def _read() -> dict[str, str]:
        path = _windows_dpapi_store_path()
        if not path.exists():
            return {}
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        credentials = payload.get("credentials")
        return credentials if isinstance(credentials, dict) else {}

    @staticmethod
    def _write(credentials: dict[str, str]) -> None:
        path = _windows_dpapi_store_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(".tmp")
        temporary.write_text(
            json.dumps(
                {"version": 1, "credentials": credentials},
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        temporary.replace(path)

    def get_password(self, service: str, account: str) -> str | None:
        encoded = self._read().get(self._entry_key(service, account))
        if not encoded:
            return None
        encrypted = base64.b64decode(encoded, validate=True)
        return _windows_dpapi_unprotect(encrypted).decode("utf-8")

    def set_password(self, service: str, account: str, value: str) -> None:
        credentials = self._read()
        encrypted = _windows_dpapi_protect(value.encode("utf-8"))
        credentials[self._entry_key(service, account)] = base64.b64encode(
            encrypted
        ).decode("ascii")
        self._write(credentials)

    def delete_password(self, service: str, account: str) -> None:
        credentials = self._read()
        credentials.pop(self._entry_key(service, account), None)
        self._write(credentials)


def _credential_store():
    if platform.system() == "Windows":
        # Use native, user-scoped DPAPI directly. It remains available in a
        # frozen executable and avoids keyring backend discovery failures.
        return _WindowsDpapiStore()
    import keyring

    return keyring.get_keyring()


def _credential_store_error(action: str, error: Exception) -> str:
    message = f"系统安全凭据存储不可用，无法{action}云同步密钥"
    if platform.system() != "Windows":
        return message
    reason = str(error).strip() or type(error).__name__
    return f"{message}（Windows DPAPI：{reason[:240]}）"


def _get_secret(account: str) -> str | None:
    test_path = _test_secret_store_path()
    if test_path is not None:
        return _read_test_secrets().get(account)
    try:
        return _credential_store().get_password(KEYRING_SERVICE, account)
    except Exception as error:
        raise HTTPException(
            status_code=503,
            detail=_credential_store_error("读取", error),
        ) from error


def _set_secret(account: str, value: str) -> None:
    test_path = _test_secret_store_path()
    if test_path is not None:
        secrets_payload = _read_test_secrets()
        secrets_payload[account] = value
        test_path.parent.mkdir(parents=True, exist_ok=True)
        test_path.write_text(
            json.dumps(secrets_payload, ensure_ascii=False),
            encoding="utf-8",
        )
        return
    try:
        _credential_store().set_password(KEYRING_SERVICE, account, value)
    except Exception as error:
        raise HTTPException(
            status_code=503,
            detail=_credential_store_error("保存", error),
        ) from error


def _delete_secret(account: str) -> None:
    test_path = _test_secret_store_path()
    if test_path is not None:
        secrets_payload = _read_test_secrets()
        secrets_payload.pop(account, None)
        test_path.parent.mkdir(parents=True, exist_ok=True)
        test_path.write_text(
            json.dumps(secrets_payload, ensure_ascii=False),
            encoding="utf-8",
        )
        return
    try:
        import keyring

        try:
            _credential_store().delete_password(KEYRING_SERVICE, account)
        except keyring.errors.PasswordDeleteError:
            pass
    except Exception as error:
        raise HTTPException(
            status_code=503,
            detail=_credential_store_error("删除", error),
        ) from error


def generate_recovery_key() -> str:
    encoded = base64.urlsafe_b64encode(secrets.token_bytes(32)).decode("ascii")
    return f"{KEY_PREFIX}{encoded.rstrip('=')}"


def parse_recovery_key(value: str) -> bytes:
    cleaned = value.strip()
    if not cleaned.startswith(KEY_PREFIX):
        raise ValueError("恢复密钥格式不正确")
    encoded = cleaned[len(KEY_PREFIX):]
    try:
        padding = "=" * (-len(encoded) % 4)
        key = base64.urlsafe_b64decode(encoded + padding)
    except (ValueError, TypeError) as error:
        raise ValueError("恢复密钥格式不正确") from error
    if len(key) != 32:
        raise ValueError("恢复密钥长度不正确")
    return key


def encrypt_snapshot(plain: bytes, recovery_key: str) -> bytes:
    key = parse_recovery_key(recovery_key)
    nonce = secrets.token_bytes(12)
    aad = MAGIC + ENV_ID.encode("utf-8")
    return MAGIC + nonce + AESGCM(key).encrypt(nonce, plain, aad)


def decrypt_snapshot(encrypted: bytes, recovery_key: str) -> bytes:
    if not encrypted.startswith(MAGIC) or len(encrypted) < len(MAGIC) + 28:
        raise ValueError("云端备份格式不正确")
    key = parse_recovery_key(recovery_key)
    nonce_offset = len(MAGIC)
    nonce = encrypted[nonce_offset:nonce_offset + 12]
    ciphertext = encrypted[nonce_offset + 12:]
    aad = MAGIC + ENV_ID.encode("utf-8")
    try:
        return AESGCM(key).decrypt(nonce, ciphertext, aad)
    except InvalidTag as error:
        raise ValueError("恢复密钥不正确，或云端备份已损坏") from error


def _database_counts() -> dict[str, int]:
    table_names = {
        "parts": "parts",
        "employees": "employees",
        "machines": "machines",
        "orders": "production_orders",
        "weeks": "week_plans",
        "assignments": "assignments",
    }
    with connect() as connection:
        return {
            key: int(
                connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            )
            for key, table in table_names.items()
        }


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _windows_system_proxy() -> str | None:
    if platform.system() != "Windows":
        return None
    try:
        proxies = urllib.request.getproxies()
    except OSError:
        return None
    value = proxies.get("https") or proxies.get("http")
    if not isinstance(value, str) or not value.strip():
        return None
    cleaned = value.strip()
    return cleaned if "://" in cleaned else f"http://{cleaned}"


def _cloud_http_client_options(timeout_seconds: float) -> dict[str, Any]:
    options: dict[str, Any] = {
        "timeout": httpx.Timeout(timeout_seconds, connect=15.0),
        "follow_redirects": False,
    }
    if platform.system() != "Windows":
        return options

    # WebView automatically honors the Windows proxy and certificate store,
    # while HTTPX otherwise only reads *_PROXY environment variables and a
    # bundled CA file. Match the backend connection to the browser behavior.
    proxy = _windows_system_proxy()
    if proxy:
        options["proxy"] = proxy
    try:
        import truststore

        options["verify"] = truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    except ImportError:
        # Source installations may not have the optional Windows dependency;
        # keep the standard HTTPX verification rather than disabling TLS.
        pass
    return options


def _cloud_http_client(timeout_seconds: float) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        **_cloud_http_client_options(timeout_seconds),
    )


def _network_error_detail(error: Exception) -> str:
    details: list[str] = []
    current: BaseException | None = error
    visited: set[int] = set()
    while current is not None and id(current) not in visited:
        visited.add(id(current))
        message = str(current).strip()
        label = type(current).__name__
        details.append(f"{label}: {message}" if message else label)
        current = current.__cause__ or current.__context__
    return " → ".join(details)[:500]


def _decode_saved_session(value: str) -> dict[str, str | None]:
    try:
        payload = json.loads(value)
    except json.JSONDecodeError:
        # 兼容 3.5.4 及更早版本只保存 refresh token 的钥匙串项。
        return {"access_token": None, "refresh_token": value}
    if not isinstance(payload, dict):
        return {"access_token": None, "refresh_token": None}
    access_token = payload.get("access_token")
    refresh_token = payload.get("refresh_token")
    return {
        "access_token": (
            access_token if isinstance(access_token, str) else None
        ),
        "refresh_token": (
            refresh_token if isinstance(refresh_token, str) else None
        ),
    }


def _active_cloud_user_and_token() -> tuple[str, str]:
    state = _load_state()
    active_user = state.get("active_user")
    if not isinstance(active_user, str) or not active_user:
        raise HTTPException(status_code=401, detail="请先登录云端账户")
    saved_secret = _get_secret(_secret_account("session", active_user))
    if not saved_secret:
        raise HTTPException(status_code=401, detail="云端登录信息不存在")
    session = _decode_saved_session(saved_secret)
    access_token = session.get("access_token")
    if not access_token:
        raise HTTPException(
            status_code=401,
            detail="云端登录信息已过期，请重新登录",
        )
    return active_user, access_token


def validate_cloud_path(value: str, user_id: str) -> str:
    cleaned = value.strip().strip("/")
    segments = cleaned.split("/")
    expected_prefix = f"production-scheduler/{user_namespace(user_id)}/"
    if (
        not CLOUD_PATH_PATTERN.fullmatch(cleaned)
        or any(segment in {"", ".", ".."} for segment in segments)
        or not cleaned.startswith(expected_prefix)
    ):
        raise ValueError("云存储文件路径不受信任")
    return cleaned


def validate_cos_storage_url(value: str) -> str:
    cleaned = value.strip()
    parsed = urlsplit(cleaned)
    hostname = (parsed.hostname or "").lower()
    expected_suffix = f".cos.{REGION}.myqcloud.com"
    download_hostname = f"{STORAGE_BUCKET_ID}.tcb.qcloud.la".lower()
    if (
        parsed.scheme != "https"
        or parsed.username is not None
        or parsed.password is not None
        or parsed.port not in {None, 443}
        or (
            not hostname.endswith(expected_suffix)
            and hostname != download_hostname
        )
    ):
        raise ValueError("云存储地址不受信任")
    return cleaned


def validate_cos_upload_url(value: str) -> str:
    cleaned = validate_cos_storage_url(value)
    hostname = (urlsplit(cleaned).hostname or "").lower()
    expected_suffix = f".cos.{REGION}.myqcloud.com"
    if not hostname.endswith(expected_suffix):
        raise ValueError("云存储上传地址不受信任")
    return cleaned


async def _cloud_api_call(
    client: httpx.AsyncClient,
    access_token: str,
    action: str,
    payload: dict[str, Any],
) -> dict[str, Any]:
    endpoint = (
        f"https://{ENV_ID}.{REGION}.tcb-api.tencentcloudapi.com/web"
    )
    try:
        result = await client.post(
            endpoint,
            params={"env": ENV_ID},
            json={
                "action": action,
                "dataVersion": CLOUD_API_VERSION,
                "env": ENV_ID,
                "access_token": access_token,
                **payload,
            },
            headers={
                "X-TCB-Region": REGION,
                "X-SDK-Version": "@cloudbase/js-sdk/3.6.1",
            },
        )
        result.raise_for_status()
        response = result.json()
    except (httpx.HTTPError, ValueError) as error:
        raise HTTPException(
            status_code=502,
            detail=f"连接 CloudBase 失败：{_network_error_detail(error)}",
        ) from error
    if not isinstance(response, dict):
        raise HTTPException(status_code=502, detail="CloudBase 返回格式不正确")
    code = response.get("code")
    if code:
        message = response.get("message") or "CloudBase 请求失败"
        raise HTTPException(status_code=502, detail=f"{code}: {message}")
    return response


def _cloud_file_id(path: str) -> str:
    return f"cloud://{ENV_ID}.{STORAGE_BUCKET_ID}/{path}"


def _is_missing_cloud_file(value: Any) -> bool:
    return bool(
        re.search(
            r"FILE_NOT_FOUND|STORAGE_FILE_(?:NONEXIST|NOT_EXISTS?)",
            str(value or ""),
            re.IGNORECASE,
        )
    )


async def _cloud_download_info(
    client: httpx.AsyncClient,
    access_token: str,
    path: str,
) -> dict[str, Any]:
    response = await _cloud_api_call(
        client,
        access_token,
        "storage.batchGetDownloadUrl",
        {
            "file_list": [
                {"fileid": _cloud_file_id(path), "max_age": 600}
            ]
        },
    )
    data = response.get("data")
    items = data.get("download_list") if isinstance(data, dict) else None
    if not isinstance(items, list) or not items:
        raise HTTPException(
            status_code=502,
            detail="CloudBase 未返回云存储文件信息",
        )
    item = items[0]
    if not isinstance(item, dict):
        raise HTTPException(
            status_code=502,
            detail="CloudBase 返回的文件信息不正确",
        )
    return item


@router.get("/config")
def cloud_config():
    return {
        "env_id": ENV_ID,
        "region": REGION,
        "publishable_key": PUBLISHABLE_KEY or None,
        "publishable_key_required": not bool(PUBLISHABLE_KEY),
        "storage_bucket_id": STORAGE_BUCKET_ID or None,
        "storage_mode": (
            STORAGE_MODE
            if STORAGE_MODE in {"classic", "pg", "auto"}
            else "auto"
        ),
        "device": _device(),
        "max_snapshot_bytes": MAX_SNAPSHOT_BYTES,
    }


@router.get("/storage-info")
async def cloud_storage_info(
    path: str = Query(min_length=1, max_length=2048),
):
    user_id, access_token = _active_cloud_user_and_token()
    try:
        trusted_path = validate_cloud_path(path, user_id)
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    async with _cloud_http_client(25.0) as client:
        item = await _cloud_download_info(
            client,
            access_token,
            trusted_path,
        )
    code = item.get("code")
    if _is_missing_cloud_file(code) or _is_missing_cloud_file(
        item.get("message")
    ):
        return {"exists": False}
    if code and code != "SUCCESS":
        raise HTTPException(
            status_code=502,
            detail=f"云存储文件检查失败：{code}",
        )
    return {"exists": True}


@router.get("/storage-download")
async def download_from_cloud_storage(
    path: str = Query(min_length=1, max_length=2048),
):
    user_id, access_token = _active_cloud_user_and_token()
    try:
        trusted_path = validate_cloud_path(path, user_id)
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    async with _cloud_http_client(40.0) as client:
        item = await _cloud_download_info(
            client,
            access_token,
            trusted_path,
        )
        code = item.get("code")
        if _is_missing_cloud_file(code) or _is_missing_cloud_file(
            item.get("message")
        ):
            raise HTTPException(status_code=404, detail="云端文件不存在")
        if code and code != "SUCCESS":
            raise HTTPException(
                status_code=502,
                detail=f"云存储下载授权失败：{code}",
            )
        download_url = item.get("tempFileURL") or item.get("download_url")
        if not isinstance(download_url, str) or not download_url:
            raise HTTPException(
                status_code=502,
                detail="CloudBase 未返回云存储下载地址",
            )
        try:
            trusted_url = validate_cos_storage_url(download_url)
        except ValueError as error:
            raise HTTPException(status_code=422, detail=str(error)) from error
        try:
            result = await client.get(trusted_url)
            result.raise_for_status()
        except httpx.HTTPError as error:
            raise HTTPException(
                status_code=502,
                detail=(
                    "下载云端文件失败："
                    f"{_network_error_detail(error)}"
                ),
            ) from error
    if len(result.content) > MAX_SNAPSHOT_BYTES:
        raise HTTPException(status_code=413, detail="云端文件超过大小限制")
    return Response(
        content=result.content,
        media_type=result.headers.get(
            "content-type",
            "application/octet-stream",
        ),
    )


@router.post("/storage-upload")
async def upload_to_signed_cloud_storage(
    snapshot: UploadFile = File(...),
    cloud_path: str = Form(..., min_length=1, max_length=2048),
    content_type: str = Form(
        default="application/octet-stream",
        max_length=200,
    ),
):
    _UPLOAD_STATUS.update(
        {
            "stage": "request_received",
            "updated_at": _utc_now(),
            "size": None,
            "http_status": None,
            "error": None,
        }
    )
    user_id, access_token = _active_cloud_user_and_token()
    try:
        trusted_path = validate_cloud_path(cloud_path, user_id)
    except ValueError as error:
        _UPLOAD_STATUS.update(
            {"stage": "path_rejected", "error": str(error)}
        )
        raise HTTPException(status_code=422, detail=str(error)) from error

    payload = await snapshot.read(MAX_SNAPSHOT_BYTES + 1)
    _UPLOAD_STATUS.update(
        {"stage": "uploading_to_cloud", "size": len(payload)}
    )
    if len(payload) > MAX_SNAPSHOT_BYTES:
        _UPLOAD_STATUS.update(
            {"stage": "payload_too_large", "error": "size_limit"}
        )
        raise HTTPException(status_code=413, detail="云同步快照超过大小限制")

    try:
        async with _cloud_http_client(40.0) as client:
            _UPLOAD_STATUS.update({"stage": "requesting_cloud_authorization"})
            metadata_response = await _cloud_api_call(
                client,
                access_token,
                "storage.getUploadMetadata",
                {
                    "path": trusted_path,
                    "method": "put",
                    "headers": {
                        "Content-Type": (
                            content_type or "application/octet-stream"
                        )
                    },
                },
            )
            metadata = metadata_response.get("data")
            if not isinstance(metadata, dict):
                raise HTTPException(
                    status_code=502,
                    detail="CloudBase 未返回上传授权信息",
                )
            upload_url = metadata.get("url")
            authorization = metadata.get("authorization")
            security_token = metadata.get("token")
            cos_file_id = metadata.get("cosFileId")
            if not all(
                isinstance(value, str) and value
                for value in (
                    upload_url,
                    authorization,
                    security_token,
                    cos_file_id,
                )
            ):
                raise HTTPException(
                    status_code=502,
                    detail="CloudBase 返回的上传授权信息不完整",
                )
            try:
                trusted_url = validate_cos_upload_url(upload_url)
            except ValueError as error:
                _UPLOAD_STATUS.update(
                    {"stage": "url_rejected", "error": str(error)}
                )
                raise HTTPException(
                    status_code=422,
                    detail=str(error),
                ) from error
            headers = {
                "Authorization": authorization,
                "Content-Type": (
                    content_type or "application/octet-stream"
                ),
                "x-cos-meta-fileid": cos_file_id,
                "x-cos-security-token": security_token,
            }
            _UPLOAD_STATUS.update({"stage": "uploading_to_cloud"})
            result = await client.put(
                trusted_url,
                content=payload,
                headers=headers,
            )
    except httpx.HTTPError as error:
        _UPLOAD_STATUS.update(
            {
                "stage": "cloud_connection_failed",
                "error": error.__class__.__name__,
            }
        )
        raise HTTPException(
            status_code=502,
            detail=(
                "连接腾讯云存储失败："
                f"{_network_error_detail(error)}"
            ),
        ) from error
    if not 200 <= result.status_code < 300:
        _UPLOAD_STATUS.update(
            {
                "stage": "cloud_rejected",
                "http_status": result.status_code,
                "error": "cloud_http_error",
            }
        )
        raise HTTPException(
            status_code=502,
            detail=f"腾讯云存储上传失败（HTTP {result.status_code}）",
        )
    _UPLOAD_STATUS.update(
        {
            "stage": "completed",
            "http_status": result.status_code,
            "error": None,
        }
    )
    return {"uploaded": True, "size": len(payload)}


@router.get("/storage-upload-status")
def storage_upload_status():
    return dict(_UPLOAD_STATUS)


@router.get("/session")
def get_saved_session():
    state = _load_state()
    active_user = state.get("active_user")
    if not isinstance(active_user, str) or not active_user:
        return {"available": False}
    saved_secret = _get_secret(_secret_account("session", active_user))
    if not saved_secret:
        return {"available": False}
    session = _decode_saved_session(saved_secret)
    if not session["refresh_token"]:
        return {"available": False}
    user_state = state.get("users", {}).get(active_user, {})
    return {
        "available": True,
        "user_id": active_user,
        "access_token": session["access_token"],
        "refresh_token": session["refresh_token"],
        "display_name": user_state.get("display_name"),
    }


@router.put("/session")
def save_session(payload: SessionPayload):
    user_id = _validate_user_id(payload.user_id)
    _set_secret(
        _secret_account("session", user_id),
        json.dumps(
            {
                "access_token": payload.access_token,
                "refresh_token": payload.refresh_token,
            },
            separators=(",", ":"),
        ),
    )
    state = _load_state()
    state["active_user"] = user_id
    user_state = state.setdefault("users", {}).setdefault(user_id, {})
    user_state["display_name"] = payload.display_name
    user_state["last_login_at"] = _utc_now()
    _save_state(state)
    return {"status": "saved"}


@router.delete("/session", status_code=204)
def delete_session():
    state = _load_state()
    active_user = state.get("active_user")
    if isinstance(active_user, str) and active_user:
        _delete_secret(_secret_account("session", active_user))
    state.pop("active_user", None)
    _save_state(state)
    return Response(status_code=204)


@router.get("/key")
def recovery_key_status(user_id: str = Query(min_length=1, max_length=256)):
    user_id = _validate_user_id(user_id)
    return {
        "available": bool(_get_secret(_secret_account("recovery", user_id))),
    }


@router.post("/key/generate")
def create_recovery_key(payload: UserPayload):
    user_id = _validate_user_id(payload.user_id)
    account = _secret_account("recovery", user_id)
    existing = _get_secret(account)
    if existing:
        return {"created": False, "recovery_key": existing}
    recovery_key = generate_recovery_key()
    _set_secret(account, recovery_key)
    return {"created": True, "recovery_key": recovery_key}


@router.post("/key/import")
def import_recovery_key(payload: RecoveryKeyPayload):
    user_id = _validate_user_id(payload.user_id)
    try:
        parse_recovery_key(payload.recovery_key)
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    _set_secret(
        _secret_account("recovery", user_id),
        payload.recovery_key.strip(),
    )
    return {"status": "saved"}


@router.delete("/key", status_code=204)
def delete_recovery_key(user_id: str = Query(min_length=1, max_length=256)):
    user_id = _validate_user_id(user_id)
    _delete_secret(_secret_account("recovery", user_id))
    return Response(status_code=204)


@router.get("/state")
def get_sync_state(user_id: str = Query(min_length=1, max_length=256)):
    user_id = _validate_user_id(user_id)
    state = _load_state()
    user_state = state.get("users", {}).get(user_id, {})
    return {
        "user_namespace": user_namespace(user_id),
        "device": _device(),
        "last_revision_id": user_state.get("last_revision_id"),
        "last_plain_sha256": user_state.get("last_plain_sha256"),
        "last_sync_at": user_state.get("last_sync_at"),
        "counts": _database_counts(),
    }


@router.get("/fingerprint")
def local_database_fingerprint(
    user_id: str = Query(min_length=1, max_length=256),
):
    _validate_user_id(user_id)
    backup = temporary_database_backup(prefix="scheduler-fingerprint-")
    try:
        plain = backup.read_bytes()
    finally:
        backup.unlink(missing_ok=True)
    return {
        "plain_sha256": _sha256(plain),
        "database_size": len(plain),
        "counts": _database_counts(),
    }


@router.put("/state")
def save_sync_state(payload: SyncStatePayload):
    user_id = _validate_user_id(payload.user_id)
    state = _load_state()
    user_state = state.setdefault("users", {}).setdefault(user_id, {})
    user_state.update(
        {
            "last_revision_id": payload.last_revision_id,
            "last_plain_sha256": payload.last_plain_sha256,
            "last_sync_at": payload.last_sync_at or _utc_now(),
        }
    )
    _save_state(state)
    return {"status": "saved"}


@router.post("/snapshot")
def create_encrypted_snapshot(payload: UserPayload):
    user_id = _validate_user_id(payload.user_id)
    recovery_key = _get_secret(_secret_account("recovery", user_id))
    if not recovery_key:
        raise HTTPException(status_code=409, detail="本机尚未保存恢复密钥")
    backup = temporary_database_backup()
    try:
        plain = backup.read_bytes()
    finally:
        backup.unlink(missing_ok=True)
    if len(plain) > MAX_SNAPSHOT_BYTES:
        raise HTTPException(status_code=413, detail="数据库超过云同步大小限制")
    encrypted = encrypt_snapshot(plain, recovery_key)
    return Response(
        content=encrypted,
        media_type="application/octet-stream",
        headers={
            "X-Database-Sha256": _sha256(plain),
            "X-Encrypted-Sha256": _sha256(encrypted),
            "X-Database-Size": str(len(plain)),
        },
    )


@router.post("/restore")
async def restore_encrypted_snapshot(
    user_id: str = Query(min_length=1, max_length=256),
    snapshot: UploadFile = File(...),
):
    user_id = _validate_user_id(user_id)
    recovery_key = _get_secret(_secret_account("recovery", user_id))
    if not recovery_key:
        raise HTTPException(status_code=409, detail="请先输入此账户的恢复密钥")
    encrypted = await snapshot.read(MAX_SNAPSHOT_BYTES + 65)
    if len(encrypted) > MAX_SNAPSHOT_BYTES + 64:
        raise HTTPException(status_code=413, detail="云端备份超过恢复大小限制")
    try:
        plain = decrypt_snapshot(encrypted, recovery_key)
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error

    target = database_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    handle = tempfile.NamedTemporaryFile(
        prefix="scheduler-restore-",
        suffix=".db",
        dir=target.parent,
        delete=False,
    )
    temporary = Path(handle.name)
    try:
        handle.write(plain)
        handle.flush()
        handle.close()
        backup = install_database_snapshot(temporary)
    except ValueError as error:
        temporary.unlink(missing_ok=True)
        raise HTTPException(status_code=422, detail=str(error)) from error
    except Exception as error:
        temporary.unlink(missing_ok=True)
        reason = str(error).strip() or type(error).__name__
        raise HTTPException(
            status_code=500,
            detail=f"安装云端数据库失败：{reason[:300]}",
        ) from error
    return {
        "status": "restored",
        "plain_sha256": _sha256(plain),
        "safety_backup": str(backup) if backup else None,
        "counts": _database_counts(),
    }
