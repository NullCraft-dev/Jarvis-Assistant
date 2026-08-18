import { describe, expect, it } from "vitest";
import type { RuntimeEvent } from "@jarvis/shared";
import { findRetryableModelStepId } from "@/features/command/composables/retryableFailedStep";

function event(
  id: string,
  type: string,
  payload: Record<string, unknown>,
  stepId?: string,
): RuntimeEvent {
  return {
    id,
    type,
    task_id: "task-1",
    run_id: "run-1",
    step_id: stepId,
    timestamp: "2026-07-30T00:00:00Z",
    payload,
  } as RuntimeEvent;
}

describe("findRetryableModelStepId", () => {
  it("does not reuse an earlier recoverable model failure after retries are exhausted", () => {
    const events = [
      event("m1", "model.call.failed", { recoverable: true }, "step-1"),
      event("m2", "model.call.failed", { recoverable: false }, "step-2"),
      event("f", "agent.run.failed", {
        error: { code: "MODEL_OUTPUT_INVALID", recoverable: false },
      }),
    ];

    expect(findRetryableModelStepId("failed", events)).toBeNull();
  });

  it("returns the latest model step only when terminal and step are recoverable", () => {
    const events = [
      event("m", "model.call.failed", { recoverable: true }, "step-safe"),
      event("f", "agent.run.failed", {
        error: { code: "MODEL_TIMEOUT", recoverable: true },
      }),
    ];

    expect(findRetryableModelStepId("failed", events)).toBe("step-safe");
  });
});
