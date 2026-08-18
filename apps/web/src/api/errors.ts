import type { AppError } from "@jarvis/shared";

export const NETWORK_UNAVAILABLE_ERROR: AppError = {
  code: "NETWORK_UNAVAILABLE",
  message: "无法连接 Jarvis 服务，请检查服务状态后重试",
  category: "internal",
  recoverable: true,
};

export function normalizeClientError(error: unknown, fallback: string): AppError {
  if (error && typeof error === "object") {
    const candidate = error as Partial<AppError>;
    if (
      typeof candidate.code === "string" &&
      typeof candidate.message === "string" &&
      typeof candidate.category === "string" &&
      typeof candidate.recoverable === "boolean"
    ) {
      return candidate as AppError;
    }
  }

  return {
    ...NETWORK_UNAVAILABLE_ERROR,
    message: fallback,
  };
}
