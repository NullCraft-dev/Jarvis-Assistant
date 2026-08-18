/**
 * useConversationHistory — 会话历史加载与竞态保护。
 *
 * 竞态模型：
 *   - conversationGeneration：递增代数，切换会话/refresh(null) 时递增，使旧会话所有请求失效
 *   - refreshGuard：同一会话内多次 refresh 的独立 request token
 *   - loadOlderGuard：同一会话内多次 loadOlder 的独立 request token
 *   - 两个 guard 相互独立，refresh 不会让 loadOlder 的 finally 永久卡住 loading
 *
 * 服务端 next_cursor 是唯一分页真源。
 * 结构化 ApiResult 错误通过 FetchResult.error 传递，不被伪装为空成功。
 */

import { ref, type Ref } from "vue";
import type { AppError, ID, MessageDTO } from "@jarvis/shared";
import { getConversation } from "@/api/client";

// ── 竞态守卫（纯函数） ──

export interface RaceGuard {
  acquire(): number;
  isLatest(token: number): boolean;
}

export function createRaceGuard(): RaceGuard {
  let _seq = 0;
  return { acquire: () => { _seq += 1; return _seq; }, isLatest: (token: number) => _seq === token };
}

// ── 结构化 fetch 结果 ──

export interface FetchOk {
  ok: true;
  messages: MessageDTO[];
  nextCursor: string | null;
}

export interface FetchErr {
  ok: false;
  error: AppError;
}

export type FetchResult = FetchOk | FetchErr;

export type FetchHistoryFn = (
  convId: ID, limit?: number, before?: string,
) => Promise<FetchResult>;

export async function defaultFetchHistory(
  convId: ID, limit?: number, before?: string,
): Promise<FetchResult> {
  const result = await getConversation(convId, { limit, before });
  if (result.ok) {
    return { ok: true, messages: result.data.messages, nextCursor: result.data.next_cursor ?? null };
  }
  return { ok: false, error: result.error };
}

function networkError(message: string): AppError {
  return {
    code: "NETWORK_ERROR",
    message,
    category: "internal",
    recoverable: true,
  };
}

// ── Composable ──

export function useConversationHistory(
  fetchFn: FetchHistoryFn = defaultFetchHistory,
) {
  const historyMessages: Ref<MessageDTO[]> = ref([]);
  const isLoading = ref(false);
  const isLoadingOlder = ref(false);
  const historyError: Ref<AppError | null> = ref(null);
  const loadOlderError: Ref<AppError | null> = ref(null);
  const nextCursor: Ref<string | null> = ref(null);

  let conversationGeneration = 0;
  let currentConversationId: ID | null = null;
  const refreshGuard = createRaceGuard();
  const loadOlderGuard = createRaceGuard();

  // ── refresh ──

  async function refresh(convId: ID | null): Promise<void> {
    // refresh(null)：清空一切 + 使所有新旧请求失效
    if (!convId) {
      conversationGeneration++;
      currentConversationId = null;
      refreshGuard.acquire();
      loadOlderGuard.acquire();
      historyMessages.value = [];
      isLoading.value = false;
      isLoadingOlder.value = false;
      historyError.value = null;
      loadOlderError.value = null;
      nextCursor.value = null;
      return;
    }

    // 开始新会话
    currentConversationId = convId;
    const gen = ++conversationGeneration;
    const myConvId = convId;
    // 使上一会话的 loadOlder 失效
    loadOlderGuard.acquire();
    loadOlderError.value = null;
    isLoadingOlder.value = false;
    nextCursor.value = null;

    const myToken = refreshGuard.acquire();
    isLoading.value = true;
    historyError.value = null;

    try {
      const result = await fetchFn(myConvId);
      // 三重检查
      if (conversationGeneration !== gen) return;
      if (currentConversationId !== myConvId) return;
      if (!refreshGuard.isLatest(myToken)) return;

      if (result.ok) {
        historyMessages.value = result.messages;
        nextCursor.value = result.nextCursor;
      } else {
        historyError.value = result.error;
        historyMessages.value = [];
        nextCursor.value = null;
      }
    } catch {
      if (conversationGeneration === gen && currentConversationId === myConvId && refreshGuard.isLatest(myToken)) {
        historyError.value = networkError("网络异常，请重试");
      }
    } finally {
      if (conversationGeneration === gen && currentConversationId === myConvId && refreshGuard.isLatest(myToken)) {
        isLoading.value = false;
      }
    }
  }

  // ── loadOlder ──

  async function loadOlder(): Promise<void> {
    // 前置守卫
    if (isLoading.value) return;
    if (isLoadingOlder.value) return;
    if (!currentConversationId || !nextCursor.value) return;

    const myConvId = currentConversationId;
    const gen = conversationGeneration;
    const cursor = nextCursor.value;

    const myToken = loadOlderGuard.acquire();
    isLoadingOlder.value = true;
    loadOlderError.value = null;

    try {
      const result = await fetchFn(myConvId, undefined, cursor);
      if (conversationGeneration !== gen) return;
      if (currentConversationId !== myConvId) return;
      if (!loadOlderGuard.isLatest(myToken)) return;

      if (result.ok) {
        nextCursor.value = result.nextCursor;
        if (result.messages.length > 0) {
          const existingIds = new Set(historyMessages.value.map(m => m.id));
          const newMsgs = result.messages.filter(m => !existingIds.has(m.id));
          historyMessages.value = [...newMsgs, ...historyMessages.value];
        }
      } else {
        loadOlderError.value = result.error;
      }
    } catch {
      if (conversationGeneration === gen && currentConversationId === myConvId && loadOlderGuard.isLatest(myToken)) {
        loadOlderError.value = networkError("加载更早消息失败，请重试");
      }
    } finally {
      if (conversationGeneration === gen && currentConversationId === myConvId && loadOlderGuard.isLatest(myToken)) {
        isLoadingOlder.value = false;
      }
    }
  }

  return {
    historyMessages,
    isLoading,
    isLoadingOlder,
    historyError,
    loadOlderError,
    nextCursor,
    refresh,
    loadOlder,
  };
}
