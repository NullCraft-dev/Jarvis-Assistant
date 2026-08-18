import { defineStore } from "pinia";
import { ref } from "vue";
import type { AppError, ID, PermissionRequestDTO, RagDocumentDTO } from "@jarvis/shared";
import * as api from "@/api/client";
import { normalizeClientError } from "@/api/errors";
import {
  isRagBatchEligible,
  isRagVersionConflict,
  RAG_BATCH_SELECTION_LIMIT,
  ragBatchActionLabels,
  type RagBatchAction,
} from "@/features/knowledge/ragDocumentPresentation";

export type RagBatchResult = {
  document_id: ID;
  title: string;
  status: "succeeded" | "failed" | "skipped";
  message: string;
  error_code?: string;
};

async function sha256(file: File): Promise<string> {
  const digest = await crypto.subtle.digest("SHA-256", await file.arrayBuffer());
  return Array.from(new Uint8Array(digest), (byte) => byte.toString(16).padStart(2, "0")).join("");
}

function isTerminalUploadFailure(failure: AppError): boolean {
  return failure.recoverable !== true;
}

export const useRagDocumentStore = defineStore("ragDocuments", () => {
  const documents = ref<RagDocumentDTO[]>([]);
  const loading = ref(false);
  const error = ref<string | null>(null);
  const uploading = ref(false);
  const uploadRequest = ref<PermissionRequestDTO | null>(null);
  const restartingDocumentId = ref<ID | null>(null);
  const mutatingDocumentId = ref<ID | null>(null);
  const deleteRequest = ref<PermissionRequestDTO | null>(null);
  const uploadMessage = ref<string | null>(null);
  const loadedWorkspaceId = ref<ID | null>(null);
  const operationError = ref<AppError | null>(null);
  const batchRunning = ref(false);
  const batchAction = ref<RagBatchAction | null>(null);
  const batchResults = ref<RagBatchResult[]>([]);
  const batchDeleteQueue = ref<RagDocumentDTO[]>([]);
  const batchDeleteTotal = ref(0);
  const currentDeleteDocument = ref<RagDocumentDTO | null>(null);
  let deleteMode: "single" | "batch" = "single";
  let pendingUploadFile: File | null = null;
  let pendingUploadWorkspaceId: ID | null = null;
  let batchWorkspaceId: ID | null = null;
  let generation = 0;

  async function recordMutationFailure(workspaceId: ID, failure: AppError) {
    operationError.value = failure;
    const conflict = isRagVersionConflict(failure.code);
    if (conflict) await load(workspaceId);
    error.value = conflict
      ? `${failure.message}。列表已刷新，请基于最新版本重试。`
      : failure.message;
  }

  function clearOperationFeedback() {
    operationError.value = null;
    uploadMessage.value = null;
    error.value = null;
  }

  async function load(workspaceId: ID | null) {
    const currentGeneration = ++generation;
    if (!workspaceId) {
      documents.value = [];
      loadedWorkspaceId.value = null;
      error.value = null;
      loading.value = false;
      return;
    }
    loading.value = true;
    error.value = null;
    try {
      const result = await api.listRagDocuments(workspaceId, true);
      if (currentGeneration !== generation) return;
      if (!result.ok) {
        error.value = result.error.message;
        documents.value = [];
        return;
      }
      documents.value = result.data.documents;
      loadedWorkspaceId.value = workspaceId;
    } catch {
      if (currentGeneration !== generation) return;
      error.value = "RAG 文档服务不可用";
      documents.value = [];
    } finally {
      if (currentGeneration === generation) loading.value = false;
    }
  }

  async function upload(workspaceId: ID | null, file: File) {
    if (!workspaceId) {
      error.value = "请先选择工作区";
      return false;
    }
    uploading.value = true;
    clearOperationFeedback();
    try {
      const contentSha256 = await sha256(file);
      const result = await api.createRagUploadRequest(
        workspaceId,
        file.name,
        file.size,
        contentSha256,
      );
      if (!result.ok) {
        error.value = result.error.message;
        return false;
      }
      pendingUploadFile = file;
      pendingUploadWorkspaceId = workspaceId;
      uploadRequest.value = result.data;
      return true;
    } catch {
      error.value = "RAG 文档上传失败";
      return false;
    } finally {
      uploading.value = false;
    }
  }

  async function resolveUpload(decision: "allow_once" | "deny") {
    const request = uploadRequest.value;
    const workspaceId = pendingUploadWorkspaceId;
    const file = pendingUploadFile;
    if (!request || !workspaceId || !file) return false;
    uploading.value = true;
    error.value = null;
    try {
      const resolution = await api.resolveRagUploadRequest(request.id, decision);
      if (!resolution.ok) {
        operationError.value = resolution.error;
        error.value = resolution.error.message;
        return false;
      }
      uploadRequest.value = resolution.data;
      if (decision === "deny") {
        uploadMessage.value = "已取消上传；未创建 Artifact、RAG 文档或入库作业";
        clearPendingUpload();
        return true;
      }
      const result = await api.uploadRagDocument(workspaceId, request.id, file);
      if (!result.ok) {
        operationError.value = result.error;
        error.value = result.error.message;
        if (isTerminalUploadFailure(result.error)) clearPendingUpload();
        return false;
      }
      uploadMessage.value = result.data.created
        ? "PDF 已获单次授权并进入 RAG 入库队列"
        : "该 PDF 已存在，已恢复现有入库状态";
      clearPendingUpload();
      await load(workspaceId);
      return true;
    } catch (cause) {
      const failure = normalizeClientError(cause, "RAG 文档上传失败");
      operationError.value = failure;
      error.value = failure.message;
      if (isTerminalUploadFailure(failure)) clearPendingUpload();
      return false;
    } finally {
      uploading.value = false;
    }
  }

  function clearPendingUpload() {
    uploadRequest.value = null;
    pendingUploadFile = null;
    pendingUploadWorkspaceId = null;
  }

  async function restart(workspaceId: ID | null, documentId: ID, expectedVersion: number) {
    if (!workspaceId) {
      error.value = "请先选择工作区";
      return false;
    }
    restartingDocumentId.value = documentId;
    clearOperationFeedback();
    try {
      const result = await api.restartRagDocument(workspaceId, documentId, expectedVersion);
      if (!result.ok) {
        await recordMutationFailure(workspaceId, result.error);
        return false;
      }
      uploadMessage.value = "RAG 作业已从解析阶段重新排队";
      await load(workspaceId);
      return true;
    } catch (cause) {
      const failure = normalizeClientError(cause, "RAG 作业重新执行失败");
      operationError.value = failure;
      error.value = failure.message;
      return false;
    } finally {
      restartingDocumentId.value = null;
    }
  }

  async function setEnabled(
    workspaceId: ID | null,
    documentId: ID,
    expectedVersion: number,
    enabled: boolean,
  ) {
    if (!workspaceId) return false;
    mutatingDocumentId.value = documentId;
    clearOperationFeedback();
    try {
      const result = await api.updateRagDocument(
        workspaceId,
        documentId,
        expectedVersion,
        enabled,
      );
      if (!result.ok) {
        await recordMutationFailure(workspaceId, result.error);
        return false;
      }
      uploadMessage.value = enabled ? "RAG 文档已恢复检索" : "RAG 文档已停用";
      await load(workspaceId);
      return true;
    } catch (cause) {
      const failure = normalizeClientError(cause, "RAG 文档状态更新失败");
      operationError.value = failure;
      error.value = failure.message;
      return false;
    } finally {
      mutatingDocumentId.value = null;
    }
  }

  async function cancel(
    workspaceId: ID | null,
    documentId: ID,
    expectedVersion: number,
  ) {
    if (!workspaceId) return false;
    mutatingDocumentId.value = documentId;
    clearOperationFeedback();
    try {
      const result = await api.cancelRagDocument(workspaceId, documentId, expectedVersion);
      if (!result.ok) {
        await recordMutationFailure(workspaceId, result.error);
        return false;
      }
      uploadMessage.value = "RAG 作业已取消，可稍后重新执行";
      await load(workspaceId);
      return true;
    } catch (cause) {
      const failure = normalizeClientError(cause, "RAG 作业取消失败");
      operationError.value = failure;
      error.value = failure.message;
      return false;
    } finally {
      mutatingDocumentId.value = null;
    }
  }

  async function requestDelete(workspaceId: ID | null, document: RagDocumentDTO) {
    if (!workspaceId) return false;
    deleteMode = "single";
    currentDeleteDocument.value = document;
    mutatingDocumentId.value = document.id;
    clearOperationFeedback();
    try {
      const result = await api.createRagDeleteRequest(workspaceId, document.id, document.version);
      if (!result.ok) {
        currentDeleteDocument.value = null;
        await recordMutationFailure(workspaceId, result.error);
        return false;
      }
      deleteRequest.value = result.data;
      return true;
    } catch (cause) {
      currentDeleteDocument.value = null;
      const failure = normalizeClientError(cause, "创建永久删除确认失败");
      operationError.value = failure;
      error.value = failure.message;
      return false;
    } finally {
      mutatingDocumentId.value = null;
    }
  }

  async function resolveDelete(decision: "allow_once" | "deny", stopBatch = false) {
    if (!deleteRequest.value) return false;
    const documentId = String(deleteRequest.value.arguments_summary.document_id ?? "");
    mutatingDocumentId.value = documentId;
    error.value = null;
    try {
      const result = await api.resolveRagDeleteRequest(deleteRequest.value.id, decision);
      if (!result.ok) {
        operationError.value = result.error;
        error.value = result.error.message;
        return false;
      }
      uploadMessage.value = decision === "allow_once"
        ? result.data.cleanup_pending_count
          ? `索引已删除，仍有 ${result.data.cleanup_pending_count} 个派生文件等待清理`
          : "RAG 文档及派生索引已永久删除；原始上传文件已保留"
        : "已取消永久删除";
      if (deleteMode === "batch" && currentDeleteDocument.value) {
        batchResults.value.push({
          document_id: currentDeleteDocument.value.id,
          title: currentDeleteDocument.value.title,
          status: decision === "allow_once" ? "succeeded" : "skipped",
          message: decision === "allow_once" ? "已永久删除派生索引，原始 Artifact 已保留" : "用户跳过本项删除",
        });
      }
      deleteRequest.value = null;
      currentDeleteDocument.value = null;
      if (deleteMode === "batch") {
        if (stopBatch) batchDeleteQueue.value = [];
        await openNextBatchDeleteRequest();
      } else if (loadedWorkspaceId.value) {
        await load(loadedWorkspaceId.value);
      }
      return true;
    } catch (cause) {
      const failure = normalizeClientError(cause, "永久删除操作失败");
      operationError.value = failure;
      error.value = failure.message;
      return false;
    } finally {
      mutatingDocumentId.value = null;
    }
  }

  async function executeBatch(
    workspaceId: ID | null,
    selectedDocuments: RagDocumentDTO[],
    action: Exclude<RagBatchAction, "delete">,
  ) {
    if (!workspaceId) return false;
    if (!selectedDocuments.length || selectedDocuments.length > RAG_BATCH_SELECTION_LIMIT) {
      error.value = `每次请选择 1–${RAG_BATCH_SELECTION_LIMIT} 个文档`;
      return false;
    }
    clearOperationFeedback();
    batchRunning.value = true;
    batchAction.value = action;
    batchResults.value = [];
    for (const document of selectedDocuments) {
      if (!isRagBatchEligible(document, action)) {
        batchResults.value.push({
          document_id: document.id,
          title: document.title,
          status: "skipped",
          message: "当前状态不适用此操作",
        });
        continue;
      }
      try {
        const result = action === "restart"
          ? await api.restartRagDocument(workspaceId, document.id, document.version)
          : action === "cancel"
            ? await api.cancelRagDocument(workspaceId, document.id, document.version)
            : await api.updateRagDocument(
              workspaceId,
              document.id,
              document.version,
              action === "enable",
            );
        if (!result.ok) {
          operationError.value ??= result.error;
          batchResults.value.push({
            document_id: document.id,
            title: document.title,
            status: "failed",
            message: result.error.message,
            error_code: result.error.code,
          });
          continue;
        }
        batchResults.value.push({
          document_id: document.id,
          title: document.title,
          status: "succeeded",
          message: `${ragBatchActionLabels[action]}成功`,
        });
      } catch (cause) {
        const failure = normalizeClientError(cause, `${ragBatchActionLabels[action]}失败`);
        operationError.value ??= failure;
        batchResults.value.push({
          document_id: document.id,
          title: document.title,
          status: "failed",
          message: failure.message,
          error_code: failure.code,
        });
      }
    }
    await load(workspaceId);
    const failures = batchResults.value.filter((item) => item.status === "failed").length;
    const successes = batchResults.value.filter((item) => item.status === "succeeded").length;
    uploadMessage.value = `${ragBatchActionLabels[action]}完成：成功 ${successes} 项，失败 ${failures} 项`;
    error.value = failures ? `${failures} 个文档处理失败，请查看批量结果。` : null;
    batchRunning.value = false;
    return failures === 0;
  }

  async function startBatchDelete(workspaceId: ID | null, selectedDocuments: RagDocumentDTO[]) {
    if (!workspaceId) return false;
    if (!selectedDocuments.length || selectedDocuments.length > RAG_BATCH_SELECTION_LIMIT) {
      error.value = `每次请选择 1–${RAG_BATCH_SELECTION_LIMIT} 个文档`;
      return false;
    }
    clearOperationFeedback();
    deleteMode = "batch";
    batchWorkspaceId = workspaceId;
    batchAction.value = "delete";
    batchResults.value = selectedDocuments
      .filter((document) => !isRagBatchEligible(document, "delete"))
      .map((document) => ({
        document_id: document.id,
        title: document.title,
        status: "skipped" as const,
        message: "索引中的文档必须先取消作业",
      }));
    batchDeleteQueue.value = selectedDocuments.filter((document) => isRagBatchEligible(document, "delete"));
    batchDeleteTotal.value = batchDeleteQueue.value.length;
    batchRunning.value = true;
    await openNextBatchDeleteRequest();
    return true;
  }

  async function openNextBatchDeleteRequest() {
    if (deleteMode !== "batch" || !batchWorkspaceId) return;
    while (batchDeleteQueue.value.length) {
      const document = batchDeleteQueue.value.shift()!;
      currentDeleteDocument.value = document;
      mutatingDocumentId.value = document.id;
      try {
        const result = await api.createRagDeleteRequest(
          batchWorkspaceId,
          document.id,
          document.version,
        );
        if (result.ok) {
          deleteRequest.value = result.data;
          return;
        }
        operationError.value ??= result.error;
        batchResults.value.push({
          document_id: document.id,
          title: document.title,
          status: "failed",
          message: result.error.message,
          error_code: result.error.code,
        });
      } catch (cause) {
        const failure = normalizeClientError(cause, "创建永久删除确认失败");
        operationError.value ??= failure;
        batchResults.value.push({
          document_id: document.id,
          title: document.title,
          status: "failed",
          message: failure.message,
          error_code: failure.code,
        });
      } finally {
        mutatingDocumentId.value = null;
      }
    }
    currentDeleteDocument.value = null;
    batchRunning.value = false;
    const failures = batchResults.value.filter((item) => item.status === "failed").length;
    const successes = batchResults.value.filter((item) => item.status === "succeeded").length;
    if (batchWorkspaceId) await load(batchWorkspaceId);
    uploadMessage.value = `批量永久删除完成：成功 ${successes} 项，失败 ${failures} 项`;
    error.value = failures ? `${failures} 个文档未能完成删除，请查看批量结果。` : null;
    batchWorkspaceId = null;
  }

  function clearBatchResults() {
    if (batchRunning.value) return;
    batchAction.value = null;
    batchResults.value = [];
  }

  function closeDeleteRequest() {
    if (deleteMode === "batch") return;
    deleteRequest.value = null;
    currentDeleteDocument.value = null;
  }

  return {
    documents,
    loading,
    uploading,
    uploadRequest,
    restartingDocumentId,
    mutatingDocumentId,
    deleteRequest,
    error,
    uploadMessage,
    operationError,
    loadedWorkspaceId,
    batchRunning,
    batchAction,
    batchResults,
    batchDeleteQueue,
    batchDeleteTotal,
    currentDeleteDocument,
    load,
    upload,
    resolveUpload,
    restart,
    setEnabled,
    cancel,
    requestDelete,
    resolveDelete,
    executeBatch,
    startBatchDelete,
    clearBatchResults,
    closeDeleteRequest,
  };
});
