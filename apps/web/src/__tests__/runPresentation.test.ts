import { describe, expect, it } from "vitest";
import type { AgentRunStatus, RuntimeEvent } from "@jarvis/shared";
import {
  getLatestRunError,
  getRunStatusPresentation,
  isActiveRunStatus,
} from "@/features/command/composables/runPresentation";

const statuses: AgentRunStatus[] = [
  "created",
  "queued",
  "running",
  "pause_requested",
  "paused",
  "resume_requested",
  "waiting_for_permission",
  "waiting_for_user",
  "blocked",
  "failed",
  "completed",
  "cancelled",
];

describe("run presentation", () => {
  it("maps every contracted status to user-facing Chinese copy", () => {
    for (const status of statuses) {
      const presentation = getRunStatusPresentation(status);
      expect(presentation.label).not.toBe(status);
      expect(presentation.description.length).toBeGreaterThan(4);
    }
  });

  it("treats queued and blocked runs as active until a terminal event arrives", () => {
    expect(isActiveRunStatus("queued")).toBe(true);
    expect(isActiveRunStatus("blocked")).toBe(true);
    expect(isActiveRunStatus("completed")).toBe(false);
    expect(isActiveRunStatus("failed")).toBe(false);
    expect(isActiveRunStatus("cancelled")).toBe(false);
  });

  it("only exposes a structured AppError from a failed event", () => {
    const event: RuntimeEvent = {
      id: "event-1",
      type: "agent.run.failed",
      task_id: "task-1",
      run_id: "run-1",
      timestamp: "2026-07-31T00:00:00Z",
      payload: {
        error: {
          code: "MODEL_TIMEOUT",
          message: "模型响应超时",
          category: "model",
          recoverable: true,
          details: { secret: "must-not-be-rendered" },
        },
      },
    };

    expect(getLatestRunError([event])).toMatchObject({
      code: "MODEL_TIMEOUT",
      message: "模型响应超时",
      recoverable: true,
    });
  });
});
