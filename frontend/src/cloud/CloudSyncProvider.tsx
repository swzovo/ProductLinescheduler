import {
  createContext,
  FormEvent,
  ReactNode,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useRef,
  useState,
} from "react";
import { api, ApiError } from "../api";
import {
  CloudConfig,
  CloudSession,
  cloudErrorMessage,
  cloudFiles,
  CloudUser,
  configuredPublishableKey,
  configuredStorageBucketId,
  isCloudStorageNotFound,
  isInvalidCloudSessionError,
  refreshCloudSession,
  resolveCloudStorage,
  savePublishableKey,
  saveStorageBucketId,
  signInWithPassword,
  signOutCloud,
} from "./cloudbase";
import {
  appendRevision,
  CloudManifest,
  CloudRevision,
  currentRevision,
  isManifest,
  revisionId,
} from "./revisions";

type SyncStatus =
  | "offline"
  | "checking"
  | "syncing"
  | "synced"
  | "attention"
  | "error";

type LocalState = {
  user_namespace: string;
  device: CloudConfig["device"];
  last_revision_id: string | null;
  last_plain_sha256: string | null;
  last_sync_at: string | null;
  counts: Record<string, number>;
};

type Fingerprint = {
  plain_sha256: string;
  database_size: number;
  counts: Record<string, number>;
};

type SavedSession = {
  available: boolean;
  user_id?: string;
  access_token?: string;
  refresh_token?: string;
  display_name?: string;
};

type SyncContextValue = {
  status: SyncStatus;
  statusText: string;
  user: CloudUser | null;
  offline: boolean;
  openCenter: () => void;
  syncNow: () => Promise<void>;
};

const defaultContext: SyncContextValue = {
  status: "offline",
  statusText: "仅本机模式",
  user: null,
  offline: true,
  openCenter: () => undefined,
  syncNow: async () => undefined,
};

const SyncContext = createContext<SyncContextValue>(defaultContext);

function storagePath(namespace: string, file: string): string {
  return `production-scheduler/${namespace}/${file}`;
}

function hasBusinessData(counts: Record<string, number>): boolean {
  return Object.values(counts).some((value) => value > 0);
}

function errorText(error: unknown): string {
  if (error instanceof ApiError) return error.message;
  return cloudErrorMessage(error);
}


async function responseErrorText(
  response: Response,
  fallback: string,
): Promise<string> {
  const raw = await response.text().catch(() => "");
  let detail = "";
  try {
    const payload = JSON.parse(raw) as { detail?: unknown };
    if (typeof payload.detail === "string") detail = payload.detail;
  } catch {
    detail = raw.trim().slice(0, 240);
  }
  return detail
    ? `${fallback}（HTTP ${response.status}）：${detail}`
    : `${fallback}（HTTP ${response.status}）`;
}


async function waitAtMost<T>(
  operation: Promise<T>,
  milliseconds: number,
  timeoutMessage: string,
): Promise<T> {
  let timer = 0;
  const timeout = new Promise<never>((_, reject) => {
    timer = window.setTimeout(
      () => reject(new Error(timeoutMessage)),
      milliseconds,
    );
  });
  try {
    return await Promise.race([operation, timeout]);
  } finally {
    window.clearTimeout(timer);
  }
}

export function useCloudSync(): SyncContextValue {
  return useContext(SyncContext);
}

export function CloudSyncProvider({ children }: { children: ReactNode }) {
  const [config, setConfig] = useState<CloudConfig | null>(null);
  const [booting, setBooting] = useState(true);
  const [offline, setOffline] = useState(false);
  const [user, setUser] = useState<CloudUser | null>(null);
  const [status, setStatus] = useState<SyncStatus>("checking");
  const [statusText, setStatusText] = useState("正在检查云端账户");
  const [loginError, setLoginError] = useState("");
  const [centerOpen, setCenterOpen] = useState(false);
  const [dialog, setDialog] = useState<
    "none" | "recovery-created" | "recovery-import" | "conflict"
  >("none");
  const [recoveryKey, setRecoveryKey] = useState("");
  const [recoveryConfirmed, setRecoveryConfirmed] = useState(false);
  const [recoveryIsNew, setRecoveryIsNew] = useState(false);
  const [recoveryReplacing, setRecoveryReplacing] = useState(false);
  const [dialogError, setDialogError] = useState("");
  const [busy, setBusy] = useState(false);
  const [advancedOpen, setAdvancedOpen] = useState(false);
  const [publishableKey, setPublishableKeyState] = useState("");
  const [storageBucketId, setStorageBucketIdState] = useState("");
  const [storageMode, setStorageMode] = useState<"classic" | "pg" | "">("");
  const [bucketEditorOpen, setBucketEditorOpen] = useState(false);
  const syncTimer = useRef<number | null>(null);
  const syncPromise = useRef<Promise<void> | null>(null);
  const cloudSession = useRef<CloudSession | null>(null);

  const saveSession = useCallback(
    async (
      activeUser: CloudUser,
      session: CloudSession,
    ) => {
      await api("/cloud-sync/session", {
        method: "PUT",
        body: JSON.stringify({
          user_id: activeUser.id,
          access_token: session.accessToken,
          refresh_token: session.refreshToken,
          display_name: activeUser.displayName,
        }),
      });
      cloudSession.current = session;
    },
    [],
  );

  const getManifest = useCallback(
    async (
      activeConfig: CloudConfig,
      namespace: string,
    ): Promise<CloudManifest | null> => {
      const files = await cloudFiles(activeConfig);
      const path = storagePath(namespace, "manifest.json");
      let existsResult;
      try {
        existsResult = await files.exists(path);
      } catch (error) {
        if (isCloudStorageNotFound(error)) return null;
        throw error;
      }
      if (existsResult.error && isCloudStorageNotFound(existsResult.error)) {
        return null;
      }
      if (existsResult.error) throw existsResult.error;
      if (!existsResult.data) return null;
      const result = await files.download(path);
      if (result.error) throw result.error;
      const payload = JSON.parse(await result.data.text()) as unknown;
      if (!isManifest(payload)) {
        throw new Error("云端同步索引格式不正确");
      }
      return payload;
    },
    [],
  );

  const putCloudFile = useCallback(
    async (
      activeConfig: CloudConfig,
      path: string,
      blob: Blob,
      contentType: string,
      overwrite: boolean,
    ) => {
      const files = await cloudFiles(activeConfig);
      const options = { contentType, upsert: overwrite };
      const result = overwrite
        ? await files.update(path, blob, options)
        : await files.upload(path, blob, options);
      if (result.error) throw result.error;
    },
    [],
  );

  const saveLocalState = useCallback(
    async (
      activeUser: CloudUser,
      revision: CloudRevision,
    ) => {
      await api("/cloud-sync/state", {
        method: "PUT",
        body: JSON.stringify({
          user_id: activeUser.id,
          last_revision_id: revision.id,
          last_plain_sha256: revision.plain_sha256,
          last_sync_at: new Date().toISOString(),
        }),
      });
    },
    [],
  );

  const uploadLocal = useCallback(
    async (
      activeConfig: CloudConfig,
      activeUser: CloudUser,
      local: LocalState,
      expectedManifest: CloudManifest | null,
    ) => {
      setStatus("syncing");
      setStatusText("正在加密并上传本机数据");
      const response = await fetch("/api/cloud-sync/snapshot", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ user_id: activeUser.id }),
      });
      if (!response.ok) {
        const payload = await response.json().catch(() => null);
        throw new Error(payload?.detail || "本机数据库加密失败");
      }
      const encrypted = await response.blob();
      const plainSha = response.headers.get("X-Database-Sha256") ?? "";
      const encryptedSha = response.headers.get("X-Encrypted-Sha256") ?? "";
      const databaseSize = Number(
        response.headers.get("X-Database-Size") ?? encrypted.size,
      );
      const id = revisionId(local.device.id);
      const snapshotPath = storagePath(
        local.user_namespace,
        `revisions/${id}.plsync`,
      );
      await putCloudFile(
        activeConfig,
        snapshotPath,
        encrypted,
        "application/octet-stream",
        false,
      );

      const latestManifest = await getManifest(
        activeConfig,
        local.user_namespace,
      );
      if (
        latestManifest?.current_revision_id
        !== expectedManifest?.current_revision_id
      ) {
        setDialog("conflict");
        setStatus("attention");
        setStatusText("检测到另一台设备刚刚更新了数据");
        return null;
      }
      const now = new Date().toISOString();
      const revision: CloudRevision = {
        id,
        parent_revision_id: latestManifest?.current_revision_id ?? null,
        created_at: now,
        device_id: local.device.id,
        device_name: local.device.name,
        plain_sha256: plainSha,
        encrypted_sha256: encryptedSha,
        database_size: databaseSize,
        snapshot_path: snapshotPath,
      };
      const manifest = appendRevision(latestManifest, revision);
      const manifestPath = storagePath(
        local.user_namespace,
        "manifest.json",
      );
      const manifestBlob = new Blob(
        [JSON.stringify(manifest, null, 2)],
        { type: "application/json" },
      );
      const exists = latestManifest !== null;
      await putCloudFile(
        activeConfig,
        manifestPath,
        manifestBlob,
        "application/json",
        exists,
      );
      await saveLocalState(activeUser, revision);
      setStatus("synced");
      setStatusText("云端数据已同步");
      return revision;
    },
    [getManifest, putCloudFile, saveLocalState],
  );

  const restoreRemote = useCallback(
    async (
      activeConfig: CloudConfig,
      activeUser: CloudUser,
      revision: CloudRevision,
    ) => {
      setStatus("syncing");
      setStatusText("正在恢复云端数据");
      const files = await cloudFiles(activeConfig);
      const result = await files.download(revision.snapshot_path);
      if (result.error) throw result.error;
      const form = new FormData();
      form.append("snapshot", result.data, `${revision.id}.plsync`);
      const response = await fetch(
        `/api/cloud-sync/restore?user_id=${encodeURIComponent(activeUser.id)}`,
        { method: "POST", body: form },
      );
      if (!response.ok) {
        throw new Error(await responseErrorText(response, "云端数据恢复失败"));
      }
      await saveLocalState(activeUser, revision);
      setStatus("synced");
      setStatusText("云端数据已恢复");
      window.location.reload();
    },
    [saveLocalState],
  );

  const reconcile = useCallback(
    async (
      activeConfig: CloudConfig,
      activeUser: CloudUser,
      forceKeepLocal = false,
    ) => {
      const activeStorage = await resolveCloudStorage(activeConfig);
      setStorageBucketIdState(activeStorage.bucketId);
      setStorageMode(activeStorage.mode);
      const local = await api<LocalState>(
        `/cloud-sync/state?user_id=${encodeURIComponent(activeUser.id)}`,
      );
      const key = await api<{ available: boolean }>(
        `/cloud-sync/key?user_id=${encodeURIComponent(activeUser.id)}`,
      );
      const manifest = await getManifest(
        activeConfig,
        local.user_namespace,
      );
      if (!key.available) {
        if (manifest) {
          setRecoveryReplacing(false);
          setDialog("recovery-import");
          setStatus("attention");
          setStatusText("需要恢复密钥才能读取云端数据");
          return;
        }
        const generated = await api<{
          created: boolean;
          recovery_key: string;
        }>("/cloud-sync/key/generate", {
          method: "POST",
          body: JSON.stringify({ user_id: activeUser.id }),
        });
        setRecoveryKey(generated.recovery_key);
        setRecoveryConfirmed(false);
        setRecoveryIsNew(true);
        setDialog("recovery-created");
        setStatus("attention");
        setStatusText("请先保存云端恢复密钥");
        return;
      }

      const fingerprint = await api<Fingerprint>(
        `/cloud-sync/fingerprint?user_id=${encodeURIComponent(activeUser.id)}`,
      );
      const remote = currentRevision(manifest);
      if (!remote) {
        await uploadLocal(activeConfig, activeUser, local, null);
        return;
      }
      if (fingerprint.plain_sha256 === remote.plain_sha256) {
        await saveLocalState(activeUser, remote);
        setStatus("synced");
        setStatusText("云端数据已同步");
        return;
      }
      if (forceKeepLocal) {
        await uploadLocal(activeConfig, activeUser, local, manifest);
        setDialog("none");
        return;
      }
      if (local.last_revision_id === remote.id) {
        await uploadLocal(activeConfig, activeUser, local, manifest);
        return;
      }
      if (
        local.last_revision_id
        && local.last_plain_sha256 === fingerprint.plain_sha256
      ) {
        await restoreRemote(activeConfig, activeUser, remote);
        return;
      }
      if (!local.last_revision_id && !hasBusinessData(fingerprint.counts)) {
        await restoreRemote(activeConfig, activeUser, remote);
        return;
      }
      setDialog("conflict");
      setStatus("attention");
      setStatusText("本机与云端均有不同数据，请选择保留版本");
    },
    [getManifest, restoreRemote, saveLocalState, uploadLocal],
  );

  const syncNow = useCallback(async () => {
    if (!config || !user || offline) return;
    if (syncPromise.current) return syncPromise.current;
    const operation = (async () => {
      try {
        setBusy(true);
        setDialogError("");
        const savedSession = cloudSession.current;
        if (!savedSession) {
          throw new Error("云端登录会话尚未恢复，请重新登录");
        }
        // auth({ persistence: "none" }) 每次都会创建独立的内存凭据库，
        // 因此不能在这里调用一个全新的 auth.refreshSession()。始终把
        // 钥匙串中最近一次保存的完整会话交给 setSession()，由 SDK
        // 刷新令牌并返回下一组令牌，保证重启后的首次修改也能同步。
        const refreshed = await refreshCloudSession(config, savedSession);
        const activeUser = {
          id: user.id,
          displayName: refreshed.user.displayName || user.displayName,
        };
        await saveSession(activeUser, refreshed.session);
        setUser(activeUser);
        await reconcile(config, activeUser);
      } catch (error) {
        setStatus("error");
        setStatusText(errorText(error));
        setDialogError(errorText(error));
      } finally {
        setBusy(false);
        syncPromise.current = null;
      }
    })();
    syncPromise.current = operation;
    return operation;
  }, [config, offline, reconcile, saveSession, user]);

  const acceptGeneratedKey = useCallback(async () => {
    setDialog("none");
    setRecoveryKey("");
    if (config && user) {
      await reconcile(config, user);
    }
  }, [config, reconcile, user]);

  const importKey = useCallback(async () => {
    if (!config || !user || !recoveryKey.trim()) return;
    try {
      setBusy(true);
      setDialogError("");
      const local = await api<LocalState>(
        `/cloud-sync/state?user_id=${encodeURIComponent(user.id)}`,
      );
      const manifest = await getManifest(config, local.user_namespace);
      const remote = currentRevision(manifest);
      if (!remote) throw new Error("云端没有可用于验证密钥的数据版本");
      const files = await cloudFiles(config);
      const downloaded = await files.download(remote.snapshot_path);
      if (downloaded.error) throw downloaded.error;
      const form = new FormData();
      form.append("recovery_key", recoveryKey.trim());
      form.append("snapshot", downloaded.data, `${remote.id}.plsync`);
      const validation = await fetch(
        `/api/cloud-sync/key/validate?user_id=${encodeURIComponent(user.id)}`,
        { method: "POST", body: form },
      );
      if (!validation.ok) {
        throw new Error(
          await responseErrorText(validation, "恢复密钥验证失败"),
        );
      }
      await api("/cloud-sync/key/import", {
        method: "POST",
        body: JSON.stringify({
          user_id: user.id,
          recovery_key: recoveryKey,
        }),
      });
      setDialog("none");
      setRecoveryKey("");
      setRecoveryReplacing(false);
      await reconcile(config, user);
    } catch (error) {
      setDialogError(errorText(error));
    } finally {
      setBusy(false);
    }
  }, [config, getManifest, reconcile, recoveryKey, user]);

  const beginRecoveryKeyReplacement = useCallback(async () => {
    if (!user) return;
    try {
      setBusy(true);
      setDialogError("");
      setRecoveryKey("");
      setRecoveryConfirmed(false);
      setRecoveryIsNew(false);
      setRecoveryReplacing(true);
      setCenterOpen(false);
      setDialog("recovery-import");
      setStatus("attention");
      setStatusText("请输入能够解密云端最新数据的恢复密钥");
    } catch (error) {
      setDialogError(errorText(error));
    } finally {
      setBusy(false);
    }
  }, [user]);

  const chooseRemote = useCallback(async () => {
    if (!config || !user) return;
    try {
      setBusy(true);
      setDialogError("");
      const local = await api<LocalState>(
        `/cloud-sync/state?user_id=${encodeURIComponent(user.id)}`,
      );
      const manifest = await getManifest(config, local.user_namespace);
      const remote = currentRevision(manifest);
      if (!remote) throw new Error("云端没有可恢复的数据版本");
      await restoreRemote(config, user, remote);
    } catch (error) {
      setDialogError(errorText(error));
    } finally {
      setBusy(false);
    }
  }, [config, getManifest, restoreRemote, user]);

  const chooseLocal = useCallback(async () => {
    if (!config || !user) return;
    try {
      setBusy(true);
      setDialogError("");
      await reconcile(config, user, true);
    } catch (error) {
      setDialogError(errorText(error));
    } finally {
      setBusy(false);
    }
  }, [config, reconcile, user]);

  const repairRemoteFromLocal = useCallback(async () => {
    if (!window.confirm(
      "确认以这台设备当前显示的排班数据修复云端吗？云端会保留历史修订，但最新版本将改为本机数据。",
    )) return;
    await chooseLocal();
  }, [chooseLocal]);

  const showRecoveryKey = useCallback(async () => {
    if (!user) return;
    try {
      setBusy(true);
      setDialogError("");
      const saved = await api<{
        created: boolean;
        recovery_key: string;
      }>("/cloud-sync/key/generate", {
        method: "POST",
        body: JSON.stringify({ user_id: user.id }),
      });
      setRecoveryKey(saved.recovery_key);
      setRecoveryConfirmed(false);
      setRecoveryIsNew(false);
      setRecoveryReplacing(false);
      setCenterOpen(false);
      setDialog("recovery-created");
    } catch (error) {
      setDialogError(errorText(error));
    } finally {
      setBusy(false);
    }
  }, [user]);

  useEffect(() => {
    let cancelled = false;
    void (async () => {
      try {
        const loadedConfig = await api<CloudConfig>("/cloud-sync/config");
        if (cancelled) return;
        setConfig(loadedConfig);
        setPublishableKeyState(configuredPublishableKey(loadedConfig));
        setStorageBucketIdState(configuredStorageBucketId(loadedConfig));
        setStorageMode(
          loadedConfig.storage_mode === "auto"
            ? ""
            : loadedConfig.storage_mode,
        );
        const saved = await waitAtMost(
          api<SavedSession>("/cloud-sync/session"),
          10_000,
          "读取本机安全登录信息超时",
        );
        if (
          saved.available
          && saved.access_token
          && saved.refresh_token
          && saved.user_id
        ) {
          try {
            const refreshed = await refreshCloudSession(
              loadedConfig,
              {
                accessToken: saved.access_token,
                refreshToken: saved.refresh_token,
              },
            );
            if (cancelled) return;
            // CloudBase setSession() 在传统环境的恢复响应中，user.id
            // 可能是完整用户对象而不是首次登录时的字符串 ID。钥匙串
            // 中的 user_id 已在首次登录时验证，因此恢复和续期始终沿用
            // 该稳定 ID，避免跨设备命名空间发生漂移。
            const activeUser = {
              id: saved.user_id,
              displayName: (
                saved.display_name
                || refreshed.user.displayName
                || saved.user_id
              ),
            };
            await saveSession(activeUser, refreshed.session);
            setUser(activeUser);
            setBooting(false);
            await reconcile(loadedConfig, activeUser);
            return;
          } catch (error) {
            const message = errorText(error);
            if (isInvalidCloudSessionError(error)) {
              await signOutCloud(loadedConfig).catch(() => undefined);
              await api("/cloud-sync/session", {
                method: "DELETE",
              }).catch(() => undefined);
              setLoginError(
                `${message}。原登录已失效，请重新输入账号密码。`,
              );
            } else {
              setLoginError(
                `${message}。已保留本机登录信息，您可以重新输入密码后继续。`,
              );
            }
          }
        } else if (saved.available && saved.refresh_token) {
          setLoginError(
            "本机保存的是旧版登录信息，请重新输入账号密码完成安全升级。",
          );
        }
        setBooting(false);
        setStatus("offline");
        setStatusText("尚未登录云端账户");
      } catch (error) {
        if (cancelled) return;
        setBooting(false);
        const message = errorText(error);
        setLoginError(
          message.includes("安全登录信息")
            ? `${message}。请重新输入账号密码，本机排班数据未受影响。`
            : message,
        );
        setStatus("error");
        setStatusText("云端初始化失败");
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [reconcile, saveSession]);

  useEffect(() => {
    if (!user || offline) return;
    const onMutation = () => {
      setStatus("syncing");
      setStatusText("本机有新修改，等待同步");
      if (syncTimer.current !== null) {
        window.clearTimeout(syncTimer.current);
      }
      syncTimer.current = window.setTimeout(() => {
        void syncNow();
      }, 1800);
    };
    window.addEventListener("scheduler:data-mutated", onMutation);
    return () => {
      window.removeEventListener("scheduler:data-mutated", onMutation);
      if (syncTimer.current !== null) {
        window.clearTimeout(syncTimer.current);
      }
    };
  }, [offline, syncNow, user]);

  async function handleLogin(
    event: FormEvent<HTMLFormElement>,
  ): Promise<void> {
    event.preventDefault();
    if (!config) return;
    const form = new FormData(event.currentTarget);
    const identity = String(form.get("identity") ?? "");
    const password = String(form.get("password") ?? "");
    const loginType = String(form.get("loginType") ?? "username") as
      "username" | "email" | "phone";
    try {
      setBusy(true);
      setLoginError("");
      const result = await signInWithPassword(
        config,
        loginType,
        identity,
        password,
      );
      await saveSession(result.user, result.session);
      setUser(result.user);
      setOffline(false);
      setStatus("checking");
      setStatusText("正在核对本机与云端版本");
      await reconcile(config, result.user);
    } catch (error) {
      setLoginError(errorText(error));
    } finally {
      setBusy(false);
    }
  }

  async function logout(): Promise<void> {
    if (!config) return;
    try {
      setBusy(true);
      await signOutCloud(config).catch(() => undefined);
      await api("/cloud-sync/session", { method: "DELETE" });
      cloudSession.current = null;
      setUser(null);
      setOffline(false);
      setCenterOpen(false);
      setDialog("none");
      setStatus("offline");
      setStatusText("尚未登录云端账户");
    } finally {
      setBusy(false);
    }
  }

  function enableOffline(): void {
    setOffline(true);
    setBooting(false);
    setStatus("offline");
    setStatusText("仅本机模式");
  }

  function applyPublishableKey(): void {
    savePublishableKey(publishableKey);
    if (config) saveStorageBucketId(config, storageBucketId);
    setLoginError("发布密钥已保存，请重新登录");
    setAdvancedOpen(false);
  }

  async function applyStorageBucket(): Promise<void> {
    if (!config || !user) return;
    saveStorageBucketId(config, storageBucketId);
    try {
      setBusy(true);
      setDialogError("");
      setStatus("checking");
      setStatusText("正在检查云存储桶");
      await reconcile(config, user);
      setBucketEditorOpen(false);
    } catch (error) {
      const message = errorText(error);
      setStatus("error");
      setStatusText(message);
      setDialogError(message);
    } finally {
      setBusy(false);
    }
  }

  const context = useMemo<SyncContextValue>(
    () => ({
      status,
      statusText,
      user,
      offline,
      openCenter: () => setCenterOpen(true),
      syncNow,
    }),
    [offline, status, statusText, syncNow, user],
  );

  if (booting) {
    return (
      <div className="cloud-gate loading">
        <div className="cloud-logo">PL</div>
        <h1>产线排班</h1>
        <p>正在安全地检查本机与云端数据…</p>
      </div>
    );
  }

  if (!user && !offline) {
    return (
      <div className="cloud-gate">
        <div className="cloud-login-card">
          <div className="cloud-login-brand">
            <div className="cloud-logo">PL</div>
            <div>
              <span>PRODUCTION FLOW</span>
              <h1>登录产线排班</h1>
              <p>同一账户可在 Mac 与 Windows 设备之间同步数据。</p>
            </div>
          </div>
          <form onSubmit={handleLogin}>
            <label>
              <span>登录方式</span>
              <select name="loginType" defaultValue="username">
                <option value="username">用户名</option>
                <option value="email">邮箱</option>
                <option value="phone">手机号（含 +86）</option>
              </select>
            </label>
            <label>
              <span>账户</span>
              <input
                name="identity"
                autoComplete="username"
                required
                placeholder="输入 CloudBase 测试账户"
              />
            </label>
            <label>
              <span>密码</span>
              <input
                name="password"
                type="password"
                autoComplete="current-password"
                required
                placeholder="输入密码"
              />
            </label>
            {loginError && <div className="cloud-form-error">{loginError}</div>}
            <button className="primary-button cloud-login-submit" disabled={busy}>
              {busy ? "正在登录…" : "登录并同步"}
            </button>
          </form>
          <button className="text-button" onClick={() => setAdvancedOpen(!advancedOpen)}>
            {advancedOpen ? "收起连接设置" : "连接设置"}
          </button>
          {advancedOpen && config && (
            <div className="cloud-advanced">
              <p>环境：{config.env_id}（上海）</p>
              <label>
                <span>Web 发布密钥（Publishable Key）</span>
                <input
                  value={publishableKey}
                  onChange={(event) => setPublishableKeyState(event.target.value)}
                  placeholder="如控制台要求，请粘贴 Publishable Key"
                />
              </label>
              <label>
                <span>云存储 Bucket ID</span>
                <input
                  value={storageBucketId}
                  onChange={(event) => setStorageBucketIdState(event.target.value)}
                  placeholder="留空时自动识别唯一的私有 Bucket"
                />
              </label>
              <small>只能填写 Publishable Key，请勿填写 SecretId 或 SecretKey。</small>
              <button className="secondary-button" onClick={applyPublishableKey}>
                保存连接设置
              </button>
            </div>
          )}
          <button className="ghost-button cloud-offline-button" onClick={enableOffline}>
            暂不登录，仅在本机使用
          </button>
        </div>
      </div>
    );
  }

  return (
    <SyncContext.Provider value={context}>
      {children}
      {centerOpen && (
        <div className="modal-backdrop">
          <div className="modal cloud-center">
            <div className="modal-header">
              <div>
                <span className="eyebrow">CLOUD SYNC</span>
                <h2>账户与云同步</h2>
              </div>
              <button className="icon-button" onClick={() => setCenterOpen(false)}>×</button>
            </div>
            <div className="modal-content">
              <div className={`cloud-status-card ${status}`}>
                <span className="cloud-status-dot" />
                <div>
                  <strong>{statusText}</strong>
                  <small>
                    {offline
                      ? "当前修改仅保存在此设备"
                      : `账户：${user?.displayName ?? ""}`}
                  </small>
                </div>
              </div>
              {user && config && (
                <dl className="cloud-account-detail">
                  <div><dt>云环境</dt><dd>{config.env_id}</dd></div>
                  <div>
                    <dt>云存储桶</dt>
                    <dd>
                      {storageBucketId || "等待自动识别"}
                      {storageMode && (
                        <span className="cloud-storage-mode">
                          {storageMode === "classic" ? "传统模式" : "PG 模式"}
                        </span>
                      )}
                      <button
                        className="text-button cloud-inline-action"
                        onClick={() => setBucketEditorOpen(!bucketEditorOpen)}
                      >
                        {bucketEditorOpen ? "取消修改" : "修改"}
                      </button>
                    </dd>
                  </div>
                  <div>
                    <dt>本地来源</dt>
                    <dd>{window.location.host}（CloudBase 默认来源）</dd>
                  </div>
                  <div><dt>当前设备</dt><dd>{config.device.name} · {config.device.platform}</dd></div>
                  <div><dt>保护方式</dt><dd>AES-256 加密快照 + 系统安全凭据存储</dd></div>
                </dl>
              )}
              {user && config && bucketEditorOpen && (
                <div className="cloud-bucket-editor">
                  <label>
                    <span>Bucket ID</span>
                    <input
                      value={storageBucketId}
                      onChange={(event) => setStorageBucketIdState(event.target.value)}
                      placeholder="例如 production-scheduler"
                    />
                  </label>
                  <small>
                    在 CloudBase 控制台“云存储”页面复制 Bucket ID；留空保存会重新自动识别。
                  </small>
                  <button
                    className="secondary-button"
                    disabled={busy}
                    onClick={() => void applyStorageBucket()}
                  >
                    保存并重新同步
                  </button>
                </div>
              )}
              {dialogError && <div className="cloud-form-error">{dialogError}</div>}
              <div className="form-actions">
                {!offline && (
                  <button className="secondary-button" disabled={busy} onClick={() => void syncNow()}>
                    {busy ? "同步中…" : "立即同步"}
                  </button>
                )}
                {user && status === "error" && (
                  <button className="warning-button" disabled={busy} onClick={() => void repairRemoteFromLocal()}>
                    以本机数据修复云端
                  </button>
                )}
                {user && (
                  <button className="secondary-button" disabled={busy} onClick={() => void showRecoveryKey()}>
                    查看恢复密钥
                  </button>
                )}
                {user && (
                  <button className="secondary-button" disabled={busy} onClick={() => void beginRecoveryKeyReplacement()}>
                    更换恢复密钥
                  </button>
                )}
                {user ? (
                  <button className="ghost-button danger" disabled={busy} onClick={() => void logout()}>
                    退出账户
                  </button>
                ) : (
                  <button className="secondary-button" onClick={() => {
                    setOffline(false);
                    setCenterOpen(false);
                  }}>
                    登录云端账户
                  </button>
                )}
              </div>
            </div>
          </div>
        </div>
      )}
      {dialog === "recovery-created" && (
        <div className="modal-backdrop">
          <div className="modal recovery-dialog">
            <div className="modal-header">
              <div>
                <span className="eyebrow">IMPORTANT</span>
                <h2>保存云端恢复密钥</h2>
              </div>
              {!recoveryIsNew && (
                <button
                  className="icon-button"
                  onClick={() => {
                    setDialog("none");
                    setRecoveryKey("");
                  }}
                >
                  ×
                </button>
              )}
            </div>
            <div className="modal-content">
              <p>
                {recoveryIsNew
                  ? "这是解密云端排班数据的唯一密钥。请保存到公司密码管理器或其他安全位置；在 Windows 设备首次登录时需要输入。"
                  : "这是当前账户的恢复密钥。请只复制到公司的密码管理器或其他安全位置，不要通过普通聊天工具发送。"}
              </p>
              <div className="recovery-key-box">
                <code>{recoveryKey}</code>
                <button
                  className="secondary-button"
                  onClick={() => void navigator.clipboard.writeText(recoveryKey)}
                >
                  复制密钥
                </button>
              </div>
              {recoveryIsNew && (
                <label className="check-field recovery-confirm">
                  <input
                    type="checkbox"
                    checked={recoveryConfirmed}
                    onChange={(event) => setRecoveryConfirmed(event.target.checked)}
                  />
                  <span>我已将恢复密钥保存在安全位置</span>
                </label>
              )}
              <div className="form-actions">
                <button
                  className="primary-button"
                  disabled={(recoveryIsNew && !recoveryConfirmed) || busy}
                  onClick={() => void acceptGeneratedKey()}
                >
                  {recoveryIsNew ? "开始首次加密同步" : "关闭"}
                </button>
              </div>
            </div>
          </div>
        </div>
      )}
      {dialog === "recovery-import" && (
        <div className="modal-backdrop">
          <div className="modal recovery-dialog">
            <div className="modal-header">
              <div>
                <span className="eyebrow">{recoveryReplacing ? "REPLACE KEY" : "NEW DEVICE"}</span>
                <h2>{recoveryReplacing ? "更换恢复密钥" : "输入恢复密钥"}</h2>
              </div>
            </div>
            <div className="modal-content">
              <p>
                {recoveryReplacing
                  ? "请输入与云端最新版本匹配的恢复密钥。系统会先实际解密并校验数据库，验证成功后才保存。"
                  : "此账户已有加密云端数据。请输入在第一台设备保存的恢复密钥，系统验证成功后会自动恢复最新排班。"}
              </p>
              <label className="recovery-input">
                <span>恢复密钥</span>
                <input
                  value={recoveryKey}
                  onChange={(event) => setRecoveryKey(event.target.value)}
                  placeholder="PLS1.…"
                  autoFocus
                />
              </label>
              {dialogError && <div className="cloud-form-error">{dialogError}</div>}
              <div className="form-actions">
                <button
                  className="primary-button"
                  disabled={!recoveryKey.trim() || busy}
                  onClick={() => void importKey()}
                >
                  {busy ? "正在验证…" : "验证并恢复云端数据"}
                </button>
              </div>
            </div>
          </div>
        </div>
      )}
      {dialog === "conflict" && (
        <div className="modal-backdrop">
          <div className="modal conflict-dialog">
            <div className="modal-header">
              <div>
                <span className="eyebrow">SYNC CONFLICT</span>
                <h2>本机与云端数据不同</h2>
              </div>
            </div>
            <div className="modal-content">
              <p>系统没有自动覆盖任何一方。请选择要继续使用的数据；选择云端前，本机数据库仍会自动生成安全备份。</p>
              {dialogError && <div className="cloud-form-error">{dialogError}</div>}
              <div className="conflict-options">
                <button className="secondary-button" disabled={busy} onClick={() => void chooseRemote()}>
                  使用云端最新数据
                  <small>先备份本机，再恢复另一台设备的版本</small>
                </button>
                <button className="warning-button" disabled={busy} onClick={() => void chooseLocal()}>
                  保留本机并上传
                  <small>把本机当前数据设为新的云端版本</small>
                </button>
              </div>
              <button
                className="ghost-button"
                disabled={busy}
                onClick={() => void beginRecoveryKeyReplacement()}
              >
                当前密钥不匹配？更换恢复密钥
              </button>
            </div>
          </div>
        </div>
      )}
    </SyncContext.Provider>
  );
}
