import { beforeEach, describe, expect, it, vi } from "vitest";
import { useRagFeedbackQueue } from "@/features/feedback/composables/useRagFeedbackQueue";

const apiMocks = vi.hoisted(() => ({
  listRagFeedback: vi.fn(), inspectRagFeedback: vi.fn(), triageRagFeedback: vi.fn(), resolveRagFeedback: vi.fn(),
}));
vi.mock("@/api/client", () => apiMocks);

describe("RAG feedback diagnostic queue", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    apiMocks.listRagFeedback.mockResolvedValue({ ok: true, data: { feedback: [] } });
  });

  it("loads redacted evidence and submits only structured draft selections", async () => {
    apiMocks.inspectRagFeedback.mockResolvedValue({
      ok: true,
      data: {
        feedback: { id: "feedback-1", kind: "unhelpful", status: "pending" }, query_hash: "hash",
        query: null, privacy_status: "pending", pipeline_versions: {}, result_count: 1,
        context_truncated: false, evidence: [{ chunk_id: "chunk-1", snippet: null }], label: null,
      },
    });
    apiMocks.triageRagFeedback.mockResolvedValue({ ok: true, data: { feedback: { id: "feedback-1", status: "reviewed" } } });
    const queue = useRagFeedbackQueue();

    expect(await queue.inspect("feedback-1")).toBe(true);
    expect(queue.detail.value?.query).toBeNull();
    expect(await queue.triage("workspace-1", "feedback-1", {
      failure_category: "answer_generation", positive_chunk_ids: [], hard_negative_chunk_ids: [],
    })).toBe(true);
    expect(apiMocks.triageRagFeedback).toHaveBeenCalledWith("feedback-1", {
      failure_category: "answer_generation", positive_chunk_ids: [], hard_negative_chunk_ids: [],
    });
  });
});
