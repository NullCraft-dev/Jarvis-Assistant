# 数据模型与 Storage Schema 文档

> **当前架构（2026-07-14）**：Python Application 是唯一持久化 Owner，PostgreSQL 是唯一持久化真相。旧 Go SQLite schema、adapter 与 `.db` 文件均已删除；历史章节仅用于解释迁移背景，不再构成实现契约。

## 文档目的

本文档定义 MVP 阶段需要落地的核心数据对象、Storage Interface 和关系型 schema 契约。

它服务于三个目标：

- Runtime 可以持久化 Task、AgentRun、ExecutionStep、ToolCall、Permission、AuditLog、Settings 和 Memory。
- UI 可以从 Storage Layer 恢复历史任务、时间线、权限请求和最终产物。
- Codex 审查时可以依据 schema 判断实现是否遗漏状态、审计或恢复能力。

本文档不负责 Web API / IPC DTO 细节，接口 DTO 由 `13-interface-contract.md` 定义。

## 存储原则

- Runtime 业务逻辑只能依赖 Store interfaces，不能直接依赖具体数据库。
- 具体 backend 可以是本地轻量关系型数据库、PostgreSQL、其他嵌入式数据库或组合存储。
- 本文 SQL 用作关系型 schema 契约和 migration 参考，不代表项目永久绑定某个数据库。
- 所有主对象使用字符串 ID。
- 时间字段使用 ISO 8601 字符串，或由 adapter 映射到目标数据库的 timestamp 类型。
- 结构化但易变的字段使用 JSON 字段；具体 backend 可映射为 JSON text、JSONB 或等价类型。
- 用户可见状态字段必须可索引。
- 工具调用、权限决策和审计日志不能只存在内存中。
- 高风险动作即使被拒绝，也必须写入 audit log。
- Redis 是运行时通信层，可以承载 run queue、worker command、runtime event、worker heartbeat 和短期协调状态；它不是 Storage 真源。
- Task、AgentRun、ExecutionStep、ToolCall、PermissionRequest、PermissionGrant、AuditLog、Settings 和 Memory 的最终状态不能只存在 Redis。
- 单机 mock 阶段可以用 in-memory bus 实现同一接口；进入多 worker / 多 Agent 阶段应使用 Redis Streams 或等价消息系统。
- 向量检索可以后置，等 memory 和项目知识库需求明确后再选择 pgvector、LanceDB、Chroma 或其他方案。

## Storage Interface

Runtime 应通过以下 store 访问持久化状态：

```text
TaskStore
RunStore
StepStore
ToolCallStore
PermissionStore
AuditStore
ArtifactStore
ConversationStore
MemoryStore
SettingsStore
McpStore
EventStore
```

Store interface 负责屏蔽具体 backend：

```text
Runtime / Managers
-> Store interfaces
-> Storage adapter
-> Concrete database / file store
```

禁止模式：

```text
AgentRunner -> raw SQL
ToolGateway -> raw SQL
PermissionManager -> concrete database client
Web UI / Renderer -> database
```

## 表清单

```text
tasks
workspaces
agent_runs
execution_steps
tool_calls
permission_requests
permission_grants
audit_logs
artifacts
conversations
memories
settings
mcp_servers
mcp_tools
runtime_events
```

## tasks

保存用户任务。

```sql
CREATE TABLE tasks (
  id TEXT PRIMARY KEY,
  title TEXT NOT NULL,
  user_goal TEXT NOT NULL,
  status TEXT NOT NULL,
  priority INTEGER NOT NULL DEFAULT 0,
  workspace_path TEXT,
  workspace_id TEXT,
  active_run_id TEXT,
  last_step_summary TEXT,
  risk_level TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  completed_at TEXT,
  cancelled_at TEXT,
  metadata_json TEXT NOT NULL DEFAULT '{}'
);
```

索引：

```sql
CREATE INDEX idx_tasks_status_updated_at ON tasks (status, updated_at);
CREATE INDEX idx_tasks_workspace_path ON tasks (workspace_path);
CREATE INDEX idx_tasks_workspace_id ON tasks (workspace_id);
```

`workspace_path` 是 Task 创建时的安全快照；`workspace_id` 可为空以兼容历史 Task。

## agent_runs

保存一次 Agent 执行。

```sql
CREATE TABLE agent_runs (
  id TEXT PRIMARY KEY,
  task_id TEXT NOT NULL,
  agent_id TEXT NOT NULL,
  mode TEXT NOT NULL,
  status TEXT NOT NULL,
  current_step_id TEXT,
  final_output_artifact_id TEXT,
  max_steps INTEGER NOT NULL DEFAULT 20,
  step_count INTEGER NOT NULL DEFAULT 0,
  input_tokens INTEGER NOT NULL DEFAULT 0,
  output_tokens INTEGER NOT NULL DEFAULT 0,
  estimated_cost_usd REAL,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  started_at TEXT,
  completed_at TEXT,
  failed_at TEXT,
  error_json TEXT,
  metadata_json TEXT NOT NULL DEFAULT '{}',
  FOREIGN KEY (task_id) REFERENCES tasks(id)
);
```

索引：

```sql
CREATE INDEX idx_agent_runs_task_id ON agent_runs (task_id);
CREATE INDEX idx_agent_runs_status ON agent_runs (status);
```

`step_count` 表示该 Run 已首次投影的 `ExecutionStep` 数量，不是 LLM iteration 或工具调用次数。
创建新 Step 时，Runtime 必须在锁定同一 Run 行的事务内使用当前 `step_count` 作为 0-based
`order_index`，随后把 `step_count + 1` 与 `current_step_id` 一起持久化。相同 Step 的后续生命周期
事件、权限恢复与 event 重放不能再次计数。

## execution_steps

保存 Timeline 的核心事件节点。

```sql
CREATE TABLE execution_steps (
  id TEXT PRIMARY KEY,
  run_id TEXT NOT NULL,
  task_id TEXT NOT NULL,
  parent_step_id TEXT,
  type TEXT NOT NULL,
  status TEXT NOT NULL,
  title TEXT NOT NULL,
  summary TEXT,
  input_json TEXT,
  output_json TEXT,
  error_json TEXT,
  started_at TEXT,
  completed_at TEXT,
  duration_ms INTEGER,
  order_index INTEGER NOT NULL,
  metadata_json TEXT NOT NULL DEFAULT '{}',
  FOREIGN KEY (run_id) REFERENCES agent_runs(id),
  FOREIGN KEY (task_id) REFERENCES tasks(id)
);
```

索引：

```sql
CREATE INDEX idx_execution_steps_run_order ON execution_steps (run_id, order_index);
CREATE INDEX idx_execution_steps_type_status ON execution_steps (type, status);
```

新 Run 的 `order_index` 必须为 `0..step_count-1` 且语义唯一。Model/Tool Step 使用同一 Run 全局
单调序列生成确定性 ID；投影发现既有 ID 的 Run、Task、type 或 call identity 不一致时必须拒绝，不能
覆写原 Step。现有 schema 暂未增加唯一约束；历史不一致只由对账报告，不自动回写审计事实。

## tool_calls

保存 Native Tool、System Tool 和 MCP Tool 调用。

```sql
CREATE TABLE tool_calls (
  id TEXT PRIMARY KEY,
  task_id TEXT NOT NULL,
  run_id TEXT NOT NULL,
  step_id TEXT NOT NULL,
  provider TEXT NOT NULL,
  tool_name TEXT NOT NULL,
  mcp_server_id TEXT,
  risk_level TEXT NOT NULL,
  arguments_json TEXT NOT NULL,
  arguments_summary_json TEXT,
  result_json TEXT,
  result_summary TEXT,
  permission_request_id TEXT,
  permission_status TEXT NOT NULL,
  status TEXT NOT NULL,
  error_json TEXT,
  started_at TEXT,
  completed_at TEXT,
  duration_ms INTEGER,
  FOREIGN KEY (task_id) REFERENCES tasks(id),
  FOREIGN KEY (run_id) REFERENCES agent_runs(id),
  FOREIGN KEY (step_id) REFERENCES execution_steps(id)
);
```

索引：

```sql
CREATE INDEX idx_tool_calls_run_id ON tool_calls (run_id);
CREATE INDEX idx_tool_calls_tool_name ON tool_calls (tool_name);
CREATE INDEX idx_tool_calls_mcp_server_id ON tool_calls (mcp_server_id);
CREATE INDEX idx_tool_calls_status ON tool_calls (status);
```

`permission_status` 与执行 `status` 是正交字段。权限拒绝保存为
`permission_status='denied'`、`status='failed'` 和 `error_json.code='PERMISSION_DENIED'`；
不得向 `status` 写入未在约束中的 `denied`。批准后 executor 失败保存为
`permission_status='approved'`、`status='failed'` 及真实工具错误。请求未获批准便随 Run 终态失效时
保存 `permission_status='expired'`；只允许覆盖 pending，不得覆盖 approved/denied。
授权等待超时同样投影为 expired，不得复用 denied。

迁移 `024_tool_permission_expired` 将 `ck_tool_calls_permission_status` 扩展为
`not_required/pending/approved/denied/expired`，没有新增列。降级先把 expired 映射回旧版本可接受的
pending，再恢复旧 check constraint。

## permission_requests

保存等待用户确认或已解决的权限请求。

```sql
CREATE TABLE permission_requests (
  id TEXT PRIMARY KEY,
  task_id TEXT NOT NULL,
  run_id TEXT NOT NULL,
  step_id TEXT,
  tool_call_id TEXT,
  tool_name TEXT NOT NULL,
  action_summary TEXT NOT NULL,
  reason TEXT,
  risk_level TEXT NOT NULL,
  scope_json TEXT NOT NULL,
  arguments_summary_json TEXT NOT NULL,
  allowed_decisions_json TEXT NOT NULL,
  checkpoint_json TEXT NOT NULL,
  status TEXT NOT NULL,
  decision TEXT,
  decided_at TEXT,
  note TEXT,
  created_at TEXT NOT NULL,
  expires_at TEXT NOT NULL,
  FOREIGN KEY (task_id) REFERENCES tasks(id),
  FOREIGN KEY (run_id) REFERENCES agent_runs(id)
);
```

索引：

```sql
CREATE INDEX idx_permission_requests_status ON permission_requests (status);
CREATE INDEX idx_permission_requests_run_id ON permission_requests (run_id);
CREATE INDEX idx_permission_requests_status_expires ON permission_requests (status, expires_at);
```

Migration `025_permission_request_expiry` 为 PostgreSQL 请求增加非空 `expires_at`，并把升级前记录回填为
`created_at + 15 minutes`。只有 `status='pending' AND expires_at <= now()` 参与有界到期扫描；历史
approved/denied/consumed 记录保留截止时间用于审计，不回写其决定事实。

## permission_grants

保存可复用授权规则。

```sql
CREATE TABLE permission_grants (
  id TEXT PRIMARY KEY,
  grant_type TEXT NOT NULL,
  tool_name TEXT,
  mcp_server_id TEXT,
  workspace_path TEXT,
  path TEXT,
  risk_level_max TEXT NOT NULL,
  created_from_request_id TEXT,
  created_at TEXT NOT NULL,
  expires_at TEXT,
  revoked_at TEXT,
  metadata_json TEXT NOT NULL DEFAULT '{}'
);
```

索引：

```sql
CREATE INDEX idx_permission_grants_tool ON permission_grants (tool_name);
CREATE INDEX idx_permission_grants_workspace ON permission_grants (workspace_path);
CREATE INDEX idx_permission_grants_revoked ON permission_grants (revoked_at);
```

## audit_logs

保存权限、安全、本地影响操作和模型连通性测试的审计记录。

已知 event_type 值：
- `tool.executed` / `tool.failed` — 工具执行
- `permission.granted` / `permission.denied` — 权限决策
- `workspace.registered` / `workspace.revoked` — 工作区操作
- `model.test` — 模型连通性测试（Phase 6）。details 含 provider/model/safe_url/timeout_ms，result_summary 含 success/latency_ms 或 error_code。不含 API key、prompt 或原始响应。

```sql
CREATE TABLE audit_logs (
  id TEXT PRIMARY KEY,
  task_id TEXT,
  run_id TEXT,
  step_id TEXT,
  tool_call_id TEXT,
  event_type TEXT NOT NULL,
  actor TEXT NOT NULL,
  risk_level TEXT,
  permission_decision TEXT,
  action_summary TEXT NOT NULL,
  details_json TEXT NOT NULL DEFAULT '{}',
  result_summary TEXT,
  error_json TEXT,
  created_at TEXT NOT NULL
);
```

索引：

```sql
CREATE INDEX idx_audit_logs_task_run ON audit_logs (task_id, run_id);
CREATE INDEX idx_audit_logs_event_type ON audit_logs (event_type);
CREATE INDEX idx_audit_logs_created_at ON audit_logs (created_at);
```

审计查询页面只能经 Application Service / `AuditRepository` 读取该表。查询按
`created_at DESC, id DESC` 有界分页；`details_json` 与 `error_json` 是内部审计字段，
不得原样进入 Web DTO，必须在 Application 层生成限长、递归脱敏的 `details_summary`，
且错误仅公开安全 `error_code`。

## artifacts

保存最终输出和中间产物。

当前最终 Markdown 回复小于阈值时写入 `artifacts.content`；超过阈值时写入受控本地目录，
本表只保存相对 `file_path`、`file_size_bytes`、`mime_type` 与 `content_hash`，并由
`agent_runs.final_output_artifact_id` 引用。创建 Artifact、更新 Run 引用、追加
`artifact.created` RuntimeEvent 与 Outbox 属于同一事务；重复确定性 event/artifact id 幂等。
文件名只能由 Artifact UUID 派生；读取必须重新校验目录边界、大小与 SHA-256。文本 Artifact
额外校验 UTF-8；`literature.download_arxiv_pdf` 产生的二进制 PDF 只保存受控相对路径、MIME、
大小、哈希与 arXiv 来源元数据，当前不通过文本预览接口返回正文，留给后续 RAG ingestion 使用。
新写入路径按 Workspace/Run 分桶，格式为
`scoped/<workspace bucket>/<run UUID>/<prefix>/<artifact UUID>.<suffix>`；Workspace bucket 仅保存
Workspace UUID 或绝对路径摘要，不保存绝对路径。分桶只用于执行单 Run/Workspace 容量预算，
PostgreSQL 的 Artifact/Task/Run 关联仍是业务真源。旧版 `<prefix>/<artifact UUID>.<suffix>` 引用
继续只读兼容并计入根目录总量，不迁移、不删除历史文件。
来源检索阶段的 `download.available/reference/mime_type/url` 是有界 ToolResult DTO，不新增业务真源表；
只有下载工具成功并创建 Artifact 后，才产生可持久化的本地原文事实。RAG 只引用该 Artifact，不能把
来源 DTO 中的公开 URL 当成已下载文件。

业务真源对账不新增表或派生真源。当前只读查询按 `agent_runs.updated_at DESC, id DESC` 有界选取
最近 Run，再通过现有 Repository 读取关联 Task、RuntimeEvent、ExecutionStep 和 Artifact。
对账检查 active Task/Run 状态映射、Event sequence/终态、Step/Artifact 引用、Run Step 计数、
`order_index` 连续唯一性、Model/Tool Event 与 StepType 对应，以及外置 Artifact 大小和 SHA-256；
结果为瞬时诊断 DTO，不持久化、不回写状态、不自动修复。Artifact 路径和正文不得进入对账响应。

`permission_requests` 同时承载
`tool_name="runtime.repair_missing_terminal_event"` 的 L3 单次修复确认。checkpoint 只保存
`action/run_id/event_type`，批准后追加 `repaired_event_id`；不保存任意 SQL、事件 payload 或
文件内容。批准原子追加 RuntimeEvent、OutboxEvent、AuditLog 并将请求置为 `consumed`；拒绝
置为 `denied` 并写 AuditLog。该流程不创建 PermissionGrant，不改变现有 schema。

```sql
CREATE TABLE artifacts (
  id TEXT PRIMARY KEY,
  task_id TEXT NOT NULL,
  run_id TEXT NOT NULL,
  step_id TEXT,
  kind TEXT NOT NULL,
  title TEXT NOT NULL,
  purpose TEXT NOT NULL,
  producer_type TEXT NOT NULL,
  source_tool_call_id TEXT,
  content TEXT,
  file_path TEXT,
  file_size_bytes INTEGER,
  mime_type TEXT,
  content_hash TEXT,
  metadata_json TEXT NOT NULL DEFAULT '{}',
  created_at TEXT NOT NULL,
  FOREIGN KEY (task_id) REFERENCES tasks(id),
  FOREIGN KEY (run_id) REFERENCES agent_runs(id),
  FOREIGN KEY (source_tool_call_id) REFERENCES tool_calls(id)
);
```

索引：

```sql
CREATE INDEX idx_artifacts_task_id ON artifacts (task_id);
CREATE INDEX idx_artifacts_run_id ON artifacts (run_id);
CREATE INDEX idx_artifacts_purpose ON artifacts (purpose);
CREATE INDEX idx_artifacts_source_tool_call_id ON artifacts (source_tool_call_id);
```

## conversations

保存用户与 Agent 的消息。

```sql
CREATE TABLE conversations (
  id TEXT PRIMARY KEY,
  task_id TEXT,
  run_id TEXT,
  role TEXT NOT NULL,
  content TEXT NOT NULL,
  created_at TEXT NOT NULL,
  metadata_json TEXT NOT NULL DEFAULT '{}'
);
```

索引：

```sql
CREATE INDEX idx_conversations_task_created ON conversations (task_id, created_at);
```

## memories

保存结构化记忆和任务摘要。MVP 不要求向量检索。

```sql
CREATE TABLE memories (
  id TEXT PRIMARY KEY,
  type TEXT NOT NULL,
  key TEXT NOT NULL,
  value_json TEXT NOT NULL,
  source TEXT NOT NULL,
  sensitivity TEXT NOT NULL DEFAULT 'normal',
  enabled INTEGER NOT NULL DEFAULT 1,
  importance INTEGER NOT NULL DEFAULT 0,
  project_path TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  last_used_at TEXT,
  metadata_json TEXT NOT NULL DEFAULT '{}'
);
```

索引：

```sql
CREATE INDEX idx_memories_type_key ON memories (type, key);
CREATE INDEX idx_memories_project_path ON memories (project_path);
CREATE INDEX idx_memories_enabled ON memories (enabled);
```

## settings

保存本地设置。敏感值不直接明文保存在普通数据库字段中；API key 应放入系统 keychain 或加密存储。

```sql
CREATE TABLE settings (
  key TEXT PRIMARY KEY,
  value_json TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
```

建议 key：

```text
model
workspace
permissions
mcp
ui
logs
advanced
```

## mcp_servers

保存 MCP server 配置和状态。

```sql
CREATE TABLE mcp_servers (
  id UUID PRIMARY KEY,
  slug VARCHAR(80) NOT NULL UNIQUE,
  name TEXT NOT NULL,
  transport VARCHAR(20) NOT NULL CHECK (transport IN ('stdio')),
  command TEXT NOT NULL,
  args_json JSONB NOT NULL DEFAULT '[]',
  env_keys_json JSONB NOT NULL DEFAULT '[]',
  enabled BOOLEAN NOT NULL DEFAULT true,
  status VARCHAR(20) NOT NULL DEFAULT 'disconnected'
    CHECK (status IN ('disconnected','connected','error')),
  last_error_code VARCHAR(80),
  last_connected_at TIMESTAMPTZ,
  version INTEGER NOT NULL DEFAULT 1,
  created_at TIMESTAMPTZ NOT NULL,
  updated_at TIMESTAMPTZ NOT NULL
);
```

索引：

```sql
CREATE INDEX idx_mcp_servers_enabled ON mcp_servers (enabled);
```

## mcp_tools

保存从 MCP server discover 出来的工具 manifest。

```sql
CREATE TABLE mcp_tools (
  id UUID PRIMARY KEY,
  server_id UUID NOT NULL REFERENCES mcp_servers(id) ON DELETE CASCADE,
  original_name VARCHAR(200) NOT NULL,
  internal_name VARCHAR(300) NOT NULL UNIQUE,
  description VARCHAR(500) NOT NULL DEFAULT '',
  input_schema_json JSONB NOT NULL DEFAULT '{}',
  risk_level VARCHAR(5) NOT NULL DEFAULT 'L3',
  enabled BOOLEAN NOT NULL DEFAULT true,
  discovered_at TIMESTAMPTZ NOT NULL,
  updated_at TIMESTAMPTZ NOT NULL,
  UNIQUE (server_id, original_name)
);
```

索引：

```sql
CREATE INDEX idx_mcp_tools_server_enabled ON mcp_tools (server_id, enabled);
```

以上结构由 migration `012_mcp_foundation` 建立。`mcp_tools` 是发现缓存，不是第二个运行时
Registry；Worker 仍把 enabled 记录装配进唯一 ToolRegistry。

## runtime_events

保存可回放的运行事件。MVP 可先保存关键事件，后续再做完整 event sourcing。

```sql
CREATE TABLE runtime_events (
  id TEXT PRIMARY KEY,
  task_id TEXT,
  run_id TEXT,
  step_id TEXT,
  type TEXT NOT NULL,
  payload_json TEXT NOT NULL,
  created_at TEXT NOT NULL
);
```

索引：

```sql
CREATE INDEX idx_runtime_events_run_created ON runtime_events (run_id, created_at);
CREATE INDEX idx_runtime_events_type ON runtime_events (type);
```

## 状态约束

### TaskStatus

```text
pending
running
waiting_for_user
blocked
failed
completed
cancelled
```

### AgentRunStatus

```text
created
queued
running
pause_requested
paused
resume_requested
waiting_for_permission
waiting_for_user
blocked
failed
completed
cancelled
```

### StepStatus

```text
pending
running
waiting_for_permission
completed
failed
cancelled
skipped
```

### PermissionStatus

```text
pending
approved
denied
expired
cancelled
```

## 恢复流程

App 启动后：

```text
1. 读取 settings。
2. 初始化 mcp_servers，但不直接暴露完整 secret 给 Web UI 或后续桌面 Renderer。
3. 查询未完成 tasks。
4. 查询 active agent_runs。
5. 查询每个 run 的 execution_steps。
6. 查询 pending permission_requests。
7. UI 恢复 Task Dashboard、Timeline 和 Permission Dialog 状态。
```

## Migration 策略

MVP 阶段建议使用顺序 migration 文件。migration 文件应面向当前选定 backend，但本文档仍保持 backend-neutral 的 schema 契约。

P7-3 起，本地 PostgreSQL migration 操作统一由 `scripts/data-lifecycle.py` 编排。普通应用启动只校验
database current 等于唯一 code head，不再隐式升级。显式升级必须在应用停止后先创建 `0600` custom-format
备份，恢复到受限命名的隔离临时数据库，并对 Alembic revision、全部 public 表集合与逐表行数精确对账；
全部通过后才可执行 `alembic upgrade head`。空数据库以 `base` 表达并走相同链路，不允许成为无备份旁路。

自动恢复只面向隔离临时数据库，禁止覆盖业务真源。真实源库恢复需要独立人工确认目标、停机窗口和回滚点。

```text
migrations/0001_initial.sql
migrations/0002_mcp.sql
migrations/0003_memory.sql
```

每次 schema 变更必须：

- 更新本文档。
- 增加 migration。
- 增加或更新 storage 测试。
- 在 `12-development-progress.md` 记录 schema 变更。

---

## 当前 PostgreSQL Schema

> **实现位置**：SQLAlchemy models 位于 `apps/agent-worker/src/jarvis_worker/storage/postgres/models.py`，Alembic migration 位于 `apps/agent-worker/src/jarvis_worker/migrations/versions/`。

### 设计原则

- 所有主键使用 `UUID`，由 Application Service 生成（不依赖数据库自增）
- 所有时间使用 `TIMESTAMPTZ`，由 Application Service 设置（`utcnow()`）
- 结构化数据使用 `JSONB`
- 状态字段使用 `VARCHAR` + `CHECK` 约束
- 外键引用使用数据库级 `FOREIGN KEY`
- 唯一约束用于幂等保护（Inbox, Outbox, RuntimeEvent）

### 表清单（17 张表）

```
conversations
messages
tasks
workspaces
agent_runs
execution_steps
runtime_events
tool_calls
permission_requests
permission_grants
audit_logs
artifacts
outbox_events
inbox_events
memories
memory_candidates
memory_extraction_jobs
```

### conversations

```sql
CREATE TABLE conversations (
    id UUID PRIMARY KEY,
    task_id UUID,
    title VARCHAR(500),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX idx_conversations_task_id ON conversations (task_id);
```

### messages

```sql
CREATE TABLE messages (
    id UUID PRIMARY KEY,
    conversation_id UUID NOT NULL REFERENCES conversations(id),
    task_id UUID REFERENCES tasks(id),
    run_id UUID,
    role VARCHAR(20) NOT NULL CHECK (role IN ('system', 'user', 'assistant', 'tool')),
    content TEXT NOT NULL,
    tool_call_id UUID,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    metadata_json JSONB NOT NULL DEFAULT '{}'
);

CREATE INDEX idx_messages_conv_created ON messages (conversation_id, created_at);
CREATE INDEX idx_messages_task_id ON messages (task_id);
```

### tasks

```sql
CREATE TABLE tasks (
    id UUID PRIMARY KEY,
    conversation_id UUID NOT NULL REFERENCES conversations(id),
    title VARCHAR(500) NOT NULL,
    user_goal TEXT NOT NULL,
    status VARCHAR(30) NOT NULL CHECK (status IN (
        'pending', 'running', 'waiting_for_user', 'blocked', 'failed', 'completed', 'cancelled'
    )),
    priority INTEGER NOT NULL DEFAULT 0,
    workspace_path TEXT,
    workspace_id UUID REFERENCES workspaces(id) ON DELETE SET NULL,
    active_run_id UUID,
    last_step_summary TEXT,
    risk_level VARCHAR(5),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    completed_at TIMESTAMPTZ,
    cancelled_at TIMESTAMPTZ,
    metadata_json JSONB NOT NULL DEFAULT '{}'
);

CREATE INDEX idx_tasks_status_updated ON tasks (status, updated_at);
CREATE INDEX idx_tasks_workspace_path ON tasks (workspace_path);
CREATE INDEX idx_tasks_workspace_id ON tasks (workspace_id);
CREATE INDEX idx_tasks_conversation_id ON tasks (conversation_id);
```

`workspace_path` 是创建 Task 时验证后的 immutable snapshot；旧 Task 的 `workspace_id` 允许为空。Workspace 后续 revoke 不修改历史快照。

## Obsidian Personal Knowledge Base v1

Migration `010_obsidian_knowledge_vault` 新增：

- `knowledge_vaults`：独立 Vault 的路径注册、`active/revoked` 状态和 `jarvis_managed` 来源；`canonical_path`
  唯一。应用服务把 active 解释为单一当前 Vault：连接或重新连接时在同一事务中停用其他 active 记录并激活
  目标记录，不删除任何 Vault 文件、索引或历史文档元数据。
- `knowledge_documents`：Jarvis 创建 Markdown 的标题、`report/note/source` 类型、Vault 内相对路径、SHA-256、大小、标签和可选 Task/Run 来源；`(vault_id, relative_path)` 唯一。

Markdown 正文不复制进 PostgreSQL，文件系统是内容真源；数据库用于查询、审计关联和后续任务来源追踪。
`relative_path` 由 Knowledge Application Service 根据 `kind + 纯语义 title` 确定，例如
`Notes/MobileNet 深度可分离卷积.md`。UUID、Task/Run ID 和生成 revision 不进入文件名；UUID 继续作为
数据库与 frontmatter 的不可变身份。`(vault_id, relative_path)` 唯一约束同时承担语义路径冲突保护，
当前 create 契约命中同名文件时 fail closed，不推断合并、不自动改名且不覆盖。未来更新/合并必须使用
独立显式契约，同时原子更新文件、哈希、大小、`updated_at` 和审计记录。

Vault 根 `索引.md` 是可重建投影，不是业务真源。投影按 `report/note/source` 分区；报告按创建时间倒序，
笔记和来源按标题排序。Markdown 数学方言规范化发生在 Application Service 写入边界，文件适配器只接收
已确定的安全相对路径和规范化正文。

跨 Run 来源不新增第二份业务表。`messages.run_id` 将最近完整 assistant turn 绑定到历史 Run，Runtime 再
通过该 Run 的 `tool_calls` 中 `status=completed` 的持久化 `result_json` 重建可信 RAG/Artifact provenance。
该侧链有界为最近一个完整轮次和最多 50 条关联，不从 Message 正文、模型参数或任意更早 Run 猜测身份。
Migration `011_scheduled_knowledge_tasks` 为文档增加 `source_urls_json`，并新增：

- `scheduled_tasks`：计划定义、daily/weekly 规则、IANA timezone、active/paused、下一次执行、固定授权工具、version。
- `scheduled_task_executions`：每个时间槽或手动触发的持久化执行，状态为
  `pending/dispatching/dispatched/failed`，保存 lease、attempts 和 Task/Run 关联；
  `(scheduled_task_id, scheduled_for)` 唯一。
- `tasks.scheduled_execution_id`：可空唯一外键，保证一个执行实例只创建一个 Task。

Migration `013_scheduled_source_reports` 为 `scheduled_tasks` 增加：

- `task_kind`：`knowledge_report | source_report`；
- `source_policy_json`：服务端拥有的有界来源策略。当前只允许
  `{provider: arxiv, query: string, max_results: 1..10}`，不保存通用 MCP 授权。

来源去重不建立第二套“已读”真源：Worker 通过
`knowledge_documents.source_urls_json -> tasks.scheduled_execution_id -> scheduled_task_executions`
读取该计划已经成功写入报告的来源。只有文档落库后才被视为已收录，失败运行不会吞掉来源。

RAG chunk、embedding 和向量索引不进入这些表。

## RAG Ingestion Foundation v1

Migration `014_rag_ingestion_foundation` 新增独立于 Obsidian 与 Memory 的业务对象：

- `rag_documents`：Workspace 内由受控 Artifact 派生的可检索文档身份，保存来源 SHA-256、MIME、
  ingestion policy、parser/chunker/embedding 版本和 `indexing/ready/failed/disabled` 生命周期；不复制
  Artifact 文件。
- `rag_ingestion_jobs`：状态为
  `queued/parsing/chunking/embedding/completed/failed/cancelled` 的可恢复作业，保存幂等键、尝试上限、
  独立的解析/Embedding 尝试计数、worker lease、重试时间、安全错误码和 `progress_json`。
- `rag_chunks`：确定性 ordinal、文本、SHA-256、token 数、来源定位和可空 `embedding_key`；当前
  不保存向量值。
- `rag_elements`：图片、figure、chart、table、diagram、equation 的页码、PDF point bbox、页面尺寸、
  稳定 locator、图注、OCR、结构化数据、派生描述、提取方法/版本与置信度。
- `rag_assets`：元素裁剪、内嵌图片或页面渲染的 MIME、SHA-256、大小、尺寸与安全内部相对引用；
  二进制正文不进入 PostgreSQL。
- `rag_chunk_element_links`：以 contains/references/explains/caption_of/nearby 关系连接文本与非文本
  元素，同时保存置信度和顺序。

Workspace 隔离不是仅靠查询过滤：作业通过 `(document_id, workspace_id)` 复合外键引用文档，chunk
同时通过 `(document_id, workspace_id)` 和 `(ingestion_job_id, workspace_id)` 复合外键引用上游，
数据库直接拒绝跨 Workspace 组合。`(workspace_id, source_artifact_id, source_content_hash)`、
`idempotency_key`、`(document_id, ingestion_policy_version)` 和 `(ingestion_job_id, ordinal)` 分别保证
来源、作业与分块幂等。

Element、Asset 和 Link 继续通过 `(id, document_id, workspace_id)` 复合键约束同一文档与
Workspace；Asset 不暴露绝对路径。Element locator key、Element/Asset/Link UUID 由页面位置、类型、
提取版本、内容哈希和关系确定性生成，不接受 LLM 自由声明。

Embedding 向量由可替换 `VectorIndex` adapter 持有；当前 PostgreSQL + pgvector adapter 使用
`rag_chunk_embeddings` 保存 1536 维向量、content hash、provider/model 和更新时间，并通过
`(chunk_id, document_id, workspace_id)` 复合外键绑定 `rag_chunks`。Cosine HNSW 索引用于候选检索，
所有 search/delete 必须携带 Workspace 条件。PostgreSQL 仍是文档、作业状态、chunk
正文、非文本元数据和来源关系的业务真源。当前 ingestion service 在解析/分片投影提交后把 job
置为 `embedding`，同时清空 `claimed_by/lease_until`，表示等待下一阶段而非正在占用 Worker；
`rag_documents` 仍保持 `indexing`，但已记录 parser/chunker version 与 chunk_count。只有向量写入成功
后才能进入 `completed/ready`。重试会在同一事务内先删除旧 Chunk/Element 投影，再以确定性 ID
重建；外部 asset 文件按确定性引用写入，事务成功后清理不再引用的旧文件。

解析/分片 Worker 崩溃后，过期 lease 可以在 `attempts < max_attempts` 时被新 Worker 领取；当
`attempts == max_attempts` 时必须原子收口而不是继续领取。repository 将耗尽作业转换为
`failed/RAG_INGESTION_ATTEMPTS_EXHAUSTED` 并把该终态返回 Application Service；Service 在同一事务把仍为
`indexing` 的 `rag_documents` 行转为 `failed`，清空 job owner/lease/retry，并写含 attempts/max_attempts
的 AuditLog。该行为只使用既有字段和枚举，不需要新 migration；`failed` 文档仍可通过显式 restart 重置。

Migration `015_rag_openai_embeddings` 负责启用 vector extension 和创建索引表。当前已提供 OpenAI
Embedding adapter、pgvector `VectorIndex` adapter、Embedding application service、独立常驻
RAG Worker 和检索 Tool。RAG 文档管理读模型以 `rag_documents.workspace_id` 为强制过滤条件，批量
读取文档后再以相同 Workspace 条件查询每个文档最近一次 `rag_ingestion_jobs`；它不新增投影表，也
不把 Redis 状态作为业务真源。

Migration `016_rag_job_progress` 为 `rag_ingestion_jobs` 增加非空 JSONB 进度快照。当前固定字段为
`active_executor/page_count/native_extraction_done/visual_pages_total/visual_pages_completed/
visual_route_counts/chunks_total/embedding_total/embedding_completed`；`visual_route_counts` 只保存有界原因
计数，不保存页面正文或图片。领域契约约束所有计数非负且 completed 不得超过
total。该快照由持有 lease 的 ingestion/embedding service 写入，适合页面轮询恢复，也为后续新增
索引构建、rerank 等阶段保留扩展空间，但不能存放正文、路径、异常或模型凭据。
同一 ingestion policy 的显式重新执行不会新增表或绕过唯一约束，而是原子重置对应
`rag_ingestion_jobs`：状态回到 queued，
解析/Embedding attempts 归零，lease、重试时间、错误、生命周期终态和 progress 清空；对应 Document
回到 indexing 并清空旧索引版本读模型。旧 chunks/向量可暂存至新投影事务提交，但检索查询必须继续
要求 `rag_documents.status = ready`，因此重建期间不会返回旧证据。

文档管理写操作使用 `rag_documents.version` 做乐观并发保护。`ready -> disabled` 只关闭检索可见性，
保留 Chunk、Element、Asset 和向量；`disabled -> ready` 必须验证 parser/chunker/embedding 元数据及
chunk_count 完整。非终态 Job 可进入 cancelled，对应 indexing Document 进入 failed，之后可显式重建。
每次文档状态转换递增 version，且写入 AuditLog。永久删除以现有外键级联清除文档的 Job、Chunk、
Embedding、Element、Asset 和 Link；原始 Artifact 不删除。派生文件引用先写入 PermissionRequest
checkpoint，再在事务提交后清理，失败引用留在 checkpoint 供相同请求幂等补偿。

## RAG Evaluation Flywheel v1

Migration `017_rag_evaluation_flywheel` 增加两个 PostgreSQL 业务真源表：

- `rag_evaluation_traces`：绑定 Workspace、Task、Run 和可选 Step，保存真实 `rag.search` 的 query、
  query SHA-256、请求边界、各阶段版本、Candidate/Reranker 无正文排序快照、Context chunk ids、截断状态
  与结果数。初始隐私状态固定为 `pending`；排序快照只含 chunk/document id、rank、score、content hash
  和召回来源，不保存 Chunk 正文、Embedding 向量、模型凭据或异常原文。
- `rag_evaluation_labels`：一个 trace 至多一个当前标签，保存 positive chunk ids、hard-negative chunk
  ids、反馈来源、`draft/confirmed/rejected/promoted` 生命周期和复核备注。数据库要求 positive 非空；领域
  契约继续禁止 positive 与 hard-negative 重叠。

自动采集的 trace 不是金标。只有 `privacy_status=approved` 且 label 为 `confirmed/promoted`，Eval 才可
投影为回归样本。`rejected` 轨迹禁止外发或晋升；Redis 不保存这些对象，ToolCall result 也不承担评估
业务真源职责。

P4-6 不新增表或 migration：人工审核 API 只推进上述既有状态机。所有读取按 `workspace_id` 约束；隐私、
标签复核和晋升分别写 AuditLog。`promoted` 数据库状态表示“可生成发布候选”，不自动改变源码中的版本化
cohort manifest；manifest 仍由 release commit 拥有，避免运行时数据静默改变发布门禁分母。

Migration `019_rag_eval_feedback` 新增 `rag_evaluation_feedback`，作为用户信号与金标之间的隔离层：

- 绑定 trace、Workspace、Task、Run、持久化 Assistant Message 和可选 citation chunk；外键删除时跟随
  对应业务对象清理，不进入 Redis。
- `kind` 只允许 `helpful/unhelpful/citation_incorrect/evidence_insufficient`，`status` 只允许
  `pending/reviewed/dismissed`；非引用反馈的 `citation_chunk_id` 必须为空。
- 服务端 fingerprint 唯一约束负责 answer 或具体引用目标的幂等更新，前端不能指定 fingerprint。
- 只保存结构化信号和关联 ID，不保存用户 query、回答正文、Chunk 正文或自由文本备注。提交与处理均写
  AuditLog。该表不能直接充当 `rag_evaluation_labels`，审核状态也不能代替隐私批准、标签确认或晋升。

Migration `020_rag_feedback_triage` 为反馈增加可空 `failure_category`，只允许 candidate miss、reranker miss、
context omission/truncated、citation mismatch、answer generation、insufficient evidence 和 other 八类。诊断详情
中的 query/Chunk 摘要是从已获批 trace 与 RAG Chunk 临时投影，不写回反馈表。triage 可在同一事务内写入
`user_feedback/draft` label，但不得覆盖人工或终态 label。

Migration `021_rag_quality_gate_runs` 新增 append-only `rag_quality_gate_runs`，保存离线发布门禁的脱敏
运行摘要：`gate_id/cohort_id/baseline_id/revision/status/sample_count/metrics_json/checks_json/generated_at`。
`status` 只允许 `passed/blocked/insufficient_evidence`，`sample_count` 非负，且同一
`(gate_id, revision, generated_at)` 唯一。应用层只允许白名单聚合指标和检查字段写入；该表不关联 Task、
Run 或 Workspace，不保存原始 query/answer、Chunk、Embedding、异常详情、本地文件路径或凭据，也不承担
cohort manifest / baseline 文件的 owner 职责。Control Plane 和 Gateway 仅提供按时间倒序的有界只读查询。
P5-2 不新增 migration 或派生表；趋势、退化提醒和失败簇在查询时从最近的 append-only 门禁记录确定性
计算。只有相同 `gate_id + cohort_id` 的记录可比较，避免把 cohort 变化写成质量变化；Redis 与前端均不
保存第二份洞察真相。

Migration `022_rag_quality_failure_targets` 为 `rag_quality_gate_runs` 增加非空 JSONB
`failure_targets_json`（旧记录回填 `[]`）。每项仅允许 candidate ID、trace ID、query hash、失败类型、
阶段、严重度和白名单 metric IDs；不保存 Workspace 或正文。读取时用 trace ID + query hash 回查当前
`rag_evaluation_traces` 和 label，因此历史诊断定位不能覆盖当前隐私/审核业务真相。

Migration `023_rag_quality_issues` 新增 `rag_quality_issues`：candidate ID 唯一，关联 trace 及首次/最近/
验证 gate run，保存 gate/cohort、failure type、owner、status、occurrence count、resolution note、version 和
时间戳。状态仅允许 `open/in_progress/resolved/verified/dismissed`；owner 仅允许
`data_quality/candidate_recall/reranker/context_assembly`。trace 删除时级联清理；客户端以 version 乐观
更新。该表是治理状态真源，不替代 append-only 门禁、评测 trace、label 或源码 cohort manifest。
P5-5 不新增 migration 或台账派生表；问题列表、全局状态统计和门禁 revision 轨迹在查询时从
`rag_quality_issues`、`rag_evaluation_traces`、label 与 `rag_quality_gate_runs` 投影，避免产生第二份治理真相。

## Memory v1

`memories` 保存用户明确确认的长期记忆，PostgreSQL 是唯一业务真源：

```text
id UUID PK
scope_type global | workspace
workspace_id UUID NULL FK workspaces ON DELETE CASCADE
category preference | user_fact | project_fact | rule
key VARCHAR(128)
content TEXT
status active | disabled
source_type user_explicit | candidate_approved
source_task_id UUID NULL FK tasks ON DELETE SET NULL
importance INTEGER 0..100
version INTEGER
created_at / updated_at TIMESTAMPTZ
```

数据库 check 保证 global 的 workspace_id 为空、workspace 的 workspace_id 非空；两个 partial
unique index 分别保证 global 与 workspace 的 `(owner, category, key)` 唯一。
`idx_memories_context(status, scope_type, workspace_id, importance)` 支持每个 Task 的有界读取。
Memory v1 migration 为 `007_long_term_memory`。未来 LLM 自动提取使用独立候选表，不向 active
Memory 直接写入；向量索引作为可替换检索适配器后置。

## Memory v2 Candidate

Migration `008_memory_candidates` 扩展正式 Memory 的 `source_type` 为
`user_explicit | candidate_approved`，并新增两个独立业务对象。

`memory_candidates`：

```text
id UUID PK
scope_type global | workspace
workspace_id UUID NULL FK
category preference | user_fact | project_fact | rule
suggested_key VARCHAR(128)
content TEXT
status pending | approved | rejected | expired
source_task_id / source_run_id UUID FK
source_message_ids JSONB
extraction_input_fingerprint CHAR(64)
confidence FLOAT 0..1
importance INTEGER 0..100
sensitivity normal | sensitive
deduplication_key CHAR(64)
extraction_policy_version / extractor_provider / extractor_model
conflict_memory_id / approved_memory_id UUID NULL FK memories
expires_at / resolved_at
resolution_note
version
created_at / updated_at
```

`(source_run_id, extraction_policy_version, deduplication_key)` 唯一，防止消息重投或重试产生
重复候选。Candidate 与正式 Memory 物理分表，因此 pending/rejected/expired 不可能被现有
Memory context query 读取。

Migration `009_memory_candidate_maintenance` 增加：

- `UNIQUE (deduplication_key) WHERE status='pending'`，防止不同 Run 并发产生完全相同的待确认项；
- `(status, expires_at)` 索引，支持有界、skip-locked 到期扫描；
- 升级时若已有重复 pending，保留最早一条并把其余记录转换为 expired，不删除来源历史。

`memory_extraction_jobs` 持久化异步提取状态：

```text
id UUID PK
source_task_id / source_run_id UUID FK
extraction_policy_version
status queued | running | completed | failed
attempts 0..10
next_retry_at
error_code
created_at / updated_at
```

每个 `(source_run_id, extraction_policy_version)` 只能有一个作业。当前轻量实现直接将
PostgreSQL Job 作为可靠工作队列：`queued`、到期可重试的 `failed`、以及超过 stale 边界的
`running` 均可被 `FOR UPDATE SKIP LOCKED` 领取；`attempts`、`next_retry_at` 和
`updated_at` 支持有界重试与崩溃恢复。未引入新的 Redis stream，PostgreSQL
Job/Candidate/Memory 继续作为业务真源。

### workspaces

```sql
CREATE TABLE workspaces (
    id UUID PRIMARY KEY,
    name VARCHAR(500) NOT NULL,
    root_path TEXT NOT NULL,
    canonical_path TEXT NOT NULL UNIQUE,
    status VARCHAR(20) NOT NULL DEFAULT 'active'
        CHECK (status IN ('active', 'revoked')),
    source VARCHAR(20) NOT NULL DEFAULT 'user_picker'
        CHECK (source IN ('configured', 'user_picker')),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    revoked_at TIMESTAMPTZ,
    metadata_json JSONB NOT NULL DEFAULT '{}'
);

CREATE INDEX idx_workspaces_status ON workspaces (status);
```

- `canonical_path` 是并发幂等键，Repository 使用 PostgreSQL `ON CONFLICT`。
- `root_path` 保留用户选择或服务端配置提供的路径；安全校验与 Task 快照只使用 realpath 后的 `canonical_path`。
- `configured` 来源优先级高于 `user_picker`，且不可通过 Web revoke。
- revoke 为软撤销；历史 Task 继续保留 `workspace_path` 快照。

### agent_runs

```sql
CREATE TABLE agent_runs (
    id UUID PRIMARY KEY,
    task_id UUID NOT NULL REFERENCES tasks(id),
    agent_id VARCHAR(100) NOT NULL,
    mode VARCHAR(20) NOT NULL DEFAULT 'single_agent' CHECK (mode IN ('single_agent', 'multi_agent')),
    status VARCHAR(30) NOT NULL CHECK (status IN (
        'queued', 'running', 'waiting_permission',
        'pause_requested', 'paused', 'resume_requested',
        'cancel_requested', 'cancelling',
        'completed', 'failed', 'cancelled'
    )),
    version INTEGER NOT NULL DEFAULT 1,
    worker_id VARCHAR(100),
    lease_until TIMESTAMPTZ,
    current_step_id UUID,
    final_output_artifact_id UUID,
    max_steps INTEGER NOT NULL DEFAULT 20,
    step_count INTEGER NOT NULL DEFAULT 0,
    input_tokens INTEGER NOT NULL DEFAULT 0,
    output_tokens INTEGER NOT NULL DEFAULT 0,
    estimated_cost_usd REAL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    started_at TIMESTAMPTZ,
    completed_at TIMESTAMPTZ,
    failed_at TIMESTAMPTZ,
    error_json JSONB,
    checkpoint_json JSONB NOT NULL DEFAULT '{}',
    metadata_json JSONB NOT NULL DEFAULT '{}'
);

CREATE INDEX idx_agent_runs_task_id ON agent_runs (task_id);
CREATE INDEX idx_agent_runs_status ON agent_runs (status);
CREATE INDEX idx_agent_runs_worker_id ON agent_runs (worker_id);
CREATE INDEX idx_agent_runs_recovery_lease ON agent_runs (lease_until) WHERE status = 'running';
```

### execution_steps

```sql
CREATE TABLE execution_steps (
    id UUID PRIMARY KEY,
    run_id UUID NOT NULL REFERENCES agent_runs(id),
    task_id UUID NOT NULL REFERENCES tasks(id),
    parent_step_id UUID,
    type VARCHAR(30) NOT NULL CHECK (type IN (
        'user_message', 'system_event', 'model_call', 'plan_created',
        'tool_call', 'mcp_call', 'observation', 'permission_request',
        'review', 'final_output'
    )),
    status VARCHAR(30) NOT NULL CHECK (status IN (
        'pending', 'running', 'waiting_for_permission', 'completed',
        'failed', 'cancelled', 'skipped'
    )),
    title VARCHAR(500) NOT NULL,
    summary TEXT,
    input_json JSONB,
    output_json JSONB,
    error_json JSONB,
    started_at TIMESTAMPTZ,
    completed_at TIMESTAMPTZ,
    duration_ms INTEGER,
    order_index INTEGER NOT NULL DEFAULT 0,
    metadata_json JSONB NOT NULL DEFAULT '{}'
);

CREATE INDEX idx_execution_steps_run_order ON execution_steps (run_id, order_index);
CREATE INDEX idx_execution_steps_type_status ON execution_steps (type, status);
```

### runtime_events

```sql
CREATE TABLE runtime_events (
    id UUID PRIMARY KEY,
    event_id UUID NOT NULL UNIQUE,  -- 对外幂等键
    task_id UUID,
    run_id UUID,
    step_id UUID,
    type VARCHAR(50) NOT NULL,
    event_sequence BIGINT NOT NULL,  -- 同一 run 内单调递增
    payload_json JSONB NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX idx_runtime_events_run_created ON runtime_events (run_id, created_at);
CREATE INDEX idx_runtime_events_type ON runtime_events (type);
CREATE UNIQUE INDEX idx_runtime_events_event_id ON runtime_events (event_id);
```

### tool_calls

```sql
CREATE TABLE tool_calls (
    id UUID PRIMARY KEY,
    task_id UUID NOT NULL REFERENCES tasks(id),
    run_id UUID NOT NULL REFERENCES agent_runs(id),
    step_id UUID NOT NULL REFERENCES execution_steps(id),
    provider VARCHAR(20) NOT NULL CHECK (provider IN ('native', 'mcp', 'system')),
    tool_name VARCHAR(200) NOT NULL,
    mcp_server_id VARCHAR(200),
    risk_level VARCHAR(5) NOT NULL,
    arguments_json JSONB NOT NULL,
    arguments_summary_json JSONB,
    result_json JSONB,
    result_summary TEXT,
    permission_request_id UUID,
    permission_status VARCHAR(20) NOT NULL DEFAULT 'not_required'
        CHECK (permission_status IN ('not_required', 'pending', 'approved', 'denied', 'expired')),
    status VARCHAR(20) NOT NULL CHECK (status IN ('pending', 'running', 'completed', 'failed', 'cancelled')),
    error_json JSONB,
    started_at TIMESTAMPTZ,
    completed_at TIMESTAMPTZ,
    duration_ms INTEGER
);

CREATE INDEX idx_tool_calls_run_id ON tool_calls (run_id);
CREATE INDEX idx_tool_calls_tool_name ON tool_calls (tool_name);
CREATE INDEX idx_tool_calls_status ON tool_calls (status);
```

### permission_requests

```sql
CREATE TABLE permission_requests (
    id UUID PRIMARY KEY,
    task_id UUID NOT NULL REFERENCES tasks(id),
    run_id UUID NOT NULL REFERENCES agent_runs(id),
    step_id UUID,
    tool_call_id UUID,
    tool_name VARCHAR(200) NOT NULL,
    action_summary TEXT NOT NULL,
    reason TEXT,
    risk_level VARCHAR(5) NOT NULL,
    scope_json JSONB NOT NULL,
    arguments_summary_json JSONB NOT NULL,
    allowed_decisions_json JSONB NOT NULL,
    checkpoint_json JSONB NOT NULL DEFAULT '{}'::jsonb,
    status VARCHAR(20) NOT NULL CHECK (status IN ('pending', 'approved', 'denied', 'expired', 'consumed')),
    decision VARCHAR(30),
    decided_at TIMESTAMPTZ,
    note TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at TIMESTAMPTZ NOT NULL
);

CREATE INDEX idx_permission_requests_status ON permission_requests (status);
CREATE INDEX idx_permission_requests_run_id ON permission_requests (run_id);
CREATE INDEX idx_permission_requests_status_expires
    ON permission_requests (status, expires_at);
```

### permission_grants

```sql
CREATE TABLE permission_grants (
    id UUID PRIMARY KEY,
    grant_type VARCHAR(30) NOT NULL CHECK (grant_type IN (
        'once', 'task', 'tool_path', 'workspace', 'global'
    )),
    tool_name VARCHAR(200),
    mcp_server_id VARCHAR(200),
    workspace_path TEXT,
    path TEXT,
    risk_level_max VARCHAR(5) NOT NULL,
    created_from_request_id UUID REFERENCES permission_requests(id),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at TIMESTAMPTZ,
    revoked_at TIMESTAMPTZ,
    metadata_json JSONB NOT NULL DEFAULT '{}'
);

CREATE INDEX idx_permission_grants_tool ON permission_grants (tool_name);
CREATE INDEX idx_permission_grants_workspace ON permission_grants (workspace_path);
CREATE INDEX idx_permission_grants_revoked ON permission_grants (revoked_at);
```

### audit_logs

```sql
CREATE TABLE audit_logs (
    id UUID PRIMARY KEY,
    task_id UUID,
    run_id UUID,
    step_id UUID,
    tool_call_id UUID,
    event_type VARCHAR(50) NOT NULL,
    actor VARCHAR(100) NOT NULL,
    risk_level VARCHAR(5),
    permission_decision VARCHAR(30),
    action_summary TEXT NOT NULL,
    details_json JSONB NOT NULL DEFAULT '{}',
    result_summary TEXT,
    error_json JSONB,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX idx_audit_logs_task_run ON audit_logs (task_id, run_id);
CREATE INDEX idx_audit_logs_event_type ON audit_logs (event_type);
CREATE INDEX idx_audit_logs_created_at ON audit_logs (created_at);
```

### artifacts

```sql
CREATE TABLE artifacts (
    id UUID PRIMARY KEY,
    task_id UUID NOT NULL REFERENCES tasks(id),
    run_id UUID NOT NULL REFERENCES agent_runs(id),
    step_id UUID,
    kind VARCHAR(30) NOT NULL CHECK (kind IN (
        'markdown', 'text', 'json', 'file', 'diff', 'screenshot'
    )),
    title VARCHAR(500) NOT NULL,
    purpose VARCHAR(30) NOT NULL CHECK (purpose IN (
        'final_response', 'deliverable'
    )),
    producer_type VARCHAR(20) NOT NULL CHECK (producer_type IN (
        'runtime', 'tool'
    )),
    source_tool_call_id UUID REFERENCES tool_calls(id),
    content TEXT,
    file_path TEXT,
    file_size_bytes BIGINT,
    mime_type VARCHAR(100),
    content_hash VARCHAR(128),
    metadata_json JSONB NOT NULL DEFAULT '{}',
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX idx_artifacts_task_id ON artifacts (task_id);
CREATE INDEX idx_artifacts_run_id ON artifacts (run_id);
CREATE INDEX idx_artifacts_purpose ON artifacts (purpose);
CREATE INDEX idx_artifacts_source_tool_call_id
    ON artifacts (source_tool_call_id);
```

`producer_type=runtime` 时 `source_tool_call_id` 必须为空；`producer_type=tool` 时必须存在。
现有 Artifact 在 migration `006_artifact_v2_contract` 中回填为
`purpose=final_response`、`producer_type=runtime`，因为该 migration 之前生产链路只会创建
最终回复 Artifact。新业务逻辑以显式 purpose 判断 `final_output_artifact_id`，不再以
`metadata_json.is_final_output` 作为业务真相。

`workspace.create_file` 交付物保存 `kind=file`、`purpose=deliverable`、
`producer_type=tool` 和来源 ToolCall；`file_path` 保持为空，避免与
`JARVIS_ARTIFACT_ROOT` 下的受控正文引用混淆。workspace 相对路径仅放在
`metadata_json.workspace_relative_path`，并保存执行器计算的 size、MIME 与 SHA-256。
ToolCall 完成、Artifact、两个 RuntimeEvent 和 Outbox 必须在同一事务中提交。

读取该 workspace 文件时不能把 `metadata_json.workspace_relative_path` 单独当作授权。Artifact
Application Service 必须同时读取关联 Task 与来源 ToolCall，核对 Task/Run、工具名、完成状态，
以及 ToolCall result 中的 `artifact_ids/data/deliverables`。实际文件只能从 Task 的持久化
`workspace_path` 通过不跟随 symlink 的 dir-fd 路径读取，并再次核对 size 与 SHA-256；API
不得返回 Task 的绝对 `workspace_path`。

用户显式 PDF 上传复用现有 Artifact schema，不新增文件事实表：它创建 `producer_type=runtime`、
`kind=file`、`purpose=deliverable` 的 Artifact，并以受控 metadata
`storage=local_file/source=user_upload/explicit_user_action=true` 标识来源。Artifact 仍通过专用、
已完成的上传 Task/Run 绑定 Workspace；RAG command service 只在 Task/Run 的
`operation_type=rag_user_upload`、文件 SHA-256/大小/MIME 与受控相对路径均一致时接受该来源。
这与 `producer_type=tool` 的 Agent/ToolGateway 来源并列，但不伪造 ToolCall。

### outbox_events

```sql
CREATE TABLE outbox_events (
    id UUID PRIMARY KEY,
    event_id UUID NOT NULL UNIQUE,
    aggregate_type VARCHAR(50) NOT NULL,
    aggregate_id UUID NOT NULL,
    event_type VARCHAR(100) NOT NULL,
    schema_version VARCHAR(20) NOT NULL,
    payload JSONB NOT NULL,
    trace_id UUID NOT NULL,
    correlation_id UUID,
    causation_id UUID,
    status VARCHAR(20) NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending', 'dispatching', 'delivered', 'failed', 'dead')),
    retry_count INTEGER NOT NULL DEFAULT 0,
    max_retries INTEGER NOT NULL DEFAULT 20,
    next_retry_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    claimed_by VARCHAR(100),
    claimed_at TIMESTAMPTZ,
    lease_until TIMESTAMPTZ,
    error_code VARCHAR(50),
    error_message TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    delivered_at TIMESTAMPTZ,
    CHECK (retry_count >= 0 AND max_retries BETWEEN 1 AND 100)
);

CREATE INDEX idx_outbox_status_next_retry ON outbox_events (status, next_retry_at)
    WHERE status IN ('pending', 'failed');
CREATE INDEX idx_outbox_lease ON outbox_events (lease_until)
    WHERE status = 'dispatching';
CREATE UNIQUE INDEX idx_outbox_event_id ON outbox_events (event_id);
```

Outbox 的 Redis 传输失败使用有界指数退避，默认最多 20 次、单次等待最多 60 秒。旧的 5 次预算会在
约 3 秒内把正常 Redis 重启误判为永久失败，因此 migration `018_outbox_redis_recovery` 将默认
预算扩展为 20，并只重新激活 `error_code=REDIS_PUBLISH_ERROR`、仍低于新预算的 `dead` 事件；契约错误、
未知事件类型和已耗尽 20 次的事件继续保持 `dead`。数据库约束拒绝小于 1 或大于 100 的预算，避免
无限重试配置。

### inbox_events

```sql
CREATE TABLE inbox_events (
    id UUID PRIMARY KEY,
    source VARCHAR(100) NOT NULL,
    source_event_id VARCHAR(200) NOT NULL,
    status VARCHAR(20) NOT NULL DEFAULT 'processing'
        CHECK (status IN ('processing', 'processed', 'failed')),
    processed_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (source, source_event_id)
);

CREATE INDEX idx_inbox_source_event ON inbox_events (source, source_event_id);
```

### AgentRun 状态机约束

```text
合法迁移：
  queued → running              (Worker claim，条件：status='queued' AND version=N)
  queued → cancel_requested     (用户取消)
  running → waiting_permission  (Agent 请求权限)
  running → completed           (Agent 正常完成)
  running → failed              (Agent 执行失败)
  running → paused              (lease 过期且 checkpoint 可安全恢复)
  running → pause_requested     (用户请求在下一个安全 checkpoint 暂停)
  pause_requested → paused      (Worker 持久化 agent.run.paused)
  pause_requested → completed   (安全边界前任务已自然完成)
  pause_requested → failed      (执行失败或恢复不安全)
  running → cancel_requested    (用户取消)
  waiting_permission → running  (用户批准)
  waiting_permission → failed   (用户拒绝)
  paused → resume_requested     (用户请求恢复并创建 Outbox RunJob)
  paused → running              (系统崩溃恢复的 Worker claim)
  resume_requested → running    (用户恢复 RunJob 被 Worker claim)
  paused → failed               (恢复校验失败或预算耗尽)
  cancel_requested → cancelling (Worker 确认收到取消命令)
  cancelling → cancelled        (Worker 完成清理)

终态：completed, failed, cancelled

实现方式：
  UPDATE agent_runs
  SET status = $new_status, version = version + 1, updated_at = now()
  WHERE id = $id AND status = $expected_status AND version = $expected_version;
  -- affected_rows = 0 → 并发冲突或非法迁移 → AppError
```

`waiting_permission → running` 同时也是权限恢复的执行占用点：Worker 必须在执行获批
工具前完成条件更新并写入 worker/lease。`permission_requests.checkpoint_json` 仅由
Python Runtime/Storage 使用，不投影到 RuntimeEvent、Outbox 或 DTO。迁移
`002_permission_resume_checkpoint` 为现有 PostgreSQL 数据库增加该非空 JSONB 字段。Runtime 在 claim
前必须校验 checkpoint 内部结构，并将 request/task/run/step/tool-call/tool-name 与当前锁定的
PermissionRequest 对账；JSONB 存在或版本相同本身不构成执行授权。

`agent_runs.checkpoint_json` 是普通 Run 恢复真源，保存版本化 AgentState、下一事件序号和
允许恢复的 LangGraph node；不进入 RuntimeEvent、Outbox、Redis 或 Web DTO。迁移
`004_run_recovery_checkpoint` 增加该字段及 `idx_agent_runs_recovery_lease` 部分索引。
`tool_in_flight` checkpoint 只用于 fail closed，不能自动恢复。

当前 checkpoint payload 版本为 v5，复用既有 JSONB 列，不新增数据库 migration。v5 在 AgentState 中冻结
CompletionContract、LoopProgressSnapshot、StopDecision 与 RunControlState；后者冻结开始时间、deadline、
模型/工具预算及已用模型次数。v4 仅允许读取，恢复后下一安全边界写回 v5，
不得覆盖或删除原 JSONB 再原地猜测。v1-v3 及同版本损坏结构继续 fail closed。

`model.call.started/completed/failed` 使用同一确定性 `step_id` 投影一条
`execution_steps.type=model_call` 记录。可恢复模型失败允许 failed Run 保留
`resume_node=call_model` checkpoint；其他终态清空 checkpoint。失败步骤重试不修改源
ExecutionStep 或源 Run 状态，而是创建新的 AgentRun，并在源/新 Run metadata 中保存
`replacement_run_id / previous_run_id / retry_step_id` 关联。Task 的 `active_run_id` 原子切换
到 replacement Run；该实现复用现有 JSONB metadata/checkpoint 字段，不新增 migration。

迁移 `005_run_pause_resume` 扩展 `ck_agent_runs_status`，加入 `pause_requested` 与
`resume_requested`。过渡态是 PostgreSQL 权威事实：前端不能在 API 返回后提前伪造
`paused/running`；它只在收到 `agent.run.paused/agent.run.resumed` 后更新展示状态。

### Permission 状态机约束

`permission_requests` 也承载 `tool_name="runtime.retry_failed_run"` 的运维型 L3 单次确认。该请求仍关联原 failed Task/Run，`checkpoint_json` 只保存 DLQ source/message id、错误码、routing id 和 payload 指纹，不保存 payload。批准后状态直接进入 `consumed`，拒绝进入 `denied`；两者都写 AuditLog。并发决定必须通过 `SELECT ... FOR UPDATE` 锁定请求，确保最多创建一个 replacement Run。此用途不创建 `permission_grants`，不改变表结构或权限枚举。

```text
合法迁移：
  pending → approved  (用户批准)
  pending → denied    (用户拒绝)
  pending → expired   (超时未处理)
  approved → consumed (已用于对应工具调用)

幂等：重复提交相同 decision → 返回已有结果（不改变状态）
冲突：提交不同于已有 decision → 返回 CONFLICT AppError
```

### RuntimeEvent 分类规则

| 分类 | 事件类型 | 持久化 | 发布方式 |
|------|----------|--------|----------|
| 关键事件 | task.*, agent.run.*, agent.step.*, model.call.*, tool.call.*, permission.*, artifact.created, AuditLog | PostgreSQL runtime_events 表 | Outbox → Redis |
| 临时事件 | model.delta, 心跳, 进度动画 | 不持久化 | 直接 Redis |
| 大型内容 | 文件, 截图, diff, 长工具输出 | Artifact 文件系统 + metadata 在 PostgreSQL | 按需引用 |
