from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from backend.app import cloud_sync
from backend.app.cloud_sync import (
    decrypt_snapshot,
    encrypt_snapshot,
    generate_recovery_key,
    user_namespace,
    validate_cloud_path,
    validate_cos_storage_url,
    validate_cos_upload_url,
)
from backend.app.main import app


@pytest.fixture()
def cloud_client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("SCHEDULER_DB_PATH", str(tmp_path / "scheduler.db"))
    monkeypatch.setenv(
        "SCHEDULER_TEST_SECRET_STORE",
        str(tmp_path / "test-secrets.json"),
    )
    with TestClient(app) as client:
        yield client


def test_recovery_key_encrypts_and_authenticates_snapshot():
    recovery_key = generate_recovery_key()
    plain = b"sqlite snapshot contents"
    encrypted = encrypt_snapshot(plain, recovery_key)

    assert encrypted != plain
    assert decrypt_snapshot(encrypted, recovery_key) == plain
    with pytest.raises(ValueError, match="恢复密钥不正确"):
        decrypt_snapshot(encrypted, generate_recovery_key())


def test_windows_credential_store_uses_dpapi(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
):
    monkeypatch.setenv("SCHEDULER_DB_PATH", str(tmp_path / "scheduler.db"))
    monkeypatch.delenv("SCHEDULER_TEST_SECRET_STORE", raising=False)
    monkeypatch.setattr(cloud_sync.platform, "system", lambda: "Windows")
    monkeypatch.setattr(
        cloud_sync,
        "_windows_dpapi_protect",
        lambda value: b"protected:" + value,
    )
    monkeypatch.setattr(
        cloud_sync,
        "_windows_dpapi_unprotect",
        lambda value: value.removeprefix(b"protected:"),
    )

    store = cloud_sync._credential_store()
    assert isinstance(store, cloud_sync._WindowsDpapiStore)
    store.set_password("test-service", "test-account", "secret-value")
    assert store.get_password("test-service", "test-account") == "secret-value"
    assert "secret-value" not in (
        cloud_sync._windows_dpapi_store_path().read_text(encoding="utf-8")
    )
    store.delete_password("test-service", "test-account")
    assert store.get_password("test-service", "test-account") is None


def test_cos_upload_url_only_accepts_configured_region():
    valid = (
        "https://7072-production-schedule-test-d73e723-1460691865"
        ".cos.ap-shanghai.myqcloud.com/path/file.plsync?signature=test"
    )
    assert validate_cos_upload_url(valid) == valid

    with pytest.raises(ValueError, match="不受信任"):
        validate_cos_upload_url("https://example.com/path/file.plsync")

    with pytest.raises(ValueError, match="不受信任"):
        validate_cos_upload_url(
            "https://bucket.cos.ap-guangzhou.myqcloud.com/file.plsync"
        )

    download = (
        "https://7072-production-schedule-test-d73e723-1460691865"
        ".tcb.qcloud.la/production-scheduler/user/manifest.json"
    )
    assert validate_cos_storage_url(download) == download
    with pytest.raises(ValueError, match="上传地址不受信任"):
        validate_cos_upload_url(download)


def test_cloud_path_is_limited_to_active_user_namespace():
    user_id = "cloud-user-path"
    namespace = user_namespace(user_id)
    valid = f"production-scheduler/{namespace}/manifest.json"
    assert validate_cloud_path(valid, user_id) == valid

    with pytest.raises(ValueError, match="不受信任"):
        validate_cloud_path(
            "production-scheduler/another-user/manifest.json",
            user_id,
        )

    with pytest.raises(ValueError, match="不受信任"):
        validate_cloud_path(
            f"production-scheduler/{namespace}/../other.json",
            user_id,
        )


def _save_test_session(cloud_client: TestClient, user_id: str) -> None:
    response = cloud_client.put(
        "/api/cloud-sync/session",
        json={
            "user_id": user_id,
            "access_token": "access-token-value-for-testing",
            "refresh_token": "refresh-token-value-for-testing",
            "display_name": "测试管理员",
        },
    )
    assert response.status_code == 200


def test_storage_info_treats_cloudbase_nonexistent_code_as_missing(
    cloud_client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
):
    user_id = "cloud-storage-info"
    _save_test_session(cloud_client, user_id)

    async def fake_cloud_api_call(*_args, **_kwargs):
        return {
            "data": {
                "download_list": [
                    {"code": "STORAGE_FILE_NONEXIST"}
                ]
            }
        }

    monkeypatch.setattr(
        cloud_sync,
        "_cloud_api_call",
        fake_cloud_api_call,
    )
    path = (
        f"production-scheduler/{user_namespace(user_id)}/manifest.json"
    )
    response = cloud_client.get(
        "/api/cloud-sync/storage-info",
        params={"path": path},
    )
    assert response.status_code == 200
    assert response.json() == {"exists": False}


def test_storage_upload_gets_authorization_on_local_backend(
    cloud_client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
):
    user_id = "cloud-storage-upload"
    _save_test_session(cloud_client, user_id)
    cloud_calls: list[tuple[str, dict]] = []

    async def fake_cloud_api_call(
        _client,
        _access_token,
        action,
        payload,
    ):
        cloud_calls.append((action, payload))
        return {
            "data": {
                "url": (
                    "https://7072-production-schedule-test-d73e723-"
                    "1460691865.cos.ap-shanghai.myqcloud.com/"
                    "production-scheduler/test/revision.plsync"
                ),
                "authorization": "signed-authorization",
                "token": "security-token",
                "cosFileId": "cos-file-id",
            }
        }

    async def fake_put(_self, _url, **_kwargs):
        return httpx.Response(200)

    monkeypatch.setattr(
        cloud_sync,
        "_cloud_api_call",
        fake_cloud_api_call,
    )
    monkeypatch.setattr(httpx.AsyncClient, "put", fake_put)
    path = (
        f"production-scheduler/{user_namespace(user_id)}/"
        "revisions/test.plsync"
    )
    response = cloud_client.post(
        "/api/cloud-sync/storage-upload",
        data={
            "cloud_path": path,
            "content_type": "application/octet-stream",
        },
        files={
            "snapshot": (
                "test.plsync",
                b"encrypted-snapshot",
                "application/octet-stream",
            )
        },
    )
    assert response.status_code == 200
    assert cloud_calls == [
        (
            "storage.getUploadMetadata",
            {
                "path": path,
                "method": "put",
                "headers": {
                    "Content-Type": "application/octet-stream"
                },
            },
        )
    ]
    assert cloud_client.get(
        "/api/cloud-sync/storage-upload-status"
    ).json()["stage"] == "completed"


def test_snapshot_restore_replaces_database_and_keeps_safety_backup(
    cloud_client: TestClient,
):
    user_id = "cloud-user-1"
    key_response = cloud_client.post(
        "/api/cloud-sync/key/generate",
        json={"user_id": user_id},
    )
    assert key_response.status_code == 200
    assert key_response.json()["recovery_key"].startswith("PLS1.")

    first = cloud_client.post(
        "/api/parts",
        json={
            "code": "SYNC-1",
            "name": "云同步零件",
            "standard_hours": 1.25,
            "active": True,
        },
    )
    assert first.status_code == 201

    snapshot = cloud_client.post(
        "/api/cloud-sync/snapshot",
        json={"user_id": user_id},
    )
    assert snapshot.status_code == 200
    assert snapshot.headers["x-database-sha256"]
    assert snapshot.content.startswith(b"PLSYNC1\x00")

    second = cloud_client.post(
        "/api/parts",
        json={
            "code": "SYNC-2",
            "name": "稍后增加的零件",
            "standard_hours": 0.5,
            "active": True,
        },
    )
    assert second.status_code == 201
    assert len(cloud_client.get("/api/parts").json()) == 2

    restored = cloud_client.post(
        f"/api/cloud-sync/restore?user_id={user_id}",
        files={
            "snapshot": (
                "revision.plsync",
                snapshot.content,
                "application/octet-stream",
            )
        },
    )
    assert restored.status_code == 200
    payload = restored.json()
    assert payload["status"] == "restored"
    assert payload["safety_backup"]
    parts = cloud_client.get("/api/parts").json()
    assert [part["code"] for part in parts] == ["SYNC-1"]
    assert Path(payload["safety_backup"]).is_file()


def test_session_secret_and_sync_state_are_device_local(
    cloud_client: TestClient,
    tmp_path: Path,
):
    user_id = "test-account"
    response = cloud_client.put(
        "/api/cloud-sync/session",
        json={
            "user_id": user_id,
            "access_token": "access-token-value-for-testing",
            "refresh_token": "refresh-token-value-for-testing",
            "display_name": "测试管理员",
        },
    )
    assert response.status_code == 200
    saved = cloud_client.get("/api/cloud-sync/session").json()
    assert saved["available"] is True
    assert saved["access_token"] == "access-token-value-for-testing"
    assert saved["refresh_token"] == "refresh-token-value-for-testing"

    state = cloud_client.get(
        f"/api/cloud-sync/state?user_id={user_id}"
    ).json()
    assert len(state["user_namespace"]) == 32
    assert state["device"]["id"]
    assert "refresh-token-value-for-testing" not in (
        tmp_path / "cloud_sync_state.json"
    ).read_text(encoding="utf-8")

    secret_payload = json.loads(
        (tmp_path / "test-secrets.json").read_text(encoding="utf-8")
    )
    stored_sessions = [
        json.loads(value)
        for value in secret_payload.values()
        if value.startswith("{")
    ]
    assert any(
        value["access_token"] == "access-token-value-for-testing"
        and value["refresh_token"] == "refresh-token-value-for-testing"
        for value in stored_sessions
    )

    deleted = cloud_client.delete("/api/cloud-sync/session")
    assert deleted.status_code == 204
    assert cloud_client.get("/api/cloud-sync/session").json() == {
        "available": False
    }
