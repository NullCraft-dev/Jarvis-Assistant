import { beforeEach, describe, expect, it, vi } from "vitest";
import { useRagEvaluationReview } from "@/features/feedback/composables/useRagEvaluationReview";

const apiMocks = vi.hoisted(() => ({
  listRagEvaluationTraces: vi.fn(), inspectRagEvaluationTrace: vi.fn(),
  reviewRagEvaluationPrivacy: vi.fn(), reviewRagEvaluationLabel: vi.fn(),
  promoteRagEvaluationTrace: vi.fn(),
}));
vi.mock("@/api/client", () => apiMocks);

const detail = {
  trace: { trace_id: "trace-1", workspace_id: "workspace-1", privacy_status: "approved", query_hash: "hash", label_status: "confirmed" },
  query: "safe query", request: {}, evidence: [{ chunk_id: "chunk-1", snippet: "safe evidence" }],
  label: { status: "confirmed", positive_chunk_ids: ["chunk-1"], hard_negative_chunk_ids: [] }, promotion_candidate: null,
};

describe("RAG evaluation review lifecycle", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    apiMocks.listRagEvaluationTraces.mockResolvedValue({ ok: true, data: { traces: [] } });
    apiMocks.inspectRagEvaluationTrace.mockResolvedValue({ ok: true, data: detail });
    apiMocks.reviewRagEvaluationPrivacy.mockResolvedValue({ ok: true, data: detail });
    apiMocks.reviewRagEvaluationLabel.mockResolvedValue({ ok: true, data: detail });
    apiMocks.promoteRagEvaluationTrace.mockResolvedValue({ ok: true, data: { ...detail, promotion_candidate: { trace_id: "trace-1", query_hash: "hash", raw_query_included: false, raw_chunk_content_included: false } } });
  });

  it("keeps privacy, confirmation and promotion as explicit operations", async () => {
    const review = useRagEvaluationReview();
    expect(await review.inspect("workspace-1", "trace-1")).toBe(true);
    await review.reviewPrivacy("workspace-1", "trace-1", "approved");
    await review.saveLabel("workspace-1", "trace-1", { status: "confirmed", positive_chunk_ids: ["chunk-1"], hard_negative_chunk_ids: [], notes: "checked" });
    await review.promote("workspace-1", "trace-1");

    expect(apiMocks.reviewRagEvaluationPrivacy).toHaveBeenCalledWith("workspace-1", "trace-1", "approved");
    expect(apiMocks.reviewRagEvaluationLabel).toHaveBeenCalledWith("trace-1", expect.objectContaining({ workspace_id: "workspace-1", status: "confirmed" }));
    expect(review.detail.value?.promotion_candidate?.raw_query_included).toBe(false);
  });
});
