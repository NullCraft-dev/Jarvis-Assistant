import { describe, expect, it } from "vitest";
import { shouldProjectLiveRunText } from "@/features/command/conversationProjection";

describe("conversation message projection", () => {
  it.each(["completed", "failed", "cancelled"] as const)(
    "never shadows persisted content with a live draft after %s",
    (status) => {
      expect(shouldProjectLiveRunText(status, true)).toBe(false);
    },
  );

  it("keeps projecting live text while a run is active", () => {
    expect(shouldProjectLiveRunText("running", true)).toBe(true);
    expect(shouldProjectLiveRunText("waiting_for_permission", true)).toBe(true);
    expect(shouldProjectLiveRunText("running", false)).toBe(false);
  });
});
