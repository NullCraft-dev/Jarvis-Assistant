// Transport Layer — 封装 fetch + SSE，提供类型安全的 API 通信
// 分层：Frontend State → transport → Go Gateway
// 真源：docs/13-interface-contract.md

import type { ApiResult, RuntimeEvent, ID } from "@jarvis/shared";
import { NETWORK_UNAVAILABLE_ERROR } from "./errors";

const BASE_URL = "/api";

async function request<T>(path: string, init?: RequestInit): Promise<ApiResult<T>> {
  let res: Response;
  try {
    res = await fetch(`${BASE_URL}${path}`, init);
  } catch {
    return { ok: false, error: { ...NETWORK_UNAVAILABLE_ERROR } };
  }

  if (!res.ok) {
    return extractError(res);
  }

  try {
    return await res.json();
  } catch {
    return {
      ok: false,
      error: {
        code: "INVALID_SERVICE_RESPONSE",
        message: "Jarvis 服务返回了无法识别的响应，请稍后重试",
        category: "internal",
        recoverable: true,
      },
    };
  }
}

/** 尝试从非 2xx 响应中提取后端 AppError */
async function extractError(res: Response): Promise<ApiResult<any>> {
  try {
    const body = await res.json();
    if (body && typeof body === "object" && "error" in body && body.error) {
      return { ok: false, error: body.error };
    }
  } catch {
    // 无法解析 JSON，使用 fallback
  }
  // 502 Bad Gateway → Gateway 未运行
  if (res.status === 502) {
    return {
      ok: false,
      error: {
        code: "GATEWAY_UNAVAILABLE",
        message: "Gateway 未连接，请先启动后端服务",
        category: "internal",
        recoverable: true,
      },
    };
  }
  return {
    ok: false,
    error: {
      code: "HTTP_ERROR",
      message: `HTTP ${res.status}: ${res.statusText}`,
      category: "internal",
      recoverable: true,
    },
  };
}

/** 通用 GET 请求 */
export async function apiGet<T>(path: string): Promise<ApiResult<T>> {
  return request<T>(path);
}

/** 通用 POST 请求 */
export async function apiPost<T>(
  path: string,
  body?: unknown
): Promise<ApiResult<T>> {
  return request<T>(path, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: body ? JSON.stringify(body) : undefined,
  });
}

/** 受控 multipart 上传；调用方不得自行设置 Content-Type boundary。 */
export async function apiUpload<T>(path: string, body: FormData): Promise<ApiResult<T>> {
  return request<T>(path, { method: "POST", body });
}

/** 通用 DELETE 请求 */
export async function apiDelete<T>(path: string): Promise<ApiResult<T>> {
  return request<T>(path, {
    method: "DELETE",
  });
}

export async function apiPatch<T>(path: string, body: unknown): Promise<ApiResult<T>> {
  return request<T>(path, {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
}

export type Unsubscribe = () => void;
export type EventConnectionState = "connecting" | "open" | "reconnecting" | "closed";

/** 订阅 SSE RuntimeEvent 流 */
export function subscribeEvents(
  runId: ID,
  handler: (event: RuntimeEvent) => void,
  onConnectionState?: (state: EventConnectionState) => void,
): Unsubscribe {
  onConnectionState?.("connecting");
  const eventSource = new EventSource(
    `${BASE_URL}/runs/${runId}/events`
  );

  eventSource.onopen = () => {
    onConnectionState?.("open");
  };

  eventSource.onmessage = (msg) => {
    try {
      const event: RuntimeEvent = JSON.parse(msg.data);
      handler(event);
    } catch {
      // 忽略解析错误
    }
  };

  // 不在瞬时网络错误或 Gateway 重启时主动 close。
  // EventSource 会携带 Last-Event-ID 自动重连，Gateway 负责按 event_id 恢复历史并去重。
  // 终态事件由 runStore.unsubscribe() 显式关闭连接。
  eventSource.onerror = () => {
    onConnectionState?.(eventSource.readyState === EventSource.CLOSED ? "closed" : "reconnecting");
  };

  return () => {
    eventSource.close();
    onConnectionState?.("closed");
  };
}
