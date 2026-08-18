// @vitest-environment happy-dom

import { createPinia, setActivePinia } from "pinia";
import { beforeEach, describe, expect, it } from "vitest";
import type { RuntimeEvent } from "@jarvis/shared";
import { useRunStore } from "@/stores/runStore";

function delta(id: string, value: string): RuntimeEvent {
  return {
    id,
    type: "model.delta",
    task_id: "task-1",
    run_id: "run-1",
    step_id: "step-1",
    timestamp: "2026-07-20T00:00:00Z",
    payload: { step_id: "step-1", delta: value },
  };
}

describe("runStore streaming output", () => {
  beforeEach(() => setActivePinia(createPinia()));

  it("appends bounded delta events without requiring accumulated", () => {
    const store = useRunStore();
    store.appendEvent(delta("delta-1", "第一段"));
    store.appendEvent(delta("delta-2", "第二段"));

    expect(store.getStreamingText("run-1")).toBe("第一段第二段");
  });

  it("tracks durable pause and resume events", () => {
    const store = useRunStore();
    const base = {
      task_id: "task-1",
      run_id: "run-1",
      timestamp: "2026-07-23T00:00:00Z",
      payload: {},
    };
    store.appendEvent({ id: "pause-1", type: "agent.run.paused", ...base });
    expect(store.getStatus("run-1")).toBe("paused");

    store.appendEvent({ id: "resume-1", type: "agent.run.resumed", ...base });
    expect(store.getStatus("run-1")).toBe("running");
  });

  it("deduplicates permission acknowledgement and later durable resolution by request id", () => {
    const store = useRunStore();
    const base = {
      task_id: "task-1",
      run_id: "run-1",
      step_id: "step-1",
      timestamp: "2026-07-30T00:00:00Z",
    };
    store.appendEvent({
      id: "permission-required",
      type: "permission.required",
      ...base,
      payload: {},
    });
    store.appendEvent({
      id: "permission-ack",
      type: "permission.resolved",
      ...base,
      payload: { request_id: "request-1", decision: "allow_once", acknowledged: true },
    });
    store.appendEvent({
      id: "permission-durable",
      type: "permission.resolved",
      ...base,
      payload: { request_id: "request-1", decision: "allow_once" },
    });

    expect(store.getStatus("run-1")).toBe("running");
    expect(store.getEvents("run-1").filter((event) => event.type === "permission.resolved"))
      .toHaveLength(1);
  });

  it("keeps terminal status and output when late events arrive", () => {
    const store = useRunStore();
    const base = {
      task_id: "task-1",
      run_id: "run-1",
      timestamp: "2026-08-12T00:00:00Z",
    };

    store.appendEvent({
      id: "completed",
      type: "agent.run.completed",
      sequence: 10,
      ...base,
      payload: { output: "最终答案" },
    });
    store.appendEvent({
      id: "late-resume",
      type: "agent.run.resumed",
      sequence: 9,
      ...base,
      payload: {},
    });
    store.appendEvent({
      id: "late-delta",
      type: "model.delta",
      ...base,
      payload: { delta: "不应重开正文" },
    });

    expect(store.getStatus("run-1")).toBe("completed");
    expect(store.getFinalOutputText("run-1")).toBe("最终答案");
    expect(store.getStreamingText("run-1")).toBe("");
    expect(store.getEvents("run-1")).toHaveLength(3);
  });

  it("does not let an older durable event regress a projected run state", () => {
    const store = useRunStore();
    const base = {
      task_id: "task-1",
      run_id: "run-1",
      timestamp: "2026-08-12T00:00:00Z",
      payload: {},
    };

    store.appendEvent({ id: "resume", type: "agent.run.resumed", sequence: 8, ...base });
    store.appendEvent({ id: "old-pause", type: "agent.run.paused", sequence: 7, ...base });

    expect(store.getStatus("run-1")).toBe("running");
    expect(store.getEvents("run-1")).toHaveLength(2);
  });
});
