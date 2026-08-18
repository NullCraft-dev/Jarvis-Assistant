import { createPinia, setActivePinia } from "pinia";
import { beforeEach, describe, expect, it, vi } from "vitest";

const apiMocks = vi.hoisted(() => ({
  getRuntimeHealth: vi.fn(),
  getStorageReconciliation: vi.fn(),
  inspectTerminalEventRepair: vi.fn(),
  createTerminalEventRepairRequest: vi.fn(),
  resolveTerminalEventRepairRequest: vi.fn(),
  listRuntimeDeadLetters: vi.fn(),
  inspectRuntimeDeadLetterRetry: vi.fn(),
  createRuntimeDeadLetterRetryRequest: vi.fn(),
  resolveRuntimeDeadLetterRetryRequest: vi.fn(),
}));
vi.mock("@/api/client", () => apiMocks);
import { useRuntimeHealthStore } from "@/stores/runtimeHealthStore";

describe("runtimeHealthStore", () => {
  beforeEach(() => { setActivePinia(createPinia()); vi.clearAllMocks(); });
  it("loads the Gateway runtime health projection", async () => {
    apiMocks.getRuntimeHealth.mockResolvedValue({ ok: true, data: { status: "healthy", runtime_bus: "redis", generated_at: "2026-07-22T00:00:00Z", workers: { total: 1, online: 1, busy: 0, stale: 0 }, streams: [], dead_letters: [], counters: {}, warnings: [] } });
    const store = useRuntimeHealthStore();
    await store.load();
    expect(store.health?.status).toBe("healthy");
    expect(store.error).toBeNull();
  });
  it("keeps a user-safe error when diagnostics are unavailable", async () => {
    apiMocks.getRuntimeHealth.mockRejectedValue(new Error("secret connection details"));
    const store = useRuntimeHealthStore();
    await store.load();
    expect(store.error).toBe("运行时诊断服务不可用");
  });

  it("loads the bounded storage reconciliation projection", async () => {
    apiMocks.getStorageReconciliation.mockResolvedValue({
      ok: true,
      data: {
        status: "healthy", generated_at: "2026-07-24T00:00:00Z",
        scanned_runs: 3, scanned_events: 8, scanned_steps: 4,
        scanned_artifacts: 1, issue_count: 0, truncated: false, issues: [],
      },
    });
    const store = useRuntimeHealthStore();
    await store.loadReconciliation();
    expect(apiMocks.getStorageReconciliation).toHaveBeenCalledWith(50);
    expect(store.reconciliation?.scanned_runs).toBe(3);
    expect(store.reconciliationError).toBeNull();
  });

  it("requires L3 inspection and request before terminal event repair", async () => {
    const runId = "00000000-0000-4000-8000-000000000002";
    const taskId = "00000000-0000-4000-8000-000000000001";
    const requestId = "00000000-0000-4000-8000-000000000003";
    const issue = {
      code: "TERMINAL_EVENT_MISSING", severity: "error", entity_type: "run",
      entity_id: runId, summary: "missing", task_id: taskId, run_id: runId,
    } as const;
    apiMocks.inspectTerminalEventRepair.mockResolvedValue({ ok: true, data: {
      eligible: true, reason_code: "TERMINAL_EVENT_REPAIR_ELIGIBLE", reason: "eligible",
      task_id: taskId, run_id: runId, expected_event_type: "agent.run.failed",
      risk_level: "L3", requires_confirmation: true, allowed_decisions: ["allow_once", "deny"],
    } });
    apiMocks.createTerminalEventRepairRequest.mockResolvedValue({ ok: true, data: {
      request: {
        id: requestId, task_id: taskId, run_id: runId,
        tool_name: "runtime.repair_missing_terminal_event",
        action_summary: "repair", risk_level: "L3", scope: { type: "once" },
        arguments_summary: {}, allowed_decisions: ["allow_once", "deny"],
        created_at: "2026-07-24T00:00:00Z", expires_at: "2099-07-24T00:15:00Z", status: "pending",
      },
    } });
    apiMocks.resolveTerminalEventRepairRequest.mockResolvedValue({ ok: true, data: {
      request: { id: requestId, status: "denied" },
    } });
    apiMocks.getStorageReconciliation.mockResolvedValue({ ok: true, data: {
      status: "degraded", generated_at: "2026-07-24T00:00:00Z",
      scanned_runs: 1, scanned_events: 1, scanned_steps: 0,
      scanned_artifacts: 0, issue_count: 1, truncated: false, issues: [issue],
    } });
    const store = useRuntimeHealthStore();
    await store.inspectRepair(issue);
    expect(store.repairInspection?.eligible).toBe(true);
    await store.createRepairRequest();
    expect(store.repairRequest?.status).toBe("pending");
    await store.resolveRepair("deny", "preserve history");
    expect(apiMocks.resolveTerminalEventRepairRequest).toHaveBeenCalledWith(
      requestId, "deny", "preserve history",
    );
    expect(store.repairResolution?.request.status).toBe("denied");
  });

  it("loads and paginates safe dead-letter records", async () => {
    apiMocks.listRuntimeDeadLetters
		.mockResolvedValueOnce({ ok: true, data: { records: [{ id: "2-0", source: "run_queue", error_code: "RUN_QUEUE_MALFORMED" }], next_cursor: "2-0" } })
		.mockResolvedValueOnce({ ok: true, data: { records: [{ id: "1-0", source: "run_queue", error_code: "RUN_QUEUE_RETRY_EXHAUSTED" }] } });
    const store = useRuntimeHealthStore();
		await store.loadDeadLetters({ source: "run_queue", limit: 20 });
		expect(store.deadLetters.map((record) => record.id)).toEqual(["2-0"]);
		await store.loadMoreDeadLetters();
		expect(store.deadLetters.map((record) => record.id)).toEqual(["2-0", "1-0"]);
		expect(apiMocks.listRuntimeDeadLetters).toHaveBeenLastCalledWith({ source: "run_queue", limit: 20, before: "2-0" });
  });

  it("requires an inspection and persistent permission request before resolving retry", async () => {
    const record = {
      id: "2-0", source: "run_queue", original_stream: "run", original_message_id: "1-0",
      consumer_group: "workers", delivery_count: 3, reclaimed: true,
      error_code: "RUN_QUEUE_RETRY_EXHAUSTED", error_message: "exhausted",
      failed_at: "2026-07-22T00:00:00Z", payload_sha256: "a".repeat(64),
      payload_size_bytes: 42, task_id: "00000000-0000-4000-8000-000000000001",
      run_id: "00000000-0000-4000-8000-000000000002",
    } as const;
    apiMocks.inspectRuntimeDeadLetterRetry.mockResolvedValue({ ok: true, data: {
      eligible: true, reason_code: "DLQ_RETRY_ELIGIBLE", reason: "eligible",
      task_id: record.task_id, run_id: record.run_id, risk_level: "L3",
      requires_confirmation: true, allowed_decisions: ["allow_once", "deny"],
    } });
    apiMocks.createRuntimeDeadLetterRetryRequest.mockResolvedValue({ ok: true, data: { request: {
      id: "00000000-0000-4000-8000-000000000003", task_id: record.task_id,
      run_id: record.run_id, tool_name: "runtime.retry_failed_run", action_summary: "retry",
      risk_level: "L3", scope: { type: "once" }, arguments_summary: {},
      allowed_decisions: ["allow_once", "deny"], created_at: "2026-07-22T00:00:00Z",
      expires_at: "2099-07-22T00:15:00Z", status: "pending",
    } } });
    apiMocks.resolveRuntimeDeadLetterRetryRequest.mockResolvedValue({ ok: true, data: {
      request: { id: "00000000-0000-4000-8000-000000000003", status: "denied" },
      previous_run_id: record.run_id,
    } });
    const store = useRuntimeHealthStore();
    await store.inspectRecovery(record);
    expect(store.recoveryInspection?.eligible).toBe(true);
    await store.createRecoveryRequest();
    expect(store.recoveryRequest?.status).toBe("pending");
    await store.resolveRecovery("deny", "do not retry");
    expect(apiMocks.resolveRuntimeDeadLetterRetryRequest).toHaveBeenCalledWith(
      "00000000-0000-4000-8000-000000000003", "deny", "do not retry",
    );
    expect(store.recoveryResolution?.request.status).toBe("denied");
  });
});
