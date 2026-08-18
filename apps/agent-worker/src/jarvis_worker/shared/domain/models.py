"""领域模型 — 纯 Python dataclass，无 ORM 依赖。

这些数据类定义业务对象的结构和状态规则。
不允许在 domain 层引入任何持久化或网络依赖。
"""

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Optional
from uuid import UUID, uuid4

# ── ID / 时间工具 ──

def new_id() -> UUID:
    return uuid4()


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


# ── 状态枚举 ──

class TaskStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    WAITING_FOR_USER = "waiting_for_user"
    BLOCKED = "blocked"
    FAILED = "failed"
    COMPLETED = "completed"
    CANCELLED = "cancelled"


class RunStatus(str, Enum):
    QUEUED = "queued"
    RUNNING = "running"
    WAITING_PERMISSION = "waiting_permission"
    PAUSE_REQUESTED = "pause_requested"
    PAUSED = "paused"
    RESUME_REQUESTED = "resume_requested"
    CANCEL_REQUESTED = "cancel_requested"
    CANCELLING = "cancelling"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class StepType(str, Enum):
    USER_MESSAGE = "user_message"
    SYSTEM_EVENT = "system_event"
    MODEL_CALL = "model_call"
    PLAN_CREATED = "plan_created"
    TOOL_CALL = "tool_call"
    MCP_CALL = "mcp_call"
    OBSERVATION = "observation"
    PERMISSION_REQUEST = "permission_request"
    REVIEW = "review"
    FINAL_OUTPUT = "final_output"


class StepStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    WAITING_FOR_PERMISSION = "waiting_for_permission"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    SKIPPED = "skipped"


class PermissionStatus(str, Enum):
    PENDING = "pending"
    APPROVED = "approved"
    DENIED = "denied"
    EXPIRED = "expired"
    CONSUMED = "consumed"


class OutboxStatus(str, Enum):
    PENDING = "pending"
    DISPATCHING = "dispatching"
    DELIVERED = "delivered"
    FAILED = "failed"
    DEAD = "dead"


class InboxStatus(str, Enum):
    PROCESSING = "processing"
    PROCESSED = "processed"
    FAILED = "failed"


class WorkspaceStatus(str, Enum):
    ACTIVE = "active"
    REVOKED = "revoked"


class WorkspaceSource(str, Enum):
    CONFIGURED = "configured"  # 从 JARVIS_ALLOWED_WORKSPACE_PATHS 启动时幂等注册
    USER_PICKER = "user_picker"  # 用户通过系统目录选择器添加


class KnowledgeVaultStatus(str, Enum):
    ACTIVE = "active"
    REVOKED = "revoked"


class KnowledgeVaultSource(str, Enum):
    JARVIS_MANAGED = "jarvis_managed"


class KnowledgeDocumentKind(str, Enum):
    REPORT = "report"
    NOTE = "note"
    SOURCE = "source"


class ScheduledTaskStatus(str, Enum):
    ACTIVE = "active"
    PAUSED = "paused"


class ScheduleRecurrence(str, Enum):
    DAILY = "daily"
    WEEKLY = "weekly"


class ScheduledExecutionStatus(str, Enum):
    PENDING = "pending"
    DISPATCHING = "dispatching"
    DISPATCHED = "dispatched"
    FAILED = "failed"


class McpServerStatus(str, Enum):
    DISCONNECTED = "disconnected"
    CONNECTED = "connected"
    ERROR = "error"


class McpTransport(str, Enum):
    STDIO = "stdio"


class MemoryScopeType(str, Enum):
    GLOBAL = "global"
    WORKSPACE = "workspace"


class MemoryCategory(str, Enum):
    PREFERENCE = "preference"
    USER_FACT = "user_fact"
    PROJECT_FACT = "project_fact"
    RULE = "rule"


class MemoryStatus(str, Enum):
    ACTIVE = "active"
    DISABLED = "disabled"


class MemorySourceType(str, Enum):
    USER_EXPLICIT = "user_explicit"
    CANDIDATE_APPROVED = "candidate_approved"


class MemoryCandidateStatus(str, Enum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    EXPIRED = "expired"


class MemorySensitivity(str, Enum):
    NORMAL = "normal"
    SENSITIVE = "sensitive"


class MemoryExtractionJobStatus(str, Enum):
    QUEUED = "queued"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"


# ── 领域模型 ──

@dataclass
class KnowledgeVault:
    """Jarvis 管理的独立 Obsidian Vault。"""

    id: UUID
    name: str
    root_path: str
    canonical_path: str
    status: KnowledgeVaultStatus = KnowledgeVaultStatus.ACTIVE
    source: KnowledgeVaultSource = KnowledgeVaultSource.JARVIS_MANAGED
    created_at: datetime = field(default_factory=utcnow)
    updated_at: datetime = field(default_factory=utcnow)
    revoked_at: Optional[datetime] = None


@dataclass
class KnowledgeDocument:
    """Obsidian Markdown 文档的可查询元数据；正文真源仍是本地文件。"""

    id: UUID
    vault_id: UUID
    title: str
    kind: KnowledgeDocumentKind
    relative_path: str
    content_hash: str
    size_bytes: int
    tags: list[str] = field(default_factory=list)
    source_urls: list[str] = field(default_factory=list)
    source_task_id: Optional[UUID] = None
    source_run_id: Optional[UUID] = None
    created_at: datetime = field(default_factory=utcnow)
    updated_at: datetime = field(default_factory=utcnow)


@dataclass
class ScheduledTask:
    id: UUID
    name: str
    user_goal: str
    recurrence: ScheduleRecurrence
    timezone: str
    hour: int
    minute: int
    next_run_at: datetime
    weekday: Optional[int] = None
    workspace_id: Optional[UUID] = None
    status: ScheduledTaskStatus = ScheduledTaskStatus.ACTIVE
    authorized_tools: list[str] = field(default_factory=lambda: ["knowledge.create_document"])
    task_kind: str = "knowledge_report"
    source_policy: dict = field(default_factory=dict)
    last_run_at: Optional[datetime] = None
    last_task_id: Optional[UUID] = None
    last_run_id: Optional[UUID] = None
    version: int = 1
    created_at: datetime = field(default_factory=utcnow)
    updated_at: datetime = field(default_factory=utcnow)


@dataclass
class ScheduledTaskExecution:
    id: UUID
    scheduled_task_id: UUID
    scheduled_for: datetime
    status: ScheduledExecutionStatus = ScheduledExecutionStatus.PENDING
    task_id: Optional[UUID] = None
    run_id: Optional[UUID] = None
    attempts: int = 0
    lease_until: Optional[datetime] = None
    error_code: Optional[str] = None
    created_at: datetime = field(default_factory=utcnow)
    updated_at: datetime = field(default_factory=utcnow)


@dataclass
class McpServer:
    id: UUID
    slug: str
    name: str
    transport: McpTransport
    command: str
    args: list[str] = field(default_factory=list)
    env_keys: list[str] = field(default_factory=list)
    enabled: bool = True
    status: McpServerStatus = McpServerStatus.DISCONNECTED
    last_error_code: Optional[str] = None
    last_connected_at: Optional[datetime] = None
    version: int = 1
    created_at: datetime = field(default_factory=utcnow)
    updated_at: datetime = field(default_factory=utcnow)


@dataclass
class McpTool:
    id: UUID
    server_id: UUID
    original_name: str
    internal_name: str
    description: str
    input_schema: dict
    risk_level: str = "L3"
    enabled: bool = True
    discovered_at: datetime = field(default_factory=utcnow)
    updated_at: datetime = field(default_factory=utcnow)

@dataclass
class Conversation:
    """对话会话。1:N Task, 1:N Message。"""
    id: UUID
    title: Optional[str] = None
    created_at: datetime = field(default_factory=utcnow)
    updated_at: datetime = field(default_factory=utcnow)


@dataclass
class Message:
    id: UUID
    conversation_id: UUID
    role: str
    content: str
    task_id: Optional[UUID] = None
    run_id: Optional[UUID] = None
    tool_call_id: Optional[UUID] = None
    created_at: datetime = field(default_factory=utcnow)
    metadata: dict = field(default_factory=dict)

    def __post_init__(self):
        if self.role not in ("system", "user", "assistant", "tool"):
            raise ValueError(f"非法 role: {self.role}")


@dataclass
class Memory:
    """经用户确认、可跨会话复用的长期记忆。"""

    id: UUID
    scope_type: MemoryScopeType
    category: MemoryCategory
    key: str
    content: str
    workspace_id: Optional[UUID] = None
    status: MemoryStatus = MemoryStatus.ACTIVE
    source_type: MemorySourceType = MemorySourceType.USER_EXPLICIT
    source_task_id: Optional[UUID] = None
    importance: int = 50
    version: int = 1
    created_at: datetime = field(default_factory=utcnow)
    updated_at: datetime = field(default_factory=utcnow)

    def __post_init__(self):
        if self.scope_type is MemoryScopeType.GLOBAL and self.workspace_id is not None:
            raise ValueError("global memory 不能绑定 workspace_id")
        if self.scope_type is MemoryScopeType.WORKSPACE and self.workspace_id is None:
            raise ValueError("workspace memory 必须绑定 workspace_id")
        if not 0 <= self.importance <= 100:
            raise ValueError("memory importance 必须在 0..100")


@dataclass
class MemoryCandidate:
    """LLM 提取但尚未获用户确认的长期记忆候选。"""

    id: UUID
    scope_type: MemoryScopeType
    category: MemoryCategory
    suggested_key: str
    content: str
    source_task_id: UUID
    source_run_id: UUID
    confidence: float
    importance: int
    deduplication_key: str
    extraction_policy_version: str
    workspace_id: Optional[UUID] = None
    source_message_ids: list[UUID] = field(default_factory=list)
    extraction_input_fingerprint: str = ""
    sensitivity: MemorySensitivity = MemorySensitivity.NORMAL
    extractor_provider: str = ""
    extractor_model: str = ""
    status: MemoryCandidateStatus = MemoryCandidateStatus.PENDING
    conflict_memory_id: Optional[UUID] = None
    approved_memory_id: Optional[UUID] = None
    expires_at: Optional[datetime] = None
    resolved_at: Optional[datetime] = None
    resolution_note: str = ""
    version: int = 1
    created_at: datetime = field(default_factory=utcnow)
    updated_at: datetime = field(default_factory=utcnow)

    def __post_init__(self):
        if self.scope_type is MemoryScopeType.GLOBAL and self.workspace_id is not None:
            raise ValueError("global memory candidate 不能绑定 workspace_id")
        if self.scope_type is MemoryScopeType.WORKSPACE and self.workspace_id is None:
            raise ValueError("workspace memory candidate 必须绑定 workspace_id")
        if not 0.0 <= self.confidence <= 1.0:
            raise ValueError("memory candidate confidence 必须在 0..1")
        if not 0 <= self.importance <= 100:
            raise ValueError("memory candidate importance 必须在 0..100")


@dataclass
class MemoryExtractionJob:
    """异步候选提取的持久化状态；不属于源 AgentRun 的事件流。"""

    id: UUID
    source_task_id: UUID
    source_run_id: UUID
    extraction_policy_version: str
    status: MemoryExtractionJobStatus = MemoryExtractionJobStatus.QUEUED
    attempts: int = 0
    next_retry_at: Optional[datetime] = None
    error_code: Optional[str] = None
    created_at: datetime = field(default_factory=utcnow)
    updated_at: datetime = field(default_factory=utcnow)


@dataclass
class Task:
    id: UUID
    title: str
    user_goal: str
    conversation_id: UUID  # FK → conversations.id（1:N）
    status: TaskStatus = TaskStatus.PENDING
    priority: int = 0
    workspace_path: Optional[str] = None
    workspace_id: Optional[UUID] = None  # FK → workspaces.id
    active_run_id: Optional[UUID] = None
    last_step_summary: Optional[str] = None
    risk_level: Optional[str] = None
    created_at: datetime = field(default_factory=utcnow)
    updated_at: datetime = field(default_factory=utcnow)
    completed_at: Optional[datetime] = None
    cancelled_at: Optional[datetime] = None
    metadata: dict = field(default_factory=dict)
    scheduled_execution_id: Optional[UUID] = None


@dataclass
class AgentRun:
    id: UUID
    task_id: UUID
    agent_id: str = "default"
    mode: str = "single_agent"
    status: RunStatus = RunStatus.QUEUED
    version: int = 1
    worker_id: Optional[str] = None
    lease_until: Optional[datetime] = None
    current_step_id: Optional[UUID] = None
    final_output_artifact_id: Optional[UUID] = None
    max_steps: int = 20
    step_count: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    estimated_cost_usd: Optional[float] = None
    created_at: datetime = field(default_factory=utcnow)
    updated_at: datetime = field(default_factory=utcnow)
    started_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None
    failed_at: Optional[datetime] = None
    error: Optional[dict] = None
    checkpoint: dict = field(default_factory=dict)
    metadata: dict = field(default_factory=dict)

    @property
    def trace_id(self) -> Optional[UUID]:
        """返回创建 Run 时保存的链路 trace_id。"""
        value = self.metadata.get("trace_id")
        if not value:
            return None
        try:
            return UUID(str(value))
        except (TypeError, ValueError):
            return None


@dataclass
class ExecutionStep:
    id: UUID
    run_id: UUID
    task_id: UUID
    type: StepType
    status: StepStatus = StepStatus.PENDING
    parent_step_id: Optional[UUID] = None
    title: str = ""
    summary: Optional[str] = None
    input_data: Optional[dict] = None
    output_data: Optional[dict] = None
    error: Optional[dict] = None
    started_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None
    duration_ms: Optional[int] = None
    order_index: int = 0
    metadata: dict = field(default_factory=dict)


@dataclass
class RuntimeEvent:
    id: UUID
    event_id: UUID
    type: str
    payload: dict
    task_id: Optional[UUID] = None
    run_id: Optional[UUID] = None
    step_id: Optional[UUID] = None
    event_sequence: int = 0
    created_at: datetime = field(default_factory=utcnow)


@dataclass
class ToolCall:
    id: UUID
    task_id: UUID
    run_id: UUID
    step_id: UUID
    provider: str
    tool_name: str
    risk_level: str
    arguments: dict
    mcp_server_id: Optional[str] = None
    arguments_summary: Optional[dict] = None
    result: Optional[dict] = None
    result_summary: Optional[str] = None
    permission_request_id: Optional[UUID] = None
    permission_status: str = "not_required"
    status: str = "pending"
    error: Optional[dict] = None
    started_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None
    duration_ms: Optional[int] = None


@dataclass
class PermissionRequest:
    id: UUID
    task_id: UUID
    run_id: UUID
    tool_name: str
    action_summary: str
    risk_level: str
    scope: dict
    arguments_summary: dict
    allowed_decisions: list = field(default_factory=list)
    # 仅供 Runtime 恢复权限中断使用，不属于对外 DTO，也不得写入 RuntimeEvent。
    checkpoint: dict = field(default_factory=dict)
    step_id: Optional[UUID] = None
    tool_call_id: Optional[UUID] = None
    reason: Optional[str] = None
    status: PermissionStatus = PermissionStatus.PENDING
    decision: Optional[str] = None
    decided_at: Optional[datetime] = None
    note: Optional[str] = None
    created_at: datetime = field(default_factory=utcnow)
    expires_at: Optional[datetime] = None


@dataclass
class PermissionGrant:
    id: UUID
    grant_type: str
    risk_level_max: str
    tool_name: Optional[str] = None
    mcp_server_id: Optional[str] = None
    workspace_path: Optional[str] = None
    path: Optional[str] = None
    created_from_request_id: Optional[UUID] = None
    created_at: datetime = field(default_factory=utcnow)
    expires_at: Optional[datetime] = None
    revoked_at: Optional[datetime] = None
    metadata: dict = field(default_factory=dict)


@dataclass
class AuditLog:
    id: UUID
    event_type: str
    actor: str
    action_summary: str
    task_id: Optional[UUID] = None
    run_id: Optional[UUID] = None
    step_id: Optional[UUID] = None
    tool_call_id: Optional[UUID] = None
    risk_level: Optional[str] = None
    permission_decision: Optional[str] = None
    details: dict = field(default_factory=dict)
    result_summary: Optional[str] = None
    error: Optional[dict] = None
    created_at: datetime = field(default_factory=utcnow)


@dataclass
class Artifact:
    id: UUID
    task_id: UUID
    run_id: UUID
    kind: str
    title: str
    purpose: str
    producer_type: str
    source_tool_call_id: Optional[UUID] = None
    step_id: Optional[UUID] = None
    content: Optional[str] = None
    file_path: Optional[str] = None
    file_size_bytes: Optional[int] = None
    mime_type: Optional[str] = None
    content_hash: Optional[str] = None
    metadata: dict = field(default_factory=dict)
    created_at: datetime = field(default_factory=utcnow)


@dataclass
class OutboxEvent:
    id: UUID
    event_id: UUID
    aggregate_type: str
    aggregate_id: UUID
    event_type: str
    schema_version: str
    payload: dict
    trace_id: UUID
    correlation_id: Optional[UUID] = None
    causation_id: Optional[UUID] = None
    status: OutboxStatus = OutboxStatus.PENDING
    retry_count: int = 0
    # Redis 短暂重启不能在约 3 秒内耗尽 durable Outbox；20 次在 60 秒封顶退避下
    # 仍然有界，同时覆盖本地基础设施的正常重启窗口。
    max_retries: int = 20
    next_retry_at: datetime = field(default_factory=utcnow)
    claimed_by: Optional[str] = None
    claimed_at: Optional[datetime] = None
    lease_until: Optional[datetime] = None
    error_code: Optional[str] = None
    error_message: Optional[str] = None
    created_at: datetime = field(default_factory=utcnow)
    delivered_at: Optional[datetime] = None


@dataclass
class InboxEvent:
    id: UUID
    source: str
    source_event_id: str
    status: InboxStatus = InboxStatus.PROCESSING
    processed_at: Optional[datetime] = None
    created_at: datetime = field(default_factory=utcnow)


# ── Workspace ─────────────────────────────────────────────────


@dataclass
class Workspace:
    """注册的工作区。canonical_path 使用 realpath 后的绝对路径且唯一。"""
    id: UUID
    name: str
    root_path: str  # 用户选择的原始路径
    canonical_path: str  # realpath 后的规范化路径（unique）
    status: WorkspaceStatus = WorkspaceStatus.ACTIVE
    source: WorkspaceSource = WorkspaceSource.USER_PICKER
    created_at: datetime = field(default_factory=utcnow)
    updated_at: datetime = field(default_factory=utcnow)
    revoked_at: Optional[datetime] = None
    metadata: dict = field(default_factory=dict)
