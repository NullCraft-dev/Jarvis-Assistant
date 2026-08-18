// ============================================================
// Jarvis Assistant — 共享类型契约
// 真源：docs/13-interface-contract.md
// 本文件是跨层类型的唯一定义，禁止在 UI / mock / handler 中重复定义
// ============================================================

// -- 基础类型 --

/** 所有主对象 ID 使用字符串 */
export type ID = string;

/** ISO 8601 格式时间字符串 */
export type ISODateTime = string;

// -- 统一返回与错误 --

/** 统一 API 返回结构 */
export type ApiResult<T> =
  | { ok: true; data: T }
  | { ok: false; error: AppError };

/** 统一错误结构 */
export type AppError = {
  code: string;
  message: string;
  category: AppErrorCategory;
  recoverable: boolean;
  details?: Record<string, unknown>;
  cause_id?: string;
};

export type AppErrorCategory =
  | "validation"
  | "permission"
  | "not_found"
  | "runtime"
  | "model"
  | "tool"
  | "mcp"
  | "storage"
  | "internal";

// -- 风险等级 --

export type RiskLevel = "L0" | "L1" | "L2" | "L3" | "L4" | "L5";

// -- 成本 --

export type CostSummary = {
  input_tokens?: number;
  output_tokens?: number;
  total_tokens?: number;
  estimated_cost_usd?: number;
};

// ============================================================
// Task
// ============================================================

export type TaskStatus =
  | "pending"
  | "running"
  | "waiting_for_user"
  | "blocked"
  | "failed"
  | "completed"
  | "cancelled";

export type AgentRunStatus =
  | "created"
  | "queued"
  | "running"
  | "pause_requested"
  | "paused"
  | "resume_requested"
  | "waiting_for_permission"
  | "waiting_for_user"
  | "blocked"
  | "failed"
  | "completed"
  | "cancelled";

export type TaskDTO = {
  id: ID;
  conversation_id: ID;
  title: string;
  user_goal: string;
  status: TaskStatus;
  workspace_path?: string;
  workspace_id?: ID;  // FK → workspaces.id
  active_run_id?: ID;
  last_step_summary?: string;
  risk_level?: RiskLevel;
  cost_summary?: CostSummary;
  created_at: ISODateTime;
  updated_at: ISODateTime;
};

// ============================================================
// AgentRun
// ============================================================

export type AgentRunDTO = {
  id: ID;
  task_id: ID;
  agent_id: ID;
  mode: "single_agent" | "multi_agent";
  status: AgentRunStatus;
  current_step_id?: ID;
  final_output?: ArtifactDTO;
  created_at: ISODateTime;
  updated_at: ISODateTime;
};

// ============================================================
// ExecutionStep
// ============================================================

export type StepType =
  | "user_message"
  | "system_event"
  | "model_call"
  | "plan_created"
  | "tool_call"
  | "mcp_call"
  | "observation"
  | "permission_request"
  | "review"
  | "final_output";

export type StepStatus =
  | "pending"
  | "running"
  | "waiting_for_permission"
  | "completed"
  | "failed"
  | "cancelled"
  | "skipped";

export type ExecutionStepDTO = {
  id: ID;
  run_id: ID;
  parent_step_id?: ID;
  type: StepType;
  status: StepStatus;
  title: string;
  summary?: string;
  input?: Record<string, unknown>;
  output?: Record<string, unknown>;
  error?: AppError;
  started_at?: ISODateTime;
  completed_at?: ISODateTime;
  duration_ms?: number;
};

// ============================================================
// Permission
// ============================================================

export type PermissionScopeDTO = {
  type: "once" | "task" | "tool_path" | "workspace" | "global";
  workspace_path?: string;
  path?: string;
  tool_name?: string;
  mcp_server_id?: string;
  task_id?: ID;
};

export type PermissionDecisionType =
  | "allow_once"
  | "allow_for_task"
  | "always_allow_for_tool_and_path"
  | "always_allow_for_workspace"
  | "deny";

export type PermissionRequestDTO = {
  id: ID;
  task_id: ID;
  run_id: ID;
  step_id?: ID;
  tool_name: string;
  action_summary: string;
  reason?: string;
  risk_level: RiskLevel;
  scope: PermissionScopeDTO;
  arguments_summary: Record<string, unknown>;
  allowed_decisions: PermissionDecisionType[];
  created_at: ISODateTime;
  expires_at: ISODateTime;
  status?: "pending" | "approved" | "denied" | "expired" | "consumed";
  decision?: PermissionDecisionType;
};

export type PermissionDecisionDTO = {
  request_id: ID;
  decision: PermissionDecisionType;
  note?: string;
};

// ============================================================
// ToolCall
// ============================================================

export type ToolCallDTO = {
  id: ID;
  run_id: ID;
  step_id: ID;
  tool_name: string;
  provider: "native" | "mcp" | "system";
  mcp_server_id?: string;
  risk_level: RiskLevel;
  arguments: Record<string, unknown>;
  result?: ToolResultDTO;
  permission_request_id?: ID;
  permission_status: "not_required" | "pending" | "approved" | "denied" | "expired";
  status: "pending" | "running" | "completed" | "failed" | "cancelled";
  error?: AppError;
  started_at?: ISODateTime;
  completed_at?: ISODateTime;
  duration_ms?: number;
};

export type ToolResultDTO = {
  kind: "text" | "json" | "file" | "artifact" | "empty";
  summary: string;
  data?: unknown;
  artifact_ids?: ID[];
  deliverables?: ToolDeliverableDTO[];
};

export type ToolDeliverableDTO = {
  kind: "file";
  title: string;
  path: string;
  size_bytes: number;
  mime_type: string;
  content_hash: string;
};

// ============================================================
// Artifact
// ============================================================

export type ArtifactPurpose = "final_response" | "deliverable";

export type ArtifactProducerDTO =
  | { type: "runtime" }
  | { type: "tool"; tool_call_id: ID };

export type ArtifactDTO = {
  id: ID;
  task_id: ID;
  run_id: ID;
  kind: "markdown" | "text" | "json" | "file" | "diff" | "screenshot";
  title: string;
  purpose: ArtifactPurpose;
  producer: ArtifactProducerDTO;
  content?: string;
  file_size_bytes?: number;
  mime_type?: string;
  content_hash?: string;
  file_path?: string;
  metadata?: Record<string, unknown>;
  created_at: ISODateTime;
};

// ============================================================
// Settings
// ============================================================

export type SettingsDTO = {
  model: ModelSettingsDTO;
  workspace: WorkspaceSettingsDTO;
  permissions: PermissionSettingsDTO;
  mcp: McpSettingsDTO;
  runtime: RuntimeSettingsDTO;
};

export type RuntimeSettingsDTO = {
  storage_backend: string; // "postgresql"（唯一持久化真相）
  persistence_status: "ready" | "degraded" | "unavailable";
  runtime_bus: "redis" | "inmemory";
  control_plane_status: "ready" | "degraded" | "unavailable";
};

export type ModelSettingsDTO = {
  cloud_provider?: string;
  default_model?: string;
  local_endpoint?: string;
  fallback_enabled: boolean;
  api_key_configured: boolean;
};

export type WorkspaceSettingsDTO = {
  default_workspace_path?: string;
  allowed_workspace_paths: string[];
};

export type PermissionSettingsDTO = {
  default_shell_policy: "deny" | "confirm" | "allow_low_risk";
  high_risk_policy: "always_confirm" | "deny";
};

export type McpSettingsDTO = {
  servers: McpServerConfigDTO[];
};

export type McpServerConfigDTO = {
  id: ID;
  name: string;
  transport: "stdio" | "sse";
  command?: string;
  args?: string[];
  url?: string;
  enabled: boolean;
};

// ============================================================
// RuntimeEvent
// ============================================================

export type RuntimeEventType =
  | "task.created"
  | "task.updated"
  | "agent.run.started"
  | "agent.run.paused"
  | "agent.run.resumed"
  | "agent.run.completed"
  | "agent.run.failed"
  | "agent.run.cancelled"
  | "agent.step.started"
  | "agent.step.updated"
  | "agent.step.completed"
  | "agent.step.failed"
  | "model.call.started"
  | "model.context.prepared"
  | "model.delta"
  | "model.call.completed"
  | "model.call.failed"
  | "tool.call.started"
  | "tool.call.finished"
  | "tool.call.failed"
  | "mcp.call.started"
  | "mcp.call.finished"
  | "mcp.call.failed"
  | "permission.required"
  | "permission.resolved"
  | "permission.expired"
  | "artifact.created"
  | "log.appended";

export type RuntimeEvent = {
  id: ID;
  type: RuntimeEventType;
  task_id?: ID;
  run_id?: ID;
  step_id?: ID;
  sequence?: number;
  timestamp: ISODateTime;
  payload: Record<string, unknown>;
};

// -- Event Payloads --

export type TaskCreatedPayload = {
  task: TaskDTO;
  run: AgentRunDTO;
};

export type StepPayload = {
  step: ExecutionStepDTO;
};

export type ModelDeltaPayload = {
  step_id: ID;
  delta: string;
  accumulated?: string;
};

export type ModelContextPreparedPayload = {
  provider: string;
  model_name: string;
  fingerprint: string;
  /** v15+；旧持久化事件可能缺失。 */
  action_mode?: "normal" | "finish_only" | "tool_required";
  skill_id?: string;
  skill_version?: string;
  skill_fingerprint?: string;
  policy_version: string;
  estimator: string;
  estimated_input_tokens: number;
  input_budget_tokens: number;
  context_window_tokens: number;
  max_output_tokens: number;
  safety_margin_tokens: number;
  included_history_turns: number;
  dropped_history_turns: number;
  included_observations: number;
  dropped_observations: number;
  included_memories: number;
  dropped_memories: number;
  message_count: number;
  truncated: boolean;
};

export type ToolCallPayload = {
  tool_call: ToolCallDTO;
};

export type PermissionRequiredPayload = {
  request: PermissionRequestDTO;
};

export type PermissionResolvedPayload = {
  request_id: ID;
  decision: PermissionDecisionType;
  tool_call_id?: ID;
};

export type ArtifactCreatedPayload = {
  artifact: ArtifactDTO;
};

// ============================================================
// API Input / Output
// ============================================================

// -- Task API --

export type CreateTaskInput = {
  user_goal: string;
  conversation_id?: ID;
  workspace_path?: string;
  workspace_id?: ID;  // 优先于 workspace_path
  attachments?: AttachmentInput[];
  model_policy?: ModelPolicyInput;
};

export type AttachmentInput = {
  name: string;
  kind: "file" | "text" | "url";
  content?: string;
  path?: string;
  url?: string;
};

export type ModelPolicyInput = {
  provider?: string;
  model?: string;
  max_steps?: number;
};

export type CreateTaskOutput = {
  task: TaskDTO;
  run: AgentRunDTO;
  conversation: ConversationDTO;
  message: MessageDTO;
};

export type ConversationDTO = {
  id: ID;
  title?: string;
  created_at: ISODateTime;
  updated_at: ISODateTime;
};

export type MessageDTO = {
  id: ID;
  conversation_id: ID;
  task_id?: ID;
  run_id?: ID;
  role: "system" | "user" | "assistant" | "tool";
  content: string;
  created_at: ISODateTime;
};

export type ListTasksInput = {
  status?: TaskStatus[];
  query?: string;
  limit?: number;
  cursor?: string;
};

export type ListTasksOutput = {
  tasks: TaskDTO[];
  next_cursor?: string;
};

export type GetTaskInput = {
  task_id: ID;
};

export type TaskDetailOutput = {
  task: TaskDTO;
  active_run?: AgentRunDTO;
  steps: ExecutionStepDTO[];
  artifacts: ArtifactDTO[];
};

// -- Run API --

export type RunIdInput = {
  run_id: ID;
};

export type RetryStepInput = {
  run_id: ID;
  step_id: ID;
};

// -- Settings API --

export type UpdateSettingsInput = Partial<SettingsDTO>;

// -- Permission API Output (actual) --

/** 权限决议返回：请求 DTO + 后续事件（前端直接追加到 runStore） */
export type ResolvePermissionOutput = {
  request: PermissionRequestDTO;
  events: RuntimeEvent[];
};

// -- Worker Status API (3B heartbeat) --

/** Worker 模型配置状态（Phase 6B-1） */
export type ModelStatusDTO = {
  provider: string;            // "deepseek" | "custom_openai_compatible"
  protocol: string;            // 当前为 "openai_chat_completions"
  model_name: string;          // 模型名称
  api_key_configured: boolean; // API key 是否已配置
  thinking_mode: string;       // "" | "disabled"
  status: string;              // "configured" | "not_configured"
  last_error_code: string | null;
};

/** Worker 进程启动以来的 Redis Run Queue 累计指标；不是业务真源。 */
export type WorkerRuntimeBusMetricsDTO = {
  reclaimed: number;
  retry_deferred: number;
  dead_lettered: number;
  malformed: number;
  command_reclaimed: number;
  command_dead_lettered: number;
  command_malformed: number;
};

/** Worker 状态（Gateway 内存视图，来自 heartbeat stream） */
export type WorkerStatusDTO = {
  worker_id: ID;
  /** agent 执行 AgentRun；rag 执行持久化 RAG 作业 */
  worker_kind: "agent" | "rag";
  /** starting | idle | busy | draining | stopped | failed */
  status: string;
  /** Agent Worker 当前活跃 run_id；RAG Worker 不使用该字段 */
  active_run_id: string;
  /** worker 上报心跳的时间（ISO 8601） */
  reported_at: ISODateTime;
  /** Gateway 收到心跳的本地时间（ISO 8601） */
  last_seen_at: ISODateTime;
  /** worker 是否超时未发送心跳 */
  is_stale: boolean;
  /** 模型配置状态（Phase 6B-1），来自 heartbeat */
  model?: ModelStatusDTO;
  /** Redis Runtime Bus 进程级累计指标，来自 heartbeat */
  runtime_bus?: WorkerRuntimeBusMetricsDTO;
};

/** GET /api/runtime/workers 返回 */
export type WorkersOutput = {
  workers: WorkerStatusDTO[];
};

export type RuntimeHealthStatus = "healthy" | "degraded" | "unavailable";
export type RuntimeStreamHealthDTO = {
  name: "run_queue" | "worker_command" | "runtime_event";
  stream: string;
  consumer_group: string;
  available: boolean;
  lag: number;
  pending: number;
  consumers: number;
  oldest_pending_ms: number;
  error_code?: string;
};
export type RuntimeDeadLetterSummaryDTO = {
  name: "run_queue" | "worker_command" | "runtime_event";
  stream: string;
  count: number;
};
export type RuntimeHealthCountersDTO = {
  run_reclaimed: number; run_retry_deferred: number; run_dead_lettered: number; run_malformed: number;
  command_reclaimed: number; command_dead_lettered: number; command_malformed: number;
  event_reclaimed: number; event_retry_deferred: number; event_dead_lettered: number; event_malformed: number;
};
export type RuntimeHealthDTO = {
  status: RuntimeHealthStatus;
  runtime_bus: "redis" | "inmemory";
  generated_at: ISODateTime;
  workers: { total: number; online: number; busy: number; stale: number };
  streams: RuntimeStreamHealthDTO[];
  dead_letters: RuntimeDeadLetterSummaryDTO[];
  counters: RuntimeHealthCountersDTO;
  warnings: string[];
};

export type StorageReconciliationIssueDTO = {
  code: string;
  severity: "warning" | "error";
  entity_type: "task" | "run" | "step" | "artifact";
  entity_id: ID;
  summary: string;
  task_id?: ID;
  run_id?: ID;
};
export type StorageReconciliationDTO = {
  status: "healthy" | "degraded";
  generated_at: ISODateTime;
  scanned_runs: number;
  scanned_events: number;
  scanned_steps: number;
  scanned_artifacts: number;
  issue_count: number;
  truncated: boolean;
  issues: StorageReconciliationIssueDTO[];
};
export type TerminalEventRepairInspectionDTO = {
  eligible: boolean;
  reason_code: string;
  reason: string;
  task_id?: ID;
  run_id: ID;
  expected_event_type?: string;
  risk_level: "L3";
  requires_confirmation: true;
  allowed_decisions: Array<"allow_once" | "deny">;
};
export type TerminalEventRepairRequestOutput = {
  request: PermissionRequestDTO;
};
export type TerminalEventRepairResolutionOutput = {
  request: PermissionRequestDTO;
  repaired_event_id?: ID;
  repaired_event_type?: string;
};

export type RuntimeDeadLetterSource = "run_queue" | "worker_command" | "runtime_event";
export type RuntimeDeadLetterDTO = {
  id: string;
  source: RuntimeDeadLetterSource;
  original_stream: string;
  original_message_id: string;
  consumer_group: string;
  delivery_count: number;
  reclaimed: boolean;
  error_code: string;
  error_message: string;
  failed_at: ISODateTime;
  payload_sha256: string;
  payload_size_bytes: number;
  task_id?: ID;
  run_id?: ID;
};
export type ListRuntimeDeadLettersInput = {
  source: RuntimeDeadLetterSource;
  limit?: number;
  before?: string;
  error_code?: string;
  task_id?: ID;
  run_id?: ID;
};
export type ListRuntimeDeadLettersOutput = {
  records: RuntimeDeadLetterDTO[];
  next_cursor?: string;
};

export type DlqRetryRecordInput = {
  source: RuntimeDeadLetterSource;
  record_id: string;
};

export type DlqRetryInspectionDTO = {
  eligible: boolean;
  reason_code: string;
  reason: string;
  task_id: ID;
  run_id: ID;
  risk_level: "L3";
  requires_confirmation: true;
  allowed_decisions: Array<"allow_once" | "deny">;
};

export type DlqRetryRequestOutput = {
  request: PermissionRequestDTO;
};

export type DlqRetryResolutionOutput = {
  request: PermissionRequestDTO;
  previous_run_id: ID;
  new_run?: AgentRunDTO | null;
};

// -- Model Config API（Phase 6）--

/** 模型配置安全投影（绝不包含 API key 原值或环境变量名） */
export type ModelConfigDTO = {
  provider: string;                     // "deepseek" | "custom_openai_compatible" | ""
  protocol: string;                     // 底层协议，不作为供应商身份
  model_name: string;                   // 模型名称
  base_url_display: string;             // 脱敏后的 base URL（无 userinfo/query/fragment）
  api_key_configured: boolean;          // 仅状态，不返回 key 值或环境变量名
  timeout_seconds: number;
  max_retries: number;
  max_tokens: number;
  thinking_mode: string;                // "" | "disabled"
  worker_status: string;                // 来自 heartbeat 的 worker 状态，无 worker 时为 "unknown"
  last_heartbeat_at: string | null;     // 最近心跳 ISO 时间
  last_error_code: string | null;       // 最后安全错误码
};

/** 模型连通性测试结果（不含 prompt/key/原始响应） */
export type ModelTestOutput = {
  provider: string;
  model: string;
  latency_ms: number;
  tested_at: string;                    // ISO 时间
  status: "ok" | "failed";
  error: AppError | null;               // 失败时安全 AppError
};

// -- Conversation API（多轮对话 MVP）--

export type ListConversationsOutput = {
  conversations: ConversationDTO[];
};

export type ConversationDetailOutput = {
  conversation: ConversationDTO;
  messages: MessageDTO[];
  next_cursor?: string;  // 分页 cursor（base64(json([created_at, message_id]))）
};

// -- Workspace --

export type WorkspaceStatus = "active" | "revoked";

export type WorkspaceSource = "configured" | "user_picker";

export type WorkspaceDTO = {
  id: ID;
  name: string;
  root_path: string;
  canonical_path: string;
  status: WorkspaceStatus;
  source: WorkspaceSource;
  created_at: ISODateTime;
  updated_at: ISODateTime;
  revoked_at?: ISODateTime;
};

export type ListWorkspacesOutput = {
  workspaces: WorkspaceDTO[];
};

export type PickWorkspaceOutput = {
  workspace: WorkspaceDTO | null;
  cancelled: boolean;
};

export type RevokeWorkspaceOutput = {
  workspace: WorkspaceDTO;
};

// -- Obsidian Personal Knowledge Base --

export type KnowledgeVaultDTO = {
  id: ID;
  name: string;
  root_path: string;
  canonical_path: string;
  status: "active" | "revoked";
  source: "jarvis_managed";
  created_at: ISODateTime;
  updated_at: ISODateTime;
};

export type KnowledgeDocumentKind = "report" | "note" | "source";
export type KnowledgeDocumentDTO = {
  id: ID;
  vault_id: ID;
  title: string;
  kind: KnowledgeDocumentKind;
  relative_path: string;
  content_hash: string;
  size_bytes: number;
  tags: string[];
  source_urls: string[];
  source_task_id?: ID;
  source_run_id?: ID;
  created_at: ISODateTime;
  updated_at: ISODateTime;
};
export type ListKnowledgeVaultsOutput = { vaults: KnowledgeVaultDTO[]; suggested_path: string };
export type ConnectKnowledgeVaultInput = { path: string };
export type KnowledgeVaultMutationOutput = { vault: KnowledgeVaultDTO };
export type ListKnowledgeDocumentsOutput = { documents: KnowledgeDocumentDTO[] };
export type CreateKnowledgeDocumentInput = { title: string; kind: KnowledgeDocumentKind; content: string; tags: string[]; source_urls?: string[] };
export type KnowledgeDocumentMutationOutput = { document: KnowledgeDocumentDTO };

// -- Workspace-scoped RAG Document Library --

export type RagDocumentStatus = "indexing" | "ready" | "failed" | "disabled";
export type RagIndexState = "current" | "stale" | "building" | "unavailable";
export type RagIngestionStatus = "queued" | "parsing" | "chunking" | "embedding" | "completed" | "failed" | "cancelled";
export type RagJobProgressDTO = {
  active_executor?: string;
  page_count: number;
  native_extraction_done: boolean;
  visual_pages_total: number;
  visual_pages_completed: number;
  visual_route_counts: Record<string, number>;
  chunks_total: number;
  embedding_total: number;
  embedding_completed: number;
};
export type RagIngestionJobDTO = {
  id: ID;
  status: RagIngestionStatus;
  attempts: number;
  max_attempts: number;
  embedding_attempts: number;
  embedding_max_attempts: number;
  progress: RagJobProgressDTO;
  error_code?: string;
  next_retry_at?: ISODateTime;
  started_at?: ISODateTime;
  completed_at?: ISODateTime;
  failed_at?: ISODateTime;
  created_at: ISODateTime;
  updated_at: ISODateTime;
};
export type RagDocumentDTO = {
  id: ID;
  workspace_id: ID;
  source_artifact_id: ID;
  title: string;
  mime_type: string;
  status: RagDocumentStatus;
  ingestion_policy_version: string;
  parser_version: string;
  chunker_version: string;
  embedding_provider: string;
  embedding_model: string;
  embedding_dimensions?: number;
  chunk_count: number;
  indexed_at?: ISODateTime;
  version: number;
  created_at: ISODateTime;
  updated_at: ISODateTime;
  latest_job?: RagIngestionJobDTO;
  index_state: RagIndexState;
  index_stale_reasons: string[];
  index_target: Record<string, string | number>;
};
export type ListRagDocumentsOutput = { documents: RagDocumentDTO[] };
export type RagFeedbackKind = "helpful" | "unhelpful" | "citation_incorrect" | "evidence_insufficient";
export type RagFeedbackStatus = "pending" | "reviewed" | "dismissed";
export type RagFeedbackFailureCategory = "candidate_miss" | "reranker_miss" | "context_omission" | "context_truncated" | "citation_mismatch" | "answer_generation" | "insufficient_evidence" | "other";
export type RagFeedbackDTO = {
  id: ID;
  trace_id: ID;
  workspace_id: ID;
  task_id: ID;
  run_id: ID;
  message_id: ID;
  kind: RagFeedbackKind;
  citation_chunk_id?: ID;
  status: RagFeedbackStatus;
  failure_category?: RagFeedbackFailureCategory;
  query_hash?: string;
  pipeline_versions?: Record<string, string>;
  result_count?: number;
  context_truncated?: boolean;
  created_at: ISODateTime;
  updated_at: ISODateTime;
};
export type SubmitRagFeedbackInput = {
  message_id: ID;
  kind: RagFeedbackKind;
  citation_chunk_id?: ID;
};
export type RagFeedbackMutationOutput = { feedback: RagFeedbackDTO };
export type RagFeedbackEvidenceDTO = { chunk_id: ID; document_id: ID; content_hash: string; candidate_rank: number | null; reranked_rank: number | null; in_context: boolean; sources: string[]; snippet: string | null };
export type RagFeedbackDetailOutput = { feedback: RagFeedbackDTO; query_hash: string; query: string | null; privacy_status: "pending" | "approved" | "rejected"; pipeline_versions: Record<string, string>; result_count: number; context_truncated: boolean; evidence: RagFeedbackEvidenceDTO[]; label: null | { id: ID; source: string; status: "draft" | "confirmed" | "rejected" | "promoted"; positive_chunk_ids: ID[]; hard_negative_chunk_ids: ID[] } };
export type TriageRagFeedbackInput = { failure_category: RagFeedbackFailureCategory; positive_chunk_ids: ID[]; hard_negative_chunk_ids: ID[] };
export type TriageRagFeedbackOutput = { feedback: RagFeedbackDTO; label_status?: string };
export type ListRagFeedbackOutput = { feedback: RagFeedbackDTO[] };
export type RagEvaluationPrivacyStatus = "pending" | "approved" | "rejected";
export type RagEvaluationLabelStatus = "draft" | "confirmed" | "rejected" | "promoted";
export type RagEvaluationTraceDTO = {
  trace_id: ID;
  workspace_id: ID;
  task_id: ID;
  run_id: ID;
  query_hash: string;
  privacy_status: RagEvaluationPrivacyStatus;
  label_status?: RagEvaluationLabelStatus;
  label_source?: string;
  candidate_count: number;
  reranked_count: number;
  context_chunk_count: number;
  context_truncated: boolean;
  pipeline_versions: Record<string, string>;
  created_at: ISODateTime;
};
export type ListRagEvaluationTracesOutput = { traces: RagEvaluationTraceDTO[] };
export type RagQualityGateStatus = "passed" | "blocked" | "insufficient_evidence";
export type RagQualityGateCheckDTO = {
  check_id: string;
  passed: boolean;
  actual?: number | string | null;
  required?: number | null;
  required_minimum?: number | null;
  absolute_minimum?: number | null;
  baseline?: number | null;
  maximum_regression?: number | null;
  failure_count?: number;
  maximum?: number;
};
export type RagQualityGateRunDTO = {
  id: ID;
  gate_id: string;
  cohort_id: string;
  baseline_id: string;
  revision: string;
  status: RagQualityGateStatus;
  sample_count: number;
  metrics: Record<string, number>;
  checks: RagQualityGateCheckDTO[];
  generated_at: ISODateTime;
};
export type RagQualityMetricTrendDTO = {
  metric_id: string;
  current: number;
  previous: number;
  delta: number;
  direction: "improved" | "stable" | "regressed";
};
export type RagQualityAlertDTO = {
  code: "status_regressed" | "check_failed" | "metric_regressed";
  severity: "warning" | "critical";
  subject_id: string;
  current: number | null;
  previous: number | null;
  delta: number | null;
};
export type RagQualityFailureClusterDTO = {
  failure_type: string;
  priority: "low" | "medium" | "high" | "critical";
  latest_rate: number;
  latest_count: number;
  previous_rate: number | null;
  rate_delta: number | null;
  occurrence_count: number;
  threshold: number;
  check_passed: boolean;
};
export type RagQualityGateInsightsDTO = {
  comparison_state: "ready" | "insufficient_history";
  compatible_history_count: number;
  previous_run_id: ID | null;
  metric_trends: RagQualityMetricTrendDTO[];
  alerts: RagQualityAlertDTO[];
  failure_clusters: RagQualityFailureClusterDTO[];
};
export type ListRagQualityGateRunsOutput = {
  runs: RagQualityGateRunDTO[];
  insights: RagQualityGateInsightsDTO;
};
export type RagQualityFailureTargetDTO = {
  candidate_id: string;
  trace_id: ID;
  workspace_id: ID;
  query_hash: string;
  failure_type: string;
  suspected_stage: string;
  severity: "low" | "medium" | "high" | "critical";
  metric_ids: string[];
  privacy_status: RagEvaluationPrivacyStatus;
  label_status: RagEvaluationLabelStatus | null;
  label_source: string | null;
  review_state: "privacy_required" | "privacy_rejected" | "label_review_required" | "promotion_ready" | "fixed_regression_sample";
  issue: RagQualityIssueDTO | null;
};
export type ListRagQualityFailureTargetsOutput = { targets: RagQualityFailureTargetDTO[] };
export type RagQualityIssueDTO = {
  id: ID; candidate_id: string; trace_id: ID; gate_id: string; cohort_id: string; failure_type: string;
  owner: "data_quality" | "candidate_recall" | "reranker" | "context_assembly";
  status: "open" | "in_progress" | "resolved" | "verified" | "dismissed";
  occurrence_count: number; first_seen_run_id: ID; last_seen_run_id: ID; verified_run_id: ID | null;
  resolution_note: string; version: number; created_at: ISODateTime; updated_at: ISODateTime;
};
export type UpdateRagQualityIssueInput = { expected_version: number; owner: RagQualityIssueDTO["owner"]; status: Exclude<RagQualityIssueDTO["status"], "verified">; resolution_note: string };
export type UpdateRagQualityIssueOutput = { issue: RagQualityIssueDTO };
export type RagEvaluationReviewTargetDTO = { trace_id: ID; workspace_id: ID };
export type RagQualityIssueLedgerItemDTO = RagEvaluationReviewTargetDTO & {
  issue: RagQualityIssueDTO; query_hash: string; privacy_status: RagEvaluationPrivacyStatus;
  label_status: RagEvaluationLabelStatus | null;
  review_state: RagQualityFailureTargetDTO["review_state"];
  first_seen_revision: string; last_seen_revision: string; verified_revision: string | null;
};
export type RagQualityIssueSummaryDTO = {
  total: number; open: number; in_progress: number; resolved: number; verified: number; dismissed: number;
};
export type ListRagQualityIssuesOutput = { issues: RagQualityIssueLedgerItemDTO[]; summary: RagQualityIssueSummaryDTO };
export type RagEvaluationLabelDTO = {
  id: ID;
  source: string;
  status: RagEvaluationLabelStatus;
  positive_chunk_ids: ID[];
  hard_negative_chunk_ids: ID[];
  notes: string;
};
export type RagPromotionCandidateDTO = {
  schema_version: number;
  trace_id: ID;
  query_hash: string;
  raw_query_included: false;
  raw_chunk_content_included: false;
};
export type RagEvaluationTraceDetailOutput = {
  trace: RagEvaluationTraceDTO;
  query: string | null;
  request: Record<string, unknown> | null;
  evidence: RagFeedbackEvidenceDTO[];
  label: RagEvaluationLabelDTO | null;
  promotion_candidate: RagPromotionCandidateDTO | null;
};
export type ReviewRagTraceLabelInput = {
  workspace_id: ID;
  status: Exclude<RagEvaluationLabelStatus, "promoted">;
  positive_chunk_ids: ID[];
  hard_negative_chunk_ids: ID[];
  notes: string;
};
export type UploadRagDocumentOutput = {
  artifact_id: ID;
  document_id: ID;
  job_id: ID;
  status: RagIngestionStatus;
  uploaded: boolean;
  created: boolean;
};
export type CreateRagUploadRequestInput = {
  workspace_id: ID;
  filename: string;
  size_bytes: number;
  content_sha256: string;
};
export type ResolveRagUploadRequestInput = {
  decision: "allow_once" | "deny";
  note?: string;
};
export type RestartRagDocumentOutput = {
  document_id: ID;
  job_id: ID;
  status: RagIngestionStatus;
};
export type UpdateRagDocumentOutput = {
  document_id: ID;
  status: RagDocumentStatus;
  version: number;
};
export type CancelRagDocumentOutput = {
  document_id: ID;
  status: RagDocumentStatus;
  version: number;
  job_id: ID;
  job_status: RagIngestionStatus;
};
export type RagDeleteResolutionOutput = {
  permission: PermissionRequestDTO;
  document_id: ID;
  deleted: boolean;
  cleanup_pending_count: number;
  source_artifact_retained: boolean;
};

export type ScheduledTaskDTO = {
  id: ID; name: string; user_goal: string; recurrence: "daily" | "weekly";
  timezone: string; hour: number; minute: number; weekday?: number; workspace_id?: ID;
  status: "active" | "paused"; authorized_tools: string[]; next_run_at: ISODateTime;
  task_kind: "knowledge_report" | "source_report";
  source_policy: { provider?: "arxiv"; query?: string; max_results?: number };
  last_run_at?: ISODateTime; last_task_id?: ID; last_run_id?: ID; version: number;
  created_at: ISODateTime; updated_at: ISODateTime;
};
export type ScheduledExecutionDTO = { id: ID; scheduled_task_id: ID; scheduled_for: ISODateTime; status: "pending" | "dispatching" | "dispatched" | "failed"; task_id?: ID; run_id?: ID; attempts: number; error_code?: string; created_at: ISODateTime; updated_at: ISODateTime };
export type ListScheduledTasksOutput = { scheduled_tasks: ScheduledTaskDTO[] };
export type CreateScheduledTaskInput = { name: string; user_goal: string; recurrence: "daily" | "weekly"; timezone: string; hour: number; minute: number; weekday?: number; workspace_id?: ID; task_kind: "knowledge_report" | "source_report"; source_query?: string; source_max_results?: number };
export type UpdateScheduledTaskInput = { expected_version: number; status: "active" | "paused" };
export type ScheduledTaskMutationOutput = { scheduled_task: ScheduledTaskDTO };
export type ScheduledExecutionOutput = { execution: ScheduledExecutionDTO };

// -- MCP servers and discovered tools --

export type McpToolDTO = {
  id: ID; original_name: string; internal_name: string; description: string;
  input_schema: Record<string, unknown>; risk_level: RiskLevel; enabled: boolean;
};
export type McpServerDTO = {
  id: ID; slug: string; name: string; transport: "stdio"; command: string;
  args: string[]; env_keys: string[]; enabled: boolean;
  status: "disconnected" | "connected" | "error"; last_error_code?: string;
  last_connected_at?: ISODateTime; version: number; created_at: ISODateTime;
  updated_at: ISODateTime; tools: McpToolDTO[];
};
export type ListMcpServersOutput = { servers: McpServerDTO[]; worker_restart_required?: boolean };
export type CreateMcpServerInput = { slug: string; name: string; command: string; args: string[]; env_keys: string[] };
export type UpdateMcpServerInput = { enabled: boolean; expected_version: number };
export type McpServerMutationOutput = { server: McpServerDTO; worker_restart_required: boolean };
export type McpDiscoveryRefreshOutput = {
  command_id: ID; status: "accepted"; worker_restart_required: boolean;
};

// -- Long-term Memory --

export type MemoryScopeType = "global" | "workspace";
export type MemoryCategory = "preference" | "user_fact" | "project_fact" | "rule";
export type MemoryStatus = "active" | "disabled";
export type MemoryCandidateStatus = "pending" | "approved" | "rejected" | "expired";

export type MemoryDTO = {
  id: ID;
  scope_type: MemoryScopeType;
  workspace_id?: ID;
  category: MemoryCategory;
  key: string;
  content: string;
  status: MemoryStatus;
  source_type: "user_explicit" | "candidate_approved";
  importance: number;
  version: number;
  created_at: ISODateTime;
  updated_at: ISODateTime;
};

export type CreateMemoryInput = {
  scope_type: MemoryScopeType;
  workspace_id?: ID;
  category: MemoryCategory;
  key: string;
  content: string;
  importance: number;
};

export type UpdateMemoryInput = {
  expected_version: number;
  content?: string;
  status?: MemoryStatus;
  importance?: number;
};

export type ListMemoriesOutput = { memories: MemoryDTO[] };
export type MemoryMutationOutput = { memory: MemoryDTO };

export type MemoryCandidateDTO = {
  id: ID;
  scope_type: MemoryScopeType;
  workspace_id?: ID;
  category: MemoryCategory;
  suggested_key: string;
  content: string;
  status: MemoryCandidateStatus;
  source_task_id: ID;
  source_run_id: ID;
  confidence: number;
  importance: number;
  sensitivity: "normal" | "sensitive";
  conflict_memory_id?: ID;
  approved_memory_id?: ID;
  extraction_policy_version: string;
  expires_at?: ISODateTime;
  resolved_at?: ISODateTime;
  resolution_note?: string;
  version: number;
  created_at: ISODateTime;
  updated_at: ISODateTime;
};

export type UpdateMemoryCandidateInput = {
  expected_version: number;
  scope_type?: MemoryScopeType;
  workspace_id?: ID;
  category?: MemoryCategory;
  suggested_key?: string;
  content?: string;
  importance?: number;
};

export type ResolveMemoryCandidateInput = {
  expected_version: number;
  note?: string;
};

export type ListMemoryCandidatesOutput = { candidates: MemoryCandidateDTO[] };
export type MemoryCandidateMutationOutput = { candidate: MemoryCandidateDTO };
export type ApproveMemoryCandidateOutput = {
  candidate: MemoryCandidateDTO;
  memory: MemoryDTO;
};

// -- Audit Log（安全投影 + L4 保留执行）--

/**
 * 浏览器可见的审计记录。details_summary 已在 Python Application 层脱敏、限长；
 * 不包含原始 details、error、异常堆栈或任何凭据。
 */
export type AuditLogDTO = {
  id: ID;
  event_type: string;
  actor: string;
  action_summary: string;
  task_id?: ID;
  run_id?: ID;
  step_id?: ID;
  tool_call_id?: ID;
  risk_level?: RiskLevel;
  permission_decision?: string;
  result_summary?: string;
  error_code?: string;
  details_summary: Record<string, unknown>;
  created_at: ISODateTime;
};

export type ListAuditLogsInput = {
  limit?: number;
  event_type?: string;
  actor?: string;
  task_id?: ID;
  run_id?: ID;
  before?: string;
};

export type ListAuditLogsOutput = {
  audit_logs: AuditLogDTO[];
  next_cursor?: string;
};

export type AuditRetentionPreviewDTO = {
  dry_run: true;
  standard_days: number;
  extended_days: number;
  standard_before: ISODateTime;
  extended_before: ISODateTime;
  max_scan: number;
  max_candidates: number;
  scanned_records: number;
  candidate_records: number;
  protected_records: number;
  extended_retained_records: number;
  has_more: boolean;
};

export type AuditRetentionPreviewInput = {
  standard_days?: number;
  extended_days?: number;
  max_scan?: number;
  max_candidates?: number;
};

export type CreateAuditRetentionRequestInput = {
  standard_days: number;
  extended_days: number;
  max_scan: number;
  max_candidates: number;
};

export type CreateAuditRetentionRequestOutput = {
  request: PermissionRequestDTO;
};

export type ResolveAuditRetentionRequestInput = {
  decision: "allow_once" | "deny";
  note?: string;
};

export type AuditRetentionResolutionDTO = {
  permission: PermissionRequestDTO;
  deleted_records: number;
  has_more: boolean;
};
