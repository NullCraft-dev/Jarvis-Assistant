// @vitest-environment happy-dom

import { createPinia, setActivePinia } from "pinia";
import { beforeEach, describe, expect, it, vi } from "vitest";

const apiMocks = vi.hoisted(() => ({ listAuditLogs: vi.fn() }));
vi.mock("@/api/client", () => apiMocks);
import { useAuditLogStore } from "@/stores/auditLogStore";

describe("auditLogStore", () => {
  beforeEach(() => { setActivePinia(createPinia()); vi.clearAllMocks(); });

  it("keeps server filters and appends only a deduplicated next page", async () => {
    apiMocks.listAuditLogs
      .mockResolvedValueOnce({ ok: true, data: { audit_logs: [{ id: "a1", event_type: "model.test", actor: "system", action_summary: "测试", details_summary: {}, created_at: "2026-07-20T00:00:00Z" }], next_cursor: "next" } })
      .mockResolvedValueOnce({ ok: true, data: { audit_logs: [{ id: "a1", event_type: "model.test", actor: "system", action_summary: "测试", details_summary: {}, created_at: "2026-07-20T00:00:00Z" }, { id: "a2", event_type: "tool.executed", actor: "agent", action_summary: "读取", details_summary: {}, created_at: "2026-07-19T00:00:00Z" }], next_cursor: undefined } });
    const store = useAuditLogStore();
    await store.load({ limit: 25, event_type: "model.test" });
    await store.loadMore();
    expect(apiMocks.listAuditLogs).toHaveBeenLastCalledWith({ limit: 25, event_type: "model.test", before: "next" });
    expect(store.auditLogs.map((item) => item.id)).toEqual(["a1", "a2"]);
    expect(store.nextCursor).toBeNull();
  });

  it("shows a safe error instead of retaining stale data on a failed filter request", async () => {
    apiMocks.listAuditLogs.mockResolvedValue({ ok: false, error: { message: "查询失败" } });
    const store = useAuditLogStore();
    await store.load({ actor: "agent" });
    expect(store.error).toBe("查询失败");
  });
});
