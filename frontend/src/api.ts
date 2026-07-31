export class ApiError extends Error {
  constructor(
    message: string,
    public status: number,
  ) {
    super(message);
  }
}

export async function api<T>(
  path: string,
  options: RequestInit = {},
): Promise<T> {
  const headers = new Headers(options.headers);
  if (!(options.body instanceof FormData) && !headers.has("Content-Type")) {
    headers.set("Content-Type", "application/json");
  }
  const response = await fetch(`/api${path}`, {
    ...options,
    headers,
  });
  if (!response.ok) {
    let message = "操作失败，请稍后重试";
    try {
      const payload = await response.json();
      message = Array.isArray(payload.detail)
        ? payload.detail.map((item: { msg: string }) => item.msg).join("；")
        : payload.detail || message;
    } catch {
      // Keep the generic message when the server did not return JSON.
    }
    throw new ApiError(message, response.status);
  }
  const method = (options.method ?? "GET").toUpperCase();
  if (
    !["GET", "HEAD", "OPTIONS"].includes(method)
    && !path.startsWith("/cloud-sync")
  ) {
    window.dispatchEvent(
      new CustomEvent("scheduler:data-mutated", {
        detail: { path, method },
      }),
    );
  }
  if (response.status === 204) return undefined as T;
  const payload = await response.json() as T;
  return payload;
}
