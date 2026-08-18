import { describe, expect, it } from "vitest";
import type { RagDocumentDTO } from "@jarvis/shared";

import {
  isRagBatchEligible,
  isRagVersionConflict,
  ragBatchImpact,
  sourceSummary,
  vectorCountText,
} from "@/features/knowledge/ragDocumentPresentation";

function document(status: RagDocumentDTO["status"]): RagDocumentDTO {
  return {
    id: "document",
    workspace_id: "workspace",
    source_artifact_id: "artifact",
    title: "paper.pdf",
    mime_type: "application/pdf",
    status,
    ingestion_policy_version: "rag-v2",
    parser_version: "pymupdf-v2",
    chunker_version: "structure-v2",
    embedding_provider: "openai",
    embedding_model: "text-embedding-3-small",
    embedding_dimensions: 1536,
    chunk_count: 8,
    version: 4,
    created_at: "2026-07-31T00:00:00Z",
    updated_at: "2026-07-31T00:01:00Z",
    index_state: "current",
    index_stale_reasons: [],
    index_target: {},
  };
}

describe("ragDocumentPresentation", () => {
  it("derives batch eligibility from authoritative document status", () => {
    expect(isRagBatchEligible(document("disabled"), "enable")).toBe(true);
    expect(isRagBatchEligible(document("ready"), "disable")).toBe(true);
    expect(isRagBatchEligible(document("failed"), "restart")).toBe(true);
    expect(isRagBatchEligible(document("indexing"), "cancel")).toBe(true);
    expect(isRagBatchEligible(document("indexing"), "delete")).toBe(false);
  });

  it("does not invent a vector count for an old completed document", () => {
    expect(vectorCountText(document("ready"))).toBe("历史作业未记录");
    const item = document("ready");
    item.latest_job = {
      id: "job",
      status: "completed",
      attempts: 1,
      max_attempts: 3,
      embedding_attempts: 1,
      embedding_max_attempts: 3,
      progress: {
        page_count: 2,
        native_extraction_done: true,
        visual_pages_total: 0,
        visual_pages_completed: 0,
        visual_route_counts: {},
        chunks_total: 8,
        embedding_total: 8,
        embedding_completed: 8,
      },
      created_at: "2026-07-31T00:00:00Z",
      updated_at: "2026-07-31T00:01:00Z",
    };
    expect(vectorCountText(item)).toBe("8/8");
  });

  it("labels trusted source, destructive impact, and server conflicts", () => {
    expect(sourceSummary(document("ready"))).toBe("受控 Artifact · artifact");
    expect(ragBatchImpact("delete")).toContain("L4");
    expect(isRagVersionConflict("RAG_DOCUMENT_VERSION_CONFLICT")).toBe(true);
    expect(isRagVersionConflict("RAG_RESTART_CONFLICT")).toBe(true);
    expect(isRagVersionConflict("NETWORK_UNAVAILABLE")).toBe(false);
  });
});
