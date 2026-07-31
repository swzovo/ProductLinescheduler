import Cloudbase from "@cloudbase/js-sdk";

export type CloudConfig = {
  env_id: string;
  region: string;
  publishable_key: string | null;
  publishable_key_required: boolean;
  storage_bucket_id: string | null;
  storage_mode: "classic" | "pg" | "auto";
  device: {
    id: string;
    name: string;
    platform: string;
  };
  max_snapshot_bytes: number;
};

export type CloudUser = {
  id: string;
  displayName: string;
};

export type CloudSession = {
  accessToken: string;
  refreshToken: string;
};

type AuthResult = {
  data?: {
    user?: Record<string, unknown> | null;
    session?: Record<string, unknown> | null;
  } | null;
  error?: { message?: string; code?: string } | null;
};

type RestoreSessionAuth = {
  setSession: (tokens: {
    access_token: string;
    refresh_token: string;
  }) => Promise<AuthResult>;
};

let currentApp: ReturnType<typeof Cloudbase.init> | null = null;
let currentSignature = "";
let resolvedBucketSignature = "";
let resolvedBucketId = "";
let resolvedStorageMode: "classic" | "pg" | "" = "";

type StorageBucket = {
  id: string;
  name: string;
  public: boolean;
};

type CloudFileResult<T> =
  | { data: T; error: null }
  | { data: null; error: Error };

type CloudFileClient = {
  exists: (path: string) => Promise<CloudFileResult<boolean>>;
  download: (path: string) => Promise<CloudFileResult<Blob>>;
  upload: (
    path: string,
    file: Blob,
    options?: { contentType?: string; upsert?: boolean },
  ) => Promise<CloudFileResult<{ path: string }>>;
  update: (
    path: string,
    file: Blob,
    options?: { contentType?: string; upsert?: boolean },
  ) => Promise<CloudFileResult<{ path: string }>>;
};

const PREFERRED_BUCKET_ID = "production-scheduler";
const CLOUD_OPERATION_TIMEOUT_MS = 25_000;

async function withCloudTimeout<T>(
  operation: Promise<T>,
  label: string,
): Promise<T> {
  let timer = 0;
  const timeout = new Promise<never>((_, reject) => {
    timer = window.setTimeout(() => {
      reject(new Error(`${label}超时，请检查网络后重试`));
    }, CLOUD_OPERATION_TIMEOUT_MS);
  });
  try {
    return await Promise.race([operation, timeout]);
  } finally {
    window.clearTimeout(timer);
  }
}

function resolvePublishableKey(config: CloudConfig): string | undefined {
  return (
    config.publishable_key
    ?? localStorage.getItem("cloudbase:publishable-key")
    ?? undefined
  );
}

export function savePublishableKey(value: string): void {
  const cleaned = value.trim();
  if (cleaned) {
    localStorage.setItem("cloudbase:publishable-key", cleaned);
  } else {
    localStorage.removeItem("cloudbase:publishable-key");
  }
  currentApp = null;
  currentSignature = "";
  resolvedBucketSignature = "";
  resolvedBucketId = "";
  resolvedStorageMode = "";
}

export function configuredPublishableKey(config: CloudConfig): string {
  return resolvePublishableKey(config) ?? "";
}

function storageBucketKey(config: CloudConfig): string {
  return `cloudbase:storage-bucket:${config.env_id}`;
}

export function configuredStorageBucketId(config: CloudConfig): string {
  return (
    config.storage_bucket_id
    ?? localStorage.getItem(storageBucketKey(config))
    ?? ""
  ).trim();
}

export function saveStorageBucketId(
  config: CloudConfig,
  value: string,
): void {
  const cleaned = value.trim();
  if (cleaned) {
    localStorage.setItem(storageBucketKey(config), cleaned);
  } else {
    localStorage.removeItem(storageBucketKey(config));
  }
  resolvedBucketSignature = "";
  resolvedBucketId = "";
  resolvedStorageMode = "";
}

export function selectStorageBucket(
  buckets: StorageBucket[],
): StorageBucket | null {
  const named = buckets.find(
    (bucket) => (
      bucket.id === PREFERRED_BUCKET_ID
      || bucket.name === PREFERRED_BUCKET_ID
    ),
  );
  if (named) return named;
  if (buckets.length === 1) return buckets[0];
  const privateBuckets = buckets.filter((bucket) => !bucket.public);
  return privateBuckets.length === 1 ? privateBuckets[0] : null;
}

export function cloudEndpointMode(
  storageMode: CloudConfig["storage_mode"],
): "CLOUD_API" | "GATEWAY" {
  // CloudBase JS SDK 3.6 默认使用 GATEWAY。该路由会把上传路径的
  // 第一段当成 PG Bucket ID；传统云存储必须改走 CLOUD_API，
  // 才会使用环境自带 COS Bucket 及传统安全规则完成签名上传。
  return storageMode === "classic" ? "CLOUD_API" : "GATEWAY";
}

export function cloudApp(
  config: CloudConfig,
  storageMode: CloudConfig["storage_mode"] = config.storage_mode,
) {
  const accessKey = resolvePublishableKey(config);
  const endPointMode = cloudEndpointMode(storageMode);
  const signature = (
    `${config.env_id}:${config.region}:${accessKey ?? ""}:${endPointMode}`
  );
  if (!currentApp || currentSignature !== signature) {
    currentApp = Cloudbase.init({
      env: config.env_id,
      region: config.region,
      accessKey,
      endPointMode,
      persistence: "none",
      timeout: 20_000,
    });
    currentSignature = signature;
  }
  return currentApp;
}

function normalizeResult(result: AuthResult): {
  user: CloudUser;
  session: CloudSession;
} {
  if (result.error) {
    throw new Error(result.error.message || "CloudBase 登录失败");
  }
  const rawUser = result.data?.user;
  const rawSession = result.data?.session;
  if (!rawUser || !rawSession) {
    throw new Error("CloudBase 未返回有效登录会话");
  }
  const refreshToken = String(
    rawSession.refresh_token
    ?? rawSession.refreshToken
    ?? "",
  );
  const accessToken = String(
    rawSession.access_token
    ?? rawSession.accessToken
    ?? "",
  );
  const id = cloudUserIdentifier(rawUser, accessToken);
  if (!id || !refreshToken || !accessToken) {
    throw new Error("CloudBase 登录会话缺少用户标识或刷新令牌");
  }
  const metadata = (
    typeof rawUser.user_metadata === "object"
    && rawUser.user_metadata !== null
  )
    ? rawUser.user_metadata as Record<string, unknown>
    : {};
  const displayName = String(
    [
      rawUser.username,
      rawUser.email,
      rawUser.phone,
      rawUser.phone_number,
      rawUser.name,
      metadata.username,
      metadata.email,
      metadata.phone,
      metadata.name,
    ].find(
      (value) => (
        (typeof value === "string" || typeof value === "number")
        && String(value).trim()
      ),
    ) ?? id,
  );
  return {
    user: { id, displayName },
    session: { accessToken, refreshToken },
  };
}

function primitiveText(value: unknown): string {
  if (typeof value !== "string" && typeof value !== "number") return "";
  return String(value).trim();
}

function jwtClaims(accessToken: string): Record<string, unknown> {
  const encoded = accessToken.split(".")[1];
  if (!encoded) return {};
  try {
    const base64 = encoded.replace(/-/g, "+").replace(/_/g, "/");
    const padded = base64 + "=".repeat((4 - (base64.length % 4)) % 4);
    const bytes = Uint8Array.from(
      window.atob(padded),
      (character) => character.charCodeAt(0),
    );
    const payload = JSON.parse(
      new TextDecoder().decode(bytes),
    ) as unknown;
    return typeof payload === "object" && payload !== null
      ? payload as Record<string, unknown>
      : {};
  } catch {
    return {};
  }
}

export function cloudUserIdentifier(
  rawUser: Record<string, unknown>,
  accessToken: string,
): string {
  const direct = [
    rawUser.id,
    rawUser.uid,
    rawUser.openid,
    rawUser.open_id,
    rawUser.sub,
  ].map(primitiveText).find(Boolean);
  if (direct) return direct;

  const claims = jwtClaims(accessToken);
  return (
    [
      claims.sub,
      claims.uid,
      claims.user_id,
      claims.openid,
      claims.open_id,
    ].map(primitiveText).find(Boolean)
    ?? ""
  );
}

export async function signInWithPassword(
  config: CloudConfig,
  loginType: "username" | "email" | "phone",
  identity: string,
  password: string,
) {
  const auth = cloudApp(config).auth({ persistence: "none" });
  const result = await withCloudTimeout(
    auth.signInWithPassword({
      [loginType]: identity.trim(),
      password,
    } as never) as Promise<AuthResult>,
    "CloudBase 登录",
  );
  const normalized = normalizeResult(result);
  if (normalized.user.displayName === normalized.user.id) {
    normalized.user.displayName = identity.trim();
  }
  return normalized;
}

export async function refreshCloudSession(
  config: CloudConfig,
  session: CloudSession,
) {
  const auth = cloudApp(config).auth({ persistence: "none" });
  return restoreAuthSession(auth, session);
}

export async function restoreAuthSession(
  auth: RestoreSessionAuth,
  session: CloudSession,
) {
  const refreshToken = session.refreshToken.trim();
  const accessToken = session.accessToken.trim();
  if (!refreshToken) {
    throw new Error("CloudBase 刷新令牌为空，请重新登录");
  }
  if (!accessToken) {
    throw new Error("CloudBase 访问令牌为空，请重新登录");
  }
  // CloudBase 3.6 的 refreshSession(token) 会先读取 SDK 内部凭据；
  // persistence:none 重启后内部凭据为空，因而会在使用传入 token 前失败。
  // 钥匙串保存完整会话，并通过公开 setSession() 恢复。SDK 会使用
  // refresh token 换取新令牌，返回值随后立即覆盖钥匙串中的旧会话。
  const result = await withCloudTimeout(
    auth.setSession({
      access_token: accessToken,
      refresh_token: refreshToken,
    }),
    "CloudBase 会话刷新",
  );
  return normalizeResult(result);
}

export async function signOutCloud(config: CloudConfig): Promise<void> {
  await cloudApp(config).auth({ persistence: "none" }).signOut();
}

export async function resolveStorageBucketId(
  config: CloudConfig,
): Promise<string> {
  const configured = configuredStorageBucketId(config);
  const signature = `${currentSignature}:${configured}`;
  if (resolvedBucketId && resolvedBucketSignature === signature) {
    return resolvedBucketId;
  }
  if (configured) {
    resolvedBucketId = configured;
    resolvedBucketSignature = signature;
    return configured;
  }

  const result = await withCloudTimeout(
    cloudApp(config).storage.listBuckets({
      limit: 100,
      sortColumn: "created_at",
      sortOrder: "asc",
    }),
    "云存储桶读取",
  );
  if (result.error) throw result.error;
  const buckets = result.data ?? [];
  const selected = selectStorageBucket(buckets);
  if (!selected) {
    if (buckets.length === 0) {
      throw new Error(
        "当前环境没有可用的云存储桶。请在 CloudBase 控制台的云存储中创建一个私有 Bucket，再在这里填写 Bucket ID。",
      );
    }
    const available = buckets.map((bucket) => bucket.id).join("、");
    throw new Error(
      `检测到多个云存储桶（${available}），无法安全地自动选择。请在同步中心填写要使用的 Bucket ID。`,
    );
  }

  resolvedBucketId = selected.id;
  resolvedBucketSignature = signature;
  localStorage.setItem(storageBucketKey(config), selected.id);
  return selected.id;
}

export function isClassicStorageEnvironmentError(error: unknown): boolean {
  const message = rawCloudErrorMessage(error);
  return (
    /nil PostgreSQL info/i.test(message)
    || /resolve connection info.*pgconn/i.test(message)
  );
}

export async function resolveStorageMode(
  config: CloudConfig,
): Promise<"classic" | "pg"> {
  if (resolvedStorageMode) return resolvedStorageMode;
  if (config.storage_mode !== "auto") {
    resolvedStorageMode = config.storage_mode;
    return resolvedStorageMode;
  }
  const bucketId = await resolveStorageBucketId(config);
  const pgFiles = cloudApp(config, "pg").storage.from(bucketId);
  // 不使用 exists() 探测：SDK 会把部分 400/404 环境错误当成
  // “文件不存在”并清空 error，导致传统环境被误判为 PG。
  const probe = await withCloudTimeout(
    pgFiles.list(
      `production-scheduler/.storage-mode-probe-${config.device.id}`,
      { limit: 1 },
    ),
    "云存储模式识别",
  );
  resolvedStorageMode = (
    probe.error && isClassicStorageEnvironmentError(probe.error)
  )
    ? "classic"
    : "pg";
  return resolvedStorageMode;
}

function cleanCloudPath(path: string): string {
  return path.replace(/^\/+|\/+$/g, "").replace(/\/+/g, "/");
}

export function classicCloudFileId(
  envId: string,
  bucketId: string,
  path: string,
): string {
  return `cloud://${envId}.${bucketId}/${cleanCloudPath(path)}`;
}

export function isClassicStorageNotFoundCode(value: unknown): boolean {
  return /FILE_NOT_FOUND|STORAGE_FILE_(?:NONEXIST|NOT_EXISTS?)/i.test(
    String(value ?? ""),
  );
}

async function localStorageProxyError(
  response: Response,
  fallback: string,
): Promise<Error> {
  const payload = await response.json().catch(() => null);
  const error = new Error(payload?.detail || fallback);
  if (response.status === 404) {
    (error as Error & { code?: string }).code = "STORAGE_FILE_NONEXIST";
  }
  return error;
}

function classicCloudFiles(
  config: CloudConfig,
  bucketId: string,
): CloudFileClient {
  // 参数保留用于明确该客户端只服务已解析的传统存储桶；实际网络请求
  // 统一由同源本机后端发起，以避开 macOS WebView 的跨域限制。
  void config;
  void bucketId;

  const upload = async (
    path: string,
    file: Blob,
    options?: { contentType?: string; upsert?: boolean },
  ): Promise<CloudFileResult<{ path: string }>> => {
    const cleanedPath = cleanCloudPath(path);
    try {
      const contentType = (
        options?.contentType
        || file.type
        || "application/octet-stream"
      );
      const form = new FormData();
      form.append("snapshot", file, cleanedPath.split("/").pop());
      form.append("cloud_path", cleanedPath);
      form.append("content_type", contentType);
      let uploadResponse: Response;
      try {
        uploadResponse = await withCloudTimeout(
          fetch("/api/cloud-sync/storage-upload", {
            method: "POST",
            body: form,
          }),
          "加密快照上传",
        );
      } catch (error) {
        throw new Error(
          `本机上传代理请求失败：${
            rawCloudErrorMessage(error) || "网络请求失败"
          }`,
        );
      }
      if (!uploadResponse.ok) {
        throw await localStorageProxyError(
          uploadResponse,
          "本机云存储上传代理失败",
        );
      }
      return { data: { path: cleanedPath }, error: null };
    } catch (error) {
      return {
        data: null,
        error: error instanceof Error
          ? error
          : new Error(String(error ?? "传统云存储上传失败")),
      };
    }
  };

  return {
    async exists(path) {
      try {
        const cleanedPath = cleanCloudPath(path);
        const response = await withCloudTimeout(
          fetch(
            `/api/cloud-sync/storage-info?path=${
              encodeURIComponent(cleanedPath)
            }`,
          ),
          "云存储文件检查",
        );
        if (response.status === 404) {
          return { data: false, error: null };
        }
        if (!response.ok) {
          throw await localStorageProxyError(
            response,
            "传统云存储文件检查失败",
          );
        }
        const result = await response.json() as { exists?: boolean };
        return { data: result.exists === true, error: null };
      } catch (error) {
        if (
          isClassicStorageNotFoundCode(rawCloudErrorCode(error))
          || isClassicStorageNotFoundCode(rawCloudErrorMessage(error))
        ) {
          return { data: false, error: null };
        }
        return {
          data: null,
          error: error instanceof Error
            ? error
            : new Error(String(error ?? "传统云存储文件检查失败")),
        };
      }
    },
    async download(path) {
      try {
        const cleanedPath = cleanCloudPath(path);
        const response = await withCloudTimeout(
          fetch(
            `/api/cloud-sync/storage-download?path=${
              encodeURIComponent(cleanedPath)
            }`,
          ),
          "云存储文件下载",
        );
        if (!response.ok) {
          throw await localStorageProxyError(
            response,
            `传统云存储下载失败（HTTP ${response.status}）`,
          );
        }
        return { data: await response.blob(), error: null };
      } catch (error) {
        return {
          data: null,
          error: error instanceof Error
            ? error
            : new Error(String(error ?? "传统云存储下载失败")),
        };
      }
    },
    upload,
    update: upload,
  };
}

export async function resolveCloudStorage(config: CloudConfig) {
  const bucketId = await resolveStorageBucketId(config);
  const mode = await resolveStorageMode(config);
  if (mode === "pg") {
    const files = cloudApp(config, mode).storage.from(
      bucketId,
    ) as unknown as CloudFileClient;
    return {
      bucketId,
      mode,
      files,
    } as const;
  }

  // SDK 3.6 的 Supabase 风格兼容层会丢弃传统接口返回的“不存在”
  // 错误码，导致首次同步被误报为无说明的 StorageError。传统环境直接
  // 使用本机同源代理调用官方传统接口，并统一为同一文件客户端。
  return {
    bucketId,
    mode,
    files: classicCloudFiles(config, bucketId),
  } as const;
}

export async function cloudFiles(config: CloudConfig) {
  return (await resolveCloudStorage(config)).files;
}

export function isCloudStorageNotFound(error: unknown): boolean {
  if (typeof error === "object" && error) {
    const possible = error as {
      code?: string;
      status?: number;
      statusCode?: number;
      error?: { code?: string };
    };
    if (
      possible.code === "FILE_NOT_FOUND"
      || possible.error?.code === "FILE_NOT_FOUND"
      || possible.status === 404
      || possible.statusCode === 404
    ) {
      return true;
    }
  }
  return (
    isClassicStorageNotFoundCode(rawCloudErrorCode(error))
    || isClassicStorageNotFoundCode(rawCloudErrorMessage(error))
    || /storage file not exists/i.test(rawCloudErrorMessage(error))
  );
}

function rawCloudErrorMessage(error: unknown): string {
  let message = "";
  if (error instanceof Error && error.message) message = error.message;
  if (typeof error === "object" && error) {
    const possible = error as {
      message?: string;
      msg?: string;
      error_description?: string;
      error?: { message?: string; msg?: string } | string;
    };
    const nested = typeof possible.error === "string"
      ? possible.error
      : possible.error?.message ?? possible.error?.msg;
    message = (
      possible.message
      ?? possible.msg
      ?? possible.error_description
      ?? nested
      ?? message
    );
  }
  return message;
}

function rawCloudErrorCode(error: unknown): string {
  if (typeof error !== "object" || !error) return "";
  const possible = error as {
    code?: string;
    error?: { code?: string };
  };
  return String(possible.code ?? possible.error?.code ?? "");
}

export function isInvalidCloudSessionError(error: unknown): boolean {
  const message = rawCloudErrorMessage(error);
  const code = rawCloudErrorCode(error);
  return (
    /(?:refresh|access|id)[ _-]?token.*(?:invalid|expired|missing|not found|revoked)/i.test(
      `${code} ${message}`,
    )
    || /(?:invalid|expired|missing|not found|revoked).*(?:refresh|access|id)[ _-]?token/i.test(
      `${code} ${message}`,
    )
    || /invalid.*(?:jwt|session)|(?:jwt|session).*(?:invalid|expired)/i.test(
      `${code} ${message}`,
    )
    || /云端用户标识无效|登录会话缺少用户标识|刷新令牌为空/i.test(message)
  );
}

function currentCloudHost(): string {
  if (typeof window === "undefined") return "当前程序地址";
  return window.location.host || "当前程序地址";
}

export function cloudErrorMessage(error: unknown): string {
  const message = rawCloudErrorMessage(error);
  if (/^获取云存储上传授权失败/i.test(message)) {
    return `${message}。请检查 CloudBase 的登录状态与存储写入权限。`;
  }
  if (/^本机上传代理请求失败/i.test(message)) {
    return `${message}。本机服务未能接收加密快照，请重启软件后重试。`;
  }
  if (/bucketId is not set/i.test(message)) {
    return "尚未配置云存储桶。请填写 CloudBase 云存储中的 Bucket ID 后重试。";
  }
  if (isCloudStorageNotFound(error)) {
    return "云端尚无同步数据，将在首次同步时自动创建。";
  }
  if (/nil PostgreSQL info|resolve connection info.*pgconn/i.test(message)) {
    return "当前 CloudBase 环境是传统模式，但程序未能连接传统云存储。请核对 Bucket ID 后重试。";
  }
  if (/row-level security|permission denied|not authorized|unauthorized/i.test(message)) {
    return "当前账户没有访问该私有存储桶的权限，请检查 CloudBase 的存储 RLS 安全策略。";
  }
  if (
    /\[OPERATION_FAIL\]\[storage\]:\s*$/i.test(message)
    || /cors|failed to fetch|load failed|network(?: request)? failed/i.test(message)
  ) {
    return (
      `云存储上传失败。程序已使用 CloudBase 默认本地来源 ${currentCloudHost()}，`
      + "无需购买或添加自定义域名。请检查“身份认证 → 权限控制”中的 "
      + "StoragesHttpApiAllow，以及云存储安全规则是否允许已登录用户读写。"
    );
  }
  return message || "云端操作失败";
}
