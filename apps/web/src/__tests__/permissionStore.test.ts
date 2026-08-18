// @vitest-environment happy-dom

import { createPinia, setActivePinia } from "pinia";
import { beforeEach, describe, expect, it, vi } from "vitest";
import type { PermissionRequestDTO } from "@jarvis/shared";

const apiMocks = vi.hoisted(() => ({
  listPendingPermissions: vi.fn(),
  resolvePermission: vi.fn(),
}));

vi.mock("@/api/client", () => ({ ...apiMocks }));

import { usePermissionStore } from "@/stores/permissionStore";

function request(id: string, runId = "run-1"): PermissionRequestDTO {
  return {
    id,
    task_id: "task-1",
    run_id: runId,
    tool_name: "workspace.write_file",
    action_summary: "写入文件",
    risk_level: "L2",
    scope: { type: "once" },
    arguments_summary: { path: "notes.md" },
    allowed_decisions: ["allow_once", "deny"],
    created_at: "2026-07-16T00:00:00Z",
    expires_at: "2099-07-16T00:15:00Z",
    status: "pending",
  };
}

describe("permissionStore", () => {
  beforeEach(() => {
    setActivePinia(createPinia());
    vi.clearAllMocks();
  });

  it("deduplicates events and reconciles one run from the authoritative API", async () => {
    const store = usePermissionStore();
    store.addRequest(request("req-old"));
    store.addRequest(request("req-old"));
    store.addRequest(request("req-other", "run-2"));
    apiMocks.listPendingPermissions.mockResolvedValue({
      ok: true,
      data: { requests: [request("req-current")] },
    });

    await store.loadPendingForRun("run-1");

    expect(store.getPendingByRun("run-1").map((item) => item.id)).toEqual(["req-current"]);
    expect(store.getPendingByRun("run-2").map((item) => item.id)).toEqual(["req-other"]);
  });

  it("does not register expired or malformed permission requests", () => {
    const store = usePermissionStore();
    store.addRequest(request("expired", "run-expired"));
    store.addRequest({
      ...request("malformed", "run-malformed"),
      expires_at: "not-a-date",
    });

    store.addRequest({
      ...request("expired", "run-expired"),
      expires_at: "2020-01-01T00:00:00Z",
    });

    expect(store.getPendingByRun("run-expired")).toHaveLength(0);
    expect(store.getPendingByRun("run-malformed")).toHaveLength(0);
  });

  it("prevents duplicate decisions while a request is resolving", async () => {
    const store = usePermissionStore();
    store.addRequest(request("req-1"));
    let finish!: (value: unknown) => void;
    apiMocks.resolvePermission.mockReturnValue(new Promise((resolve) => { finish = resolve; }));

    const first = store.approveOnce("req-1");
    const duplicate = await store.approveOnce("req-1");

    expect(duplicate).toBeNull();
    expect(apiMocks.resolvePermission).toHaveBeenCalledTimes(1);
    finish({
      ok: false,
      error: {
        code: "PERMISSION_NOT_PENDING",
        message: "请求已失效",
        category: "permission",
        recoverable: false,
      },
    });
    await first;
    expect(store.getError("req-1")).toMatchObject({
      code: "PERMISSION_NOT_PENDING",
      message: "请求已失效",
      recoverable: false,
    });
  });

  it("keeps a bounded per-run decision acknowledgement after success", async () => {
    const store = usePermissionStore();
    const pending = request("req-1");
    store.addRequest(pending);
    apiMocks.resolvePermission.mockResolvedValue({
      ok: true,
      data: {
        request: { ...pending, status: "approved", decision: "allow_once" },
        events: [],
      },
    });

    await store.approveOnce("req-1");

    expect(store.getPendingByRun("run-1")).toEqual([]);
    expect(store.getResolvedByRun("run-1")).toEqual([
      expect.objectContaining({
        id: "req-1",
        status: "approved",
        decision: "allow_once",
      }),
    ]);
  });
});
