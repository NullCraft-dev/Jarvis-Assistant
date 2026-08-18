// Typed API Client — 前端调用后端的唯一入口
// 分层：Frontend State → api client → Go Gateway
// UI 组件不得绕过本层直接拼业务请求
// 真源：docs/13-interface-contract.md

import type {
  ApiResult,
  CreateTaskInput,
  CreateTaskOutput,
  ListTasksOutput,
  ListConversationsOutput,
  ConversationDetailOutput,
  TaskDetailOutput,
  AgentRunDTO,
  PermissionDecisionDTO,
  SettingsDTO,
  RuntimeEvent,
  ID,
  ResolvePermissionOutput,
  PermissionRequestDTO,
  WorkersOutput,
  RuntimeHealthDTO,
  StorageReconciliationDTO,
  TerminalEventRepairInspectionDTO,
  TerminalEventRepairRequestOutput,
  TerminalEventRepairResolutionOutput,
  ListRuntimeDeadLettersInput,
  ListRuntimeDeadLettersOutput,
  DlqRetryRecordInput,
  DlqRetryInspectionDTO,
  DlqRetryRequestOutput,
  DlqRetryResolutionOutput,
  ListWorkspacesOutput,
  PickWorkspaceOutput,
  RevokeWorkspaceOutput,
  ModelConfigDTO,
  ModelTestOutput,
  ListAuditLogsInput,
  ListAuditLogsOutput,
  AuditRetentionPreviewDTO,
  AuditRetentionPreviewInput,
  CreateAuditRetentionRequestInput,
  CreateAuditRetentionRequestOutput,
  ResolveAuditRetentionRequestInput,
  AuditRetentionResolutionDTO,
  ArtifactDTO,
  CreateMemoryInput,
  UpdateMemoryInput,
  ListMemoriesOutput,
  MemoryMutationOutput,
  ListMemoryCandidatesOutput,
  UpdateMemoryCandidateInput,
  ResolveMemoryCandidateInput,
  MemoryCandidateMutationOutput,
  ApproveMemoryCandidateOutput,
  ListKnowledgeVaultsOutput,
  ConnectKnowledgeVaultInput,
  KnowledgeVaultMutationOutput,
  ListKnowledgeDocumentsOutput,
  CreateKnowledgeDocumentInput,
  KnowledgeDocumentMutationOutput,
  ListRagDocumentsOutput,
  ListRagFeedbackOutput,
  RagFeedbackMutationOutput,
  RagFeedbackDetailOutput,
  TriageRagFeedbackInput,
  TriageRagFeedbackOutput,
  ListRagEvaluationTracesOutput,
  ListRagQualityGateRunsOutput,
  ListRagQualityFailureTargetsOutput,
  ListRagQualityIssuesOutput,
  RagQualityIssueDTO,
  UpdateRagQualityIssueInput,
  UpdateRagQualityIssueOutput,
  RagEvaluationPrivacyStatus,
  RagEvaluationTraceDetailOutput,
  ReviewRagTraceLabelInput,
  RagFeedbackStatus,
  SubmitRagFeedbackInput,
  RestartRagDocumentOutput,
  UpdateRagDocumentOutput,
  CancelRagDocumentOutput,
  UploadRagDocumentOutput,
  RagDeleteResolutionOutput,
  ListScheduledTasksOutput,
  CreateScheduledTaskInput,
  UpdateScheduledTaskInput,
  ScheduledTaskMutationOutput,
  ScheduledExecutionOutput,
  ListMcpServersOutput,
  CreateMcpServerInput,
  UpdateMcpServerInput,
  McpServerMutationOutput,
  McpDiscoveryRefreshOutput,
} from "@jarvis/shared";
import { apiGet, apiPost, apiDelete, apiPatch, apiUpload, subscribeEvents } from "./transport";
import type { EventConnectionState } from "./transport";

// -- Task API --

export function createTask(
  input: CreateTaskInput
): Promise<ApiResult<CreateTaskOutput>> {
  return apiPost<CreateTaskOutput>("/tasks", input);
}

export function listTasks(): Promise<ApiResult<ListTasksOutput>> {
  return apiGet<ListTasksOutput>("/tasks");
}

export function getTask(taskId: ID): Promise<ApiResult<TaskDetailOutput>> {
  return apiGet<TaskDetailOutput>(`/tasks/${taskId}`);
}

export function getArtifact(artifactId: ID): Promise<ApiResult<ArtifactDTO>> {
  return apiGet<ArtifactDTO>(`/artifacts/${artifactId}`);
}

// -- Run API --

export function pauseRun(runId: ID): Promise<ApiResult<AgentRunDTO>> {
  return apiPost<AgentRunDTO>(`/runs/${runId}/pause`);
}

export function resumeRun(runId: ID): Promise<ApiResult<AgentRunDTO>> {
  return apiPost<AgentRunDTO>(`/runs/${runId}/resume`);
}

export function cancelRun(runId: ID): Promise<ApiResult<AgentRunDTO>> {
  return apiPost<AgentRunDTO>(`/runs/${runId}/cancel`);
}

export function retryFailedStep(runId: ID, stepId: ID): Promise<ApiResult<AgentRunDTO>> {
  return apiPost<AgentRunDTO>(`/runs/${runId}/steps/${stepId}/retry`);
}

export function subscribeRunEvents(
  runId: ID,
  handler: (event: RuntimeEvent) => void,
  onConnectionState?: (state: EventConnectionState) => void,
) {
  return subscribeEvents(runId, handler, onConnectionState);
}

// -- Permission API --

export function resolvePermission(
  decision: PermissionDecisionDTO
): Promise<ApiResult<ResolvePermissionOutput>> {
  return apiPost<ResolvePermissionOutput>("/permissions/resolve", decision);
}

export function listPendingPermissions(
  runId: ID
): Promise<ApiResult<{ requests: PermissionRequestDTO[] }>> {
  return apiGet<{ requests: PermissionRequestDTO[] }>(`/runs/${runId}/permissions`);
}

// -- Settings API --

export function getSettings(): Promise<ApiResult<SettingsDTO>> {
  return apiGet<SettingsDTO>("/settings");
}

// -- Conversation API（多轮对话 MVP）--

export function listConversations(): Promise<ApiResult<ListConversationsOutput>> {
  return apiGet<ListConversationsOutput>("/conversations");
}

export function getConversation(
  convId: ID,
  opts?: { limit?: number; before?: string },
): Promise<ApiResult<ConversationDetailOutput>> {
  const params = new URLSearchParams();
  if (opts?.limit) params.set("limit", String(opts.limit));
  if (opts?.before) params.set("before", opts.before);
  const qs = params.toString();
  return apiGet<ConversationDetailOutput>(
    `/conversations/${convId}${qs ? `?${qs}` : ""}`
  );
}

// -- Worker Status API (3B heartbeat) --

export function getWorkers(): Promise<ApiResult<WorkersOutput>> {
  return apiGet<WorkersOutput>("/runtime/workers");
}

export function getRuntimeHealth(): Promise<ApiResult<RuntimeHealthDTO>> {
  return apiGet<RuntimeHealthDTO>("/runtime/health");
}

export function getStorageReconciliation(limit = 50): Promise<ApiResult<StorageReconciliationDTO>> {
  return apiGet<StorageReconciliationDTO>(`/runtime/storage-reconciliation?limit=${limit}`);
}

export function inspectTerminalEventRepair(runId: ID): Promise<ApiResult<TerminalEventRepairInspectionDTO>> {
  return apiPost<TerminalEventRepairInspectionDTO>(
    "/runtime/storage-reconciliation/repairs/inspect", { run_id: runId },
  );
}

export function createTerminalEventRepairRequest(runId: ID): Promise<ApiResult<TerminalEventRepairRequestOutput>> {
  return apiPost<TerminalEventRepairRequestOutput>(
    "/runtime/storage-reconciliation/repairs/requests", { run_id: runId },
  );
}

export function resolveTerminalEventRepairRequest(
  requestId: ID, decision: "allow_once" | "deny", note = "",
): Promise<ApiResult<TerminalEventRepairResolutionOutput>> {
  return apiPost<TerminalEventRepairResolutionOutput>(
    `/runtime/storage-reconciliation/repairs/requests/${requestId}/resolve`,
    { decision, note },
  );
}

export function listRuntimeDeadLetters(input: ListRuntimeDeadLettersInput): Promise<ApiResult<ListRuntimeDeadLettersOutput>> {
  const params = new URLSearchParams({ source: input.source });
  if (input.limit) params.set("limit", String(input.limit));
  if (input.before) params.set("before", input.before);
  if (input.error_code) params.set("error_code", input.error_code);
  if (input.task_id) params.set("task_id", input.task_id);
  if (input.run_id) params.set("run_id", input.run_id);
  return apiGet<ListRuntimeDeadLettersOutput>(`/runtime/dead-letters?${params.toString()}`);
}

export function inspectRuntimeDeadLetterRetry(input: DlqRetryRecordInput): Promise<ApiResult<DlqRetryInspectionDTO>> {
  return apiPost<DlqRetryInspectionDTO>("/runtime/dead-letters/retry/inspect", input);
}

export function createRuntimeDeadLetterRetryRequest(input: DlqRetryRecordInput): Promise<ApiResult<DlqRetryRequestOutput>> {
  return apiPost<DlqRetryRequestOutput>("/runtime/dead-letters/retry/requests", input);
}

export function resolveRuntimeDeadLetterRetryRequest(
  requestId: ID,
  decision: "allow_once" | "deny",
  note?: string,
): Promise<ApiResult<DlqRetryResolutionOutput>> {
  return apiPost<DlqRetryResolutionOutput>(
    `/runtime/dead-letters/retry/requests/${requestId}/resolve`,
    { decision, note: note ?? "" },
  );
}

// -- Workspace API --

export function listWorkspaces(includeRevoked?: boolean): Promise<ApiResult<ListWorkspacesOutput>> {
  const qs = includeRevoked ? "?include_revoked=true" : "";
  return apiGet<ListWorkspacesOutput>(`/workspaces${qs}`);
}

export function pickWorkspace(): Promise<ApiResult<PickWorkspaceOutput>> {
  return apiPost<PickWorkspaceOutput>("/workspaces/pick");
}

export function revokeWorkspace(workspaceId: ID): Promise<ApiResult<RevokeWorkspaceOutput>> {
  return apiDelete<RevokeWorkspaceOutput>(`/workspaces/${workspaceId}`);
}

// -- Obsidian Personal Knowledge Base --

export function listKnowledgeVaults(): Promise<ApiResult<ListKnowledgeVaultsOutput>> {
  return apiGet<ListKnowledgeVaultsOutput>("/knowledge-vaults");
}
export function connectKnowledgeVault(input: ConnectKnowledgeVaultInput): Promise<ApiResult<KnowledgeVaultMutationOutput>> {
  return apiPost<KnowledgeVaultMutationOutput>("/knowledge-vaults/connect", input);
}
export function listKnowledgeDocuments(vaultId: ID): Promise<ApiResult<ListKnowledgeDocumentsOutput>> {
  return apiGet<ListKnowledgeDocumentsOutput>(`/knowledge-vaults/${vaultId}/documents`);
}
export function createKnowledgeDocument(vaultId: ID, input: CreateKnowledgeDocumentInput): Promise<ApiResult<KnowledgeDocumentMutationOutput>> {
  return apiPost<KnowledgeDocumentMutationOutput>(`/knowledge-vaults/${vaultId}/documents`, input);
}
export function listRagDocuments(workspaceId: ID, includeDisabled = false): Promise<ApiResult<ListRagDocumentsOutput>> {
  const query = new URLSearchParams({ workspace_id: workspaceId });
  if (includeDisabled) query.set("include_disabled", "true");
  return apiGet<ListRagDocumentsOutput>(`/rag/documents?${query.toString()}`);
}

export function submitRagFeedback(input: SubmitRagFeedbackInput): Promise<ApiResult<RagFeedbackMutationOutput>> {
  return apiPost<RagFeedbackMutationOutput>("/rag/feedback", input);
}

export function listRagFeedback(
  workspaceId: ID,
  status: RagFeedbackStatus = "pending",
  limit = 50,
): Promise<ApiResult<ListRagFeedbackOutput>> {
  const params = new URLSearchParams({ workspace_id: workspaceId, status, limit: String(limit) });
  return apiGet<ListRagFeedbackOutput>(`/rag/feedback?${params.toString()}`);
}

export function resolveRagFeedback(
  feedbackId: ID,
  status: "reviewed" | "dismissed",
): Promise<ApiResult<RagFeedbackMutationOutput>> {
  return apiPatch<RagFeedbackMutationOutput>(`/rag/feedback/${feedbackId}`, { status });
}
export function inspectRagFeedback(feedbackId: ID): Promise<ApiResult<RagFeedbackDetailOutput>> {
  return apiGet<RagFeedbackDetailOutput>(`/rag/feedback/${feedbackId}`);
}
export function triageRagFeedback(feedbackId: ID, input: TriageRagFeedbackInput): Promise<ApiResult<TriageRagFeedbackOutput>> {
  return apiPost<TriageRagFeedbackOutput>(`/rag/feedback/${feedbackId}/triage`, input);
}
export function listRagEvaluationTraces(
  workspaceId: ID,
  privacyStatus: RagEvaluationPrivacyStatus | "all" = "pending",
  limit = 50,
): Promise<ApiResult<ListRagEvaluationTracesOutput>> {
  const params = new URLSearchParams({ workspace_id: workspaceId, privacy_status: privacyStatus, limit: String(limit) });
  return apiGet<ListRagEvaluationTracesOutput>(`/rag/evaluation/traces?${params.toString()}`);
}
export function listRagQualityGateRuns(limit = 20): Promise<ApiResult<ListRagQualityGateRunsOutput>> {
  return apiGet<ListRagQualityGateRunsOutput>(`/rag/evaluation/gates?limit=${limit}`);
}
export function listRagQualityFailureTargets(runId: ID, failureType: string, limit = 50): Promise<ApiResult<ListRagQualityFailureTargetsOutput>> {
  const params = new URLSearchParams({ failure_type: failureType, limit: String(limit) });
  return apiGet<ListRagQualityFailureTargetsOutput>(`/rag/evaluation/gates/${runId}/failure-targets?${params.toString()}`);
}
export function updateRagQualityIssue(issueId: ID, input: UpdateRagQualityIssueInput): Promise<ApiResult<UpdateRagQualityIssueOutput>> {
  return apiPatch<UpdateRagQualityIssueOutput>(`/rag/evaluation/issues/${issueId}`, input);
}
export function listRagQualityIssues(
  status: RagQualityIssueDTO["status"] | "all" = "all",
  owner: RagQualityIssueDTO["owner"] | "all" = "all",
  failureType = "all",
  limit = 50,
): Promise<ApiResult<ListRagQualityIssuesOutput>> {
  const params = new URLSearchParams({ status, owner, failure_type: failureType, limit: String(limit) });
  return apiGet<ListRagQualityIssuesOutput>(`/rag/evaluation/issues?${params.toString()}`);
}
export function inspectRagEvaluationTrace(workspaceId: ID, traceId: ID): Promise<ApiResult<RagEvaluationTraceDetailOutput>> {
  return apiGet<RagEvaluationTraceDetailOutput>(`/rag/evaluation/traces/${traceId}?workspace_id=${encodeURIComponent(workspaceId)}`);
}
export function reviewRagEvaluationPrivacy(workspaceId: ID, traceId: ID, decision: "approved" | "rejected"): Promise<ApiResult<RagEvaluationTraceDetailOutput>> {
  return apiPost<RagEvaluationTraceDetailOutput>(`/rag/evaluation/traces/${traceId}/privacy`, { workspace_id: workspaceId, decision });
}
export function reviewRagEvaluationLabel(traceId: ID, input: ReviewRagTraceLabelInput): Promise<ApiResult<RagEvaluationTraceDetailOutput>> {
  return apiPost<RagEvaluationTraceDetailOutput>(`/rag/evaluation/traces/${traceId}/label`, input);
}
export function promoteRagEvaluationTrace(workspaceId: ID, traceId: ID): Promise<ApiResult<RagEvaluationTraceDetailOutput>> {
  return apiPost<RagEvaluationTraceDetailOutput>(`/rag/evaluation/traces/${traceId}/promote`, { workspace_id: workspaceId });
}
export function createRagUploadRequest(
  workspaceId: ID,
  filename: string,
  sizeBytes: number,
  contentSha256: string,
): Promise<ApiResult<PermissionRequestDTO>> {
  return apiPost<PermissionRequestDTO>("/rag/upload-requests", {
    workspace_id: workspaceId,
    filename,
    size_bytes: sizeBytes,
    content_sha256: contentSha256,
  });
}
export function resolveRagUploadRequest(
  requestId: ID,
  decision: "allow_once" | "deny",
  note = "",
): Promise<ApiResult<PermissionRequestDTO>> {
  return apiPost<PermissionRequestDTO>(`/rag/upload-requests/${requestId}/resolve`, { decision, note });
}
export function uploadRagDocument(workspaceId: ID, permissionRequestId: ID, file: File): Promise<ApiResult<UploadRagDocumentOutput>> {
  const form = new FormData();
  form.set("workspace_id", workspaceId);
  form.set("permission_request_id", permissionRequestId);
  form.set("file", file, file.name);
  return apiUpload<UploadRagDocumentOutput>("/rag/documents", form);
}
export function restartRagDocument(workspaceId: ID, documentId: ID, expectedVersion: number): Promise<ApiResult<RestartRagDocumentOutput>> {
  return apiPost<RestartRagDocumentOutput>(`/rag/documents/${documentId}/restart`, {
    workspace_id: workspaceId,
    expected_version: expectedVersion,
  });
}
export function updateRagDocument(workspaceId: ID, documentId: ID, expectedVersion: number, enabled: boolean): Promise<ApiResult<UpdateRagDocumentOutput>> {
  return apiPatch<UpdateRagDocumentOutput>(`/rag/documents/${documentId}`, {
    workspace_id: workspaceId,
    expected_version: expectedVersion,
    enabled,
  });
}
export function cancelRagDocument(workspaceId: ID, documentId: ID, expectedVersion: number): Promise<ApiResult<CancelRagDocumentOutput>> {
  return apiPost<CancelRagDocumentOutput>(`/rag/documents/${documentId}/cancel`, {
    workspace_id: workspaceId,
    expected_version: expectedVersion,
  });
}
export function createRagDeleteRequest(workspaceId: ID, documentId: ID, expectedVersion: number): Promise<ApiResult<PermissionRequestDTO>> {
  return apiPost<PermissionRequestDTO>(`/rag/documents/${documentId}/delete-requests`, {
    workspace_id: workspaceId,
    expected_version: expectedVersion,
  });
}
export function resolveRagDeleteRequest(requestId: ID, decision: "allow_once" | "deny", note = ""): Promise<ApiResult<RagDeleteResolutionOutput>> {
  return apiPost<RagDeleteResolutionOutput>(`/rag/delete-requests/${requestId}/resolve`, { decision, note });
}
export function listScheduledTasks(): Promise<ApiResult<ListScheduledTasksOutput>> { return apiGet<ListScheduledTasksOutput>("/scheduled-tasks"); }
export function createScheduledTask(input: CreateScheduledTaskInput): Promise<ApiResult<ScheduledTaskMutationOutput>> { return apiPost<ScheduledTaskMutationOutput>("/scheduled-tasks", input); }
export function updateScheduledTask(id: ID, input: UpdateScheduledTaskInput): Promise<ApiResult<ScheduledTaskMutationOutput>> { return apiPatch<ScheduledTaskMutationOutput>(`/scheduled-tasks/${id}`, input); }
export function triggerScheduledTask(id: ID): Promise<ApiResult<ScheduledExecutionOutput>> { return apiPost<ScheduledExecutionOutput>(`/scheduled-tasks/${id}/trigger`); }
export function listMcpServers(): Promise<ApiResult<ListMcpServersOutput>> { return apiGet<ListMcpServersOutput>("/mcp-servers"); }
export function createMcpServer(input: CreateMcpServerInput): Promise<ApiResult<McpServerMutationOutput>> { return apiPost<McpServerMutationOutput>("/mcp-servers", input); }
export function connectBuiltinLiteratureServer(): Promise<ApiResult<McpServerMutationOutput>> { return apiPost<McpServerMutationOutput>("/mcp-servers/builtin/literature"); }
export function updateMcpServer(id: ID, input: UpdateMcpServerInput): Promise<ApiResult<McpServerMutationOutput>> { return apiPatch<McpServerMutationOutput>(`/mcp-servers/${id}`, input); }
export function refreshMcpServers(): Promise<ApiResult<McpDiscoveryRefreshOutput>> { return apiPost<McpDiscoveryRefreshOutput>("/mcp-servers/refresh"); }

// -- Long-term Memory API --

export function listMemories(query = ""): Promise<ApiResult<ListMemoriesOutput>> {
  return apiGet<ListMemoriesOutput>(`/memories${query ? `?${query}` : ""}`);
}

export function createMemory(input: CreateMemoryInput): Promise<ApiResult<MemoryMutationOutput>> {
  return apiPost<MemoryMutationOutput>("/memories", input);
}

export function updateMemory(id: ID, input: UpdateMemoryInput): Promise<ApiResult<MemoryMutationOutput>> {
  return apiPatch<MemoryMutationOutput>(`/memories/${id}`, input);
}

export function deleteMemory(id: ID): Promise<ApiResult<MemoryMutationOutput>> {
  return apiDelete<MemoryMutationOutput>(`/memories/${id}`);
}

export function listMemoryCandidates(query = ""): Promise<ApiResult<ListMemoryCandidatesOutput>> {
  return apiGet<ListMemoryCandidatesOutput>(`/memory-candidates${query ? `?${query}` : ""}`);
}

export function updateMemoryCandidate(id: ID, input: UpdateMemoryCandidateInput): Promise<ApiResult<MemoryCandidateMutationOutput>> {
  return apiPatch<MemoryCandidateMutationOutput>(`/memory-candidates/${id}`, input);
}

export function approveMemoryCandidate(id: ID, input: ResolveMemoryCandidateInput): Promise<ApiResult<ApproveMemoryCandidateOutput>> {
  return apiPost<ApproveMemoryCandidateOutput>(`/memory-candidates/${id}/approve`, input);
}

export function rejectMemoryCandidate(id: ID, input: ResolveMemoryCandidateInput): Promise<ApiResult<MemoryCandidateMutationOutput>> {
  return apiPost<MemoryCandidateMutationOutput>(`/memory-candidates/${id}/reject`, input);
}

// -- Model Config API (Phase 6) --

export function getModelConfig(): Promise<ApiResult<ModelConfigDTO>> {
  return apiGet<ModelConfigDTO>("/model-config");
}

export function testModelConnection(): Promise<ApiResult<ModelTestOutput>> {
  return apiPost<ModelTestOutput>("/model-config/test");
}

// -- Audit Log API --

export function listAuditLogs(
  input: ListAuditLogsInput = {},
): Promise<ApiResult<ListAuditLogsOutput>> {
  const params = new URLSearchParams();
  if (input.limit) params.set("limit", String(input.limit));
  if (input.event_type) params.set("event_type", input.event_type);
  if (input.actor) params.set("actor", input.actor);
  if (input.task_id) params.set("task_id", input.task_id);
  if (input.run_id) params.set("run_id", input.run_id);
  if (input.before) params.set("before", input.before);
  const query = params.toString();
  return apiGet<ListAuditLogsOutput>(`/audit-logs${query ? `?${query}` : ""}`);
}

export function previewAuditRetention(
  input: AuditRetentionPreviewInput = {},
): Promise<ApiResult<AuditRetentionPreviewDTO>> {
  const params = new URLSearchParams();
  if (input.standard_days) params.set("standard_days", String(input.standard_days));
  if (input.extended_days) params.set("extended_days", String(input.extended_days));
  if (input.max_scan) params.set("max_scan", String(input.max_scan));
  if (input.max_candidates) {
    params.set("max_candidates", String(input.max_candidates));
  }
  const query = params.toString();
  return apiGet<AuditRetentionPreviewDTO>(
    `/audit-logs/retention/preview${query ? `?${query}` : ""}`,
  );
}

export function createAuditRetentionRequest(
  input: CreateAuditRetentionRequestInput,
): Promise<ApiResult<CreateAuditRetentionRequestOutput>> {
  return apiPost<CreateAuditRetentionRequestOutput>(
    "/audit-logs/retention/requests",
    input,
  );
}

export function resolveAuditRetentionRequest(
  requestId: ID,
  input: ResolveAuditRetentionRequestInput,
): Promise<ApiResult<AuditRetentionResolutionDTO>> {
  return apiPost<AuditRetentionResolutionDTO>(
    `/audit-logs/retention/requests/${requestId}/resolve`,
    input,
  );
}
