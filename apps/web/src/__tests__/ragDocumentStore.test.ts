// @vitest-environment happy-dom

import { createPinia, setActivePinia } from "pinia";
import { beforeEach, describe, expect, it, vi } from "vitest";
import type { RagDocumentDTO } from "@jarvis/shared";

const apiMocks = vi.hoisted(() => ({
  listRagDocuments: vi.fn(),
  createRagUploadRequest: vi.fn(),
  resolveRagUploadRequest: vi.fn(),
  uploadRagDocument: vi.fn(),
  restartRagDocument: vi.fn(),
  updateRagDocument: vi.fn(),
  cancelRagDocument: vi.fn(),
  createRagDeleteRequest: vi.fn(),
  resolveRagDeleteRequest: vi.fn(),
}));
vi.mock("@/api/client", () => ({ ...apiMocks }));

import { useRagDocumentStore } from "@/stores/ragDocumentStore";

const document = (id: string, overrides: Partial<RagDocumentDTO> = {}): RagDocumentDTO => ({
  id,
  workspace_id: "workspace",
  source_artifact_id: `artifact-${id}`,
  title: `${id}.pdf`,
  mime_type: "application/pdf",
  status: "ready",
  ingestion_policy_version: "rag-v1",
  parser_version: "pymupdf-v1",
  chunker_version: "structure-v1",
  embedding_provider: "openai",
  embedding_model: "text-embedding-3-small",
  embedding_dimensions: 1536,
  chunk_count: 48,
  indexed_at: "2026-07-28T00:00:30Z",
  version: 2,
  created_at: "2026-07-28T00:00:00Z",
  updated_at: "2026-07-28T00:01:00Z",
  index_state: "current",
  index_stale_reasons: [],
  index_target: { ingestion_policy_version: "rag-v1" },
  ...overrides,
});

describe("ragDocumentStore", () => {
  beforeEach(() => {
    setActivePinia(createPinia());
    vi.clearAllMocks();
  });

  it("loads documents for the selected workspace", async () => {
    apiMocks.listRagDocuments.mockResolvedValue({ ok: true, data: { documents: [document("paper")] } });
    const store = useRagDocumentStore();
    await store.load("workspace");
    expect(apiMocks.listRagDocuments).toHaveBeenCalledWith("workspace", true);
    expect(store.documents[0]?.chunk_count).toBe(48);
  });

  it("does not let an older workspace response overwrite the current one", async () => {
    let resolveOld!: (value: unknown) => void;
    apiMocks.listRagDocuments
      .mockReturnValueOnce(new Promise((resolve) => { resolveOld = resolve; }))
      .mockResolvedValueOnce({ ok: true, data: { documents: [document("new")] } });
    const store = useRagDocumentStore();
    const oldLoad = store.load("old-workspace");
    await store.load("new-workspace");
    resolveOld({ ok: true, data: { documents: [document("old")] } });
    await oldLoad;
    expect(store.documents.map((item) => item.id)).toEqual(["new"]);
    expect(store.loadedWorkspaceId).toBe("new-workspace");
  });

  it("clears workspace-scoped data when no workspace is selected", async () => {
    apiMocks.listRagDocuments.mockResolvedValue({ ok: true, data: { documents: [document("paper")] } });
    const store = useRagDocumentStore();
    await store.load("workspace");
    await store.load(null);
    expect(store.documents).toEqual([]);
    expect(store.loadedWorkspaceId).toBeNull();
  });

  it("creates an L2 request before uploading and refreshes only after approval", async () => {
    apiMocks.createRagUploadRequest.mockResolvedValue({
      ok: true,
      data: uploadPermission("pending"),
    });
    apiMocks.resolveRagUploadRequest.mockResolvedValue({
      ok: true,
      data: uploadPermission("approved"),
    });
    apiMocks.uploadRagDocument.mockResolvedValue({
      ok: true,
      data: { artifact_id: "artifact", document_id: "document", job_id: "job", status: "queued", uploaded: true, created: true },
    });
    apiMocks.listRagDocuments.mockResolvedValue({ ok: true, data: { documents: [document("paper")] } });
    const store = useRagDocumentStore();
    const file = new File(["%PDF-1.7"], "paper.pdf", { type: "application/pdf" });
    expect(await store.upload("workspace", file)).toBe(true);
    expect(apiMocks.createRagUploadRequest).toHaveBeenCalledWith(
      "workspace",
      "paper.pdf",
      8,
      expect.stringMatching(/^[0-9a-f]{64}$/),
    );
    expect(apiMocks.uploadRagDocument).not.toHaveBeenCalled();
    expect(store.uploadRequest?.status).toBe("pending");

    expect(await store.resolveUpload("allow_once")).toBe(true);
    expect(apiMocks.resolveRagUploadRequest).toHaveBeenCalledWith("upload-request", "allow_once");
    expect(apiMocks.uploadRagDocument).toHaveBeenCalledWith("workspace", "upload-request", file);
    expect(store.uploadMessage).toContain("入库队列");
    expect(store.documents).toHaveLength(1);
  });

  it("does not upload or refresh documents when the L2 request is denied", async () => {
    apiMocks.createRagUploadRequest.mockResolvedValue({ ok: true, data: uploadPermission("pending") });
    apiMocks.resolveRagUploadRequest.mockResolvedValue({ ok: true, data: uploadPermission("denied") });
    const store = useRagDocumentStore();
    const file = new File(["%PDF-1.7"], "paper.pdf", { type: "application/pdf" });

    expect(await store.upload("workspace", file)).toBe(true);
    expect(await store.resolveUpload("deny")).toBe(true);

    expect(apiMocks.uploadRagDocument).not.toHaveBeenCalled();
    expect(apiMocks.listRagDocuments).not.toHaveBeenCalled();
    expect(store.uploadRequest).toBeNull();
    expect(store.uploadMessage).toContain("未创建 Artifact");
  });

  it("clears a stale upload dialog after a non-recoverable upload failure", async () => {
    apiMocks.createRagUploadRequest.mockResolvedValue({
      ok: true,
      data: uploadPermission("pending"),
    });
    apiMocks.resolveRagUploadRequest.mockResolvedValue({
      ok: true,
      data: uploadPermission("approved"),
    });
    apiMocks.uploadRagDocument.mockResolvedValue({
      ok: false,
      error: {
        code: "RAG_UPLOAD_PERMISSION_MISMATCH",
        message: "上传文件与已批准的文件摘要不一致",
        category: "permission",
        recoverable: false,
      },
    });
    const store = useRagDocumentStore();
    const file = new File(["%PDF-1.7"], "renamed.pdf", { type: "application/pdf" });

    expect(await store.upload("workspace", file)).toBe(true);
    expect(await store.resolveUpload("allow_once")).toBe(false);

    expect(store.uploadRequest).toBeNull();
    expect(store.operationError?.code).toBe("RAG_UPLOAD_PERMISSION_MISMATCH");
    expect(store.error).toContain("摘要不一致");
  });

  it("keeps the approved upload available after a recoverable transport failure", async () => {
    apiMocks.createRagUploadRequest.mockResolvedValue({
      ok: true,
      data: uploadPermission("pending"),
    });
    apiMocks.resolveRagUploadRequest.mockResolvedValue({
      ok: true,
      data: uploadPermission("approved"),
    });
    apiMocks.uploadRagDocument.mockResolvedValue({
      ok: false,
      error: {
        code: "NETWORK_UNAVAILABLE",
        message: "服务暂不可用",
        category: "internal",
        recoverable: true,
      },
    });
    const store = useRagDocumentStore();
    const file = new File(["%PDF-1.7"], "paper.pdf", { type: "application/pdf" });

    expect(await store.upload("workspace", file)).toBe(true);
    expect(await store.resolveUpload("allow_once")).toBe(false);

    expect(store.uploadRequest?.status).toBe("approved");
    expect(store.operationError?.recoverable).toBe(true);
  });

  it("restarts the persisted job without uploading the PDF again", async () => {
    apiMocks.restartRagDocument.mockResolvedValue({
      ok: true,
      data: { document_id: "paper", job_id: "job", status: "queued" },
    });
    apiMocks.listRagDocuments.mockResolvedValue({
      ok: true,
      data: { documents: [document("paper")] },
    });
    const store = useRagDocumentStore();

    expect(await store.restart("workspace", "paper", 2)).toBe(true);

    expect(apiMocks.restartRagDocument).toHaveBeenCalledWith("workspace", "paper", 2);
    expect(apiMocks.uploadRagDocument).not.toHaveBeenCalled();
    expect(store.uploadMessage).toContain("重新排队");
  });

  it("uses optimistic versions when disabling and cancelling documents", async () => {
    apiMocks.updateRagDocument.mockResolvedValue({
      ok: true,
      data: { document_id: "paper", status: "disabled", version: 3 },
    });
    apiMocks.cancelRagDocument.mockResolvedValue({
      ok: true,
      data: { document_id: "paper", status: "failed", version: 4, job_id: "job", job_status: "cancelled" },
    });
    apiMocks.listRagDocuments.mockResolvedValue({ ok: true, data: { documents: [] } });
    const store = useRagDocumentStore();

    expect(await store.setEnabled("workspace", "paper", 2, false)).toBe(true);
    expect(apiMocks.updateRagDocument).toHaveBeenCalledWith("workspace", "paper", 2, false);
    expect(await store.cancel("workspace", "paper", 3)).toBe(true);
    expect(apiMocks.cancelRagDocument).toHaveBeenCalledWith("workspace", "paper", 3);
  });

  it("refreshes authoritative data after a version conflict instead of overwriting it", async () => {
    apiMocks.updateRagDocument.mockResolvedValue({
      ok: false,
      error: {
        code: "RAG_DOCUMENT_VERSION_CONFLICT",
        message: "文档版本已变化",
        category: "runtime",
        recoverable: true,
      },
    });
    apiMocks.listRagDocuments.mockResolvedValue({
      ok: true,
      data: { documents: [document("paper", { version: 3, status: "disabled" })] },
    });
    const store = useRagDocumentStore();

    expect(await store.setEnabled("workspace", "paper", 2, false)).toBe(false);

    expect(apiMocks.updateRagDocument).toHaveBeenCalledWith("workspace", "paper", 2, false);
    expect(store.documents[0]?.version).toBe(3);
    expect(store.operationError?.code).toBe("RAG_DOCUMENT_VERSION_CONFLICT");
    expect(store.error).toContain("列表已刷新");
  });

  it("reports success, failure, and skipped items from a bounded batch", async () => {
    apiMocks.updateRagDocument
      .mockResolvedValueOnce({ ok: true, data: { document_id: "one", status: "disabled", version: 3 } })
      .mockResolvedValueOnce({
        ok: false,
        error: {
          code: "RAG_DOCUMENT_VERSION_CONFLICT",
          message: "版本冲突",
          category: "runtime",
          recoverable: true,
        },
      });
    apiMocks.listRagDocuments.mockResolvedValue({ ok: true, data: { documents: [] } });
    const store = useRagDocumentStore();

    const ok = await store.executeBatch("workspace", [
      document("one", { version: 2 }),
      document("two", { version: 7 }),
      document("three", { status: "failed", version: 4 }),
    ], "disable");

    expect(ok).toBe(false);
    expect(apiMocks.updateRagDocument).toHaveBeenNthCalledWith(1, "workspace", "one", 2, false);
    expect(apiMocks.updateRagDocument).toHaveBeenNthCalledWith(2, "workspace", "two", 7, false);
    expect(store.batchResults.map((item) => item.status)).toEqual(["succeeded", "failed", "skipped"]);
    expect(store.batchResults[1]?.error_code).toBe("RAG_DOCUMENT_VERSION_CONFLICT");
    expect(store.error).toContain("1 个文档处理失败");
  });

  it("requires a separate L4 decision for every document in a batch delete", async () => {
    apiMocks.createRagDeleteRequest
      .mockResolvedValueOnce({ ok: true, data: deleteRequest("request-one", "one") })
      .mockResolvedValueOnce({ ok: true, data: deleteRequest("request-two", "two") });
    apiMocks.resolveRagDeleteRequest
      .mockResolvedValueOnce({
        ok: true,
        data: { permission: deleteRequest("request-one", "one"), document_id: "one", deleted: true, cleanup_pending_count: 0, source_artifact_retained: true },
      })
      .mockResolvedValueOnce({
        ok: true,
        data: { permission: deleteRequest("request-two", "two"), document_id: "two", deleted: false, cleanup_pending_count: 0, source_artifact_retained: true },
      });
    apiMocks.listRagDocuments.mockResolvedValue({ ok: true, data: { documents: [] } });
    const store = useRagDocumentStore();

    expect(await store.startBatchDelete("workspace", [document("one"), document("two")])).toBe(true);
    expect(store.deleteRequest?.id).toBe("request-one");

    expect(await store.resolveDelete("allow_once")).toBe(true);
    expect(store.deleteRequest?.id).toBe("request-two");
    expect(await store.resolveDelete("deny")).toBe(true);

    expect(apiMocks.createRagDeleteRequest).toHaveBeenCalledTimes(2);
    expect(apiMocks.resolveRagDeleteRequest).toHaveBeenNthCalledWith(1, "request-one", "allow_once");
    expect(apiMocks.resolveRagDeleteRequest).toHaveBeenNthCalledWith(2, "request-two", "deny");
    expect(store.batchResults.map((item) => item.status)).toEqual(["succeeded", "skipped"]);
    expect(store.batchRunning).toBe(false);
  });

  it("rejects selections above the batch safety limit", async () => {
    const store = useRagDocumentStore();
    const oversized = Array.from({ length: 21 }, (_, index) => document(`paper-${index}`));

    expect(await store.executeBatch("workspace", oversized, "restart")).toBe(false);
    expect(apiMocks.restartRagDocument).not.toHaveBeenCalled();
    expect(store.error).toContain("1–20");
  });
});

function deleteRequest(id: string, documentId: string) {
  return {
    id,
    task_id: "task",
    run_id: "run",
    tool_name: "rag.delete_document",
    action_summary: `永久删除 ${documentId}`,
    risk_level: "L4" as const,
    scope: { type: "once" as const },
    arguments_summary: { document_id: documentId },
    allowed_decisions: ["allow_once", "deny"] as const,
    created_at: "2026-07-31T00:00:00Z",
    expires_at: "2099-07-31T00:15:00Z",
    status: "pending" as const,
  };
}

function uploadPermission(status: "pending" | "approved" | "denied") {
  return {
    id: "upload-request",
    task_id: "upload-task",
    run_id: "upload-run",
    tool_name: "rag.upload_pdf",
    action_summary: "将 paper.pdf 加入 RAG 文档库",
    risk_level: "L2" as const,
    scope: { type: "once" as const },
    arguments_summary: {
      workspace_id: "workspace",
      filename: "paper.pdf",
      size_bytes: 8,
      content_sha256: "a".repeat(64),
    },
    allowed_decisions: ["allow_once", "deny"] as const,
    created_at: "2026-08-05T00:00:00Z",
    expires_at: "2099-08-05T00:15:00Z",
    status,
    decision: status === "approved" ? "allow_once" as const : status === "denied" ? "deny" as const : undefined,
  };
}
