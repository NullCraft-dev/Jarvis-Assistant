import { describe, expect, it } from "vitest";

import { normalizeArtifact } from "@/features/artifact/artifactContract";

const base = {
  id: "artifact-1",
  task_id: "task-1",
  run_id: "run-1",
  kind: "markdown" as const,
  title: "最终回复",
  created_at: "2026-07-24T00:00:00Z",
};

describe("Artifact v2 compatibility", () => {
  it("keeps explicit v2 deliverable semantics", () => {
    const artifact = normalizeArtifact({
      ...base,
      purpose: "deliverable",
      producer: { type: "tool", tool_call_id: "tool-1" },
    });

    expect(artifact.purpose).toBe("deliverable");
    expect(artifact.producer).toEqual({
      type: "tool",
      tool_call_id: "tool-1",
    });
  });

  it("upgrades legacy final-output events without treating them as deliverables", () => {
    const artifact = normalizeArtifact({
      ...base,
      metadata: { is_final_output: true },
    });

    expect(artifact.purpose).toBe("final_response");
    expect(artifact.producer).toEqual({ type: "runtime" });
  });
});
