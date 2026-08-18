import type { RagDocumentDTO, RagDocumentStatus, RagIngestionStatus } from "@jarvis/shared";

export const RAG_BATCH_SELECTION_LIMIT = 20;

export type RagBatchAction = "enable" | "disable" | "restart" | "cancel" | "delete";

export const ragDocumentStatusLabels: Record<RagDocumentStatus, string> = {
  indexing: "索引中",
  ready: "可检索",
  failed: "失败",
  disabled: "已停用",
};

export const ragJobStatusLabels: Record<RagIngestionStatus, string> = {
  queued: "等待处理",
  parsing: "解析中",
  chunking: "分块中",
  embedding: "向量化中",
  completed: "已完成",
  failed: "失败",
  cancelled: "已取消",
};

export const ragBatchActionLabels: Record<RagBatchAction, string> = {
  enable: "批量启用",
  disable: "批量停用",
  restart: "批量重新执行",
  cancel: "批量取消作业",
  delete: "批量永久删除",
};

export function isRagBatchEligible(document: RagDocumentDTO, action: RagBatchAction): boolean {
  if (action === "enable") return document.status === "disabled";
  if (action === "disable") return document.status === "ready";
  if (action === "restart") return document.status !== "indexing" && document.status !== "disabled";
  if (action === "cancel") return document.status === "indexing";
  return document.status !== "indexing";
}

export function ragBatchImpact(action: RagBatchAction): string {
  if (action === "enable") return "恢复选中文档的检索可见性；索引内容不会被重建。";
  if (action === "disable") return "停止选中文档参与检索；分块、向量和原始文件仍会保留。";
  if (action === "restart") return "重新执行解析、分块和向量化；构建期间文档不会提供旧索引证据。";
  if (action === "cancel") return "取消当前非终态作业；已完成阶段不会被视为可检索索引。";
  return "每个文档都必须单独通过 L4 单次确认；只删除 RAG 派生记录与文件，保留原始 Artifact。";
}

export function vectorCountText(document: RagDocumentDTO): string {
  const progress = document.latest_job?.progress;
  if (progress && (progress.embedding_total > 0 || progress.embedding_completed > 0)) {
    return `${progress.embedding_completed}/${progress.embedding_total}`;
  }
  if (document.status === "ready" && document.chunk_count > 0) return "历史作业未记录";
  return "尚未生成";
}

export function sourceSummary(document: RagDocumentDTO): string {
  return `受控 Artifact · ${document.source_artifact_id}`;
}

export function isRagVersionConflict(code: string): boolean {
  return code === "RAG_DOCUMENT_VERSION_CONFLICT" || code === "RAG_RESTART_CONFLICT";
}
