import { describe, expect, it, vi } from "vitest";
import {
  classicCloudFileId,
  cloudEndpointMode,
  cloudErrorMessage,
  isCloudStorageNotFound,
  isClassicStorageEnvironmentError,
  isClassicStorageNotFoundCode,
  isInvalidCloudSessionError,
  restoreAuthSession,
  selectStorageBucket,
} from "../cloud/cloudbase";

describe("CloudBase 云存储桶选择", () => {
  it("传统存储走 CLOUD_API，PG 与自动识别走 GATEWAY", () => {
    expect(cloudEndpointMode("classic")).toBe("CLOUD_API");
    expect(cloudEndpointMode("pg")).toBe("GATEWAY");
    expect(cloudEndpointMode("auto")).toBe("GATEWAY");
  });

  it("传统存储使用环境 Bucket 生成完整云文件 ID", () => {
    expect(
      classicCloudFileId(
        "schedule-env",
        "7072-schedule-env-123",
        "/production-scheduler/user/manifest.json/",
      ),
    ).toBe(
      "cloud://schedule-env.7072-schedule-env-123/"
      + "production-scheduler/user/manifest.json",
    );
  });

  it("识别传统接口的多种文件不存在错误码", () => {
    expect(isClassicStorageNotFoundCode("STORAGE_FILE_NONEXIST")).toBe(true);
    expect(isClassicStorageNotFoundCode("FILE_NOT_FOUND")).toBe(true);
    expect(isClassicStorageNotFoundCode("PERMISSION_DENIED")).toBe(false);
  });

  it("优先选择系统约定的存储桶", () => {
    const selected = selectStorageBucket([
      { id: "other", name: "其他数据", public: false },
      {
        id: "production-scheduler",
        name: "production-scheduler",
        public: false,
      },
    ]);

    expect(selected?.id).toBe("production-scheduler");
  });

  it("只有一个存储桶时自动选择", () => {
    const selected = selectStorageBucket([
      { id: "schedule-data", name: "排班数据", public: false },
    ]);

    expect(selected?.id).toBe("schedule-data");
  });

  it("多个存储桶中只有一个私有存储桶时选择私有项", () => {
    const selected = selectStorageBucket([
      { id: "public-assets", name: "公开文件", public: true },
      { id: "private-data", name: "私有排班", public: false },
    ]);

    expect(selected?.id).toBe("private-data");
  });

  it("多个候选私有存储桶时不擅自选择", () => {
    const selected = selectStorageBucket([
      { id: "private-a", name: "A", public: false },
      { id: "private-b", name: "B", public: false },
    ]);

    expect(selected).toBeNull();
  });

  it("能在中文错误转换前识别传统环境的 PG 探测错误", () => {
    const error = new Error(
      "failed to build requester session,err:pgconn: resolve connection info for env test: pgconn: nil PostgreSQL info",
    );

    expect(isClassicStorageEnvironmentError(error)).toBe(true);
    expect(cloudErrorMessage(error)).toContain("传统模式");
  });

  it("把传统存储首次同步的文件不存在识别为空云端", () => {
    const error = new Error("Storage file not exists.");

    expect(isCloudStorageNotFound(error)).toBe(true);
    expect(cloudErrorMessage(error)).toContain("首次同步");
  });

  it("把传统存储的空上传错误转换为免费本地来源操作提示", () => {
    const error = new Error(
      "[@cloudbase/js-sdk][OPERATION_FAIL][storage]:",
    );

    expect(cloudErrorMessage(error)).toContain("默认本地来源");
    expect(cloudErrorMessage(error)).toContain(window.location.host);
    expect(cloudErrorMessage(error)).toContain("无需购买");
    expect(cloudErrorMessage(error)).toContain("StoragesHttpApiAllow");
  });
});

describe("CloudBase 登录会话恢复", () => {
  it("使用安全凭据库保存的完整令牌恢复并刷新会话", async () => {
    const setSession = vi.fn().mockResolvedValue({
      data: {
        user: {
          id: "cloud-user-1",
          username: "管理员",
        },
        session: {
          access_token: "new-access-token",
          refresh_token: "new-refresh-token",
        },
      },
      error: null,
    });

    const result = await restoreAuthSession(
      { setSession },
      {
        accessToken: "  saved-access-token  ",
        refreshToken: "  saved-refresh-token  ",
      },
    );

    expect(setSession).toHaveBeenCalledOnce();
    expect(setSession).toHaveBeenCalledWith({
      access_token: "saved-access-token",
      refresh_token: "saved-refresh-token",
    });
    expect(result).toEqual({
      user: {
        id: "cloud-user-1",
        displayName: "管理员",
      },
      session: {
        accessToken: "new-access-token",
        refreshToken: "new-refresh-token",
      },
    });
  });

  it("拒绝空刷新令牌且不会调用 SDK", async () => {
    const setSession = vi.fn();

    await expect(
      restoreAuthSession(
        { setSession },
        { accessToken: "saved-access-token", refreshToken: "   " },
      ),
    ).rejects.toThrow("刷新令牌为空");
    expect(setSession).not.toHaveBeenCalled();
  });

  it("只把明确失效的令牌或会话识别为需要重新登录", () => {
    expect(
      isInvalidCloudSessionError({
        code: "REFRESH_TOKEN_EXPIRED",
        message: "refresh token expired",
      }),
    ).toBe(true);
    expect(
      isInvalidCloudSessionError(new Error("云端用户标识无效")),
    ).toBe(true);
    expect(
      isInvalidCloudSessionError(new Error("Network request failed")),
    ).toBe(false);
  });
});
