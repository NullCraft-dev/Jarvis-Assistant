# 接口契约文档

## 文档目的

本文档定义 Vue Web UI、Go Gateway / Runtime Orchestrator、Redis Runtime Bus、Python Agent Worker Pool 和真实后端之间的接口契约。后续桌面端可以通过 Electron preload / IPC adapter 复用同一套 DTO、RuntimeEvent 和错误结构。

产品任务统一走真实 Task / Run 契约。in-memory bus、fake transport 和模型测试替身只能由自动化测试直接注入，不得通过产品 UI 或 Gateway API 选择。

本文档不负责数据库 schema、MCP 协议细节或视觉设计。这些分别由 `14-data-schema.md`、`15-mcp-tool-gateway-design.md` 和 `11-frontend-app-ui-design.md` 负责。

## 基本原则

- Web UI 不直接访问 Go 内部 adapter、Python worker、本地文件、Shell、数据库或 MCP server。
- Web UI 只通过前端 API client / event stream client 调用能力。
- Web API / transport request / response 必须结构化。
- Go Gateway / Runtime Orchestrator 是前端契约守门人和运行时调度层，负责 DTO 校验、错误归一、AgentRun 入队、worker command 路由和事件扇出。
- Redis Runtime Bus 是 Go 与 Python worker 之间的运行时通信层，不是业务真源。
- Python Agent Worker 是 LangChain / LangGraph、ToolGateway、PermissionManager、Storage / AuditLog 的执行侧 owner。
- 后续桌面端如果引入 Electron，Renderer 只能通过 preload 暴露的安全 API 调用能力。
- Runtime 运行过程通过事件流同步给 UI。
- UI 不依赖内部类实例，只依赖 DTO 和 event payload。
- 测试替身必须遵守与真实 Runtime 相同的结构化契约。
- 所有错误都使用统一 error shape，不能直接把原始异常透传给 UI。

## 分层边界

```text
Vue Web UI
-> Typed API Client / Event Stream Client
-> Go Gateway / Runtime Orchestrator
-> Redis Runtime Bus
-> Python Agent Worker / Runtime Harness
-> Storage / Model / ToolGateway / PermissionManager
```

Web UI 可通过 API client 调用：

```text
api.agent.createTask()
api.agent.cancelRun()
api.agent.pauseRun()
api.agent.resumeRun()
api.agent.retryStep()
api.agent.subscribeRunEvents()
api.permission.resolveRequest()
api.settings.getSettings()
api.settings.updateSettings()
```

Web UI / 后续桌面 Renderer 不可调用：

```text
Python worker internal API directly
Redis runtime stream directly
database
model provider SDK
MCP server
native shell / file system
```

## 通用类型

### 请求与运行关联 ID

- `X-Request-ID` 标识一次 HTTP 请求。Gateway 校验或生成后写入 request context、响应 header，并透传到 Control Plane。
- `X-Trace-ID` 标识端到端操作。创建 Task 时，该值成为 AgentRun 的 `trace_id`，随后由 PostgreSQL Outbox、`RunJobMessage`、worker command 和 `RuntimeEventEnvelope` 原样传递。
- 未提供 `X-Trace-ID` 时，Gateway 初始令其等于本次 `X-Request-ID`；Task 创建后不得另行生成第二个 run trace。
- Control Plane 与 Worker 必须显式绑定日志上下文；不能依赖解析日志消息恢复 ID。
- `request_id` 不替代权限请求实体的 `PermissionRequest.id`；两者只是在不同边界使用相同字段名，日志中 HTTP ID 固定显示为 `request=...`。

### ID

所有主对象 ID 使用字符串。

```ts
type ID = string;
type ISODateTime = string;
```

### Result

```ts
type ApiResult<T> =
  | {
      ok: true;
      data: T;
    }
  | {
      ok: false;
      error: AppError;
    };
```

### Error

```ts
type AppError = {
  code: string;
  message: string;
  category:
    | "validation"
    | "permission"
    | "not_found"
    | "runtime"
    | "model"
    | "tool"
    | "mcp"
    | "storage"
    | "internal";
  recoverable: boolean;
  details?: Record<string, unknown>;
  cause_id?: string;
};
```

错误处理原则：

- `message` 面向用户或 UI 展示，不放敏感堆栈。
- `details` 可以给开发面板使用，但不能包含 API key、token、密码。
- 原始异常写入 Python worker、Go Gateway 或 worker error log，不直接暴露给 Web UI / Renderer。

## 核心 DTO

### Task

```ts
type TaskStatus =
  | "pending"
  | "running"
  | "waiting_for_user"
  | "blocked"
  | "failed"
  | "completed"
  | "cancelled";

type TaskDTO = {
  id: ID;
  title: string;
  user_goal: string;
  status: TaskStatus;
  workspace_path?: string;
  conversation_id?: ID;
  resume_from_checkpoint?: boolean; // 仅 Python reconciliation 生成，普通 Go 入队省略
  workspace_id?: ID;
  active_run_id?: ID;
  last_step_summary?: string;
  risk_level?: RiskLevel;
  cost_summary?: CostSummary;
  created_at: ISODateTime;
  updated_at: ISODateTime;
};
```

### AgentRun

```ts
type AgentRunStatus =
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

type AgentRunDTO = {
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
```

### ExecutionStep

```ts
type StepType =
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

type StepStatus =
  | "pending"
  | "running"
  | "waiting_for_permission"
  | "completed"
  | "failed"
  | "cancelled"
  | "skipped";

type ExecutionStepDTO = {
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
```

`order_index` 与 `AgentRun.step_count` 当前属于 Storage 内部投影，不新增公共 DTO 字段。首次创建
ExecutionStep 时，Runtime 在同一 Run 行锁/事务内分配从 0 连续的 `order_index`，并将
`step_count` 增加一次；同一 Step 的生命周期事件或权限恢复不得重复增加。Model/Tool 的持久化
`step_id` 来自同一个 Run 全局 `step_seq`，客户端不能生成或推断。

内部 Run/Permission checkpoint v5 绑定该 Step identity、`extract_intent` 恢复节点、冻结匿名 RAG 文档目录，
以及 Runtime-owned `CompletionContract / LoopProgressSnapshot / StopDecision / RunControlState`，不属于公共 DTO。
`CompletionContract` 当前写入 `completion-contract-v2`，在 Workspace 条件式创建场景保存
`workspace_effect_precondition=target_absent` 与唯一规范化相对 `workspace_effect_target`。该字段只描述
完成替代条件，不是 ToolRequest、权限或 effect 成功状态；只有同一 Run 的可信精确路径 ToolResult 可以
使它短路。已有 v5 checkpoint 内的 `completion-contract-v1` 可只读升级，未知 contract 子版本必须拒绝。
v4 只读兼容：恢复时从已校验 Intent 和可信 observation 补建 Loop 状态，下一次写入只产生 v5；旧 v1-v3
checkpoint 不能跨版本恢复。Worker 必须在
模型调用或工具副作用前将其安全失败并清除恢复资格，不能猜测或重算旧 ToolRequest 的 `step_id`、
文档范围或文档身份。Permission checkpoint 恢复还必须在 effect 前与 PostgreSQL PermissionRequest
对账 request/task/run/step/tool-call/tool-name；同版本结构或身份错误也必须 fail closed。

### Permission

```ts
type RiskLevel = "L0" | "L1" | "L2" | "L3" | "L4" | "L5";

type PermissionRequestDTO = {
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
  expires_at: ISODateTime; // 后端冻结；非空；前端不得自行延长
};

type PermissionScopeDTO = {
  type: "once" | "task" | "tool_path" | "workspace" | "global";
  workspace_path?: string;
  path?: string;
  tool_name?: string;
  mcp_server_id?: string;
};

type PermissionDecisionType =
  | "allow_once"
  | "allow_for_task"
  | "always_allow_for_tool_and_path"
  | "always_allow_for_workspace"
  | "deny";

type PermissionDecisionDTO = {
  request_id: ID;
  decision: PermissionDecisionType;
  note?: string;
};
```

### Tool Call

```ts
type ToolCallDTO = {
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

type ToolResultDTO = {
  kind: "text" | "json" | "file" | "artifact" | "empty";
  summary: string;
  data?: unknown;
  artifact_ids?: ID[];
  deliverables?: ToolDeliverableDTO[];
};

type ToolDeliverableDTO = {
  kind: "file";
  title: string;
  path: string;
  size_bytes: number;
  mime_type: string;
  content_hash: string;
};
```

`ToolResultDTO` 是 `ObservationPhase` 拥有的公开持久化投影，不等于 executor 的原始返回对象。`summary`、
`data` 和 `deliverables` 中的字符串在进入 RuntimeEvent、Observation、checkpoint、Outbox 或 Web DTO 前
必须递归执行高置信度凭据脱敏；原始 executor ToolResult 只允许在当前进程执行栈内短暂存活。脱敏不能
改变 path、计数、状态、Artifact ID 或错误码等非敏感契约字段。

Workspace 可恢复路径失败的数据约束：`workspace.read_file(FILE_NOT_FOUND)` 与
`workspace.list_files(PATH_NOT_FOUND)` 的 `result.data` 可包含
`{requested_path, suggested_paths}`；`suggested_paths` 最多 5 个、均为当前 Workspace 内已存在的同类型
相对路径，只用于下一轮导航，不代表工具已读取候选或 Runtime 已改变原请求目标。`workspace.read_files`
失败条目可保留同样有界的候选；对模型的投影不得携带 `workspace_root`、候选正文或内部扫描元数据。

权限拒绝不扩展 `ToolCallDTO.status`：统一表达为 `status="failed"`、
`permission_status="denied"` 和 `error.code="PERMISSION_DENIED"`。用户已批准后发生的
executor 错误必须保持 `permission_status="approved"`，并透传结构化工具错误码；不得标记为
`denied`。请求在未批准状态下随 Run 终态失效时使用 `permission_status="expired"`，不得继续显示
pending，也不得用于执行授权。`workspace.create_file.arguments.content` 在 RuntimeEvent / Web DTO 中必须替换为
`{ redacted: true, size_bytes, sha256 }`，完整正文只能存在于内部恢复检查点和执行边界。
`workspace.create_file` 成功时 `kind=file`，并返回一个经过执行器计算的 deliverable；
Runtime 按 `tool_call_id + path` 分配确定性 Artifact id，在持久化
`tool.call.finished` 的同一 PostgreSQL 事务内创建 `deliverable/tool` Artifact、
`artifact.created` 和 Outbox，再把 id 写入 `artifact_ids`。失败、拒绝、字段不一致或
非规范 workspace 相对路径均不得创建 Artifact。

`permission.expired` 或授权等待超时后的 `tool.call.failed` 必须携带
`permission_status="expired"`；它与用户主动拒绝产生的 `denied + PERMISSION_DENIED` 不得混用。

### Artifact

```ts
type ArtifactPurpose = "final_response" | "deliverable";

type ArtifactProducerDTO =
  | { type: "runtime" }
  | { type: "tool"; tool_call_id: ID };

type ArtifactDTO = {
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
```

当前最终文本产物事件顺序为
`model.call.completed -> artifact.created -> agent.run.completed`。`artifact.id` 确定性生成，
`task_id/run_id` 必须与 envelope 一致；`purpose=final_response` 时 Storage 同步设置
`AgentRun.final_output_artifact_id`。最终回复由 Runtime 生成，因此必须声明
`producer={type:"runtime"}`；真正交付物使用 `purpose=deliverable`，后续工具闭环必须声明
`producer={type:"tool",tool_call_id}`，LLM 不得直接生成 Artifact id、路径或持久化状态。
UTF-8 正文大于服务端阈值时，`artifact.created` 删除
`content`，只携带受控相对 reference、size、mime type、SHA-256 与 storage metadata；
`agent.run.completed` 同样删除重复 output，只携带 `output_externalized=true` 与
`final_output_artifact_id`。

受控文件引用的新格式为
`scoped/<workspace bucket>/<run UUID>/<artifact prefix>/<artifact UUID>.<suffix>`；读取端同时兼容
已持久化的旧版 `<prefix>/<artifact UUID>.<suffix>`，但任何新写入都必须携带可信 Run owner，并优先
使用 Task 的 Workspace UUID。单对象、单 Run、单 Workspace、根目录总量或容量扫描预算超限时使用
`ARTIFACT_OBJECT_CAPACITY_EXCEEDED`、`ARTIFACT_RUN_CAPACITY_EXCEEDED`、
`ARTIFACT_WORKSPACE_CAPACITY_EXCEEDED`、`ARTIFACT_TOTAL_CAPACITY_EXCEEDED` 或
`STORAGE_CAPACITY_SCAN_LIMIT_EXCEEDED`。错误响应不得包含绝对路径或当前用量明细。

Artifact v2 将业务语义从自由 `metadata` 提升为必填字段。不可变的历史 v1
`artifact.created` 事件仍允许在消费边界兼容读取：`metadata.is_final_output=true`
映射为 `purpose=final_response`，缺失 producer 映射为 Runtime；新事件和 Artifact API
不得继续依赖该旧 metadata 标记。

`GET /api/artifacts/{artifact_id}` 返回 `ApiResult<ArtifactDTO>`，允许读取 PostgreSQL 已登记的
markdown/text/json/diff，以及满足以下全部条件的 workspace file deliverable：

- `kind=file`、`purpose=deliverable`、`producer.type=tool`、`metadata.storage=workspace`；
- 来源 ToolCall 与 Artifact 的 Task/Run 一致，状态为 completed，工具为
  `workspace.create_file`；
- ToolCall result 中的唯一 `artifact_ids/data/deliverables` 与 Artifact 的 path、size、
  MIME 和 SHA-256 完全一致；
- 相对路径规范且不越界，读取过程中不跟随任意父级或叶节点 symlink，目标仍是普通文件，
  实际大小、UTF-8 和 SHA-256 与持久化元数据一致。

Go Gateway 只代理 Python Artifact Application Service；响应返回校验后的
`content/size/mime/hash`，必须省略 `file_path` 和绝对 workspace path。无效 UUID、记录不存在、
来源断裂、路径越界、超限、非 UTF-8 或 SHA-256 不一致均返回结构化 AppError，不得暴露原始异常。

### Cost

```ts
type CostSummary = {
  input_tokens?: number;
  output_tokens?: number;
  total_tokens?: number;
  estimated_cost_usd?: number;
};
```

## API / Transport Contract

### Task API

```ts
createTask(input: CreateTaskInput): Promise<ApiResult<CreateTaskOutput>>;
listTasks(input?: ListTasksInput): Promise<ApiResult<ListTasksOutput>>;
getTask(input: GetTaskInput): Promise<ApiResult<TaskDetailOutput>>;
```

```ts
type CreateTaskInput = {
  user_goal: string;
  workspace_path?: string;
  workspace_id?: ID;
  attachments?: AttachmentInput[];
  model_policy?: ModelPolicyInput;
};

type CreateTaskOutput = {
  task: TaskDTO;
  run: AgentRunDTO;
};

type ListTasksInput = {
  status?: TaskStatus[];
  query?: string;
  limit?: number;
  cursor?: string;
};

type ListTasksOutput = {
  tasks: TaskDTO[];
  next_cursor?: string;
};

type GetTaskInput = {
  task_id: ID;
};

type TaskDetailOutput = {
  task: TaskDTO;
  active_run?: AgentRunDTO;
  steps: ExecutionStepDTO[];
  artifacts: ArtifactDTO[];
};
```

### Run API

```ts
pauseRun(input: RunIdInput): Promise<ApiResult<AgentRunDTO>>;
resumeRun(input: RunIdInput): Promise<ApiResult<AgentRunDTO>>;
cancelRun(input: RunIdInput): Promise<ApiResult<AgentRunDTO>>;
retryStep(input: RetryStepInput): Promise<ApiResult<AgentRunDTO>>;
```

```ts
type RunIdInput = {
  run_id: ID;
};

type RetryStepInput = {
  run_id: ID;
  step_id: ID;
};
```

### Permission API

```ts
resolvePermission(input: PermissionDecisionDTO): Promise<ApiResult<ResolvePermissionOutput>>;
listPendingPermissions(runId: ID): Promise<ApiResult<{ requests: PermissionRequestDTO[] }>>;
```

Web endpoint 为 `GET /api/runs/{run_id}/permissions`；Gateway 代理 Control Plane 的
`GET /internal/runs/{run_id}/permissions`。只返回该 Run 的 pending
`PermissionRequestDTO`，内部 `checkpoint_json` 永不返回。

`ResolvePermissionOutput`:

```ts
type ResolvePermissionOutput = {
  request: PermissionRequestDTO;   // 被决议的权限请求
  events: RuntimeEvent[];          // 即时 acknowledgement；工具/Run 结果仍经 SSE
};
```

Control Plane 已在 PostgreSQL 中持久化接受权限决定后，Gateway 必须随成功响应返回一个
`permission.resolved` acknowledgement，使当前页面立即离开 `waiting_for_permission`。该事件只确认
用户决定已被接受，`payload.acknowledged=true`，不得声明工具成功、RAG ready 或 Run 完成。Worker 在
permission resume 收口时仍发布 durable `permission.resolved` 与后续工具/Run 事件；两条 resolved
投影按 `payload.request_id` 语义等价，客户端必须去重，不能在 Timeline 展示两次决定。

### Settings API

```ts
getSettings(): Promise<ApiResult<SettingsDTO>>;
updateSettings(input: UpdateSettingsInput): Promise<ApiResult<SettingsDTO>>;
```

```ts
type SettingsDTO = {
  model: ModelSettingsDTO;
  workspace: WorkspaceSettingsDTO;
  permissions: PermissionSettingsDTO;
  mcp: McpSettingsDTO;
  runtime: RuntimeSettingsDTO;
};

/** 只读运行时状态，不含 DSN/path/API key/token/env 原值等敏感信息 */
type RuntimeSettingsDTO = {
  storage_backend: "postgresql" | "inmemory";
  persistence_status: "ready" | "degraded" | "unavailable";
  runtime_bus: "redis" | "inmemory";
  control_plane_status: "ready" | "degraded" | "unavailable";
};

type ModelSettingsDTO = {
  cloud_provider?: string;
  default_model?: string;
  local_endpoint?: string;
  fallback_enabled: boolean;
  api_key_configured: boolean;
};

type WorkspaceSettingsDTO = {
  default_workspace_path?: string;
  allowed_workspace_paths: string[];
};

// 兼容旧设置展示；Workspace Registry 才是 Web 已注册工作区的业务真源。

type PermissionSettingsDTO = {
  default_shell_policy: "deny" | "confirm" | "allow_low_risk";
  high_risk_policy: "always_confirm" | "deny";
};

type McpSettingsDTO = {
  servers: McpServerConfigDTO[];
};
```

工作区契约：

- `GET /api/workspaces` 返回 active Workspace；`POST /api/workspaces/pick` 由用户主动打开系统 picker；`DELETE /api/workspaces/{id}` 只撤销 `source=user_picker` 的记录。
- WorkspaceDTO：`id/name/root_path/canonical_path/status/source/created_at/updated_at/revoked_at?`。`status` 为 `active | revoked`，`source` 为 `configured | user_picker`。
- `CreateTaskInput.workspace_id` 优先于兼容字段 `workspace_path`。ID 必须指向 active Workspace；Task 保存 `workspace_id` 和再次校验后的 `workspace_path` 快照。
- 两者都为空时仍使用 `WorkspacePolicy.resolve(None)` 兼容 `JARVIS_WORKSPACE_ROOT`；不得回退 cwd。

### Retrieved Source

权威来源工具返回的标准记录至少包含：

```ts
type RetrievedSourceDTO = {
  source_id: string;
  source_type: "literature" | "webpage" | "uploaded_document" | "other";
  title: string;
  canonical_url?: string;
  content_scope: "metadata_only" | "search_snippet" | "abstract" | "excerpt" | "full_text" | "indexed_evidence";
  content_text: string;
  content_locators: string[];
  content_sha256?: string;
  download: {
    available: boolean;
    reference?: string;
    mime_type?: string;
    url?: string;
  };
};
```

`download.available` 是 Source Provider 对真实下载能力的确认，不是 LLM 推断。LLM 判断来源与目标是否
相关；用户要求下载全部可下载相关来源时，应对每个相关且 `available=true` 的记录调用对应下载工具。
下载 Tool 仍须独立校验 reference、最终 URL、MIME、大小、内容头和权限。

`literature.search_arxiv` 在一次有界退避后仍收到 provider 429 时返回
`AppError(code=ARXIV_RATE_LIMITED, category=tool, recoverable=true)`；可选
`details.retry_after_seconds` 必须限制在 3～30 秒。该错误通过 `tool.call.failed` 对外可观察，Run 是否
继续由 Agent loop 的下一轮 LLM 决策决定。

### Obsidian Personal Knowledge Base

- `GET /api/knowledge-vaults`：返回 active Vault 与服务端建议路径。
- `POST /api/knowledge-vaults/connect`：用户显式连接并初始化独立 `Jarvis` Vault，并在同一事务中把它切换为
  唯一 active Vault；此前 Vault 只改为 `revoked`，文件、索引和文档元数据均不删除。重新连接已注册路径会
  恢复其 active 状态。
- `GET /api/knowledge-vaults/{id}/documents`：有界返回 Jarvis 创建文档的元数据。
- `POST /api/knowledge-vaults/{id}/documents`：创建 `report | note | source` Markdown 并刷新索引。

`KnowledgeVaultDTO` 包含 `id/name/root_path/canonical_path/status/source/created_at/updated_at`；
`KnowledgeDocumentDTO` 包含 `id/vault_id/title/kind/relative_path/content_hash/size_bytes/tags/source_urls/source_task_id?/source_run_id?/created_at/updated_at`。
客户端不得提交目标路径，正文最大 512 KiB。Web 调用代表用户显式 L2 写入；Agent 与自动任务不得调用这些
HTTP 接口模拟用户操作。

Knowledge Application Service 是 Obsidian Markdown 方言、命名和索引结构的唯一 owner：

- 文件名由规范化后的纯语义 `title` 生成，不再附加 UUID、Run ID、Git revision 或随机后缀；完整
  `jarvis_id` 以及可选 `source_task_id/source_run_id` 保存在 frontmatter。客户端和模型仍不能提交路径。
- `report/note/source` 分别写入 `Reports/Notes/Sources`；同一目录内语义文件名已存在时返回
  `KNOWLEDGE_DOCUMENT_EXISTS`，不得覆盖 Jarvis 或用户已有文件。报告周期、分析范围等属于标题语义，
  运行阶段和构建 revision 不属于标题语义。
- 正文写入前将代码范围之外、成对的 `\\(...\\)` / `\\[...\\]` 规范化为 Obsidian MathJax 的
  `$...$` / `$$...$$`；已正确的 dollar 定界符、行内代码、围栏代码块、转义或不成对定界符保持不变。
- `索引.md` 固定分为“报告 / 笔记 / 来源”；报告按创建时间倒序，笔记和来源按标题排序。

Agent 能力 `knowledge.create_document` 使用同一 Application Service，但必须经过 ToolGateway；参数为
`title/kind/content`，可选 `vault_id/tags/source_urls`。来源 Task/Run 由 Runtime 覆盖绑定；
`provenance_links[]` 属于内部工具输入契约，由 Runtime 覆盖，模型不能拥有或伪造 Artifact UUID、RAG
document/job ID 与 ingestion status。同 Run 写入从成功 ToolResult 生成；连续会话写入只允许从“同一
conversation 中、当前有界历史实际保留的最近完整 assistant turn”的持久化 `run_id -> completed
ToolCalls` 恢复，纯文本历史、模型正文和更早轮次不能成为可信 ID owner。Intent 的 knowledge effect 同时
声明 `knowledge_provenance=skip|optional|required` 与可选的用户原样 `knowledge_title`：`required` 但当前
Run 和上述最近历史 Run 都没有可信关联时，必须在 Permission 之前以
`KNOWLEDGE_PROVENANCE_REQUIRED` 失败关闭；明确标题由 Runtime 原样覆盖动作模型标题。下载/入库关联可包含
`source_id/source_url/artifact_id/artifact_sha256/rag_document_id/rag_job_id/rag_status`；从既有 RAG
检索后写知识文档的关联使用
`artifact_id/rag_document_id/rag_search_tool_call_id/rag_chunk_id`。后两项只来自成功 native
`rag.search` observation 中实际进入 Prompt 的 context Chunk，均须为 UUID；检索关联不声明不存在的
source URL、job 或 ingestion status。该内部字段不进入 KnowledgeDocumentDTO。

`rag.search` 的模型参数仍为 `query/top_k/document_ids`，其中 `document_ids` 由 Runtime 覆盖。`top_k`
允许 1–20；selected scope 下有效值自动提升到至少等于文档数。返回结果在存在 selected scope 时新增：

```ts
type RagDocumentCoverage = {
  requested_count: number;
  covered_count: number;
  complete: boolean;
  uncovered_document_ids: ID[];
};
```

该字段表示本次真正进入模型上下文的文档覆盖，不表示数据库文档总数或候选召回率。`complete=false`
时模型可以基于已有证据给出有限结论或调整检索，但不得声称已完整比较全部指定文档。

### Workspace RAG Document Library

- `GET /api/rag/documents?workspace_id={id}`：返回指定 active Workspace 内的 RAG 文档，以及每个文档
  最近一次入库作业。`workspace_id` 必填且必须是 UUID；服务端再次验证 Workspace 状态，不能只依赖
  前端当前选择。
- 可选 `include_disabled=true` 包含已停用文档；默认不返回。Knowledge 管理页显式使用该参数，以便用户
  恢复已停用文档。

`RagDocumentDTO` 包含 `id/workspace_id/source_artifact_id/title/mime_type/status`、ingestion/parser/
chunker/embedding 版本、embedding dimensions、chunk count、indexed/created/updated 时间及
`latest_job?`，并包含 `index_state=current|stale|building|unavailable`、`index_stale_reasons[]` 与
当前 `index_target`。这些字段由后端版本策略计算，Frontend State 不得根据字符串自行推断。
`RagIngestionJobDTO` 包含阶段状态、解析与 Embedding 尝试计数、可选安全
`error_code/next_retry_at`、生命周期时间和必填 `progress` 快照。`progress` 包含可选
`active_executor`，以及 `page_count/native_extraction_done/visual_pages_total/
visual_pages_completed/visual_route_counts/chunks_total/embedding_total/embedding_completed`；
`visual_route_counts` 的键为 `ocr_required | complex_image | complex_table`，值为命中页数。所有计数来自阶段 Service
已完成的真实工作，不是前端估算。接口不得返回 lease owner、文件路径、向量正文或原始异常。

该接口是管理读模型，不是检索 Tool。Frontend State 只消费 DTO，不负责推断最近作业、合成状态或
跨 Workspace 聚合。

- `POST /api/rag/upload-requests`：输入 `workspace_id/filename/size_bytes/content_sha256`，创建并返回
  持久化 L2 `PermissionRequestDTO`。SHA-256 必须是 64 位小写十六进制；文件名只接受净化后的 PDF，
  大小为 `1..50 MiB`。该步骤只创建 waiting Task/Run、Permission、RuntimeEvent 和 AuditLog，不创建
  Artifact、RagDocument 或 ingestion job。
  同一文件的 pending/approved 或成功 consumed 请求保持幂等；denied、expired 或已消费但失败的请求
  是不可复活的终态，后续显式上传必须创建新的有界 attempt，并绑定新的 Task、Run 与 PermissionRequest。
  attempt ID 由 Artifact 内容身份确定性派生，最多 32 次；Artifact 本身仍按 Workspace + SHA-256 唯一。
- `POST /api/rag/upload-requests/{request_id}/resolve`：输入 `decision=allow_once|deny` 与可选有界 note。
  相同决定幂等，冲突决定 fail closed；deny 将 Task/Run 取消且不产生上传副作用。
- `POST /api/rag/documents`：执行已批准的 PDF 上传。请求为 multipart form，含必填
  `workspace_id`、`permission_request_id` 与 `file`；Gateway 与 Control Plane 都校验 UUID、`.pdf` 文件名、PDF magic bytes
  及 50 MiB 上限。成功返回 `artifact_id/document_id/job_id/status/uploaded/created`。
- 上传不是 Agent ToolCall，但必须消费上述服务端权限真源。Control Plane 逐项核对 request 的
  Workspace、文件名、字节数与实际 SHA-256 后，才创建受控 `producer_type=runtime` Artifact 并调用
  同一 RAG command service 入队；完成后 Permission 标记 consumed，Task/Run 转为 completed。相同
  Workspace 中相同 SHA-256 内容复用确定性 Artifact 与既有 Job。客户端不提交本地路径、Artifact ID
  或状态，响应不返回二进制内容、存储路径或原始异常。
- 对首次写入，文件名仍是冻结权限摘要的一部分，任何不一致均 fail closed。对已经 consumed 的上传，只有
  服务端先确认确定性 Artifact 已存在且其 Workspace、内容哈希和大小完全一致后，才可把新的客户端文件名
  视为同一内容的展示别名并恢复既有 Job；denied/pending/approved 的非既有内容路径不得使用该例外，也不
  得为了判定别名而在权限状态校验前读取 Artifact。consumed 权限对应的 Artifact 缺失时返回不可恢复的
  `RAG_UPLOAD_INTEGRITY_ERROR`，不得重新写入或创建 Job。
- 入队校验接受两种用户上传来源：已经完成的可信上传 lineage；或仍处于
  `Task.waiting_for_user + Run.waiting_permission`、且确定性 `rag.upload_pdf` PermissionRequest 为
  `approved/allow_once` 的暂存 lineage。暂存路径必须同时核对 permission/task/run/workspace/artifact、
  `scope.type=once`、文件名、大小和 SHA-256；任一不一致使用 `RAG_SOURCE_INTEGRITY_ERROR`，不得创建
  Document/Job。已批准上传在 Artifact 持久化后失败时，客户端可重新提交完全相同的文件和
  `permission_request_id`；服务端复用原 Artifact 与幂等 Job，不产生第二次权限决定或重复文件。
  Artifact metadata 必须记录实际消费的 `permission_request_id`；入库不能重新假定首个 attempt。暂存
  lineage 同时核对 checkpoint 的 attempt/root identity 与 permission/task/run 关系，禁止把任意权限
  记录拼接到重试 Artifact。
- Control Plane 必须将上传期间的 `RagIngestionError` 映射为统一 `AppError`，不得落入通用异常处理器或
  向客户端返回原始堆栈；公共错误 shape 保持 `code/message/category/recoverable`。
- 用户上传在 Artifact 聚合配额不足时返回对应稳定 `ARTIFACT_*_CAPACITY_EXCEEDED` AppError，且不得
  创建 Task/Run/Artifact 或入队。RAG 解析产生的图片达到单对象/总量配额时使用
  `RAG_ASSET_OBJECT_CAPACITY_EXCEEDED` / `RAG_ASSET_TOTAL_CAPACITY_EXCEEDED` 收口 Job；
  容量扫描预算超限统一使用 `STORAGE_CAPACITY_SCAN_LIMIT_EXCEEDED`。
- `POST /api/rag/documents/{document_id}/restart`：用户显式重新执行已有文档，JSON 只包含必填
  `workspace_id/expected_version`。Gateway 与 Control Plane 同时校验两个 UUID、正整数版本和 Workspace 所属关系；服务端重新校验
  原 Artifact 的 lineage、文件大小与 SHA-256 后，对同一 policy 重置既有 Job；policy 变化时创建或
  复用当前版本的确定性 Job 并置为 `queued`。响应只返回
  `document_id/job_id/status`。该接口不接收文件、路径、attempts、lease、progress 或目标状态；disabled
  文档不可重新执行，并写入 `rag.ingestion.restarted` AuditLog。
- `PATCH /api/rag/documents/{document_id}`：JSON 为必填 `workspace_id/expected_version/enabled`。只有
  ready 文档可停用，只有索引元数据完整的 disabled 文档可恢复 ready；运行中作业必须先取消。响应为
  `document_id/status/version`，操作写入 L2 `rag.document.enabled|disabled` AuditLog。
- `POST /api/rag/documents/{document_id}/cancel`：JSON 为必填 `workspace_id/expected_version`。只取消最近
  一条非终态 Job，并把 indexing 文档置为 failed，响应为
  `document_id/status/version/job_id/job_status`，写入 L2 `rag.ingestion.cancelled` AuditLog。
- `POST /api/rag/documents/{document_id}/delete-requests`：以 `workspace_id/expected_version` 创建 L4
  永久删除确认，返回持久化 `PermissionRequestDTO`，只允许 `allow_once/deny`。
- `POST /api/rag/delete-requests/{request_id}/resolve`：提交 `decision/note?`。批准后删除 RAG 派生记录、
  向量和派生文件，保留原始 Artifact；返回 `deleted/cleanup_pending_count/source_artifact_retained`。
  重复提交相同决定幂等，冲突决定拒绝；运行中 Job 和版本变化均 fail closed。
- 所有 mutation 接口均以 `expected_version` 实现乐观并发保护；版本不一致返回可恢复 conflict，客户端
  必须刷新而不能覆盖新状态。

### RAG User Feedback and Review Queue

- `POST /api/rag/feedback`：输入 `message_id/kind/citation_chunk_id?`。`kind` 只允许
  `helpful | unhelpful | citation_incorrect | evidence_insufficient`。Gateway 与 Control Plane 均校验
  ID；Control Plane 要求 Message 为已持久化 Assistant 回复，并用其 `run_id` 解析最新 RAG trace。
- 客户端不得提交 `trace_id/workspace_id/task_id/run_id/status/fingerprint`。`citation_incorrect` 必须带
  `citation_chunk_id`，且该 chunk 必须属于 trace 的实际 Context；其他 kind 禁止携带 chunk。
- 同一 `trace + message + answer/citation chunk` 使用服务端 SHA-256 fingerprint 幂等去重；用户修改
  同一目标的反馈会更新 kind 并重新进入 `pending`，不会累积互相冲突的 answer 候选。
- `GET /api/rag/feedback?workspace_id={id}&status=pending&limit=50`：返回当前 Workspace 的有界审核队列。
  `RagFeedbackDTO` 含关联 ID、kind/status、可选 citation chunk、query hash、pipeline 版本、结果计数、
  Context 截断状态与时间；不得返回 query、回答、Chunk 正文、Embedding 或原始错误。
- `PATCH /api/rag/feedback/{feedback_id}`：只接受 `reviewed | dismissed`。它是低风险候选运营操作并写入
  AuditLog，不改变 trace 隐私状态、`rag_evaluation_labels` 或 promoted cohort。
- `GET /api/rag/feedback/{feedback_id}`：返回阶段证据和现有 label 投影。隐私未获批时 `query/snippet`
  必须为 null；获批后 snippet 仍限制为 320 字符，不返回回答正文、向量或异常原文。
- `POST /api/rag/feedback/{feedback_id}/triage`：输入固定 `failure_category` 与有界 positive/hard-negative
  chunk ids。所选证据必须属于本次 trace；生成标签还要求隐私已批准，且只能创建/更新
  `source=user_feedback,status=draft`。已有人工、confirmed、rejected 或 promoted 标签时拒绝证据改写；
  分类保存本身不会确认标签或修改 cohort。

### RAG 飞轮人工审核

- `GET /api/rag/evaluation/traces?workspace_id={id}&privacy_status=pending|approved|rejected|all&limit=50`：
  返回当前 Workspace 的有界 trace 审核队列，仅含 hash、阶段计数、Pipeline 版本和可选 label 状态。
- `GET /api/rag/evaluation/traces/{trace_id}?workspace_id={id}`：返回审核详情。隐私未批准时 `query`、
  `request` 与 evidence `snippet` 必须为空；批准后正文摘要仍限制为 320 字符，最多返回 100 条证据。
- `POST /api/rag/evaluation/traces/{trace_id}/privacy`：输入 `workspace_id` 与 `approved|rejected`。拒绝
  已 confirmed/promoted 标签的 trace 必须失败；动作写 `rag.evaluation.privacy_reviewed` AuditLog。
- `POST /api/rag/evaluation/traces/{trace_id}/label`：只接受 `draft|confirmed|rejected`、1–100 个正例、
  最多 100 个难负例和 500 字符 notes。Chunk 必须属于 trace Workspace；confirmed 要求隐私已批准；
  `promoted` 不能通过该接口写入，也不能再编辑。
- `POST /api/rag/evaluation/traces/{trace_id}/promote`：只允许已批准隐私且 confirmed 的人工标签进入
  promoted 终态，写 AuditLog 并返回只含 `trace_id/query_hash` 的 `promotion_candidate`，明确标记不含
  raw query/chunk。该接口不改写版本化 cohort manifest，也不执行评测或发布脚本。

### Scheduled Tasks

- `GET /api/scheduled-tasks`：列出计划。
- `POST /api/scheduled-tasks`：创建 `daily | weekly` 计划；输入包含 name、user_goal、IANA timezone、
  hour、minute、可选 weekday/workspace_id，以及 `task_kind=knowledge_report | source_report`。
  `source_report` 必须提供 `source_query`，可提供 1–10 的 `source_max_results`；provider 固定为 arXiv。
- `PATCH /api/scheduled-tasks/{id}`：以 `expected_version` 暂停或恢复。
- `POST /api/scheduled-tasks/{id}/trigger`：创建一次持久化执行并派发普通 Task/Run。

`ScheduledTaskDTO` 返回状态、重复规则、时区、下一次/上一次执行、最近 Task/Run、固定
`authorized_tools`、`task_kind`、`source_policy` 和 version。`ScheduledTaskExecutionDTO` 返回执行状态、
scheduled_for、attempts、task_id/run_id/error_code。RunJob 的
`scheduled_task_id/authorized_tools/source_policy` 是服务端可信字段，不接受模型输入。

自动计划产生的 Run 不经过 Gateway 创建入口。EventPump 遇到未知 Run 时必须调用
`GET /internal/tasks/{task_id}`，精确验证 Task ID、Run ID 与所属关系后才能建立临时实时投影；
验证失败不得仅凭 Redis envelope 创建状态。

### Long-term Memory

```ts
type MemoryScopeType = "global" | "workspace";
type MemoryCategory = "preference" | "user_fact" | "project_fact" | "rule";
type MemoryStatus = "active" | "disabled";

type MemoryDTO = {
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
```

### Memory Candidate

```ts
type MemoryCandidateStatus = "pending" | "approved" | "rejected" | "expired";

type MemoryCandidateDTO = {
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
```

API：

```text
GET   /api/memory-candidates?status=pending
PATCH /api/memory-candidates/{id}
POST  /api/memory-candidates/{id}/approve
POST  /api/memory-candidates/{id}/reject
```

PATCH 与决定请求必须携带 `expected_version`。approve 在 Python Application 的单一事务中
锁定 Candidate、重验过期/Workspace/去重与冲突，创建正式 Memory，并写入
`approved_memory_id` 和 AuditLog。Gateway 只校验 UUID/HTTP shape 并代理，不拥有候选状态机。

Candidate API 不提供 Web 创建入口；候选只能由后续受控 MemoryExtractor 流程产生。
Candidate 不属于 RuntimeEvent，也不得追加到已经完成的源 Run 事件流。

`GET /api/memory-candidates?status=pending` 只返回仍处于 pending 的记录；到期转换由 Worker
独立维护并写 AuditLog，不由前端推导。不同 Run 产生相同
`scope/workspace/category/key/content` 时，pending partial unique constraint 与 Application
Service 共同幂等抑制；并发冲突返回“未创建候选”，不会暴露数据库异常。

- `GET /api/memories`：有界列出长期记忆，可透传 `scope_type/workspace_id/status/category/query/limit`。
- `POST /api/memories`：显式创建；workspace scope 必须引用 active Workspace，global 禁止携带 workspace_id。
- `PATCH /api/memories/{id}`：更新 content/status/importance，必须携带 `expected_version` 做乐观并发校验。
- `DELETE /api/memories/{id}`：永久删除，用于隐私控制；创建、更新和删除均写 AuditLog。
- 同一 scope owner、category、key 唯一。key 使用规范化小写标识；正文上限 4000 字符。
- Go Gateway 只校验 transport 并代理；Memory 业务真相、作用域校验、审计和持久化归 Python。
- `model.context.prepared` 增加 `included_memories/dropped_memories`，仅为数量，不含正文。
- Registry 路径注册和 Task 使用都由 Python Application 校验。Web 下拉和系统 picker 只提供用户体验，不能替代服务端安全策略。
- configured seed 由环境配置管理，Web revoke 返回 `403 WORKSPACE_MANAGED_BY_CONFIG`；未知 Origin 返回 `403 ORIGIN_NOT_ALLOWED`。
- picker 成功返回 `{ workspace, cancelled:false }`；用户取消返回 `{ workspace:null, cancelled:true }`，不是错误。

### Model Config API（Phase 6）

模型配置投影与连通性测试。Go Gateway 代理 Python Control Plane；Go 不直接调模型。

```ts
getModelConfig(): Promise<ApiResult<ModelConfigDTO>>;
testModelConnection(): Promise<ApiResult<ModelTestOutput>>;
```

**ModelConfigDTO** — 安全投影，绝不包含 API key 原值或环境变量原值：

```ts
type ModelConfigDTO = {
  provider: string;                     // "deepseek" | "custom_openai_compatible" | ""
  protocol: string;                     // 当前为 "openai_chat_completions" | ""
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
```

**ModelTestOutput** — 连通性测试结果，不含 prompt/key/原始响应：

```ts
type ModelTestOutput = {
  provider: string;
  model: string;
  latency_ms: number;
  tested_at: string;                    // ISO 时间
  status: "ok" | "failed";
  error: AppError | null;               // 失败时安全 AppError（不含原始响应/headers）
};
```

**API 端点：**

```text
GET  /api/model-config        — 返回当前模型配置投影（安全脱敏）
POST /api/model-config/test   — 发起连通性测试，写入 AuditLog
```

**Python Control Plane 对应 Internal API：**

```text
GET  /internal/model-config   — 从环境变量读取 WorkerConfig 投影
POST /internal/model-config/test — 短超时 httpx 请求，写入 AuditLog
```

**安全约束：**

- `base_url_display` 不包含 userinfo、query、fragment；非法 scheme 返回 `<invalid-scheme>`。
- `api_key_configured` 只返回 boolean；不返回 key 值、环境变量名或配置状态推断路径。
- 测试请求：超时 5s、不重试、固定最小 prompt（max_tokens=1）、不记录原始响应。
- AuditLog：`event_type="model.test"`、actor="system"；details 只含 provider/model/safe_url/timeout_ms；result_summary 只含 success/latency_ms 或安全 error_code；不含 API key、Authorization header、prompt、原始 HTTP body 或异常堆栈。
- Go Gateway 不调模型、不接触 API key、不写 PostgreSQL；只做 DTO 代理与错误映射。

### Audit Log 查询（Phase 6）

```text
GET /api/audit-logs?limit=50&event_type=&actor=&task_id=&run_id=&before=
```

Gateway 将请求代理到 `GET /internal/audit-logs`；Python `AuditQueryApplicationService`
经 `AuditRepository` 查询 PostgreSQL，Go 不读取数据库也不重写审计语义。

```ts
type ListAuditLogsOutput = {
  audit_logs: AuditLogDTO[];
  next_cursor?: string; // base64(json([created_at_iso, audit_log_id]))
};
```

- `limit` 范围为 1–100（默认 50）；按 `created_at DESC, id DESC` 稳定分页。
- 支持精确筛选 `event_type`、`actor`、`task_id`、`run_id`；非法 UUID 或 cursor 返回统一 `VALIDATION_ERROR`。
- `AuditLogDTO` 是安全只读投影：仅包含关联 ID、事件、风险/权限、操作和结果摘要、`error_code` 以及有界 `details_summary`。
- 不返回原始 `details_json` / `error_json`、异常消息或堆栈、prompt、工具完整参数、文件正文、API key、token、password、Authorization、cookie 等敏感内容。

### Audit Log 安全导出（Phase 8）

```text
GET /api/audit-logs/export?format=jsonl&max_rows=5000&max_bytes=5242880&event_type=&actor=&task_id=&run_id=&before=
```

Gateway 将响应作为下载流代理到 `GET /internal/audit-logs/export`，不读取 PostgreSQL、不缓存或解析
导出正文，也不把正文包装进 `ApiResult`。Python `AuditQueryApplicationService` 是固定字段顺序、
安全投影、分页和导出结果审计的唯一 owner。

- `format` 默认 `jsonl`，可选 `csv`；JSONL 每行一个固定字段对象，CSV 首行是同一字段顺序的表头。
- 单次导出默认最多 5,000 行、5 MiB；调用方可收紧预算，但 `max_rows` 硬上限为 10,000，
  `max_bytes` 范围为 1 KiB–10 MiB。Python 每页最多读取 101 条，Gateway 再按 `max_bytes`
  限制转发，任何一层都不得在内存加载全表。
- 所有记录必须先经过与审计查询相同的安全投影；CSV 中以 `= + - @ TAB CR` 开头的单元格增加文本
  前缀，防止电子表格公式注入。
- 响应固定使用 `Cache-Control: no-store`、`X-Content-Type-Options: nosniff` 和安全文件名。
- 流正常完成写入 `audit.export.completed`；中断或失败写入 `audit.export.failed`。AuditLog 只记录
  筛选条件摘要、格式、行数、字节数、SHA-256、预算与是否截断，不记录导出正文、原始异常或 cursor。

### Audit Log 保留策略与 L4 执行（Phase 8）

```text
GET /api/audit-logs/retention/preview?standard_days=90&extended_days=365&max_scan=1000&max_candidates=100
```

Gateway 只校验有界 query 并代理到
`GET /internal/audit-logs/retention/preview`。Python Application Service 按最旧优先 cursor
扫描，不返回候选 ID、正文或原始 details。

```ts
type AuditRetentionPreviewDTO = {
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
```

- 普通审计默认保留 90 天；L3 和 `permission.*` 默认延长至 365 天。
- L4/L5、`audit.retention.*`，以及 event type 属于删除、清理、撤销、恢复、修复或还原类的记录
  永久保护，不进入候选。
- `standard_days/extended_days` 范围均为 30–3,650，且 extended 必须大于 standard。
  `max_scan` 范围 1–10,000，`max_candidates` 范围 1–1,000。
- 预演端点严格 `dry_run=true`，只返回计数和是否还有未扫描记录，不执行删除。
- 每次成功预演追加 `audit.retention.previewed`，只记录策略与计数，不记录候选 ID 或内容。

实际清理是显式的两步 L4 操作，没有自动调度入口：

```text
POST /api/audit-logs/retention/requests
POST /api/audit-logs/retention/requests/{request_id}/resolve
```

创建请求 body：

```ts
type CreateAuditRetentionRequestInput = {
  standard_days: number;   // 30..3650
  extended_days: number;   // 30..3650，且 > standard_days
  max_scan: number;        // 1..10000
  max_candidates: number;  // 1..1000
};
```

第一步只冻结候选快照并返回 `PermissionRequestDTO`：

- `tool_name = audit.apply_retention_policy`
- `risk_level = L4`
- `scope = { type: "once", resource: "audit_logs" }`
- `allowed_decisions = ["allow_once", "deny"]`
- `arguments_summary` 只含策略、分类计数和 `has_more`，不含候选 ID 或正文。

第二步 body 只允许：

```ts
type ResolveAuditRetentionRequestInput = {
  decision: "allow_once" | "deny";
  note?: string; // <= 500
};

type AuditRetentionResolutionDTO = {
  permission: PermissionRequestDTO;
  deleted_records: number;
  has_more: boolean;
};
```

`deny` 不删除任何记录，并写 `audit.retention.permission_decision`。`allow_once` 在同一 PostgreSQL
事务内锁定权限请求与全局保留执行器，使用创建请求时的 `evaluated_at` 和策略重新扫描、重新分类；
候选数量或候选 ID SHA-256 任一变化均返回 `AUDIT_RETENTION_SNAPSHOT_CHANGED`，不得部分删除。
快照一致时最多删除 `max_candidates` 条，实际删除数不一致则整笔事务回滚。权限消费与
`audit.retention.applied` 写入必须和删除原子提交。所有 `audit.retention.*` 均永久保护。

### 测试替身边界

自动化测试可以直接注入 in-memory bus、fake Redis、`MockTransport`、`MockModelProvider` 或 `MockRunner`。这些替身不得进入共享前端 DTO，不得提供 `/api/dev/mock` 一类产品可达入口，也不得成为无配置时的默认运行路径。

## Internal Runtime Bus Contract

内部 Runtime Bus 用于 Go Runtime Orchestrator、Redis 和 Python Agent Worker 之间通信。它不是 Web UI 直接调用的接口，但必须与 Web DTO / RuntimeEvent 保持同一语义。

### Runtime Command Envelope

```ts
type RuntimeCommand = {
  id: ID;
  trace_id: ID;
  type: RuntimeCommandType;
  task_id?: ID;
  run_id?: ID;
  target_worker_id?: ID;
  timestamp: ISODateTime;
  payload: Record<string, unknown>;
};

type RuntimeCommandType =
  | "run.enqueue"
  | "run.pause"
  | "run.resume"
  | "run.cancel"
  | "run.retry_step"
  | "permission.resolve"
  | "mcp.discovery.refresh"
  | "worker.shutdown";
```

### Run Enqueue Payload

```ts
type RunEnqueuePayload = {
  task: TaskDTO;
  run: AgentRunDTO;
  input: CreateTaskInput;
};
```

### Permission Resolve Payload

```ts
type PermissionResolvePayload = {
  decision: PermissionDecisionDTO;
};
```

Worker heartbeat 的当前真源：Redis stream 消息见本章的 `WorkerHeartbeatMessage`，Gateway HTTP API DTO 见本章的「Worker Heartbeat + Gateway Status API (3B)」小节。

### Redis Message Contract（2B-1a）

Redis Runtime Bus 上流转的消息类型由 `apps/gateway/internal/redis/` 包定义。以下是 Go 侧 struct 的 TS 等价描述。

#### RunJobMessage

Go Orchestrator → Redis run queue → Python worker。

```ts
type RunJobMessage = {
  job_id: ID;
  trace_id: ID;        // 链路追踪 id，贯穿此次 run 的所有 command / event
  task_id: ID;
  run_id: ID;
  user_goal: string;
  workspace_path?: string;
  created_at: ISODateTime;
  schema_version: string;   // 当前 "2B-1a.1"
};
```

#### RuntimeEventEnvelope

Python worker → Redis runtime event stream → Go Orchestrator → Web UI。

`RuntimeEventEnvelope` 在 `RuntimeEvent` 之上附加传输元数据，**不重新定义 RuntimeEvent 的 shape**。内层 `runtime_event` 字段必须完整保留 `dto.RuntimeEvent` 的所有字段（id / type / task_id / run_id / step_id / sequence / timestamp / payload）。`sequence` 是 PostgreSQL durable 历史的可选投影；Redis 中尚未持久化的低延迟事件可以不携带。

```ts
type RuntimeEventEnvelope = {
  event_id: ID;              // 必须等于 runtime_event.id
  trace_id: ID;              // 链路追踪 id，与对应 run job 的 trace_id 一致
  task_id: ID;
  run_id: ID;
  event_type: RuntimeEventType;
  runtime_event: RuntimeEvent;   // 完整 dto.RuntimeEvent，不在此层重新定义
  produced_by: string;
  schema_version: string;        // 当前 "2B-1a.1"
};
```

Python 对象可以携带仅进程内的 `internal.run_checkpoint`，但该字段不属于 Redis/DTO
契约，`to_payload_json()` 必须忽略它；Storage 在写公共 RuntimeEvent/Outbox 前消费它。

#### PermissionDecisionCommand

Go Orchestrator → Redis worker command stream → Python worker。

```ts
type PermissionDecisionCommand = {
  command_id: ID;
  trace_id: ID;              // 链路追踪 id，与对应 run job 的 trace_id 一致
  request_id: ID;
  task_id: ID;
  run_id: ID;
  decision: PermissionDecisionType;   // 复用 dto.PermissionDecisionType
  note?: string;
  decided_at: ISODateTime;
  schema_version: string;             // 当前 "2B-1a.1"
};
```

#### RunPauseCommand / RunCancelCommand

Go Orchestrator → Redis worker command stream → Python worker。

暂停和取消请求均由 Python Application Service 与业务状态、AuditLog、Outbox 在同一
PostgreSQL 事务中提交，再由 Outbox Publisher 写入 worker-command stream。Gateway
不得直接发布第二份 command。`resume` 不走 worker command；它从 PostgreSQL 的安全
checkpoint 创建 `run.resume.requested` Outbox，再以标准 `run.job` 重新入队。

**worker-command stream 多类型路由**：`jarvis:stream:worker-command` 可承载多种 command type。Python `WorkerCommandConsumer` 先校验 outer schema/type/routing fields 与 payload 一致，再按命令类型路由：
- `run.cancel` → 匹配 active run 时设置 cancel；空闲 Worker 必须以 PostgreSQL `cancel_requested` 状态收口；其他 Worker 不得误 ACK
- `run.pause` → 仅匹配 active run 时记录 pause command id；active Worker 还必须限频读取 PostgreSQL 的权威 `pause_requested`，兜底 Outbox/Redis 投递延迟。Runner 在模型调用前、工具 effect 前或工具完成后的下一个模型边界发出 durable `agent.run.paused`，不得在 `tool_in_flight` 伪装成已暂停。若命令在模型调用期间到达，即使 Provider 随后失败，也必须先按 `call_model` 安全 checkpoint 收口 paused，不能让模型错误覆盖已接受的暂停
- `permission.decision` → active 等待链路按 run/request 精确唤醒；生产链路允许空闲 Worker从 PostgreSQL Permission checkpoint 恢复并在持久化完成后 ACK
- `mcp.discovery.refresh` → 仅空闲 Worker 执行 MCP 连接、发现和持久化；Go Gateway 与 Control Plane 不执行 MCP SDK
- 合法但当前不能处理的 command 保留 PEL，由 active/idle Worker 按退避再次接管并重新读取 PostgreSQL 状态
- 未知 type、缺 type、schema/routing 不一致或 malformed payload → 原子进入脱敏 command DLQ 并 ACK，不永久阻塞 PEL

```ts
type RunCancelCommand = {
  command_id: ID;
  trace_id: ID;              // 链路追踪 id，与对应 run job 的 trace_id 一致
  task_id: ID;
  run_id: ID;
  type: "run.cancel";
  requested_at: ISODateTime;
  reason?: string;           // 可选取消原因
  schema_version: string;    // 当前 "2B-1a.1"
};

type RunPauseCommand = {
  command_id: ID;
  trace_id: ID;
  task_id: ID;
  run_id: ID;
  type: "run.pause";
  requested_at: ISODateTime;
  reason?: string;
  schema_version: string;
};
```

#### McpDiscoveryRefreshCommand

Go Gateway → Redis worker command stream → 任意空闲 Python Worker。

```ts
type McpDiscoveryRefreshCommand = {
  command_id: ID;
  trace_id: ID;
  type: "mcp.discovery.refresh";
  requested_at: ISODateTime;
  schema_version: string;
};
```

这是全局管理命令，不关联 Task/Run，因此不得伪造 `task_id/run_id`，也不得携带 MCP server
配置、环境变量值或工具 schema。Worker 必须从 PostgreSQL 读取 enabled server 配置，完成
发现和持久化后才 ACK；忙碌 Worker 保留命令，待空闲或被其他 Worker 接管。

Pause / resume API 的权威状态语义：

```text
POST /api/runs/{run_id}/pause
  running -> pause_requested -> paused(agent.run.paused)

POST /api/runs/{run_id}/resume
  paused -> resume_requested -> running(Worker claim) -> agent.run.resumed

POST /api/runs/{run_id}/steps/{step_id}/retry
  failed source Run + recoverable MODEL_CALL + extract_intent|call_model checkpoint
  -> new queued replacement Run -> Task.active_run_id replacement
```

重复 pause/resume 请求幂等返回当前过渡态；终态、queued、waiting_permission 或缺少安全
checkpoint 的恢复请求返回结构化 `AppError`。cancel 与 pause/resume 并发时 cancel 优先。

失败步骤重试由 Gateway 调用
`POST /internal/runs/{run_id}/steps/{step_id}/retry`，Control Plane 返回完整 replacement
AgentRun 投影。该路径不发送 `run.retry_step` worker command；Application Service 通过
PostgreSQL 事务写 `run.step_retry.requested` Outbox，Publisher 将其投影为携带
`retry_from_checkpoint=true` 的标准 `RunJobMessage`。源 failed Run 不发生状态回退；不符合
模型步骤、recoverable、错误一致性、active Run 或安全 checkpoint 条件时返回
`FAILED_STEP_NOT_RETRYABLE`。
`agent.run.failed` 投影在错误可恢复时只保留 `resume_node=extract_intent|call_model` 的合法 checkpoint，
确保 Web 的可恢复事件提示与重试接口能力一致；`validate_action/execute_tool/tool_in_flight` 不属于失败模型
步骤重试契约，必须清空。

#### WorkerHeartbeatMessage

Python worker → Redis worker heartbeat stream → Go Orchestrator。

心跳是状态探针，不属于 command / event 链路，**不携带 trace_id**。

```ts
type WorkerHeartbeatMessage = {
  worker_id: ID;
  worker_kind: "agent" | "rag"; // 旧消息缺省按 agent 处理
  status: "starting" | "idle" | "busy" | "draining" | "stopped" | "failed";
  active_run_id?: ID;
  reported_at: ISODateTime;
  schema_version: string;   // 当前 "2B-1a.1"
  model?: WorkerModelStatus;
  runtime_bus?: {
    reclaimed: number;       // Worker 进程启动以来累计值
    retry_deferred: number;
    dead_lettered: number;
    malformed: number;
    command_reclaimed: number;
    command_dead_lettered: number;
    command_malformed: number;
  };
};
```

`runtime_bus` 只是 Worker 进程级可观察指标，经 Gateway Worker Status 投影；它不参与
Task / Run 状态判断，也不能替代 PostgreSQL AuditLog 或 RuntimeEvent。

同一 Worker Status 列表可同时包含 Agent Worker 与 RAG Worker，由 `worker_kind` 显式区分。只有执行 AgentRun 的 Worker 才能在
`busy` 时填写 `active_run_id`；RAG Worker 在解析或向量化 Job 时同样上报 `busy`，但不填写
`active_run_id`，其 PostgreSQL ingestion job 不得伪装成 AgentRun。RAG Worker 的 `model` 可以为空，
前端选择当前模型时必须从所有在线 Worker 中寻找已配置的模型，不能假设列表第一项就是 Agent Worker。

#### RunJobDeadLetterEnvelope

Python Worker Pool → Redis run dead-letter stream，用于保存无法继续投递的 RunJob 诊断副本。

```ts
type RunJobDeadLetterEnvelope = {
  schema_version: string;
  type: "run.job.dead_letter";
  original_stream: "jarvis:stream:run-queue";
  original_message_id: string;
  consumer_group: "jarvis:group:worker-pool";
  delivery_count: number;
  reclaimed: "true" | "false";
  error_code: "RUN_QUEUE_MALFORMED" | "RUN_QUEUE_UNSUPPORTED_TYPE" | "RUN_QUEUE_SCHEMA_MISMATCH" | "RUN_QUEUE_RETRY_EXHAUSTED";
  error_message: string;      // 有界、去换行的内部诊断摘要
  failed_at: ISODateTime;
  payload_sha256: string;     // 原 payload SHA-256，仅用于关联诊断
  payload_size_bytes: string;
  job_id?: ID;
  trace_id?: ID;
  task_id?: ID;
  run_id?: ID;
};
```

DLQ 不复制原始 payload，因为 RunJob 可能包含 user_goal 或 workspace_path；只保存
SHA-256、字节数和有界错误摘要。合法 RunJob 达到重试上限时，Worker 必须先把对应 AgentRun 失败状态、
`agent.run.failed` RuntimeEvent、Outbox 和 `run.queue.dead_letter` AuditLog 原子写入
PostgreSQL，再原子执行 DLQ `XADD` 与原 Run Queue 消息 `XACK`。非法 outer/payload
因无法建立可信业务关联，可直接进入 DLQ；若 DLQ 写入失败，原消息必须留在 PEL。

#### WorkerCommandDeadLetterEnvelope / RuntimeEventDeadLetterEnvelope

两类 DLQ 都是 Redis 运行时诊断副本，使用 Lua 原子执行 `XADD DLQ + XACK source`：

```ts
type RuntimeBusDeadLetterEnvelope = {
  schema_version: string;
  type: "worker.command.dead_letter" | "runtime.event.dead_letter";
  original_stream: string;
  original_message_id: string;
  consumer_group: string;
  delivery_count: number;
  reclaimed: boolean | "true" | "false";
  error_code: string;
  error_message: string;        // 去换行、最多 300 字符
  failed_at: ISODateTime;
  payload_sha256: string;
  payload_size_bytes: number | string;
  command_id?: ID;
  event_id?: ID;
  trace_id?: ID;
  task_id?: ID;
  run_id?: ID;
  request_id?: ID;
};
```

command DLQ 只接收确定性非法消息；合法 command 必须继续依据 PostgreSQL Run / Permission / lease 状态处理，禁止仅凭 Redis delivery count 丢弃用户取消或权限决定。runtime-event 投影重试耗尽可以进入 DLQ，因为权威 RuntimeEvent 已在发布前持久化到 PostgreSQL。

#### Redis Stream / Group 命名

```text
jarvis:stream:run-queue           — run job 下发
jarvis:stream:worker-command      — 运行时命令（pause/resume/cancel/permission.resolve）
jarvis:stream:runtime-event       — RuntimeEvent 上报
jarvis:stream:worker-heartbeat    — worker 心跳与状态
jarvis:stream:pending-permission  — 待处理权限请求
jarvis:stream:run-dead-letter     — RunJob 最终投递失败的诊断副本
jarvis:stream:worker-command-dead-letter — 非法 worker command 诊断副本
jarvis:stream:runtime-event-dead-letter  — 非法或投影耗尽 RuntimeEvent 诊断副本
jarvis:group:worker-pool          — worker pool consumer group
jarvis:group:gateway-events       — gateway event consumer group
```

#### 序列化与版本

- 消息使用 JSON 序列化，优先通过标准库 `encoding/json`。
- 每条消息必须携带 `schema_version` 字段，当前值为 `"2B-1a.1"`。
- Consumer 必须校验 `schema_version` **精确等于**当前版本；缺失、非 string、为空或不匹配均视为非法消息。
- 所有必要字段（如 `job_id`、`trace_id`、`task_id`、`run_id`、`request_id`、`worker_id`）在反序列化后必须校验非空。
- `RuntimeEventEnvelope` 额外校验：envelope 层 `event_type` / `task_id` / `run_id` 必须与内层 `runtime_event` 对应字段一致；内层 `runtime_event.id` / `type` / `task_id` / `run_id` / `timestamp` 必须非空。

#### Transport 写入约定（2B-1b）

`RedisRuntimeTransport`（`apps/gateway/internal/redis/transport.go`）是 Redis 写入 adapter，职责：

- `EnqueueRunJob` — 校验 `RunJobMessage` 后 XADD 到 `jarvis:stream:run-queue`
- `PublishPermissionDecision` — 校验 `PermissionDecisionCommand` 后 XADD 到 `jarvis:stream:worker-command`
- `PublishRuntimeEvent` — 校验 `RuntimeEventEnvelope` 后 XADD 到 `jarvis:stream:runtime-event`
- `PublishWorkerHeartbeat` — 校验 `WorkerHeartbeatMessage` 后 XADD 到 `jarvis:stream:worker-heartbeat`

XADD fields 约定（所有消息统一格式）：

```text
schema_version  — 当前契约版本号（"2B-1a.1"）
payload         — 完整 message 的 JSON 字符串（可 JSON decode 回原始 struct）
+ 冗余标量路由字段（全部来自同一 message struct，按消息类型不同）
```

冗余标量路由字段按消息类型：

| 消息 | 标量字段 |
|------|----------|
| `RunJobMessage` | `job_id` / `trace_id` / `task_id` / `run_id` / `type` = "run.job" / `created_at`；恢复消息在 payload 内额外携带 `resume_from_checkpoint=true` |
| `RuntimeEventEnvelope` | `event_id` / `trace_id` / `task_id` / `run_id` / `type` = event_type / `produced_by` |
| `PermissionDecisionCommand` | `command_id` / `trace_id` / `request_id` / `task_id` / `run_id` / `type` = "permission.decision" / `decided_at` |
| `WorkerHeartbeatMessage` | `worker_id` / `type` = "worker.heartbeat" / `status` / `reported_at`（无 trace_id） |

约束：

- **nested object 不直接作为 Redis field value**：所有嵌套结构均在 `payload` JSON 字符串内。
- `payload` 必须能 JSON decode 回原始 message struct，保证消费者可完整恢复消息。
- 写入前必须经过对应 `Decode*` 函数的类型化校验；bad schema_version、缺 trace_id、缺必要字段或一致性校验失败时不调用 XAdd。
- 所有标量字段和 `payload` 内容来自同一个 message struct，不存在手写第二套不一致 shape。
- go-redis v9 通过 `GoRedisStreamClient` 窄接口包装，go-redis 类型不泄漏到 handler / bus 接口层。
- heartbeat 不强制 trace_id。
- PostgreSQL Outbox 的业务 `event_type` 不等于 Redis transport `type`。发布到 Run Queue 的 `task.created` / `run.resume.requested` 必须统一使用 `type=run.job`，并从同一个 RunJob payload 投影 `job_id/trace_id/task_id/run_id/created_at`；禁止把 `task.created` 或 Outbox `event_id` 写成 RunJob transport type / job_id。
- Outbox 发布 `run.cancel.requested` / `permission.decision` 时必须从 command payload 投影完整 `command_id/trace_id/task_id/run_id/type`，权限命令额外投影 `request_id/decided_at`；不得用 Outbox event id 代替 command id。
- Outbox 发布 durable RuntimeEvent 时必须从 envelope payload 投影 `event_id/trace_id/task_id/run_id/type/produced_by`；`event_id` 必须同时等于 `runtime_event.id`，outer routing fields 必须与 payload 一致。
- durable event registry 必须是 Outbox route registry 的子集。`task.updated`、`agent.step.updated`、
  `permission.expired` 与其他 durable RuntimeEvent 一样进入 `jarvis:stream:runtime-event`；任何缺失映射都属于
  契约错误，禁止依赖发布重试把它掩盖为暂时性 Redis 故障。

#### Transport 消费约定（2B-1c）

`RuntimeEventReader`（`apps/gateway/internal/redis/reader.go`）是 RuntimeEvent 的消费 adapter，职责：

- 通过 `RedisStreamReader` 窄接口从 `jarvis:stream:runtime-event` 读取新消息（XReadGroup，id=`>`）
- 按单条 delivery 从 `payload` JSON string 解码为 `RuntimeEventEnvelope`，保留 message id、delivery count 与 reclaimed 标记
- 调用 `DecodeRuntimeEventEnvelope` 做类型化校验
- 通过可选 `RedisStreamReliability` 窄接口执行 `XPENDING` / `XCLAIM` / 原子 DLQ
- 提供独立的 `AckEvents` 方法确认已成功投影的消息

读取与解码约束：

- **非阻塞读取**：`GoRedisStreamReader.XReadGroup` 使用 `Block: -1`（不发送 BLOCK 参数），不使用 `Block: 0`（BLOCK 0 在 Redis 语义中表示无限阻塞）。空 stream 时立即返回空列表 + nil error，不阻塞。
- **redis.Nil 归一化**：go-redis 在无新消息时可能返回 `redis.Nil`，`GoRedisStreamReader` 将其归一化为 `[]StreamMessage{}, nil`，不当错误返回。
- **poison 隔离**：一条消息解码失败只使该 delivery 进入 DLQ，不得让同一批其他合法消息回滚或永久饥饿。
- **nested object 只能来自 payload**：`RuntimeEventEnvelope` 的完整内容从 `payload` JSON string 解码，不从 Redis scalar field 拼装 `runtime_event`
- **确定性非法消息隔离**：payload 缺失、payload 非 string、payload JSON 无效、schema_version 不匹配、outer/payload routing 不一致或必需字段缺失时，仅将该 delivery 原子写入 DLQ 并 ACK 原消息
- **逐条处置**：生产 EventPump 使用 `ReadDeliveries`，同一批 XReadGroup 中一条 poison event 不得影响其他 delivery 的解码、投影或 ACK
- **ack 独立**：`AckEvents` 只对已成功处理的消息 id 调用 XAck，与 read 解耦，保持测试可单独覆盖
- go-redis v9 通过 `GoRedisStreamReader` 窄接口包装，go-redis 类型不泄漏到 handler / bus 接口层
- nil client 构造返回明确 error，不 panic

`RedisStreamReader` 接口（`apps/gateway/internal/redis/reader.go`）：

```go
type RedisStreamReader interface {
    XReadGroup(ctx, group, consumer, stream, id string, count int64) ([]StreamMessage, error)
    XAck(ctx, stream, group string, ids ...string) error
}
```

`StreamMessage` 是项目内部消息类型，不暴露 go-redis 的 `XMessage`：

```go
type StreamMessage struct {
    ID     string
    Values map[string]interface{}
}
```

Bus 约束：

- Redis Streams / consumer groups 是优先实现方式。
- 每条 command / event 必须有 `id` 和 `trace_id`。
- Python worker 必须幂等处理重复 command。
- Go Orchestrator 需要处理 pending、retry、dead letter 和 worker heartbeat 过期。
- **Redis 是运行时通信层，不是业务数据库。Task / Run / Step / ToolCall / Permission / AuditLog 的最终状态必须写入 Storage，不能只存在 Redis。**
- **RuntimeEventEnvelope 不得重新定义 RuntimeEvent shape；内层 runtime_event 必须完整保留 dto.RuntimeEvent 的所有字段。**
- **PermissionDecisionCommand.decision 复用 dto.PermissionDecisionType，不引入新的决策枚举。**

#### Run Queue PEL / retry / DLQ 约定

- 新消息只通过 `XREADGROUP ... >` 获取；Worker 每个到期扫描周期优先以 `XPENDING` + `XCLAIM` 接管至多一条 stale PEL，再读取新消息，既不做忙循环也不让 PEL 被持续新流量饿死。
- stale 时间按交付次数指数退避：默认 65 秒、130 秒、260 秒，最多交付 3 次；首次阈值必须晚于默认 60 秒 Run lease。
- 可恢复的 claim/Storage 故障不 ACK，保留 PEL；非法 schema/type/payload 不重试。
- Inbox `source=run-queue` 负责业务幂等。已处理的 stale 原消息只 ACK；Run 的恢复与重排由 PostgreSQL reconciliation 决定。
- PostgreSQL reconciliation 可为超过宽限期、仍为 `queued`、最近 RunJob Outbox 已 `delivered`，且
  Redis 对应 `jarvis:outbox:dedupe:<event_id>` 已不存在的 Run
  创建 `run.queue.reconciled` Outbox。它沿用原 RunJob payload 的任务/工作区授权快照，只替换新的
  `job_id`、`created_at` 并记录 `reconciled_from_event_id`；投影到 Redis 后仍是标准 `type=run.job`。
  payload 同时记录 `queue_reconciliation_attempt`，按 60/120/240 秒最多重投 3 次；耗尽后 PostgreSQL
  以 `RUN_QUEUE_RECONCILIATION_EXHAUSTED` 失败收口。`pending/dispatching/dead` 不通过该机制重放。
  Redis 查询失败时必须保守跳过，不能把正常 backlog 当成消息丢失。
- 最终失败使用 `source=run-queue-dlq` 幂等收口。DLQ 去重 key 保留 7 天，stream 近似保留最近 10,000 条。
- DLQ `XADD` 与原消息 `XACK` 必须在同一个 Lua 操作中完成；Lua 或 PostgreSQL 收口失败时维持 pending。
- Redis DLQ 只保存脱敏诊断副本，不保存原始 RunJob payload；业务终态、事件和审计必须落 PostgreSQL。

#### Worker command / RuntimeEvent PEL / DLQ 约定

- Worker command 每个扫描周期优先接管至多一条 stale PEL，再读取新命令；active 与 idle Worker 都必须参与接管，首次默认 5 秒并按 5 / 10 / 20 / 40 / 80 秒有界退避。
- command outer schema/type/routing/payload 确定性非法时直接原子 DLQ；合法 command 不设置仅基于 delivery count 的丢弃上限，处理前必须重新读取 PostgreSQL Run、Permission 与 lease 状态。
- EventPump 每秒优先接管至多一条 stale RuntimeEvent，按 5 / 10 / 20 秒退避，最多投递 3 次；同一 event id 在 Gateway 临时投影中必须幂等。
- RuntimeEvent 按单条 delivery 解码和处置，poison event 不得阻塞同批正常 event。Run 尚未 Seed 时保留 PEL；第 3 次仍无法投影可进入 DLQ，因为 PostgreSQL RuntimeEvent 是权威真源。
- command/runtime-event DLQ 去重 7 天、近似保留 10,000 条，仅保存 routing id、payload 指纹/大小和有界错误摘要；DLQ 写入失败必须维持原 PEL。
- Gateway consumer name 默认使用 hostname + pid，`JARVIS_GATEWAY_ID` 可显式覆盖；同一 consumer group 内的多个 Gateway 实例不得共享 consumer name。

#### Redis-backed RuntimeBus 接线约定（2B-2a / 2B-2b）

`RedisRuntimeBus`（`apps/gateway/internal/orchestrator/redis_runtime_bus.go`）是 Redis-backed RuntimeBus 的接线骨架。它组合 `InMemoryRuntimeBus`（临时 state owner）+ `RedisRuntimeTransport`（Redis 通信）+ 内部 `traceIDs` 映射，同时实现 `RuntimeBus` 和 `RuntimeStateStore` 接口。

**方法映射与职责：**

| 方法 | 实现策略 | Redis 通信 |
|------|----------|------------|
| `PrepareRun` | in-memory **最小初始状态**（仅 `task.created`，run.Status="queued"）→ 存储 trace_id → 构造 `RunJobMessage` → Redis `EnqueueRunJob` | StreamRunQueue |
| `GetEvents` | 从 Storage 初始快照与 EventPump 已接收事件中读取 | 无 |
| `ResolvePermission` | **原子 reserve pending → 构造命令（复用 trace_id）→ Redis `PublishPermissionDecision` → 成功 ack / 失败 restore** | StreamWorkerCommand |
| `GetRun` / `GetTask` / `ListTasks` / `UpdateRunStatus` | 委托 in-memory（临时 state owner） | 无 |

**最小初始状态（PrepareRun）：**

- `RedisRuntimeBus.PrepareRun` 只用 `InMemoryRuntimeBus.PrepareMinimalRun` 创建 task/run 和一个 `task.created` 事件
- **不生成**以下 worker 事件：`model.delta`、`model.call.completed`、`tool.call.*`、`artifact.created`、`agent.run.completed`、`agent.run.started`、`agent.step.*`
- run.Status 为 `"queued"`（表示已入队 Redis，等待 worker 消费）
- `InMemoryRuntimeBus.PrepareRun` 的确定性事件只用于显式测试模式，不是默认产品路径

**权限决策（ResolvePermission）：原子 reserve → publish → ack / restore**

流程：`ReservePermissionRequest`（原子占用）→ `PublishPermissionDecision`（Redis 下发）→ 成功则 `CommitPermissionDecisionAckFromReserved`（ack），失败则 `RestorePermissionRequest`（恢复 pending）

- `ReservePermissionRequest`：写锁内查找 + 删除 + 深拷贝。并发请求中只有一个能成功 reserve，防止重复 publish
- `PublishPermissionDecision`：构造命令（trace_id 与 RunJobMessage 一致）写入 Redis `StreamWorkerCommand`
- `CommitPermissionDecisionAckFromReserved`：使用 reserved permReq 生成并追加 `permission.resolved` 确认事件
- `RestorePermissionRequest`：publish 失败时恢复 pending 到 map，允许重试
- **不调用 `CommitResolvePermission`**（后者会生成 tool/step/run 完成事件，仅供 in-memory/mock 路径使用）
- **不生成** tool.call.finished / tool.call.failed / agent.step.completed / agent.run.completed
- **不更新** run 状态为 completed
- **worker outcome 事件**后续必须由 Python worker 通过 RuntimeEvent 写入
- **并发重复确认最多只有一个请求能 publish**（reserve 原子性保证）
- **Redis publish 失败时 Restore 恢复 pending，允许用户再次提交同一个 request_id 重试**

**trace_id 连续性：**

- `PrepareRun` 生成 trace_id 存入内部 `traceIDs` map（`run_id → trace_id`）
- `ResolvePermission` 从 map 中查找该 run 的 trace_id，用于 `PermissionDecisionCommand.trace_id`
- `PermissionDecisionCommand.trace_id` 必须等于对应 `RunJobMessage.trace_id`
- trace_id 不存在时按运行契约生成并存储 fallback；恢复链路应优先从持久化状态读取

**错误语义：**

- `PrepareRun`：Redis `EnqueueRunJob` 失败时返回 error。task/run 只创建了最小初始状态（仅 task.created），调用方通过 error 获知 Redis 通信失败
- `ResolvePermission`：Redis `PublishPermissionDecision` 失败时返回 error，权限保持 pending 可重试
- `GetEvents` / StateStore 方法：不涉及 Redis，错误语义与 `InMemoryRuntimeBus` 相同

**核心约束：**

- **Redis 只承载 run queue / worker command / event stream，不是 Task / Run / Step / ToolCall / Permission / AuditLog 的业务真源。**
- `InMemoryRuntimeBus` 作为临时 state owner，未来由 Storage-backed StateStore 替代。
- 2B-2a 是接线骨架：不做后台 goroutine、不做 event fan-out、不连接真实 Redis。
- 真实 Redis 是默认运行路径；in-memory 只在显式测试配置下启用。
- `RunJobMessage` 必须携带 `job_id` / `trace_id` / `task_id` / `run_id` / `user_goal` / `workspace_path` / `created_at` / `schema_version`。
- `PermissionDecisionCommand` 必须携带 `command_id` / `trace_id` / `request_id` / `task_id` / `run_id` / `decision` / `note` / `decided_at` / `schema_version`。
- trace_id 在 `PrepareRun` 时生成并存储，`ResolvePermission` 时复用，保持一致。

#### Gateway Runtime Bus 配置开关（2B-2b）

`apps/gateway/internal/orchestrator/factory.go` 提供配置读取和工厂函数。`internal/app/app.go` 通过环境变量选择 runtime bus 实现。

**环境变量：**

| 环境变量 | 默认值 | 说明 |
|----------|--------|------|
| `JARVIS_RUNTIME_BUS` | `redis` | runtime bus 类型：默认 `redis`，显式测试可用 `inmemory` |
| `JARVIS_REDIS_ADDR` | `127.0.0.1:6379` | Redis 服务地址 |
| `JARVIS_REDIS_PASSWORD` | （空） | Redis 认证密码（可选） |
| `JARVIS_REDIS_DB` | `0` | Redis 数据库编号（可选） |
| `JARVIS_GATEWAY_ID` | `gateway-<hostname>-<pid>` | Gateway Redis consumer name；多实例必须唯一 |

**工厂行为：**

- `NewRuntimeBus(cfg)` → `(RuntimeBus, RuntimeStateStore, PumpCloser, error)`
- `inmemory` → `NewInMemoryRuntimeBus()`，不需要 Redis
- `redis` → 创建真实 `go-redis` client → `GoRedisStreamClient` → `RedisRuntimeTransport` → `RedisRuntimeBus`
- 非法 `JARVIS_RUNTIME_BUS` → 启动失败并输出清晰错误
- Redis 连接失败（PING 超时 2s）→ 启动失败

**约束：**

- 默认连接 Redis；连接失败时启动失败，避免静默落入 mock/in-memory 链路
- redis 模式只写 run queue / worker command
- Python worker（切片 3A，`apps/agent-worker/`）负责消费 run queue 并生成 RuntimeEvent
- Redis 仍只是 runtime bus，不是业务真源

#### Gateway Event Fan-out（2B-2c）

`EventPump`（`apps/gateway/internal/orchestrator/event_pump.go`）是 redis 模式下的后台事件泵，从 Redis runtime event stream 读取 worker 产生的 `RuntimeEventEnvelope`，解码校验后追加到 `InMemoryRuntimeBus`，使 SSE 能通过 `GetEvents` 读取到 worker 事件。

通过 Python Control Plane 创建任务时，Gateway 必须在 worker 事件可能到达前，调用 runtime 的 `SeedAcceptedRun`，把 Control Plane 返回的权威 Task / Run / 初始事件写入实时内存投影。该步骤只建立 SSE 投影，不写 PostgreSQL、不生成 ID、也不重复入队；缺少该步骤会使 EventPump 暂时无法建立实时投影，事件必须保留在 PEL 并按退避策略重试，不能直接 ACK 丢弃。

**数据流：**

```
Redis StreamRuntimeEvent
  → stale PEL: XPENDING + XCLAIM / new: XReadGroup ">"
  → RuntimeEventReader.ReadDeliveries（逐条解码 RuntimeEventEnvelope）
  → InMemoryRuntimeBus.AppendRuntimeEvents（按 event id 幂等追加）
  → 每条成功后单独 XAck
  → GetEvents(runID) 可见
  → SSE SubscribeEvents 推送
```

**EventPump 生命周期：**

- `Start()`：幂等创建 consumer group（`XGroupCreateMkStream`，startID=`"0"`），启动后台 goroutine 轮询读取
- `Close()`：取消内部 context，等待 goroutine 退出
- `PumpCloser` 接口（`bus/bus.go`）定义 `Start() / Close()`
- `InMemoryRuntimeBus` 通过 `PumpCloser` 接口暴露生命周期
- inmemory 模式下 `PumpCloser` 为 nil，不启动 pump

**读取策略：**

- 每秒优先接管至多一条 stale PEL，再通过 `XReadGroup` id=`">"` 读取新消息；新消息单次最多 32 条
- 读取失败：指数退避（100ms → 5s），记录日志
- 空读取：50ms 间隔后继续（避免 tight loop）
- 每条 delivery 独立解码和 ACK；确定性非法消息原子写入 RuntimeEvent DLQ 并 ACK，不阻塞同批正常消息
- run 不存在：保留 pending，按 5 / 10 / 20 秒退避接管；第 3 次仍无法投影时进入 DLQ
- 临时投影以 event id 幂等，ACK 结果不确定或 stale reclaim 不得重复追加事件

**新建接口/方法：**

| 位置 | 方法 | 说明 |
|------|------|------|
| `bus/bus.go` | `PumpCloser` 接口 | `Start() error` + `Close() error` |
| `bus/in_memory_bus.go` | `AppendRuntimeEvents(runID, events)` | 深拷贝追加事件 |
| `redis/reader.go` | `XGroupCreateMkStream` / `XPending` / `XClaim` / `MoveToDeadLetter` | 消费组与 PEL/DLQ 可靠性原语 |
| `bus/backoff.go` | `EventPumpBackoff` 接口 | `Reset()` + `Wait(ctx)`，可注入测试 |
| `bus/event_pump.go` | `eventPump` 结构体 | `Start`/`Close`/`loop`/`runOnce` |

**约束：**

- Go Gateway 只做读取、校验、缓存/扇出，不生产 worker outcome
- SSE endpoint path 不变；RuntimeEvent 只增加可选 `sequence`，旧客户端可继续兼容

**SSE 持续推送（2B-2c 审查修复）：**

- `SubscribeEvents` 不再只发送初始快照
- Phase 1：把 PostgreSQL durable 历史与 Gateway 内存中的低延迟事件按 Runtime 产生时间稳定合并，按
  event.id 去重后发送；两条 durable 事件时间相同时再以 `sequence` 排序，时间完全相同的终态事件置后。
  禁止固定拼接为“全部 PostgreSQL 历史 + 全部内存事件”，否则恢复前的 ephemeral `model.delta` 会被
  错放到恢复终态之后
- Phase 2a：300ms ticker 轮询 `RuntimeBus.GetEvents`，基于 event.id 去重，低延迟推送 Redis
  实时投影中的新增事件
- Phase 2b：Control Plane 模式下每 2 秒读取一次 PostgreSQL 权威 Run 历史，以 1 秒超时为界，按
  event.id 补偿合并实时投影遗漏的 durable 事件；一次补偿读取失败不得关闭已建立的 SSE，后续周期继续
- Redis/InMemory 只是实时投影，不得因其短暂漏失而要求用户刷新页面才能看到权限恢复、工具结果或 Run
  终态；PostgreSQL 补偿不得重复发送已通过实时投影到达的事件
- `agent.run.completed/failed/cancelled` 是单个 Run SSE 的业务终态栅栏；客户端已经观察到终态后，迟到的
  内存事件可以保留在诊断总线，但不得再次发送到该 SSE、重开正文或 Timeline
- Gateway 默认且仅允许监听 loopback IP（`127.0.0.1` / `::1`）；当前没有远程认证边界，禁止以 `0.0.0.0` 或局域网地址提供 API。SSE 继承 Gateway CORS 白名单，不得自行返回 `Access-Control-Allow-Origin: *`
- 客户端断开（`r.Context().Done()`）时退出
- 不再无条件把 run 标为 `completed`（run 状态由 RuntimeEvent / worker / Storage 切片驱动）
- 不直接依赖 Redis 或 EventPump 实现细节

**SSE 客户端消费约定（2B-2c 复审修复）：**

- 前端收到 terminal RuntimeEvent（`agent.run.completed`、`agent.run.failed`、`agent.run.cancelled`）后，应主动关闭该 run 的 EventSource（`unsubscribe(runId)`），避免多任务后无意义连接堆积
- 前端不凭空推断任务成功/失败/取消，只基于 terminal RuntimeEvent 关闭订阅
- 保留按 event.id 去重逻辑；权限决定的即时 acknowledgement 与后续 durable
  `permission.resolved` 还须按 `payload.request_id` 去重，关闭前的重复事件不会导致副作用
- 瞬时网络错误或 Gateway 重启时，前端不得在 `EventSource.onerror` 中主动关闭连接；应允许浏览器携带 `Last-Event-ID` 自动重连
- 前端 Event Stream Client 可向 Frontend State 投影
  `connecting | open | reconnecting | closed` 连接生命周期；该状态只描述事件连接，不改变
  `AgentRunStatus`，也不能作为 Task/Run 终态真源
- 用户从任务列表重新打开某个 run 时，前端应重建该 run 的 SSE 订阅，以触发 PostgreSQL 历史与 Redis 实时事件的恢复合并

**Graceful shutdown（2B-2c 审查修复）：**

- 使用 `http.Server` 替代裸 `http.ListenAndServe`
- 设置 `ReadHeaderTimeout=5s`、`IdleTimeout=60s`、`MaxHeaderBytes=1MiB`；不设置全局 `WriteTimeout`，避免中断合法 SSE 流
- `signal.NotifyContext` 监听 `SIGINT` / `SIGTERM`
- 收到信号后：`server.Shutdown(ctx)`（10s 超时）+ `pump.Close()`
- 避免 goroutine 泄漏

**约束不变：**
- 不创建 consumer group 重型治理逻辑（仅最小幂等 `XGroupCreateMkStream` + BUSYGROUP 处理）
- consumer group startID=`"0"`，确保 Gateway 重启后能消费已有消息
- 不在 Go Gateway 生成 mock worker outcome
- 不修改前端 UI
- 切片 3A 已提供 Python agent-worker（`apps/agent-worker/`），可消费 run queue 并写入 RuntimeEvent 到 stream

#### Worker Heartbeat + Gateway Status API (3B)

**Heartbeat 数据流：**

```
Python Worker HeartbeatProducer
  → XADD jarvis:stream:worker-heartbeat
  → HeartbeatReader.ReadHeartbeats（解码 WorkerHeartbeatMessage）
  → HeartbeatPump（后台 goroutine，非阻塞轮询）
  → WorkerStatusView.UpdateFromHeartbeat（in-memory 缓存）
  → GET /api/runtime/workers（HTTP API）
  → Web UI AppHeader 轮询展示
```

**WorkerHeartbeatMessage 约束：**

- 心跳是状态探针，不属于 command / event 链路，**不携带 trace_id**
- `worker_id` / `status` / `reported_at` / `schema_version` 必须非空；`worker_kind` 只允许 `agent | rag`，旧消息缺省为 `agent`
- Agent Worker 的 `active_run_id` busy 时非空，idle 时为空；RAG Worker 不使用该字段
- XADD fields 格式：`schema_version` + `payload`（完整 JSON）+ 冗余标量路由字段（`worker_id` / `type` / `status` / `reported_at`），无 `trace_id`

**Worker 状态流转：**

```text
starting → idle → busy → idle → draining → stopped
                       ↓
                     failed → idle
```

- 启动时发布 `starting`
- 等待任务时周期性发布 `idle`
- 处理 job 前发布 `busy`（`active_run_id=job.run_id`）
- job 完成并 ack 后发布 `idle`
- 收到 shutdown signal 后发布 `draining`
- 退出前尽力发布 `stopped`
- claim/Storage 暂时失败时不 ACK，保留 PEL 并在退避后重试；达到交付上限后按 DLQ 契约失败收口
- Agent 执行失败时先持久化 `agent.run.failed`，成功后 ACK 原消息；Worker heartbeat 短暂发布 `failed` 后恢复 `idle`

**Heartbeat 配置：**

| 环境变量 | 默认值 | 说明 |
|----------|--------|------|
| `JARVIS_WORKER_HEARTBEAT_INTERVAL_MS` | `3000` | 心跳发布间隔（毫秒），最小 100ms |

**Gateway WorkerStatusView：**

- 按 `worker_id` 维护最新心跳状态
- `last_seen_at` 由 Gateway 在收到心跳时设置（`time.Now()`，亚秒精度）
- `is_stale` 由 Gateway 计算：`time.Since(lastSeen) > staleTimeout`，默认阈值 9s（interval × 3）
- Gateway 重启后可从 stream 读取已有心跳，但 Redis 不是业务真源
- InMemory 模式下不启动 Redis heartbeat pump，WorkerStatusView 为空

**HTTP API：**

`GET /api/runtime/workers`

返回 `ApiResult<WorkersOutput>`：

```ts
type WorkerStatusDTO = {
  worker_id: ID;
  status: string;          // starting | idle | busy | draining | stopped | failed
  active_run_id: string;
  reported_at: ISODateTime;
  last_seen_at: ISODateTime;
  is_stale: boolean;
  /** 模型配置状态（Phase 6B-1），来自 heartbeat */
  model?: {
    provider: string;            // "deepseek" | "custom_openai_compatible"
    protocol: string;            // 底层 API 协议
    model_name: string;
    api_key_configured: boolean;
    thinking_mode: string;
    status: string;              // "mock" | "configured" | "not_configured"
    last_error_code: string | null;
  };
  /** Worker 进程启动以来的 Runtime Bus 累计指标；不是业务真源 */
  runtime_bus?: {
    reclaimed: number;
    retry_deferred: number;
    dead_lettered: number;
    malformed: number;
    command_reclaimed: number;
    command_dead_lettered: number;
    command_malformed: number;
  };
};

type WorkersOutput = {
  workers: WorkerStatusDTO[];
};
```

- InMemory 模式下返回空列表 `{"workers": []}`
- 前端每 5s 轮询此 API，不直接访问 Redis heartbeat stream

#### Runtime Health 只读投影

`GET /api/runtime/health` 返回 `ApiResult<RuntimeHealthDTO>`。Gateway 通过专用 Redis diagnostics adapter 读取 consumer group 治理元数据；Handler、Web 和 Python Control Plane 不直接访问 Redis。

```ts
type RuntimeHealthDTO = {
  status: "healthy" | "degraded" | "unavailable";
  runtime_bus: "redis" | "inmemory";
  generated_at: ISODateTime;
  workers: { total: number; online: number; busy: number; stale: number };
  streams: Array<{
    name: "run_queue" | "worker_command" | "runtime_event";
    stream: string;
    consumer_group: string;
    available: boolean;
    lag: number;                 // Redis 无法计算时为 -1
    pending: number;
    consumers: number;
    oldest_pending_ms: number;  // 无 pending 时为 0
    error_code?: string;
  }>;
  dead_letters: Array<{
    name: "run_queue" | "worker_command" | "runtime_event";
    stream: string;
    count: number;
  }>;
  counters: RuntimeHealthCountersDTO;
  warnings: string[];
};
```

- `healthy/degraded/unavailable` 只描述 Runtime Bus 运行健康，不参与 Task/Run 业务状态判断。
- 无在线 Worker、stream/group 不可用、lag 或 pending 非零时返回 `degraded`。
- DLQ 只返回长度；接口不得读取、拼装或返回原始 payload、用户目标、路径、工具参数或密钥。
- 当前接口只读，不提供 DLQ 删除、重放或自动修复。

#### PostgreSQL 业务真源只读对账

`GET /api/runtime/storage-reconciliation?limit=50` 返回
`ApiResult<StorageReconciliationDTO>`。`limit` 默认为 50、范围为 1-100。Gateway 不访问数据库
或 Artifact 文件，只代理 Python Application Service 的结构化白名单结果。

```ts
type StorageReconciliationIssueDTO = {
  code: string;
  severity: "warning" | "error";
  entity_type: "task" | "run" | "step" | "artifact";
  entity_id: ID;
  summary: string;
  task_id?: ID;
  run_id?: ID;
};

type StorageReconciliationDTO = {
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
```

- Python 内部入口为 `GET /internal/runtime/storage-reconciliation?limit=50`，返回同一 `data` shape。
- 当前规则核对 active Task/Run 状态、RuntimeEvent sequence/终态、Step 引用、最终 Artifact
  双向引用和外置文本 Artifact 完整性。
- 单次最多返回 200 条异常；达到 Run 扫描范围或异常展示上限时 `truncated=true`。
- 响应禁止包含 Artifact 文件路径/正文、用户目标、Workspace 路径、工具参数、异常堆栈或密钥。
- 本接口只读；`degraded` 不改变任何业务状态，也不授权自动修复。

##### 缺失终态事件 L3 受控修复

仅支持以下三步：

```text
POST /api/runtime/storage-reconciliation/repairs/inspect
POST /api/runtime/storage-reconciliation/repairs/requests
POST /api/runtime/storage-reconciliation/repairs/requests/{request_id}/resolve
```

前两者 body 为 `{ run_id: ID }`；resolve body 为
`{ decision: "allow_once" | "deny", note?: string }`。检查结果包含 `eligible`、
`reason_code/reason`、`task_id/run_id`、`expected_event_type`、固定 `risk_level="L3"` 与
`allowed_decisions`。创建请求返回现有 `PermissionRequestDTO`；resolve 返回 request 以及批准
时的 `repaired_event_id/repaired_event_type`。

- 只允许 failed Run 补写 `agent.run.failed`；必须存在 `failed_at` 和安全 error，已有 sequence
  连续且不存在任何终态事件。
- 对账必须区分 0 个期望终态事件的 `TERMINAL_EVENT_MISSING` 与多个期望终态事件的
  `TERMINAL_EVENT_DUPLICATE`；后者只诊断，不得展示或开放修复入口。
- 批准前必须在同一事务重新检查；批准原子写 RuntimeEvent、Outbox、PermissionRequest 与
  AuditLog。拒绝不写 RuntimeEvent，但仍更新 PermissionRequest 并写 AuditLog。
- 不修改 Run/Task/已有事件，不允许批量、自动或永久批准，不接受客户端提交 event payload。
- 其他对账错误没有执行入口。

#### DLQ 脱敏诊断查询

`GET /api/runtime/dead-letters` 返回 `ApiResult<ListRuntimeDeadLettersOutput>`，只允许查询 Gateway 固定映射的一个 DLQ stream。

查询参数：

- `source=run_queue|worker_command|runtime_event`，默认 `run_queue`。
- `limit=1..50`，默认 `20`。
- `before=<redis-message-id>` 为倒序分页游标，不接受任意 Redis range 表达式。
- `error_code` 为大写错误码精确匹配；`task_id`、`run_id` 必须是 UUID 并精确匹配。

```ts
type RuntimeDeadLetterDTO = {
  id: string;
  source: "run_queue" | "worker_command" | "runtime_event";
  original_stream: string;
  original_message_id: string;
  consumer_group: string;
  delivery_count: number;
  reclaimed: boolean;
  error_code: string;
  error_message: string;       // 空白归一且有界，不是原始异常
  failed_at: ISODateTime;
  payload_sha256: string;
  payload_size_bytes: number;
  task_id?: ID;
  run_id?: ID;
};

type ListRuntimeDeadLettersOutput = {
  records: RuntimeDeadLetterDTO[];
  next_cursor?: string;
};
```

- Adapter 单次最多扫描 500 条诊断副本，避免筛选导致无界 Redis 扫描。
- 响应白名单不得出现 `payload`、`user_goal`、workspace path、工具参数、凭据或原始异常。
- 诊断查询接口保持只读；删除、原消息重放或自动修复不能扩展本接口。受控 retry 使用下述独立权限与审计契约。

#### DLQ 受控重试

Gateway 先按 `source + record_id` 从固定 DLQ stream 精确读取白名单证据，再把证据提交给 Python RunApplicationService。Web 不能提交 `task_id/run_id/error_code` 覆盖 Redis 记录，也不能提交 payload。

```text
POST /api/runtime/dead-letters/retry/inspect
POST /api/runtime/dead-letters/retry/requests
POST /api/runtime/dead-letters/retry/requests/{request_id}/resolve
```

前两个接口输入均为：

```ts
type DlqRetryRecordInput = {
  source: "run_queue" | "worker_command" | "runtime_event";
  record_id: string; // 精确 Redis message id
};
```

`inspect` 是只读资格核对，返回 `eligible/reason_code/reason/task_id/run_id/risk_level=L3/requires_confirmation=true/allowed_decisions=[allow_once,deny]`。仅当下列条件全部满足时 eligible：

- source 为 `run_queue`，DLQ 与 PostgreSQL Run 错误均为 `RUN_QUEUE_RETRY_EXHAUSTED`。
- Task/Run UUID 有效且关联一致；旧 Run 为 failed；Task 为 failed 且 active_run_id 仍指向旧 Run。
- 绑定 Workspace 仍 active 且 canonical path 未变化；legacy workspace path 仍通过当前 allowlist。

`requests` 创建确定性 ID 的持久化 `PermissionRequestDTO`：`tool_name=runtime.retry_failed_run`、risk L3、scope once，只允许 `allow_once/deny`。重复请求返回同一 PermissionRequest，不重复创建处置动作。

`resolve` 输入 `{ decision: "allow_once" | "deny", note?: string }`。服务通过行锁原子消费 pending 请求：

- `deny`：PermissionRequest → denied，写拒绝 AuditLog，不创建 Run。
- `allow_once`：再次核对上述权威条件；在同一事务中创建新的 queued AgentRun、更新 Task active_run/status、追加 `agent.run.retry_requested`、创建 `run.retry.requested` Outbox 和 AuditLog，并将 PermissionRequest → consumed。
- Outbox Publisher 把 `run.retry.requested` 投影为新的 `type=run.job`；user_goal、workspace_path、conversation_id 只从 PostgreSQL 读取。
- 原 Run、原 DLQ 记录和诊断指纹保持不变。Worker Command、RuntimeEvent、malformed 或状态不一致记录没有执行入口。

## Runtime Events

事件由 Python Agent Worker 产生并写入 Redis Runtime Bus，经 Go Gateway 校验、归一和扇出，再通过订阅接口推送给 Web UI。后续桌面端可通过 IPC adapter 复用同一事件语义。

```ts
subscribeRunEvents(
  runId: ID,
  handler: (event: RuntimeEvent) => void,
  onConnectionState?: (
    state: "connecting" | "open" | "reconnecting" | "closed"
  ) => void,
): Unsubscribe;
```

### Event Envelope

```ts
type RuntimeEvent = {
  id: ID;
  type: RuntimeEventType;
  task_id?: ID;
  run_id?: ID;
  step_id?: ID;
  sequence?: number;
  timestamp: ISODateTime;
  payload: Record<string, unknown>;
};
```

`sequence` 由 Gateway 从 PostgreSQL 权威 RuntimeEvent 历史投影，用于恢复、门禁和诊断时核对 durable
事件的严格单调性。`model.delta` 等只存在于实时内存投影的 ephemeral 事件可以省略该字段，客户端不得
因为缺少 `sequence` 丢弃它们，也不得跨 durable/ephemeral 事件直接比较 sequence。

### Event Types

```ts
type RuntimeEventType =
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
```

### Required Event Payloads

```ts
type TaskCreatedPayload = {
  task: TaskDTO;
  run: AgentRunDTO;
};

type StepPayload = {
  step: ExecutionStepDTO;
};

type ModelDeltaPayload = {
  step_id: ID;
  delta: string;
  accumulated?: string;
};

type ToolCallPayload = {
  tool_call: ToolCallDTO;
};

type PermissionRequiredPayload = {
  request: PermissionRequestDTO;
};

type PermissionResolvedPayload = {
  request_id: ID;
  decision: PermissionDecisionType;
  tool_call_id?: ID;
};

type PermissionExpiredPayload = {
  request_id: ID;
  tool_call_id?: ID;
  reason: "deadline_elapsed" | "timeout" | string;
  expires_at?: ISODateTime;
  permission_status: "expired";
};

type ArtifactCreatedPayload = {
  artifact: ArtifactDTO;
};

/** Phase 6B-2: Model Call Observability */
type ModelCallStartedPayload = {
  provider: string;      // "deepseek" | "custom_openai_compatible" | string
  model_name: string;    // 模型名
  call_id: string;       // 本次调用唯一标识
};

type ModelContextPreparedPayload = {
  provider: string;
  model_name: string;
  fingerprint: string;            // 上下文包 SHA-256，不含正文
  action_mode?: "normal" | "finish_only" | "tool_required"; // v15+；旧事件可缺失
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
  message_count: number;
  truncated: boolean;
};

type ModelCallCompletedPayload = {
  provider: string;
  model_name: string;
  call_id: string;
  duration_ms: number;          // 调用耗时
  finish_reason: string | null; // 当前为 null（后续可扩展）
  action_type: string;          // "finish" | "tool_call" | string
};

type ModelCallFailedPayload = {
  provider: string;
  model_name: string;
  call_id: string;
  duration_ms: number;
  error_code: string;    // 如 MODEL_TIMEOUT / INVALID_AGENT_ACTION / REQUIRED_TOOL_EVIDENCE_MISSING
  recoverable: boolean;  // 是否可重试
  output_failure_kind?: string; // MODEL_OUTPUT_INVALID 的安全失败分类，不含模型原文
  attempt_count?: number;       // Provider 本次调用实际尝试次数，正整数
  validation?: {
    validator_id: string;       // Runtime 固定 validator id
    reason_code: string;        // Runtime 固定原因枚举
    rejection_count: number;
    max_rewrites: number;       // answer_rewrite 为 1；tool_planning 为 0
    rewrite_available: boolean;
    recovery_mode: "answer_rewrite" | "tool_planning" | "none";
    coverage?: {                // 只含固定 schema、计数和 complete；不得含路径/正文
      schema?: string;
      required_endpoint_count?: number;
      covered_endpoint_count?: number;
      required_stage_count?: number;
      covered_stage_count?: number;
      required_evidence_slot_count?: number;
      covered_evidence_slot_count?: number;
      unique_source_paths?: number;
      complete?: boolean;
    };
    answer_denied_global_coverage?: boolean;
    uncertainty_clause_count?: number;
  };
  navigation_guard?: {
    policy_version: "source-navigation-v5";
    reason_code: "REPEATED_SOURCE_ACTION" | "DISCOVERY_NO_PROGRESS" | "COVERAGE_BUDGET_AT_RISK";
    tool_class: "discovery" | "read" | "navigation";
    missing_slot_count?: number;
    proposed_slot_count?: number;
    proposed_missing_slot_count?: number;
    discovery_count_since_read?: number;
    productive_discovery_count?: number;
    nonprogress_discovery_streak?: number;
    unique_candidate_count?: number;
    has_actionable_candidates?: boolean;
    remaining_call_count?: number;
    coverage_budget_threshold?: number; // missing_slot_count * 2
    coverage_budget_at_risk?: boolean;
    // 禁止出现 path/query/arguments/model output/feedback/source excerpt
  };
};
```

真实 AgentRunner 产生的 `tool.call.started / finished / failed` 必须携带同一个 `tool_call.id`，并保持 `run_id / step_id / tool_name / provider / risk_level / arguments / permission_status` 等 ToolCallDTO 核心字段一致。前端可将同 id 的事件聚合成一张工具卡；不得依据事件顺序猜测另一个工具调用。

`tool.call.finished` 只用于成功执行且 `tool_call.status="completed"`；工具执行错误和权限拒绝均使用 `tool.call.failed` 且 `tool_call.status="failed"`。

**model.call.* 事件语义：**

- `model.call.started`：AgentRunner 即将调用 ModelProvider。
- `model.call.completed`：ModelProvider 已返回 action，且该 action 已通过 AgentRunner 基础字段校验。
  对 finish 来说，final_message 已通过非空字符串校验并规范化为面向用户的 CommonMark Markdown；
  Runtime 只允许确定性解开一层重复的 finish AgentAction 包装或整段 markdown fence，不修改普通
  JSON/代码内容。可选 `citations` 只能包含
  `{"chunk_id": string}`，`insufficient_evidence` 必须是 boolean。
  对 call_tool 来说，tool_name 已通过非空字符串校验，arguments 已通过 dict 校验。
- `model.call.failed`：模型调用失败、provider 异常、模型输出不可解析、返回值不是 AgentAction，
  或 action 字段级校验失败。当 action 字段级校验失败时，error_code 使用 `INVALID_AGENT_ACTION`。
  **action 字段级校验失败时不得发布 model.call.completed，应发布 model.call.failed，然后进入 agent.run.failed。**
  当 finish 字段合法但缺少用户明确指定工具，或缺少版本化 Intent 策略要求的当前 Run 成功工具证据时，使用
  `REQUIRED_TOOL_EVIDENCE_MISSING`；budget 未耗尽时 `recoverable=true` 并允许 AgentRunner
  重新决策，耗尽时进入 `agent.run.failed(REQUIRED_TOOL_NOT_EXECUTED)`。该路径同样不得发布
  `model.call.completed` 或 `agent.run.completed`。
- 必需工具已有失败 ToolResult 时，终态必须保留该 ToolError 的 code/category/message，不能使用
  `REQUIRED_TOOL_NOT_EXECUTED`。相同失败工具与规范化参数在没有成功状态变化时再次出现，使用内部
  `REPEATED_FAILED_TOOL_ACTION` 模型失败证据并以原 ToolError 收口，不生成第二个 PermissionRequest。
- 工具调用预算耗尽后允许一次仅用于 `finish` 的证据收口；若模型仍请求工具，发布
  `model.call.failed(error_code=PLANNING_TOOL_BUDGET_EXHAUSTED)` 并以
  `agent.run.failed(code=MAX_ITERATIONS)` 终止。
- FinalAnswerValidator 的重写预算与工具调用预算分离。首次拒绝固定发布
  `model.call.failed(error_code=FINAL_ANSWER_VALIDATION_FAILED, recoverable=true)`，保存
  `resume_node=call_model` checkpoint，并强制下一轮使用 finish-only 协议；该轮只能重写最终回答，不能执行
  工具。最多允许一次重写，第二次仍不通过时发布不可恢复失败。`validation` 与 terminal
  `AppError.details.answer_validation` 只允许固定 validator/reason、次数、布尔值和覆盖计数，禁止保存被拒绝
  的回答、校验反馈正文、动态路径、源码或 Prompt。
- 纯历史回答转换任务中的任何 `call_tool` 在执行前以
  `model.call.failed(error_code=HISTORY_TRANSFORM_TOOL_FORBIDDEN)` 拒绝；首次为可恢复的 finish-only
  重写，耗尽后同码失败关闭。该约束不适用于用户同时明确要求保存、写入、发送等外部副作用的任务。
- `intent-llm-v7` 的 `retrieve/required` 都要求当前 Run 先产生成功 `rag.search` observation；用户
  不需要在问题中显式写出“RAG”或“向量数据库”。`document_scope=selected` 时，模型只能选择 Runtime
  提供的匿名 `doc_N` 键；目录项只暴露标题、创建时间和最多 600 字符的首 Chunk 身份摘要，数据库
  UUID 不进入 Intent Prompt。用户问题中唯一命中的书名号标题、强身份英文名或 arXiv 编号与所选键
  不一致时，Parser 必须拒绝候选；普通查询词不属于身份。强身份命中多份同名文档时，Parser 将
  `selected` 安全降级为 `unresolved`；AgentRunner 必须覆盖模型提交的 `document_ids`。`unresolved`
  必须要求用户按标题、来源、版本或上传时间澄清，不能索要 UUID/内部参数、猜测相近文档或退化为
  `all`。Intent 只进入 Worker 的可恢复
  状态与模型上下文，不作为前端自行推断任务状态的新 DTO；实际检索仍必须产生标准 ToolCall、
  AuditLog 和 RuntimeEvent。
- 对上一轮引用的序数核验必须把“第 N 个引用”解析为最近可信 assistant citation 投影，并与冻结文档目录
  交叉验证；解析成功写入 `selected + resolved_document_ids`，失败写入 `unresolved`，禁止写成
  `document_question + all`。新会话面对多个文档时，“这份/那份/刚才那份手册、论文或文档”没有历史绑定
  也必须 `unresolved`，并返回 Host-owned 文档澄清，不调用 RAG 或 Workspace 工具。Host 仲裁后的完整
  Intent 必须通过 `IntentExtraction.from_state_dict` 等价校验后才可进入 checkpoint。
- v7 的严格
  `workspace={evidence,action,ambiguity,listing_entry_types,reason}` 使用
  `evidence=skip|metadata|required`。只列目录/文件名称、判断路径存在或读取类型、大小时必须配
  `read + metadata`，并在 finish 前产生成功 `workspace.list_files` 或 `workspace.get_file_info`；需要打开、
  搜索或审查正文时必须配 `read + required`。普通全文阅读、总结与审查必须产生成功
  `workspace.read_file` 或 `workspace.read_files`；只有用户明确要求按正文查询词定位哪些文件/行时，成功
  `workspace.search_text` 才能满足该有限搜索事实。两类证据不得
  混用，且证据满足前不得向 Renderer 发布 `model.delta`；`write/destructive`
  分别要求成功的 Workspace 写工具/删除工具结果。`clarification_required` 只允许只读探查，并把非
  L0 动作确定性改为澄清回复，不发布 permission.required。若 LLM 候选把澄清后才可能需要的
  `metadata|required` 与 `write|destructive + clarification_required` 混合，Intent parser 必须先归一化为
  `evidence=skip`；不得因此执行读取后自动选定副作用目标，也不得放宽 ToolGateway 或权限边界。旧 v3
  旧 Intent state 不能被 v7 checkpoint 恢复逻辑默默补造语义；`intent-llm-v5` 只允许按缺省空
  `listing_entry_types` 兼容恢复，v6 按完整 Workspace shape 恢复。用户明确删除唯一具名目录及其全部
  内容时，Runtime 可把过度保守的 `destructive + clarification_required` 窄化为 `destructive + clear`；
  该裁决不生成路径参数、不授予权限，也不改变 `workspace.delete_path` 的非递归执行契约。删除目录中的
  重复/旧/未决候选、指代路径、glob 或越界路径不得使用该例外。明确的单一 Workspace 副作用同时形成
  host-owned 交叉校验：当 schema 合法的 LLM 候选仍为 `unknown` 或
  `clarification_required`，而原始目标能被安全 effect classifier 唯一识别为受支持的明确相对路径动作时，
  Runtime 只纠正 `primary_intent/workspace` 投影；不得覆盖候选的 RAG/Knowledge 语义、生成工具参数或
  绕过权限。“只解释/只给建议”等已经明确为 `none + clear` 的候选不得被提升为 effect。明确副作用同时形成
  Runtime 拥有的必需工具契约；动作模型提出另一种 Workspace 副作用时，必须在
  `permission.required` 和 ToolGateway effect 之前发布
  `model.call.failed(error_code=REQUIRED_TOOL_ACTION_MISMATCH, recoverable=true)` 并重新规划。只读探查
  可以先执行；连续冲突耗尽预算则以同 code 不可恢复失败，且不得生成错误动作的 PermissionRequest。
  当原始目标还明确声明“其他文件不动/leave other files unchanged”，且只包含一个可归一化文件目标时，
  一个成功 Workspace effect 已耗尽该 Run 的副作用范围。后续任何 Workspace effect（即使工具参数或目标
  不同）都必须在授权前以 `WORKSPACE_EFFECT_SCOPE_SATISFIED` 要求模型收口；连续越界以
  `WORKSPACE_EFFECT_SCOPE_EXCEEDED` 失败关闭。该守卫不适用于多目标或未明确排他的任务。
- `workspace.listing_entry_types` 是有序去重数组，元素只能是 `file|dir|symlink|other`。非空时必须配
  `metadata + read + clear`；用户要求所有条目或当前任务不是目录列举时必须为空数组。该字段只约束
  model-visible observation 与最终回答，不裁剪持久化 ToolResult、ToolCall、AuditLog 或 RuntimeEvent。
  原始目标在同一自然语言分句中以“列出/有哪些/告诉我”等列举动词明确点名类型时，Intent Parser 必须
  拒绝与显式类型集合不一致的候选；匹配不得跨越逗号、句号等分句边界，也不得把“不要创建文件”等
  否定副作用提升为 `file` 投影。结构化 Intent 穷尽失败后的只读 fallback 必须复用同一类型提取 owner。
- 多材料/完整流程/逐步依据任务必须在 finish 前完成相关父目录枚举、非零命中的更广文件发现或正文搜索，
  并读取所有发现的文件候选。至少两个不同正文来源且无未读候选才能正常收口；确认确实只有一个来源时，
  还须有第二条不同语义的有界发现证据。`search_files` 的零命中只代表文件名未命中，不能充当正文范围耗尽
  证据。该约束只消费
  当前 Run 的成功 ToolResult，不把宿主绝对路径投影给模型，也不改变 ToolGateway、权限或 Storage 契约。
  ContextManager 按类型过滤模型可见 `workspace.list_files.entries`，并基于过滤后的条目重算模型可见
  `total_count`；原始 Observation 保持完整。内置 `workspace-listing-projection-v1` 在最终回答提及原始结果中
  被排除的条目名时拒绝 finish，统一发布 `FINAL_ANSWER_VALIDATION_FAILED`，最多进行一次 finish-only
  重写；反馈和持久化诊断不得包含动态文件名、路径或被拒绝回答。没有类型投影时必须保持现有完整列举语义。
- Workspace `read + metadata|required` 与可选 `retrieval.mode=retrieve` 同时出现时，完成门禁以
  Workspace 证据为准，不得额外强制 `rag.search`；只有 `retrieval.mode=required` 的明确文档依赖可与
  Workspace 证据形成双重门禁。
- `context-v12-memory-v1-skill-v1-intent-v4` 保留未来 Codex/Developer Agent 的可选源码调用链、数据流和
  owner 边证据协议。只有 Agent composition 显式注册 `WorkspaceSourceChainCoverageValidator` 时，规划阶段
  才启用以下协议；基础 Personal Agent 始终启用与产品无关的
  `WorkspaceListingProjectionValidator`，生产容器另注册 `RagCitationValidator`，但不得发出源码专用
  补证模式、导航 Guard 或终态错误。扩展启用后，规划阶段必须
  先拆分起点、跨层交接和终点，并优先从起点与终点两侧取证；接口、DTO、adapter、helper、service 或方法
  定义只能证明局部职责，不能单独证明 caller/callee、producer/consumer 或 dispatcher/executor 关系。
  每条跨层边必须来自本次 Run 已成功读取正文中的调用点、发布/消费或 dispatch 证据；若终点外层循环或
  实际调用点没有被读取，正常阶段与 finish-only 阶段都必须明确标为未确认，不得根据相邻实现补全链路。
  Runtime 根据真实成功读取的源码扩展名自动启用该协议，不依赖模型是否设置 `source_only`。确定性 system
  反馈只包含覆盖数量、重复数量和阶段规则，不得包含动态路径或正文；当前 Run 的首尾不同源码路径及每条
  路径首个/最新片段进入 `workspace-source-evidence-ledger-v1`，以明确标记为不可信外部数据的 user message
  保留在上下文中，不能覆盖系统、安全或权限规则。账本最多保留 10 条不同路径，每个片段最多 600 字符；
  Context 预算不足时先移除 excerpt、保留路径/行范围元数据，仍不足时移除账本，不得挤掉系统规则、当前
  用户目标或最新完整 ToolResult。原始 ToolResult、Storage 和 AuditLog 仍是事实真源，账本不是第二真源。
  当用户目标同时明确要求源码/代码库证据、端到端/调用链关系并点名至少两个运行端时，Runtime 还必须生成
  `workspace-source-chain-coverage-v3`：读取路径的固定 endpoint taxonomy 只用于识别候选 owner，不能单独
  构成覆盖。每个必需端点还必须在本次 Run 成功读取正文中具有固定、表驱动的直接边证据：frontend 需要
  outbound request/call，Gateway 与 Control Plane 需要下游调用，Worker 需要真实 runner/executor/process
  调用；transport 必须同时具有 producer 与 consumer 证据。`agent-worker`、`jarvis_worker` 等项目/包容器
  名、无关前端页面、仅 claim/receive 的 Worker 外壳均不得冒充调用边。完整正文只在 Runtime 内用于确定性
  覆盖计算，不进入 Context ledger 或 answer metadata；反馈与 metadata 仍只暴露覆盖计数和固定 Runtime
  taxonomy 标签，不回显动态路径或正文。
  任一用户端点或中间阶段未覆盖时不得进入 finish-only；提前 finish 必须由
  `WorkspaceSourceChainCoverageValidator` 以 `SOURCE_CHAIN_EVIDENCE_INCOMPLETE` 拒绝。工具预算仍可用时，
  `validation.recovery_mode=tool_planning` 并进入 `tool_required` 动作模式，连续恢复最多两次且任一真实
  ToolResult 清零。该模式在 Provider parser 与 ModelCall 两层只接受 `call_tool`，非法 `finish` 使用既有
  有界结构化纠错，不消耗工具预算；可选工具、路径、query、参数和证据缺口顺序仍由模型决定，Runtime 不
  固定答案路径；
  工具预算耗尽时 `recovery_mode=none`，只失败一次，不占用答案重写。
  `workspace-source-chain-coverage-v4` 把确定性覆盖与回答中的不确定性分开：固定证据槽未闭合仍是硬失败；
  覆盖闭合后，具体、局部的未知项或证据限制允许进入 completed，并通过
  `scoped_uncertainty_count` 可观察。只有最终回答把局部未知扩大为“整条/完整/端到端调用链均未确认”，与
  Runtime 的完整覆盖状态形成全局矛盾时，才以 `SOURCE_CHAIN_GLOBAL_CONTRADICTION` 触发一次 finish-only
  重写。该规则不得迫使模型隐藏不确定性，也不得把用户要求“证据不足就明确说明”变成终态失败。
  v3 coverage 将缺口表达为 endpoint、transport producer 与 transport consumer 证据类别；
  `source-navigation-v5` 只把它们作为未覆盖集合，不再每轮开放唯一槽。Workspace source
  list/get-info/search/read 可以按任意顺序推进任一缺口或一次推进多个缺口，新的正文读取不能因为路径属于已
  部分覆盖组件而被拒绝。产生新源码候选的渐进式 discovery 不设固定两次上限；只有完全重复已经成功的同
  工具同参数动作，或已有候选时连续两次 discovery 都没有新增候选，才在 ToolGateway 前使用
  `model.call.failed(error_code=SOURCE_CHAIN_PLANNING_STALLED, recoverable=true)` 退回。连续无进展由独立
  `source_chain_guard_rejections` 计数，最多退回两次，第三次发布
  `SOURCE_CHAIN_NAVIGATION_STALLED(recoverable=false)` 并失败关闭；一次真实 ToolResult 清零连续计数。
  剩余工具调用数不大于“未覆盖类别数 × 2”时进入覆盖预算保护窗口，discovery 必须指向任一未覆盖类别，
  但类别顺序自由且允许批量补多个缺口。该规则不得绑定固定文件、符号、查询、答案路径或唯一活动槽；
  未分类的新正文读取仍允许进入 ToolGateway。
  退回事件可携带 `navigation_guard`，ExecutionStep/terminal AppError 可投影为
  `details.source_navigation`；持久化边界必须重新白名单化固定 policy/reason/tool class、计数和布尔值，
  严禁路径、query、arguments、模型输出、反馈或源码。旧 `source_chain_slot_attempts` 只为 checkpoint 向后
  兼容保留，不再影响导航决策。项目/包容器名可定位运行端但不能作为正文证据；Runtime 不代替模型选择路径
  或绕过 ToolGateway。单函数、单模块或没有明确多端点的普通源码问题不得被该硬门禁误拦。
  这些事件与诊断结构作为扩展兼容契约继续保留，但不参与当前基础产品 P0/P1/P2 门禁。
- `retrieval.query` 在 `retrieve/required` 时必须是非空有界字符串；`skip` 时允许空字符串，因为该分支
  不产生检索动作。Parser 与 checkpoint 恢复使用相同条件，纯知识库写入和仅 RAG 入库不得因空检索词
  被判为无效 Intent。
- Intent LLM 使用标准 `model.call.*`，`model.call.started.payload.purpose=intent_extraction`；持久化
  ExecutionStep metadata 保留该 purpose。结构候选失败使用 `INTENT_OUTPUT_INVALID`，Runtime 纠错耗尽
  后终态使用 `INTENT_EXTRACTION_FAILED`；固定 Registry 缺少所需能力使用
  `INTENT_CAPABILITY_UNAVAILABLE`。事件不得包含 Prompt、原始模型响应、文档 UUID 目录或校验字段值。
- 当前 Run 存在成功 `rag.search` observation 时，finish 还必须通过 `RagCitationValidator`：
  有证据的回答至少引用一个本次 ToolResult 中的 Chunk。若 Runtime 的
  `rag-evidence-sufficiency-v2` 判定不足，Validator 必须忽略模型的正文和 citations，生成当前有效语言的
  固定无证据答复，投影 `insufficient_evidence=true`、`citations=[]`、`safe_degradation=host_owned` 和
  `evidence_reason_code`；不得让无关引用、数字推测或 citation 格式失败成为用户终态。模型主动设置
  `insufficient_evidence=true` 时仍要求 citations 为空。只有最新一次成功 `rag.search` 的当前策略 assessment
  有效；缺失、schema 畸形、策略版本不匹配或只有旧 observation 携带 assessment 时必须安全降级。其余引用失败使用
  `FINAL_ANSWER_VALIDATION_FAILED`，并使用独立于工具预算的一次 finish-only 重写；第二次仍失败才进入
  `agent.run.failed`。
  Prompt 必须把当前 Run 全部成功 `rag.search` observation 中最近的至多 12 个可信 Chunk UUID 提升为动态
  引用契约；该清单随 Run 变化，不是固定答案路径。`final_message` 只能包含回答正文，模型主要通过结构化
  `citations.chunk_id` 选择证据，用户可见标题、页码和引用列表由 Runtime 从可信 ToolResult 恢复并统一
  渲染一次。若结构化 citations 已通过校验，Runtime 可以删除模型在正文末尾重复生成的“引用 / Sources /
  References”列表；删除后无正文则拒绝。citations 缺失时，只允许把正文中明确的单页或连续页码引用映射
  到本 Run 已检索 Chunk 的可信 `page_start/page_end` 区间；未检索页、无页码表述、跨 Run 身份或伪造 UUID
  仍必须失败关闭。该兼容路径在 `answer_validation.rag-citation-v1.citation_resolution` 记录
  `explicit_page_reference`；正常结构化 citations 不携带该字段。
- 用户可见引用格式固定为内部链接
  `/knowledge/rag?document_id=<document_uuid>&chunk_id=<chunk_uuid>`，两项身份必须来自通过本 Run 校验的
  `RagCitation`。标题和 location 仅是转义后的展示文本，不得成为路由身份。Web 只能用该链接定位、高亮
  文档和显示 chunk 身份，不能据此自行读取数据库或推断新的检索结果。
- RAG 引用拒绝的内部 `reason_code` 细分为 `RAG_CITATION_FORMAT_INVALID`、`RAG_CITATION_MISSING`、
  `RAG_CITATION_DUPLICATE`、`RAG_CITATION_UNTRUSTED` 与 `RAG_CITATION_BODY_MISSING`。公共终态错误仍使用
  `FINAL_ANSWER_VALIDATION_FAILED`；细分原因用于有界重写反馈、测试和脱敏诊断，不改变 Web API 错误 shape。
- RAG/引用复核的通用最终回答拒绝原因另包含
  `FINAL_MESSAGE_INCOMPLETE`、`CITATION_VERDICT_CONTRADICTORY` 和 `CITATION_VERDICT_MISSING`；它们仍投影为
  `FINAL_ANSWER_VALIDATION_FAILED` 的有界重写，不新增公共错误 shape。引用标题的 Runtime 渲染还必须过滤
  超长或高比例纯数字/OCR 导航噪声；该过滤只影响用户可见 location label，不改写可信 source locator。
- 通用显式回答约束还包括 `ANSWER_LENGTH_LIMIT_EXCEEDED`、
  `BOUNDED_RETRIEVAL_DISCLOSURE_MISSING` 与 `FACT_INFERENCE_BOUNDARY_MISSING`。它们同样只进入一次
  `FINAL_ANSWER_VALIDATION_FAILED` 有界重写，不改变公共错误 shape；Runtime 不得用机械截断伪造合规，
  也不得把有界 top-k 召回提升成“全文无遗漏”。
- `MODEL_OUTPUT_INVALID` 可以附带 `output_failure_kind` 与 `attempt_count`，用于区分 JSON 语法、
  Schema、截断、空响应等失败。它们只能来自本地枚举/计数；不得包含原始响应、字段值或纠正 Prompt。
- `finish_only/tool_required` 的首次协议违反拥有独立的一次结构化纠错，不受传输层
  `max_retries=0` 禁用。`attempt_count` 必须计入该尝试；网络、超时和 HTTP 故障仍只遵循传输重试预算。
- 当且仅当 CompletionContract 唯一缺口是 RAG 证据、Intent scope 已验证为 `selected|all`、Registry 启用
  `rag.search` 且当前 Run 尚无成功检索时，`tool_required` 结构化纠错耗尽后可以从 Intent query 恢复一次
  `rag.search` AgentAction。`unresolved`、附加 required effect、已有成功 RAG 证据或任意其他工具都禁止
  Host 恢复；恢复动作仍走完整 ToolGateway/Permission/Audit/Event 链。
- `model.context.prepared.action_mode` 从 context v15 起固定为 `normal | finish_only | tool_required`；旧持久化
  事件允许缺失。该字段只描述 Runtime 动作协议，不包含工具参数、证据路径或回答内容。
- Provider 内部纠错耗尽后，Runtime 可以额外发布一次
  `model.call.failed(recoverable=true)` 并保存 `resume_node=call_model` checkpoint；该事件不是 Run 终态，
  前端必须继续等待后续 `model.call.*` 或 terminal event。第二次 Runtime 级失败为
  `recoverable=false` 并与不可恢复的 `agent.run.failed` 收口；terminal checkpoint 清空，Web 不得使用
  第一次可恢复事件展示失败步骤重试。

ModelProvider 传输边界不新增公共 DTO。Runtime 内部仍以原子
`assistant AgentAction -> tool ToolResult` 保存历史；当前自定义 JSON 决策协议在 OpenAI-compatible wire
上把已校验 action 作为 assistant content，把严格校验后的 ToolResult 包装为带
`[Runtime ToolResult]` 标签的 user data message。该标签表示 Runtime 观测而非用户新指令，内部
`result.data` 仍是不可信数据。生产请求没有声明供应商 `tools`，因此不得同时生成 provider-native
`tool_calls` / `role=tool`；适配器的显式 native history 分支不是 Web API、RuntimeEvent 或当前产品能力。

用户可见内容与内部结构必须分离：AgentAction 外层 JSON 不得进入 Message/Artifact 正文；Assistant
Message 以安全 Markdown 展示，完整 JSON 用户答案可作为独立 JSON 视图展示，ToolResult JSON 只进入
Timeline/Inspector 的结构化展示。前端不得用字符串替换猜测或清洗未知 JSON。

`artifact.created.metadata.answer_validation` 与
`agent.run.completed.payload.answer_validation` 是可选的 Runtime 可信投影。当前
`rag-citation-v1` 包含 `insufficient_evidence` 和已归一化 citations；其中 document/title/page/
artifact 信息必须来自成功 ToolResult，不能直接采用模型字段。消费者必须兼容该字段缺失。
`workspace-source-chain-coverage-v4` 包含固定 coverage 计数与 `scoped_uncertainty_count`；它不包含源码正文、
动态路径或被拒绝的答案。
当任一 `FinalAnswerValidator.requires_buffered_output()` 返回 true 时，本次模型调用不发布
`model.delta`；消费者不得把缺少 delta 视为模型失败，应以最终 Artifact/terminal event 为准。
检索意图为 `retrieve/required` 且当前 Run 尚无成功 `rag.search` observation 时，同样必须从模型调用入口
关闭流式发布；不能仅在 delta callback 中丢弃文本。

**model.call.* 安全约束：**
- 不得包含 API key
- 不得包含 prompt
- 不得包含 raw model response
- 不得包含 HTTP headers
- 不得包含用户完整上下文
- 错误信息不得透传底层异常原文

`model.context.prepared` 在 `model.call.started` 后、真实网络调用前发布并持久化。
它只包含预算、数量、裁剪状态、策略版本和不可逆 fingerprint，不得包含消息正文、
prompt、工具结果、用户历史或敏感配置。Inspector 只能消费该结构化统计，不得自行
推断实际上下文。

当本轮激活已安装 Skill 时，payload 可额外包含 `skill_id`、`skill_version` 和
`skill_fingerprint`；未激活时省略。三个字段均由 Python Runtime 生成，客户端不能提交。
`skill_fingerprint` 只标识本轮加载的包与引用集合，不包含 Skill 正文。

Skill 不拥有任务工作流阶段或工具可见性，因此该 payload 不包含 `skill_workflow_stage` 或
`visible_tool_count`。任务进度来自 ExecutionStep、ToolCall、Permission 和各领域状态；客户端不得
根据 Skill 名称推断研究、下载、知识写入或 RAG 状态。

**model.delta 语义与边界：**

- 仅在 Provider 已识别到结构化 `finish.final_message` 后发布；不得转发原始模型 JSON、
  tool arguments、prompt、历史上下文、HTTP headers 或 API key。
- `delta` 是单个有界文本片段；生产 Worker 不发送不断增长的 `accumulated`，前端应按
  `event.id` 去重后追加 `delta`。`accumulated` 仅为旧 mock / 历史事件兼容字段。
- `model.delta` 是临时事件，直接进入 Redis runtime stream，不写 PostgreSQL / Outbox；
  最终可恢复回复仍以 `agent.run.completed.payload.output` 和 Message 持久化为准。
- 若流在产生部分文本后失败，不重试以避免重复拼接；Run 以 `model.call.failed` 和
  `agent.run.failed` 显式收口，已显示的 partial 文本可作为运行时现场保留。

## UI Event Mapping

## MCP Server API（v1）

```text
GET   /api/mcp-servers
POST  /api/mcp-servers
POST  /api/mcp-servers/builtin/literature
PATCH /api/mcp-servers/{server_id}
POST  /api/mcp-servers/refresh
```

- 创建输入为 `slug/name/command/args/env_keys`；只接受 stdio，`command` 必须是本地绝对可执行文件。
- `builtin/literature` 是用户显式点击的一键注册入口，由 Python Control Plane 使用当前受信任
  Python executable 生成固定配置；客户端不能提交或覆盖其 command/args。
- PATCH 当前只修改 `enabled`，必须携带 `expected_version`。
- DTO 包含连接状态、`last_error_code` 和已发现工具；不返回任何环境变量值。
- 创建、启停响应使用 `worker_restart_required=true`。刷新返回
  `{command_id,status:"accepted",worker_restart_required:true}`，表示命令已进入 Redis，而不是发现已完成。
- MCP discovery 只由 Worker 执行；Control Plane 没有 refresh 执行端点。用户稍后重新读取列表
  查看 server 状态和工具清单，重启 Worker 后新清单才进入 Agent 的静态 Prompt/ToolRegistry 快照。
- Web 只能通过 Go Gateway 使用这些接口，不得直接连接 MCP server。

```text
task.created -> add task, open active run
agent.run.started -> show running state
agent.step.started -> append timeline item
model.delta -> update streaming text
tool.call.started -> append tool item
tool.call.finished -> mark tool item completed
mcp.call.started -> append MCP item
mcp.call.finished -> mark MCP item completed
permission.required -> open dialog and inspector permissions tab
permission.resolved -> close dialog and update timeline
artifact.created -> show artifact preview
agent.run.completed -> show final result
agent.run.failed -> show failure state; only terminal + latest model failure both recoverable show retry
```

## 自动化事件映射验收场景

以下事件序列由自动化测试夹具直接注入，用于验证 UI store、Timeline 和权限展示映射；它们不对应产品可调用的 Dev API。

### simple_success

```text
task.created
agent.run.started
agent.step.started user_message
agent.step.started model_call
model.delta x N
agent.step.completed model_call
agent.step.started final_output
artifact.created
agent.run.completed
```

### permission_required

```text
task.created
agent.run.started
tool.call.started
permission.required
permission.resolved
tool.call.finished
agent.run.completed
```

### tool_failed

```text
task.created
agent.run.started
tool.call.started
tool.call.failed
agent.step.failed
agent.run.failed
```

### multi_agent_success

```text
task.created
agent.run.started
agent.step.started plan_created
agent.step.started worker_run
agent.step.completed worker_run
agent.step.started review
agent.step.completed review
agent.step.started final_output
agent.run.completed
```

---

## Internal Python Control Plane API（当前实现）

> **当前真源**：Go Gateway 通过此 Internal API 完成短事务写入和历史查询；Python Worker 通过同一 Application Service / Repository 持久化 Runtime 状态。Go 不执行 SQL。

### 定位

Python Control Plane 是 Go Gateway 的唯一持久化入口。它是一个仅监听 `127.0.0.1` 的 FastAPI 服务，只处理短事务（创建任务、查询历史、取消、权限决定），不执行 AgentRun 长任务。

### 通用约定

- 所有 endpoint 使用 `/internal/` 前缀
- Content-Type: application/json
- Go client 必须带 context timeout（默认 10s）
- 不允许无限重试
- 错误不泄漏数据库连接串或敏感信息
- 所有 ID 使用 UUID 字符串

### 创建任务

```text
POST /internal/tasks
Content-Type: application/json
```

Request:
```json
{
  "user_goal": "列出当前目录文件",
  "workspace_id": "workspace-uuid",
  "conversation_id": null,
  "attachments": []
}
```

Response (200):
```json
{
  "task": { "id": "...", "title": "...", ... },
  "run": { "id": "...", "task_id": "...", "status": "queued", ... },
  "conversation": { "id": "...", ... },
  "message": { "id": "...", ... }
}
```

语义：
- 在同一个 PostgreSQL 事务中写入 Conversation、Message、Task、AgentRun（status=queued）、初始 RuntimeEvent、OutboxEvent
- 事务提交成功后才返回 "任务已接受"
- Outbox Publisher 异步将 RunJob 发布到 Redis
- PostgreSQL 不可用 → 503 + AppError(code=DATABASE_UNAVAILABLE)
- `workspace_id` 在 Task 事务内锁定并校验 active 状态、realpath 和安全策略；Task 与 RunJob 保存校验后的 `workspace_path` 快照
- 兼容请求可只发送 `workspace_path`；两者都为空时使用服务端默认工作区

### Workspace Registry

```text
GET    /internal/workspaces?include_revoked=false
POST   /internal/workspaces/pick
DELETE /internal/workspaces/{workspace_id}
```

- Control Plane 启动在接受请求前 await configured Workspace bootstrap。
- 注册通过 PostgreSQL `ON CONFLICT` 保证 canonical path 幂等；状态查询和 revoke 使用行锁。
- macOS picker 使用固定 `/usr/bin/osascript` 和固定 AppleScript，不使用 shell；timeout/cancel 必须终止子进程。
- 非 macOS 平台返回结构化 `WORKSPACE_PICKER_UNAVAILABLE`，不得使用 cwd 或任意字符串路径兜底。

### 查询任务列表

```text
GET /internal/tasks?status=running&limit=20
```

Response (200):
```json
{
  "tasks": [...]
}
```

### 查询任务详情

```text
GET /internal/tasks/{task_id}
```

Response (200):
```json
{
  "task": {...},
  "active_run": {...},
  "steps": [...],
  "artifacts": [...]
}
```

### 查询任务历史（SSE 初始快照用）

```text
GET /internal/tasks/{task_id}/history
```

Response (200):
```json
{
  "task": {...},
  "runs": [...],
  "events": [...],
  "messages": [...]
}
```

### 取消运行

```text
POST /internal/runs/{run_id}/cancel
Content-Type: application/json

{ "reason": "用户取消" }
```

Response (200):
```json
{
  "run": { "id": "...", "status": "cancel_requested", ... }
}
```

语义：
- PostgreSQL 中更新 AgentRun status → cancel_requested
- 写入 OutboxEvent（event_type=run.cancel.requested）
- Outbox Publisher 异步发布到 Redis worker-command stream
- 已终态的 run → 409 CONFLICT
- 重复取消 → 幂等返回当前状态

### 权限决定

```text
POST /internal/permissions/decide
Content-Type: application/json

{
  "request_id": "...",
  "decision": "allow_once",
  "note": ""
}
```

Response (200):
```json
{
  "request": { "id": "...", "status": "approved", ... }
}
```

语义：
- 更新 permission_requests.status
- 写入 OutboxEvent（event_type=permission.decision）
- Outbox Publisher 异步发布到 Redis worker-command stream
- 重复提交相同 decision → 幂等返回已有结果
- 提交冲突 decision → 409 CONFLICT

```text
GET /internal/runs/{run_id}/permissions
```

返回 `{ "requests": PermissionRequestDTO[] }`，只包含 PostgreSQL 中 status=pending 的
请求；不存在的 Run 或非法 ID 使用统一 AppError。Gateway 对外路径为
`GET /api/runs/{run_id}/permissions`。

### 健康检查

```text
GET /internal/health
→ 200 { "status": "ok", "database": "connected" }
→ 503 { "status": "degraded", "database": "disconnected" }
```

### 会话列表（多轮对话 MVP）

```text
GET /internal/conversations?limit=50&offset=0
```

Response (200):
```json
{
  "conversations": [
    {
      "id": "...",
      "title": "...",
      "created_at": "...",
      "updated_at": "..."
    }
  ]
}
```

### 会话详情（多轮对话 MVP，有界分页）

```text
GET /internal/conversations/{conversation_id}?limit=50&before=<cursor>
```

- `limit`：每页消息数，默认 50，最大 100
- `before`：分页 cursor（`base64(json([created_at_iso, message_id]))`），不传时返回最近一页

Response (200):
```json
{
  "conversation": { "id": "...", "title": "...", "created_at": "...", "updated_at": "..." },
  "messages": [
    {
      "id": "...",
      "conversation_id": "...",
      "task_id": "...",
      "run_id": "...",
      "role": "user | assistant | system | tool",
      "content": "...",
      "created_at": "..."
    }
  ],
  "next_cursor": "base64..."  // 存在更早消息时不为 null
}
```

页面内消息按从旧到新排序。cursor 基于 `(created_at, id)` 键集分页，避免 OFFSET 漂移。

- `next_cursor` 由服务端生成，是客户端判断是否还能加载更早消息的唯一真源；客户端不得根据当前页条数自行推导。
- cursor 解码后必须是 `[timezone-aware ISO datetime string, UUID string]`，非法结构返回 `400 VALIDATION_ERROR`。
- Go Gateway 保留 Python Control Plane 的安全 `AppError` 字段，并按 category 映射 HTTP 状态：`validation→400`、`not_found→404`、`permission→403`、`storage→503`，其他类别安全收口为 500。
- 会话列表和详情仅允许 `GET`；其他方法返回结构化 `405 METHOD_NOT_ALLOWED`，并设置 `Allow: GET`。

**ConversationDTO 字段**（所有层统一——Python Control Plane、Go Gateway、shared types）：

| 字段 | 类型 | 说明 |
|------|------|------|
| `id` | ID | 会话唯一标识 |
| `title` | string (可选) | 会话标题 |
| `created_at` | ISODateTime | 创建时间 |
| `updated_at` | ISODateTime | 最后更新时间（新消息或 Task 时更新，创建/列表/详情均返回） |

创建任务返回的 conversation 与列表、详情使用同一 DTO shape。

## RAG 发布门禁历史（P5-1，只读）

```text
GET /api/rag/evaluation/gates?limit=20
GET /internal/rag/evaluation/gates?limit=20
```

- `limit` 默认为 20，范围 `1..100`；只允许 GET，其他方法返回统一 `405 METHOD_NOT_ALLOWED`。
- 返回 `{ runs: RagQualityGateRunDTO[], insights: RagQualityGateInsightsDTO }`，runs 按 `generated_at/id`
  倒序。Run DTO 固定包含 `id/gate_id/cohort_id/baseline_id/revision/status/sample_count/metrics/checks/
  generated_at`。
- `status` 只允许 `passed/blocked/insufficient_evidence`。`metrics` 与 `checks` 是服务端白名单脱敏投影，
  不得出现 query、answer、Chunk 正文、向量、异常详情或本地报告路径。
- 该 API 不接受 Workspace，因为它展示 release revision 级全局发布结果，不属于单个用户 Task/Run；
  Gateway 不提供 POST/PATCH/DELETE 对应端点，也不运行 `release-gate.sh`。
- `insights.comparison_state` 为 `ready/insufficient_history`；只有相同 gate/cohort 至少两次运行才返回
  `metric_trends`。`alerts` 只含 `status_regressed/check_failed/metric_regressed` 结构化提醒。
- `failure_clusters` 从 `failure_rate:*` check 聚合，包含 failure type、priority、最新/上次失败率、最新
  失败数、窗口出现次数、阈值和 check 状态。它是只读诊断顺序，不授权自动修改任何发布对象。

P5-3 新增失败样本定位：

```text
GET /api/rag/evaluation/gates/{run_id}/failure-targets?failure_type=...&limit=50
GET /internal/rag/evaluation/gates/{run_id}/failure-targets?failure_type=...&limit=50
```

- `run_id` 必须是 UUID，`failure_type` 仅允许评测框架白名单失败类型，`limit` 范围 `1..100`。
- 返回 `{ targets: RagQualityFailureTargetDTO[] }`；目标包含 candidate/trace/workspace ID、query hash、
  failure type、suspected stage、severity、metric IDs，以及回查得到的当前 privacy/label/review state。
- Workspace 只来自当前 trace 回查，不写入历史门禁 JSON。trace 缺失或 query hash 不匹配时安全跳过。
- 端点仅允许 GET，不接受审核 mutation；实际隐私、标签与晋升继续走既有 trace 审核契约。

P5-4 质量治理 mutation：

```text
PATCH /api/rag/evaluation/issues/{issue_id}
PATCH /internal/rag/evaluation/issues/{issue_id}
```

- 输入为 `expected_version/owner/status/resolution_note`；owner 仅允许数据与金标、候选召回、重排、上下文
  组装四类，人工 status 仅允许 `open/in_progress/resolved/dismissed`。
- `resolved/dismissed` 必须有 1..500 字说明；乐观版本冲突返回安全 AppError。`verified` 禁止由客户端写入。
- failure target DTO 可附带当前 `issue` 投影。mutation 只改变治理 metadata 并写 AuditLog，不提供门禁、
  baseline、cohort、金标或检索策略修改能力。

P5-5 质量问题台账：

```text
GET /api/rag/evaluation/issues?status=all&owner=all&failure_type=all&limit=50
GET /internal/rag/evaluation/issues?status=all&owner=all&failure_type=all&limit=50
```

- `status` 允许 `all/open/in_progress/resolved/verified/dismissed`；`owner` 允许 `all/data_quality/
  candidate_recall/reranker/context_assembly`；`failure_type` 允许 `all` 或评测白名单失败类型；`limit` 为
  `1..100`。列表按 `updated_at/id` 倒序且服务端有界。
- 返回 `{ issues, summary }`。`summary` 是未受列表筛选影响的全局状态计数；每项包含 issue DTO、权威
  trace/workspace ID、query hash、当前 privacy/label/review state，以及首次、最近和验证 revision。
- 接口不返回 raw query/answer、Chunk 正文、Embedding、报告路径或凭据。台账 GET 不新增 mutation；状态
  更新继续走 P5-4 PATCH，重新打开审核继续走既有 trace 审核契约。

## 版本策略

接口发生破坏性变化时必须：

- 更新本文档。
- 更新对应自动化测试夹具。
- 更新对应 UI store。
- 更新 `12-development-progress.md` 中的接口变更记录。
- 由 Codex 审查 event payload 是否仍满足 Timeline、Inspector 和 Permission Dialog 的展示需求。
