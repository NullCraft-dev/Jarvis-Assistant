/**
 * useConversationHistory 竞态 + cursor 链 + AppError 测试。
 */

import { describe, it, expect } from "vitest";
import type { AppError, MessageDTO } from "@jarvis/shared";
import {
  useConversationHistory,
  type FetchHistoryFn,
  type FetchResult,
} from "@/features/command/composables/useConversationHistory";

function deferred<T>() {
  let resolve!: (v: T) => void;
  let reject!: (e: unknown) => void;
  const promise = new Promise<T>((res, rej) => { resolve = res; reject = rej; });
  return { promise, resolve, reject };
}

function makeMsg(id: string, role: "user" | "assistant", content: string): MessageDTO {
  return { id, conversation_id: "c1", task_id: "t1", run_id: "r1", role, content, created_at: "2026-01-01T00:00:00Z" };
}

function okResult(msgs: MessageDTO[], nc: string | null = null): FetchResult {
  return { ok: true, messages: msgs, nextCursor: nc };
}

function errResult(msg: string): FetchResult {
  const error: AppError = {
    code: "TEST_ERROR",
    message: msg,
    category: "runtime",
    recoverable: true,
  };
  return { ok: false, error };
}

// ── refresh ──

describe("refresh", () => {
  it("双刷新旧响应不覆盖", async () => {
    const d1 = deferred<FetchResult>(); const d2 = deferred<FetchResult>();
    let call = 0;
    const fn: FetchHistoryFn = () => { call++; return call === 1 ? d1.promise : d2.promise; };
    const { historyMessages, refresh } = useConversationHistory(fn);
    const p1 = refresh("c1"); const p2 = refresh("c1");
    d2.resolve(okResult([makeMsg("m2", "user", "new")]));
    await p2;
    d1.resolve(okResult([makeMsg("m1", "user", "old")]));
    await p1;
    expect(historyMessages.value[0].content).toBe("new");
  });

  it("refresh(null) 清空所有状态", async () => {
    const d1 = deferred<FetchResult>();
    const fn: FetchHistoryFn = () => d1.promise;
    const { historyMessages, isLoading, isLoadingOlder, nextCursor, refresh } = useConversationHistory(fn);
    const p1 = refresh("c1");
    await refresh(null);
    expect(historyMessages.value).toHaveLength(0);
    expect(isLoading.value).toBe(false);
    expect(isLoadingOlder.value).toBe(false);
    expect(nextCursor.value).toBeNull();
    d1.resolve(okResult([makeMsg("m1", "user", "old")]));
    await p1;
    expect(historyMessages.value).toHaveLength(0);
  });

  it("refresh 收到 AppError → historyError 可见", async () => {
    const fn: FetchHistoryFn = async () => errResult("后端 validation 错误");
    const { historyMessages, historyError, refresh } = useConversationHistory(fn);
    await refresh("c1");
    expect(historyError.value?.message).toBe("后端 validation 错误");
    expect(historyMessages.value).toHaveLength(0);
  });

  it("refresh 收到 AppError 后重试成功清除错误", async () => {
    let call = 0;
    const fn: FetchHistoryFn = async () => {
      call++;
      if (call === 1) return errResult("失败");
      return okResult([makeMsg("m1", "user", "ok")]);
    };
    const { historyError, historyMessages, refresh } = useConversationHistory(fn);
    await refresh("c1");
    expect(historyError.value?.message).toBe("失败");
    await refresh("c1");
    expect(historyError.value).toBeNull();
    expect(historyMessages.value[0].content).toBe("ok");
  });
});

// ── loadOlder ──

describe("loadOlder", () => {
  it("三页 cursor 链", async () => {
    const cursors: (string | null)[] = [];
    let call = 0;
    const fn: FetchHistoryFn = async (_cid, _limit, before) => {
      call++; cursors.push(before ?? null);
      if (call === 1) return okResult(Array.from({ length: 50 }, (_, i) => makeMsg(`m${i + 51}`, "user", `m${i + 51}`)), "c1");
      if (before === "c1") return okResult(Array.from({ length: 50 }, (_, i) => makeMsg(`m${i + 1}`, "user", `m${i + 1}`)), "c2");
      if (before === "c2") return okResult([makeMsg("m0", "assistant", "oldest")], null);
      return okResult([]);
    };
    const { historyMessages, nextCursor, loadOlder, refresh } = useConversationHistory(fn);
    await refresh("c1");
    expect(nextCursor.value).toBe("c1");
    await loadOlder();
    expect(historyMessages.value).toHaveLength(100);
    expect(nextCursor.value).toBe("c2");
    await loadOlder();
    expect(historyMessages.value).toHaveLength(101);
    expect(nextCursor.value).toBeNull();
  });

  it("loadOlder 前置合并去重", async () => {
    let call = 0;
    const fn: FetchHistoryFn = async () => {
      call++;
      if (call === 1) return okResult([makeMsg("m3", "user", "三")], "c1");
      return okResult([makeMsg("m1", "user", "一"), makeMsg("m2", "user", "二"), makeMsg("m3", "user", "三")], null);
    };
    const { historyMessages, loadOlder, refresh } = useConversationHistory(fn);
    await refresh("c1");
    await loadOlder();
    expect(historyMessages.value.map(m => m.id)).toEqual(["m1", "m2", "m3"]);
  });

  it("loadOlder 失败保留原消息 + error 可见 + 可重试", async () => {
    const d1 = deferred<FetchResult>();
    let call = 0;
    const fn: FetchHistoryFn = async () => { call++; if (call === 1) return okResult([makeMsg("m1", "user", "ok")], "c1"); return d1.promise; };
    const { historyMessages, loadOlderError, isLoadingOlder, loadOlder, refresh } = useConversationHistory(fn);
    await refresh("c1");
    const p1 = loadOlder();
    d1.reject(new Error("fail"));
    await p1;
    expect(historyMessages.value).toHaveLength(1);
    expect(loadOlderError.value).toBeTruthy();
    expect(isLoadingOlder.value).toBe(false);
  });

  it("loadOlder 收到 AppError 保留原消息和 cursor", async () => {
    let call = 0;
    const fn: FetchHistoryFn = async () => { call++; if (call === 1) return okResult([makeMsg("m1", "user", "ok")], "c1"); return errResult("后端错误"); };
    const { historyMessages, loadOlderError, nextCursor, loadOlder, refresh } = useConversationHistory(fn);
    await refresh("c1");
    await loadOlder();
    expect(historyMessages.value).toHaveLength(1);
    expect(loadOlderError.value?.message).toBe("后端错误");
    expect(nextCursor.value).toBe("c1"); // cursor 保留以便重试
  });

  it("loadOlder pending 不重复请求", async () => {
    const d1 = deferred<FetchResult>();
    let call = 0;
    const fn: FetchHistoryFn = async () => { call++; if (call === 1) return okResult([makeMsg("m1", "user", "x")], "c1"); return d1.promise; };
    const { loadOlder, refresh } = useConversationHistory(fn);
    await refresh("c1");
    loadOlder(); // pending
    await loadOlder(); // 被拒绝
    d1.resolve(okResult([makeMsg("m0", "user", "old")]));
    await loadOlder();
    expect(call).toBe(2);
  });

  it("loadOlder pending 时切换会话：旧响应不写入", async () => {
    const dOld = deferred<FetchResult>();
    let call = 0;
    const fn: FetchHistoryFn = async (_cid) => {
      call++;
      if (call === 1) return okResult([makeMsg("a1", "user", "A")], "ca");
      if (call === 2) return dOld.promise;
      return okResult([makeMsg("b1", "user", "B")], "cb");
    };
    const { historyMessages, nextCursor, loadOlder, refresh } = useConversationHistory(fn);
    await refresh("conv-A");
    const pOld = loadOlder();
    await refresh("conv-B");
    expect(historyMessages.value.map(m => m.content)).toEqual(["B"]);
    dOld.resolve(okResult([makeMsg("a0", "user", "A-old")]));
    await pOld;
    expect(historyMessages.value.map(m => m.content)).toEqual(["B"]);
    expect(nextCursor.value).toBe("cb");
  });

  it("loadOlder pending 时 refresh(null) 清空且旧响应不恢复", async () => {
    const dOld = deferred<FetchResult>();
    let call = 0;
    const fn: FetchHistoryFn = async () => { call++; if (call === 1) return okResult([makeMsg("m1", "user", "x")], "c1"); return dOld.promise; };
    const { historyMessages, isLoading, isLoadingOlder, refresh, loadOlder } = useConversationHistory(fn);
    await refresh("c1");
    const pOld = loadOlder();
    await refresh(null);
    expect(historyMessages.value).toHaveLength(0);
    expect(isLoading.value).toBe(false);
    expect(isLoadingOlder.value).toBe(false);
    dOld.resolve(okResult([makeMsg("m0", "user", "old")]));
    await pOld;
    expect(historyMessages.value).toHaveLength(0);
  });

  it("stale loadOlder reject 不影响新会话", async () => {
    const dOld = deferred<FetchResult>();
    let call = 0;
    const fn: FetchHistoryFn = async () => {
      call++;
      if (call === 1) return okResult([makeMsg("a1", "user", "A")], "ca");
      if (call === 2) return dOld.promise;
      return okResult([makeMsg("b1", "user", "B")]);
    };
    const { historyMessages, isLoadingOlder, loadOlder, refresh } = useConversationHistory(fn);
    await refresh("conv-A");
    const pOld = loadOlder();
    await refresh("conv-B");
    dOld.reject(new Error("fail"));
    await pOld;
    expect(historyMessages.value.map(m => m.content)).toEqual(["B"]);
    expect(isLoadingOlder.value).toBe(false);
  });
});
