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
  if (response.status === 204) return undefined as T;
  return response.json() as Promise<T>;
}
