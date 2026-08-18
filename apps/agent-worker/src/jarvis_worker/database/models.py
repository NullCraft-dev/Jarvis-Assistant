"""PostgreSQL SQLAlchemy ORM 模型。

所有表使用 UUID 主键、TIMESTAMPTZ 时间、JSONB 结构化数据。
与 domain/models.py 中的纯 Python dataclass 保持独立——ORM 模型只存在于 storage/postgres 内部。
"""

import uuid
from datetime import datetime, timezone

from pgvector.sqlalchemy import VECTOR
from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    Float,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


def _uuid_pk():
    return mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)


def _utcnow():
    return datetime.now(timezone.utc)


# ── conversations ───────────────────────────────────────────────

class ConversationModel(Base):
    __tablename__ = "conversations"

    id: Mapped[uuid.UUID] = _uuid_pk()
    title: Mapped[str | None] = mapped_column(String(500), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)

    __table_args__ = ()


# ── messages ────────────────────────────────────────────────────

class MessageModel(Base):
    __tablename__ = "messages"

    id: Mapped[uuid.UUID] = _uuid_pk()
    conversation_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("conversations.id"))
    task_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("tasks.id"), nullable=True)
    run_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    role: Mapped[str] = mapped_column(String(20))
    content: Mapped[str] = mapped_column(Text)
    tool_call_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    metadata_json: Mapped[dict] = mapped_column(JSONB, default=dict)

    __table_args__ = (
        CheckConstraint("role IN ('system','user','assistant','tool')", name="ck_messages_role"),
        Index("idx_messages_conv_created", "conversation_id", "created_at"),
        Index("idx_messages_task_id", "task_id"),
    )


# ── memories ────────────────────────────────────────────────────

class MemoryModel(Base):
    __tablename__ = "memories"

    id: Mapped[uuid.UUID] = _uuid_pk()
    scope_type: Mapped[str] = mapped_column(String(20))
    workspace_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=True
    )
    category: Mapped[str] = mapped_column(String(30))
    key: Mapped[str] = mapped_column(String(128))
    content: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(20), default="active")
    source_type: Mapped[str] = mapped_column(String(30), default="user_explicit")
    source_task_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tasks.id", ondelete="SET NULL"), nullable=True
    )
    importance: Mapped[int] = mapped_column(Integer, default=50)
    version: Mapped[int] = mapped_column(Integer, default=1)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)

    __table_args__ = (
        CheckConstraint("scope_type IN ('global','workspace')", name="ck_memories_scope_type"),
        CheckConstraint(
            "(scope_type = 'global' AND workspace_id IS NULL) OR "
            "(scope_type = 'workspace' AND workspace_id IS NOT NULL)",
            name="ck_memories_scope_owner",
        ),
        CheckConstraint(
            "category IN ('preference','user_fact','project_fact','rule')",
            name="ck_memories_category",
        ),
        CheckConstraint("status IN ('active','disabled')", name="ck_memories_status"),
        CheckConstraint(
            "source_type IN ('user_explicit','candidate_approved')",
            name="ck_memories_source_type",
        ),
        CheckConstraint("importance BETWEEN 0 AND 100", name="ck_memories_importance"),
        Index("idx_memories_context", "status", "scope_type", "workspace_id", "importance"),
        Index("idx_memories_updated", "updated_at"),
    )


class MemoryCandidateModel(Base):
    __tablename__ = "memory_candidates"

    id: Mapped[uuid.UUID] = _uuid_pk()
    scope_type: Mapped[str] = mapped_column(String(20))
    workspace_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("workspaces.id", ondelete="SET NULL"), nullable=True
    )
    category: Mapped[str] = mapped_column(String(30))
    suggested_key: Mapped[str] = mapped_column(String(128))
    content: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(20), default="pending")
    source_task_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tasks.id", ondelete="CASCADE")
    )
    source_run_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("agent_runs.id", ondelete="CASCADE")
    )
    source_message_ids: Mapped[list] = mapped_column(JSONB, default=list)
    extraction_input_fingerprint: Mapped[str] = mapped_column(String(64))
    confidence: Mapped[float] = mapped_column(Float)
    importance: Mapped[int] = mapped_column(Integer)
    sensitivity: Mapped[str] = mapped_column(String(20), default="normal")
    deduplication_key: Mapped[str] = mapped_column(String(64))
    extraction_policy_version: Mapped[str] = mapped_column(String(80))
    extractor_provider: Mapped[str] = mapped_column(String(80), default="")
    extractor_model: Mapped[str] = mapped_column(String(160), default="")
    conflict_memory_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("memories.id", ondelete="SET NULL"), nullable=True
    )
    approved_memory_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("memories.id", ondelete="SET NULL"), nullable=True
    )
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    resolution_note: Mapped[str] = mapped_column(String(500), default="")
    version: Mapped[int] = mapped_column(Integer, default=1)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)

    __table_args__ = (
        CheckConstraint("scope_type IN ('global','workspace')", name="ck_memory_candidates_scope"),
        CheckConstraint(
            "(scope_type = 'global' AND workspace_id IS NULL) OR "
            "(scope_type = 'workspace' AND workspace_id IS NOT NULL)",
            name="ck_memory_candidates_scope_owner",
        ),
        CheckConstraint(
            "category IN ('preference','user_fact','project_fact','rule')",
            name="ck_memory_candidates_category",
        ),
        CheckConstraint(
            "status IN ('pending','approved','rejected','expired')",
            name="ck_memory_candidates_status",
        ),
        CheckConstraint("confidence BETWEEN 0 AND 1", name="ck_memory_candidates_confidence"),
        CheckConstraint("importance BETWEEN 0 AND 100", name="ck_memory_candidates_importance"),
        CheckConstraint("sensitivity IN ('normal','sensitive')", name="ck_memory_candidates_sensitivity"),
        Index("idx_memory_candidates_status_created", "status", "created_at"),
        Index("idx_memory_candidates_workspace_status", "workspace_id", "status"),
        Index("idx_memory_candidates_status_expires", "status", "expires_at"),
        Index(
            "uq_memory_candidates_pending_dedup",
            "deduplication_key",
            unique=True,
            postgresql_where=text("status = 'pending'"),
        ),
        UniqueConstraint(
            "source_run_id", "extraction_policy_version", "deduplication_key",
            name="uq_memory_candidates_run_policy_dedup",
        ),
    )


class MemoryExtractionJobModel(Base):
    __tablename__ = "memory_extraction_jobs"

    id: Mapped[uuid.UUID] = _uuid_pk()
    source_task_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tasks.id", ondelete="CASCADE")
    )
    source_run_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("agent_runs.id", ondelete="CASCADE")
    )
    extraction_policy_version: Mapped[str] = mapped_column(String(80))
    status: Mapped[str] = mapped_column(String(20), default="queued")
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    next_retry_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    error_code: Mapped[str | None] = mapped_column(String(80), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)

    __table_args__ = (
        CheckConstraint(
            "status IN ('queued','running','completed','failed')",
            name="ck_memory_extraction_jobs_status",
        ),
        CheckConstraint("attempts BETWEEN 0 AND 10", name="ck_memory_extraction_jobs_attempts"),
        UniqueConstraint(
            "source_run_id", "extraction_policy_version",
            name="uq_memory_extraction_jobs_run_policy",
        ),
        Index("idx_memory_extraction_jobs_retry", "status", "next_retry_at"),
    )


# ── tasks ───────────────────────────────────────────────────────

class TaskModel(Base):
    __tablename__ = "tasks"

    id: Mapped[uuid.UUID] = _uuid_pk()
    conversation_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("conversations.id"))
    title: Mapped[str] = mapped_column(String(500))
    user_goal: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(30))
    priority: Mapped[int] = mapped_column(Integer, default=0)
    workspace_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    workspace_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("workspaces.id", ondelete="SET NULL"), nullable=True)
    active_run_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    last_step_summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    risk_level: Mapped[str | None] = mapped_column(String(5), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    cancelled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    metadata_json: Mapped[dict] = mapped_column(JSONB, default=dict)
    scheduled_execution_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True, unique=True)

    __table_args__ = (
        CheckConstraint(
            "status IN ('pending','running','waiting_for_user','blocked','failed','completed','cancelled')",
            name="ck_tasks_status",
        ),
        Index("idx_tasks_status_updated", "status", "updated_at"),
    )


# ── agent_runs ──────────────────────────────────────────────────

class AgentRunModel(Base):
    __tablename__ = "agent_runs"

    id: Mapped[uuid.UUID] = _uuid_pk()
    task_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("tasks.id"))
    agent_id: Mapped[str] = mapped_column(String(100), default="default")
    mode: Mapped[str] = mapped_column(String(20), default="single_agent")
    status: Mapped[str] = mapped_column(String(30))
    version: Mapped[int] = mapped_column(Integer, default=1)
    worker_id: Mapped[str | None] = mapped_column(String(100), nullable=True)
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    current_step_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    final_output_artifact_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    max_steps: Mapped[int] = mapped_column(Integer, default=20)
    step_count: Mapped[int] = mapped_column(Integer, default=0)
    input_tokens: Mapped[int] = mapped_column(Integer, default=0)
    output_tokens: Mapped[int] = mapped_column(Integer, default=0)
    estimated_cost_usd: Mapped[float | None] = mapped_column(Float, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    failed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    error_json: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    checkpoint_json: Mapped[dict] = mapped_column(JSONB, default=dict)
    metadata_json: Mapped[dict] = mapped_column(JSONB, default=dict)

    __table_args__ = (
        CheckConstraint("mode IN ('single_agent','multi_agent')", name="ck_agent_runs_mode"),
        CheckConstraint(
            "status IN ('queued','running','waiting_permission','pause_requested','paused','resume_requested',"
            "'cancel_requested','cancelling','completed','failed','cancelled')",
            name="ck_agent_runs_status",
        ),
        Index("idx_agent_runs_task_id", "task_id"),
        Index("idx_agent_runs_status", "status"),
        Index("idx_agent_runs_worker_id", "worker_id"),
    )


# ── execution_steps ─────────────────────────────────────────────

class ExecutionStepModel(Base):
    __tablename__ = "execution_steps"

    id: Mapped[uuid.UUID] = _uuid_pk()
    run_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("agent_runs.id"))
    task_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("tasks.id"))
    parent_step_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    type: Mapped[str] = mapped_column(String(30))
    status: Mapped[str] = mapped_column(String(30))
    title: Mapped[str] = mapped_column(String(500))
    summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    input_json: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    output_json: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    error_json: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    duration_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    order_index: Mapped[int] = mapped_column(Integer, default=0)
    metadata_json: Mapped[dict] = mapped_column(JSONB, default=dict)

    __table_args__ = (
        CheckConstraint(
            "type IN ('user_message','system_event','model_call','plan_created','tool_call','mcp_call','observation','permission_request','review','final_output')",
            name="ck_execution_steps_type",
        ),
        CheckConstraint(
            "status IN ('pending','running','waiting_for_permission','completed','failed','cancelled','skipped')",
            name="ck_execution_steps_status",
        ),
        Index("idx_execution_steps_run_order", "run_id", "order_index"),
    )


# ── runtime_events ──────────────────────────────────────────────

class RuntimeEventModel(Base):
    __tablename__ = "runtime_events"

    id: Mapped[uuid.UUID] = _uuid_pk()
    event_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), unique=True)
    task_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    run_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    step_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    type: Mapped[str] = mapped_column(String(50))
    event_sequence: Mapped[int] = mapped_column(BigInteger, default=0)
    payload_json: Mapped[dict] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)

    __table_args__ = (
        Index("idx_runtime_events_run_created", "run_id", "created_at"),
        Index("idx_runtime_events_type", "type"),
    )


# ── tool_calls ──────────────────────────────────────────────────

class ToolCallModel(Base):
    __tablename__ = "tool_calls"

    id: Mapped[uuid.UUID] = _uuid_pk()
    task_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("tasks.id"))
    run_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("agent_runs.id"))
    step_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("execution_steps.id"))
    provider: Mapped[str] = mapped_column(String(20))
    tool_name: Mapped[str] = mapped_column(String(200))
    mcp_server_id: Mapped[str | None] = mapped_column(String(200), nullable=True)
    risk_level: Mapped[str] = mapped_column(String(5))
    arguments_json: Mapped[dict] = mapped_column(JSONB)
    arguments_summary_json: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    result_json: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    result_summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    permission_request_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    permission_status: Mapped[str] = mapped_column(String(20), default="not_required")
    status: Mapped[str] = mapped_column(String(20))
    error_json: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    duration_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)

    __table_args__ = (
        CheckConstraint("provider IN ('native','mcp','system')", name="ck_tool_calls_provider"),
        CheckConstraint(
            "permission_status IN ('not_required','pending','approved','denied','expired')",
            name="ck_tool_calls_permission_status",
        ),
        CheckConstraint(
            "status IN ('pending','running','completed','failed','cancelled')",
            name="ck_tool_calls_status",
        ),
        Index("idx_tool_calls_run_id", "run_id"),
        Index("idx_tool_calls_tool_name", "tool_name"),
        Index("idx_tool_calls_status", "status"),
    )


# ── permission_requests ─────────────────────────────────────────

class PermissionRequestModel(Base):
    __tablename__ = "permission_requests"

    id: Mapped[uuid.UUID] = _uuid_pk()
    task_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("tasks.id"))
    run_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("agent_runs.id"))
    step_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    tool_call_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    tool_name: Mapped[str] = mapped_column(String(200))
    action_summary: Mapped[str] = mapped_column(Text)
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    risk_level: Mapped[str] = mapped_column(String(5))
    scope_json: Mapped[dict] = mapped_column(JSONB)
    arguments_summary_json: Mapped[dict] = mapped_column(JSONB)
    allowed_decisions_json: Mapped[list] = mapped_column(JSONB, default=list)
    checkpoint_json: Mapped[dict] = mapped_column(JSONB, default=dict)
    status: Mapped[str] = mapped_column(String(20))
    decision: Mapped[str | None] = mapped_column(String(30), nullable=True)
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    __table_args__ = (
        CheckConstraint("status IN ('pending','approved','denied','expired','consumed')", name="ck_perm_req_status"),
        Index("idx_permission_requests_status", "status"),
        Index("idx_permission_requests_run_id", "run_id"),
        Index("idx_permission_requests_status_expires", "status", "expires_at"),
    )


# ── permission_grants ───────────────────────────────────────────

class PermissionGrantModel(Base):
    __tablename__ = "permission_grants"

    id: Mapped[uuid.UUID] = _uuid_pk()
    grant_type: Mapped[str] = mapped_column(String(30))
    tool_name: Mapped[str | None] = mapped_column(String(200), nullable=True)
    mcp_server_id: Mapped[str | None] = mapped_column(String(200), nullable=True)
    workspace_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    path: Mapped[str | None] = mapped_column(Text, nullable=True)
    risk_level_max: Mapped[str] = mapped_column(String(5))
    created_from_request_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("permission_requests.id"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    metadata_json: Mapped[dict] = mapped_column(JSONB, default=dict)

    __table_args__ = (
        CheckConstraint("grant_type IN ('once','task','tool_path','workspace','global')", name="ck_perm_grant_type"),
        Index("idx_permission_grants_tool", "tool_name"),
    )


# ── audit_logs ──────────────────────────────────────────────────

class AuditLogModel(Base):
    __tablename__ = "audit_logs"

    id: Mapped[uuid.UUID] = _uuid_pk()
    task_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    run_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    step_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    tool_call_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    event_type: Mapped[str] = mapped_column(String(50))
    actor: Mapped[str] = mapped_column(String(100))
    risk_level: Mapped[str | None] = mapped_column(String(5), nullable=True)
    permission_decision: Mapped[str | None] = mapped_column(String(30), nullable=True)
    action_summary: Mapped[str] = mapped_column(Text)
    details_json: Mapped[dict] = mapped_column(JSONB, default=dict)
    result_summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    error_json: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)

    __table_args__ = (
        Index("idx_audit_logs_task_run", "task_id", "run_id"),
        Index("idx_audit_logs_event_type", "event_type"),
        Index("idx_audit_logs_created_at", "created_at"),
    )


# ── artifacts ───────────────────────────────────────────────────

class ArtifactModel(Base):
    __tablename__ = "artifacts"

    id: Mapped[uuid.UUID] = _uuid_pk()
    task_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("tasks.id"))
    run_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("agent_runs.id"))
    step_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    kind: Mapped[str] = mapped_column(String(30))
    title: Mapped[str] = mapped_column(String(500))
    purpose: Mapped[str] = mapped_column(String(30), default="final_response")
    producer_type: Mapped[str] = mapped_column(String(20), default="runtime")
    source_tool_call_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tool_calls.id"), nullable=True
    )
    content: Mapped[str | None] = mapped_column(Text, nullable=True)
    file_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    file_size_bytes: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    mime_type: Mapped[str | None] = mapped_column(String(100), nullable=True)
    content_hash: Mapped[str | None] = mapped_column(String(128), nullable=True)
    metadata_json: Mapped[dict] = mapped_column(JSONB, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)

    __table_args__ = (
        CheckConstraint(
            "kind IN ('markdown','text','json','file','diff','screenshot')", name="ck_artifacts_kind"
        ),
        CheckConstraint(
            "purpose IN ('final_response','deliverable')",
            name="ck_artifacts_purpose",
        ),
        CheckConstraint(
            "producer_type IN ('runtime','tool')",
            name="ck_artifacts_producer_type",
        ),
        CheckConstraint(
            "(producer_type = 'runtime' AND source_tool_call_id IS NULL) OR "
            "(producer_type = 'tool' AND source_tool_call_id IS NOT NULL)",
            name="ck_artifacts_producer_source",
        ),
        Index("idx_artifacts_task_id", "task_id"),
        Index("idx_artifacts_run_id", "run_id"),
        Index("idx_artifacts_purpose", "purpose"),
        Index("idx_artifacts_source_tool_call_id", "source_tool_call_id"),
    )


# ── RAG ingestion ──────────────────────────────────────────────

class RagDocumentModel(Base):
    __tablename__ = "rag_documents"

    id: Mapped[uuid.UUID] = _uuid_pk()
    workspace_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("workspaces.id", ondelete="CASCADE")
    )
    source_artifact_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("artifacts.id", ondelete="RESTRICT")
    )
    title: Mapped[str] = mapped_column(String(500))
    mime_type: Mapped[str] = mapped_column(String(100))
    source_content_hash: Mapped[str] = mapped_column(String(64))
    ingestion_policy_version: Mapped[str] = mapped_column(String(80))
    status: Mapped[str] = mapped_column(String(20), default="indexing")
    parser_version: Mapped[str] = mapped_column(String(80), default="")
    chunker_version: Mapped[str] = mapped_column(String(80), default="")
    embedding_provider: Mapped[str] = mapped_column(String(80), default="")
    embedding_model: Mapped[str] = mapped_column(String(160), default="")
    embedding_dimensions: Mapped[int | None] = mapped_column(Integer, nullable=True)
    chunk_count: Mapped[int] = mapped_column(Integer, default=0)
    indexed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    disabled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    version: Mapped[int] = mapped_column(Integer, default=1)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)

    __table_args__ = (
        CheckConstraint(
            "status IN ('indexing','ready','failed','disabled')",
            name="ck_rag_documents_status",
        ),
        CheckConstraint("chunk_count >= 0", name="ck_rag_documents_chunk_count"),
        CheckConstraint(
            "embedding_dimensions IS NULL OR embedding_dimensions > 0",
            name="ck_rag_documents_embedding_dimensions",
        ),
        UniqueConstraint(
            "workspace_id", "source_artifact_id", "source_content_hash",
            name="uq_rag_documents_workspace_source",
        ),
        UniqueConstraint("id", "workspace_id", name="uq_rag_documents_id_workspace"),
        Index("idx_rag_documents_workspace_status", "workspace_id", "status"),
    )


class RagIngestionJobModel(Base):
    __tablename__ = "rag_ingestion_jobs"

    id: Mapped[uuid.UUID] = _uuid_pk()
    document_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True))
    workspace_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("workspaces.id", ondelete="CASCADE")
    )
    idempotency_key: Mapped[str] = mapped_column(String(64), unique=True)
    ingestion_policy_version: Mapped[str] = mapped_column(String(80))
    status: Mapped[str] = mapped_column(String(20), default="queued")
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    max_attempts: Mapped[int] = mapped_column(Integer, default=3)
    embedding_attempts: Mapped[int] = mapped_column(Integer, default=0)
    embedding_max_attempts: Mapped[int] = mapped_column(Integer, default=3)
    claimed_by: Mapped[str | None] = mapped_column(String(100), nullable=True)
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    next_retry_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    error_code: Mapped[str | None] = mapped_column(String(80), nullable=True)
    progress_json: Mapped[dict] = mapped_column(JSONB, default=dict)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    failed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    cancelled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)

    __table_args__ = (
        CheckConstraint(
            "status IN ('queued','parsing','chunking','embedding','completed','failed','cancelled')",
            name="ck_rag_ingestion_jobs_status",
        ),
        CheckConstraint(
            "attempts >= 0 AND max_attempts >= 1 AND attempts <= max_attempts",
            name="ck_rag_ingestion_jobs_attempts",
        ),
        CheckConstraint(
            "embedding_attempts >= 0 AND embedding_max_attempts >= 1 "
            "AND embedding_attempts <= embedding_max_attempts",
            name="ck_rag_ingestion_jobs_embedding_attempts",
        ),
        UniqueConstraint(
            "document_id", "ingestion_policy_version",
            name="uq_rag_ingestion_jobs_document_policy",
        ),
        UniqueConstraint("id", "workspace_id", name="uq_rag_ingestion_jobs_id_workspace"),
        ForeignKeyConstraint(
            ["document_id", "workspace_id"],
            ["rag_documents.id", "rag_documents.workspace_id"],
            name="fk_rag_ingestion_jobs_document_workspace",
            ondelete="CASCADE",
        ),
        Index(
            "idx_rag_ingestion_jobs_dispatch",
            "status", "next_retry_at", "lease_until", "created_at",
        ),
        Index("idx_rag_ingestion_jobs_workspace", "workspace_id", "created_at"),
    )


class RagChunkModel(Base):
    __tablename__ = "rag_chunks"

    id: Mapped[uuid.UUID] = _uuid_pk()
    document_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True))
    ingestion_job_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True))
    workspace_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("workspaces.id", ondelete="CASCADE")
    )
    ordinal: Mapped[int] = mapped_column(Integer)
    content: Mapped[str] = mapped_column(Text)
    content_hash: Mapped[str] = mapped_column(String(64))
    token_count: Mapped[int] = mapped_column(Integer)
    source_locator_json: Mapped[dict] = mapped_column(JSONB, default=dict)
    embedding_key: Mapped[str | None] = mapped_column(String(160), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)

    __table_args__ = (
        CheckConstraint("ordinal >= 0", name="ck_rag_chunks_ordinal"),
        CheckConstraint("token_count > 0", name="ck_rag_chunks_token_count"),
        UniqueConstraint(
            "ingestion_job_id", "ordinal", name="uq_rag_chunks_job_ordinal"
        ),
        UniqueConstraint(
            "id", "document_id", "workspace_id",
            name="uq_rag_chunks_id_document_workspace",
        ),
        ForeignKeyConstraint(
            ["document_id", "workspace_id"],
            ["rag_documents.id", "rag_documents.workspace_id"],
            name="fk_rag_chunks_document_workspace",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["ingestion_job_id", "workspace_id"],
            ["rag_ingestion_jobs.id", "rag_ingestion_jobs.workspace_id"],
            name="fk_rag_chunks_job_workspace",
            ondelete="CASCADE",
        ),
        Index("idx_rag_chunks_document_ordinal", "document_id", "ordinal"),
        Index("idx_rag_chunks_workspace", "workspace_id"),
    )


class RagChunkEmbeddingModel(Base):
    """Chunk 向量索引；正文仍由 rag_chunks 持有。"""

    __tablename__ = "rag_chunk_embeddings"

    chunk_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    document_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    workspace_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    provider_name: Mapped[str] = mapped_column(String(80), nullable=False)
    model_name: Mapped[str] = mapped_column(String(160), nullable=False)
    dimensions: Mapped[int] = mapped_column(Integer, nullable=False)
    embedding: Mapped[list[float]] = mapped_column(VECTOR(1536), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)

    __table_args__ = (
        CheckConstraint("dimensions = 1536", name="ck_rag_chunk_embeddings_dimensions"),
        ForeignKeyConstraint(
            ["chunk_id", "document_id", "workspace_id"],
            ["rag_chunks.id", "rag_chunks.document_id", "rag_chunks.workspace_id"],
            name="fk_rag_chunk_embeddings_chunk_scope",
            ondelete="CASCADE",
        ),
        Index("idx_rag_chunk_embeddings_workspace", "workspace_id"),
        Index(
            "idx_rag_chunk_embeddings_cosine_hnsw",
            "embedding",
            postgresql_using="hnsw",
            postgresql_ops={"embedding": "vector_cosine_ops"},
        ),
    )


class RagElementModel(Base):
    __tablename__ = "rag_elements"

    id: Mapped[uuid.UUID] = _uuid_pk()
    document_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True))
    workspace_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("workspaces.id", ondelete="CASCADE")
    )
    element_type: Mapped[str] = mapped_column(String(20))
    page_number: Mapped[int] = mapped_column(Integer)
    bounding_box_json: Mapped[list] = mapped_column(JSONB)
    page_width: Mapped[float] = mapped_column(Float)
    page_height: Mapped[float] = mapped_column(Float)
    locator_key: Mapped[str] = mapped_column(String(64))
    content_hash: Mapped[str] = mapped_column(String(64))
    extraction_method: Mapped[str] = mapped_column(String(20))
    extraction_version: Mapped[str] = mapped_column(String(80))
    confidence: Mapped[float] = mapped_column(Float)
    caption_text: Mapped[str] = mapped_column(Text, default="")
    ocr_text: Mapped[str] = mapped_column(Text, default="")
    structured_data_json: Mapped[dict] = mapped_column(JSONB, default=dict)
    derived_description: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)

    __table_args__ = (
        CheckConstraint(
            "element_type IN ('image','figure','chart','table','diagram','equation')",
            name="ck_rag_elements_type",
        ),
        CheckConstraint(
            "extraction_method IN ('native','ocr','vision','hybrid')",
            name="ck_rag_elements_extraction_method",
        ),
        CheckConstraint("page_number >= 1", name="ck_rag_elements_page_number"),
        CheckConstraint(
            "page_width > 0 AND page_height > 0", name="ck_rag_elements_page_size"
        ),
        CheckConstraint(
            "jsonb_typeof(bounding_box_json) = 'array' "
            "AND jsonb_array_length(bounding_box_json) = 4",
            name="ck_rag_elements_bounding_box",
        ),
        CheckConstraint("confidence BETWEEN 0 AND 1", name="ck_rag_elements_confidence"),
        UniqueConstraint(
            "document_id", "locator_key", name="uq_rag_elements_document_locator"
        ),
        UniqueConstraint(
            "id", "document_id", "workspace_id",
            name="uq_rag_elements_id_document_workspace",
        ),
        ForeignKeyConstraint(
            ["document_id", "workspace_id"],
            ["rag_documents.id", "rag_documents.workspace_id"],
            name="fk_rag_elements_document_workspace",
            ondelete="CASCADE",
        ),
        Index(
            "idx_rag_elements_document_page",
            "document_id", "page_number", "element_type",
        ),
        Index("idx_rag_elements_workspace", "workspace_id"),
    )


class RagAssetModel(Base):
    __tablename__ = "rag_assets"

    id: Mapped[uuid.UUID] = _uuid_pk()
    document_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True))
    element_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True))
    workspace_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("workspaces.id", ondelete="CASCADE")
    )
    asset_kind: Mapped[str] = mapped_column(String(30))
    storage_reference: Mapped[str] = mapped_column(Text, unique=True)
    mime_type: Mapped[str] = mapped_column(String(100))
    content_hash: Mapped[str] = mapped_column(String(64))
    size_bytes: Mapped[int] = mapped_column(BigInteger)
    width: Mapped[int | None] = mapped_column(Integer, nullable=True)
    height: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)

    __table_args__ = (
        CheckConstraint(
            "asset_kind IN ('crop','embedded_image','page_render')",
            name="ck_rag_assets_kind",
        ),
        CheckConstraint("size_bytes > 0", name="ck_rag_assets_size"),
        CheckConstraint(
            "(width IS NULL AND height IS NULL) OR (width > 0 AND height > 0)",
            name="ck_rag_assets_dimensions",
        ),
        CheckConstraint(
            "storage_reference <> '' AND storage_reference NOT LIKE '/%' "
            "AND storage_reference NOT LIKE '%\\\\%' "
            "AND storage_reference <> '..' AND storage_reference NOT LIKE '../%' "
            "AND storage_reference NOT LIKE '%/../%'",
            name="ck_rag_assets_storage_reference",
        ),
        ForeignKeyConstraint(
            ["document_id", "workspace_id"],
            ["rag_documents.id", "rag_documents.workspace_id"],
            name="fk_rag_assets_document_workspace",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["element_id", "document_id", "workspace_id"],
            ["rag_elements.id", "rag_elements.document_id", "rag_elements.workspace_id"],
            name="fk_rag_assets_element_document_workspace",
            ondelete="CASCADE",
        ),
        Index("idx_rag_assets_element", "element_id"),
        Index("idx_rag_assets_workspace", "workspace_id"),
    )


class RagChunkElementLinkModel(Base):
    __tablename__ = "rag_chunk_element_links"

    id: Mapped[uuid.UUID] = _uuid_pk()
    document_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True))
    workspace_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("workspaces.id", ondelete="CASCADE")
    )
    chunk_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True))
    element_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True))
    relation_type: Mapped[str] = mapped_column(String(20))
    confidence: Mapped[float] = mapped_column(Float)
    order_index: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)

    __table_args__ = (
        CheckConstraint(
            "relation_type IN ('contains','references','explains','caption_of','nearby')",
            name="ck_rag_chunk_element_links_relation",
        ),
        CheckConstraint(
            "confidence BETWEEN 0 AND 1", name="ck_rag_chunk_element_links_confidence"
        ),
        CheckConstraint(
            "order_index >= 0", name="ck_rag_chunk_element_links_order"
        ),
        UniqueConstraint(
            "chunk_id", "element_id", "relation_type",
            name="uq_rag_chunk_element_links_relation",
        ),
        ForeignKeyConstraint(
            ["chunk_id", "document_id", "workspace_id"],
            ["rag_chunks.id", "rag_chunks.document_id", "rag_chunks.workspace_id"],
            name="fk_rag_chunk_element_links_chunk_document_workspace",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["element_id", "document_id", "workspace_id"],
            ["rag_elements.id", "rag_elements.document_id", "rag_elements.workspace_id"],
            name="fk_rag_chunk_element_links_element_document_workspace",
            ondelete="CASCADE",
        ),
        Index(
            "idx_rag_chunk_element_links_chunk", "workspace_id", "chunk_id", "order_index"
        ),
        Index(
            "idx_rag_chunk_element_links_element", "workspace_id", "element_id"
        ),
    )


# ── RAG evaluation flywheel ────────────────────────────────────

class RagEvaluationTraceModel(Base):
    """一次真实生产 RAG 检索的无正文阶段快照。"""

    __tablename__ = "rag_evaluation_traces"

    id: Mapped[uuid.UUID] = _uuid_pk()
    workspace_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("workspaces.id", ondelete="CASCADE")
    )
    task_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tasks.id", ondelete="CASCADE")
    )
    run_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("agent_runs.id", ondelete="CASCADE")
    )
    step_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("execution_steps.id", ondelete="SET NULL"), nullable=True
    )
    query_text: Mapped[str] = mapped_column(Text)
    query_hash: Mapped[str] = mapped_column(String(64))
    request_json: Mapped[dict] = mapped_column(JSONB, default=dict)
    pipeline_versions_json: Mapped[dict] = mapped_column(JSONB, default=dict)
    candidate_ranking_json: Mapped[list] = mapped_column(JSONB, default=list)
    reranked_ranking_json: Mapped[list] = mapped_column(JSONB, default=list)
    context_chunk_ids_json: Mapped[list] = mapped_column(JSONB, default=list)
    context_truncated: Mapped[bool] = mapped_column(Boolean, default=False)
    result_count: Mapped[int] = mapped_column(Integer, default=0)
    privacy_status: Mapped[str] = mapped_column(String(20), default="pending")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)

    __table_args__ = (
        CheckConstraint("length(query_text) BETWEEN 1 AND 2000", name="ck_rag_eval_traces_query"),
        CheckConstraint("result_count >= 0", name="ck_rag_eval_traces_result_count"),
        CheckConstraint(
            "privacy_status IN ('pending','approved','rejected')",
            name="ck_rag_eval_traces_privacy",
        ),
        Index("idx_rag_eval_traces_workspace_created", "workspace_id", "created_at"),
        Index("idx_rag_eval_traces_privacy_created", "privacy_status", "created_at"),
        Index("idx_rag_eval_traces_run", "run_id"),
    )


class RagEvaluationLabelModel(Base):
    """由用户反馈或人工复核形成的 RAG 证据标签。"""

    __tablename__ = "rag_evaluation_labels"

    id: Mapped[uuid.UUID] = _uuid_pk()
    trace_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("rag_evaluation_traces.id", ondelete="CASCADE"),
        unique=True,
    )
    positive_chunk_ids_json: Mapped[list] = mapped_column(JSONB, default=list)
    hard_negative_chunk_ids_json: Mapped[list] = mapped_column(JSONB, default=list)
    source: Mapped[str] = mapped_column(String(30))
    status: Mapped[str] = mapped_column(String(20), default="draft")
    notes: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)

    __table_args__ = (
        CheckConstraint(
            "source IN ('user_feedback','human_review','citation_validator','judge')",
            name="ck_rag_eval_labels_source",
        ),
        CheckConstraint(
            "status IN ('draft','confirmed','rejected','promoted')",
            name="ck_rag_eval_labels_status",
        ),
        CheckConstraint(
            "jsonb_typeof(positive_chunk_ids_json) = 'array' "
            "AND jsonb_array_length(positive_chunk_ids_json) > 0",
            name="ck_rag_eval_labels_positive_chunks",
        ),
        Index("idx_rag_eval_labels_status", "status", "updated_at"),
    )


class RagEvaluationFeedbackModel(Base):
    """用户对 RAG 回答的结构化反馈；仅作为待复核候选。"""

    __tablename__ = "rag_evaluation_feedback"

    id: Mapped[uuid.UUID] = _uuid_pk()
    trace_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("rag_evaluation_traces.id", ondelete="CASCADE")
    )


    workspace_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("workspaces.id", ondelete="CASCADE")
    )
    task_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tasks.id", ondelete="CASCADE")
    )
    run_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("agent_runs.id", ondelete="CASCADE")
    )
    message_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("messages.id", ondelete="CASCADE")
    )
    kind: Mapped[str] = mapped_column(String(30))
    citation_chunk_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("rag_chunks.id", ondelete="SET NULL"), nullable=True
    )
    status: Mapped[str] = mapped_column(String(20), default="pending")
    failure_category: Mapped[str | None] = mapped_column(String(30), nullable=True)
    fingerprint: Mapped[str] = mapped_column(String(64), unique=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)

    __table_args__ = (
        CheckConstraint(
            "kind IN ('helpful','unhelpful','citation_incorrect','evidence_insufficient')",
            name="ck_rag_eval_feedback_kind",
        ),
        CheckConstraint(
            "status IN ('pending','reviewed','dismissed')",
            name="ck_rag_eval_feedback_status",
        ),
        CheckConstraint(
            "failure_category IS NULL OR failure_category IN "
            "('candidate_miss','reranker_miss','context_omission','context_truncated',"
            "'citation_mismatch','answer_generation','insufficient_evidence','other')",
            name="ck_rag_eval_feedback_failure_category",
        ),
        CheckConstraint(
            "kind = 'citation_incorrect' OR citation_chunk_id IS NULL",
            name="ck_rag_eval_feedback_citation_scope",
        ),
        Index("idx_rag_eval_feedback_workspace_status", "workspace_id", "status", "created_at"),
        Index("idx_rag_eval_feedback_trace", "trace_id"),
    )


class RagQualityGateRunModel(Base):
    """离线质量门禁的脱敏聚合结果；不关联用户查询或本地报告路径。"""

    __tablename__ = "rag_quality_gate_runs"

    id: Mapped[uuid.UUID] = _uuid_pk()
    gate_id: Mapped[str] = mapped_column(String(100))
    cohort_id: Mapped[str] = mapped_column(String(100))
    baseline_id: Mapped[str] = mapped_column(String(100))
    revision: Mapped[str] = mapped_column(String(40))
    status: Mapped[str] = mapped_column(String(30))
    sample_count: Mapped[int] = mapped_column(Integer)
    metrics_json: Mapped[dict] = mapped_column(JSONB, default=dict)
    checks_json: Mapped[list] = mapped_column(JSONB, default=list)
    failure_targets_json: Mapped[list] = mapped_column(JSONB, default=list)
    generated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)

    __table_args__ = (
        CheckConstraint(
            "status IN ('passed','blocked','insufficient_evidence')",
            name="ck_rag_quality_gate_runs_status",
        ),
        CheckConstraint("sample_count >= 0", name="ck_rag_quality_gate_runs_sample_count"),
        UniqueConstraint(
            "gate_id", "revision", "generated_at", name="uq_rag_quality_gate_runs_execution"
        ),
        Index("idx_rag_quality_gate_runs_created", "created_at"),
        Index("idx_rag_quality_gate_runs_status_created", "status", "created_at"),
    )


class RagQualityIssueModel(Base):
    """失败候选的治理状态；不保存 query、答案或 Chunk 正文。"""

    __tablename__ = "rag_quality_issues"

    id: Mapped[uuid.UUID] = _uuid_pk()
    candidate_id: Mapped[str] = mapped_column(String(64), unique=True)
    trace_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("rag_evaluation_traces.id", ondelete="CASCADE"))
    gate_id: Mapped[str] = mapped_column(String(100))
    cohort_id: Mapped[str] = mapped_column(String(100))
    failure_type: Mapped[str] = mapped_column(String(80))
    owner: Mapped[str] = mapped_column(String(40))
    status: Mapped[str] = mapped_column(String(30))
    occurrence_count: Mapped[int] = mapped_column(Integer, default=1)
    first_seen_run_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("rag_quality_gate_runs.id"))
    last_seen_run_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("rag_quality_gate_runs.id"))
    verified_run_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("rag_quality_gate_runs.id"), nullable=True)
    resolution_note: Mapped[str] = mapped_column(String(500), default="")
    version: Mapped[int] = mapped_column(Integer, default=1)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)

    __table_args__ = (
        CheckConstraint("status IN ('open','in_progress','resolved','verified','dismissed')", name="ck_rag_quality_issues_status"),
        CheckConstraint("owner IN ('data_quality','candidate_recall','reranker','context_assembly')", name="ck_rag_quality_issues_owner"),
        CheckConstraint("occurrence_count >= 1", name="ck_rag_quality_issues_occurrences"),
        CheckConstraint("version >= 1", name="ck_rag_quality_issues_version"),
        Index("idx_rag_quality_issues_status_updated", "status", "updated_at"),
        Index("idx_rag_quality_issues_gate_cohort", "gate_id", "cohort_id"),
    )


# ── outbox_events ───────────────────────────────────────────────

class OutboxEventModel(Base):
    __tablename__ = "outbox_events"

    id: Mapped[uuid.UUID] = _uuid_pk()
    event_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), unique=True)
    aggregate_type: Mapped[str] = mapped_column(String(50))
    aggregate_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True))
    event_type: Mapped[str] = mapped_column(String(100))
    schema_version: Mapped[str] = mapped_column(String(20))
    payload: Mapped[dict] = mapped_column(JSONB)
    trace_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True))
    correlation_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    causation_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    status: Mapped[str] = mapped_column(String(20), default="pending")
    retry_count: Mapped[int] = mapped_column(Integer, default=0)
    max_retries: Mapped[int] = mapped_column(Integer, default=20)
    next_retry_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    claimed_by: Mapped[str | None] = mapped_column(String(100), nullable=True)
    claimed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    error_code: Mapped[str | None] = mapped_column(String(50), nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        CheckConstraint(
            "status IN ('pending','dispatching','delivered','failed','dead')", name="ck_outbox_status"
        ),
        CheckConstraint(
            "retry_count >= 0 AND max_retries BETWEEN 1 AND 100",
            name="ck_outbox_retry_budget",
        ),
        Index("idx_outbox_status_next_retry", "status", "next_retry_at"),
    )


# ── inbox_events ────────────────────────────────────────────────

class InboxEventModel(Base):
    __tablename__ = "inbox_events"

    id: Mapped[uuid.UUID] = _uuid_pk()
    source: Mapped[str] = mapped_column(String(100))
    source_event_id: Mapped[str] = mapped_column(String(200))
    status: Mapped[str] = mapped_column(String(20), default="processing")
    processed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)

    __table_args__ = (
        CheckConstraint("status IN ('processing','processed','failed')", name="ck_inbox_status"),
        UniqueConstraint("source", "source_event_id", name="uq_inbox_source_event"),
        Index("idx_inbox_source_event", "source", "source_event_id"),
    )


# ── workspaces ───────────────────────────────────────────────────

class WorkspaceModel(Base):
    __tablename__ = "workspaces"

    id: Mapped[uuid.UUID] = _uuid_pk()
    name: Mapped[str] = mapped_column(String(500))
    root_path: Mapped[str] = mapped_column(Text)
    canonical_path: Mapped[str] = mapped_column(Text, unique=True)
    status: Mapped[str] = mapped_column(String(20), default="active")
    source: Mapped[str] = mapped_column(String(20), default="user_picker")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    metadata_json: Mapped[dict] = mapped_column(JSONB, default=dict)

    __table_args__ = (
        CheckConstraint("status IN ('active','revoked')", name="ck_workspaces_status"),
        CheckConstraint("source IN ('configured','user_picker')", name="ck_workspaces_source"),
        Index("idx_workspaces_status", "status"),
        Index("idx_workspaces_canonical_path", "canonical_path"),
    )


# ── knowledge vaults / documents ───────────────────────────────

class KnowledgeVaultModel(Base):
    __tablename__ = "knowledge_vaults"

    id: Mapped[uuid.UUID] = _uuid_pk()
    name: Mapped[str] = mapped_column(String(200))
    root_path: Mapped[str] = mapped_column(Text)
    canonical_path: Mapped[str] = mapped_column(Text, unique=True)
    status: Mapped[str] = mapped_column(String(20), default="active")
    source: Mapped[str] = mapped_column(String(30), default="jarvis_managed")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        CheckConstraint("status IN ('active','revoked')", name="ck_knowledge_vaults_status"),
        CheckConstraint("source IN ('jarvis_managed')", name="ck_knowledge_vaults_source"),
        Index("idx_knowledge_vaults_status", "status"),
    )


class KnowledgeDocumentModel(Base):
    __tablename__ = "knowledge_documents"

    id: Mapped[uuid.UUID] = _uuid_pk()
    vault_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("knowledge_vaults.id", ondelete="CASCADE")
    )
    title: Mapped[str] = mapped_column(String(500))
    kind: Mapped[str] = mapped_column(String(20))
    relative_path: Mapped[str] = mapped_column(Text)
    content_hash: Mapped[str] = mapped_column(String(64))
    size_bytes: Mapped[int] = mapped_column(BigInteger)
    tags_json: Mapped[list] = mapped_column(JSONB, default=list)
    source_urls_json: Mapped[list] = mapped_column(JSONB, default=list)
    source_task_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tasks.id", ondelete="SET NULL"), nullable=True
    )
    source_run_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("agent_runs.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)

    __table_args__ = (
        CheckConstraint("kind IN ('report','note','source')", name="ck_knowledge_documents_kind"),
        CheckConstraint("size_bytes >= 0", name="ck_knowledge_documents_size"),
        UniqueConstraint("vault_id", "relative_path", name="uq_knowledge_documents_path"),
        Index("idx_knowledge_documents_vault_created", "vault_id", "created_at"),
    )


class ScheduledTaskModel(Base):
    __tablename__ = "scheduled_tasks"
    id: Mapped[uuid.UUID] = _uuid_pk()
    name: Mapped[str] = mapped_column(String(200))
    user_goal: Mapped[str] = mapped_column(Text)
    recurrence: Mapped[str] = mapped_column(String(20))
    timezone: Mapped[str] = mapped_column(String(100))
    hour: Mapped[int] = mapped_column(Integer)
    minute: Mapped[int] = mapped_column(Integer)
    weekday: Mapped[int | None] = mapped_column(Integer, nullable=True)
    workspace_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("workspaces.id", ondelete="SET NULL"), nullable=True)
    status: Mapped[str] = mapped_column(String(20), default="active")
    authorized_tools_json: Mapped[list] = mapped_column(JSONB, default=list)
    task_kind: Mapped[str] = mapped_column(String(30), default="knowledge_report")
    source_policy_json: Mapped[dict] = mapped_column(JSONB, default=dict)
    next_run_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    last_run_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_task_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("tasks.id", ondelete="SET NULL"), nullable=True)
    last_run_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("agent_runs.id", ondelete="SET NULL"), nullable=True)
    version: Mapped[int] = mapped_column(Integer, default=1)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    __table_args__ = (
        CheckConstraint("recurrence IN ('daily','weekly')", name="ck_scheduled_tasks_recurrence"),
        CheckConstraint("status IN ('active','paused')", name="ck_scheduled_tasks_status"),
        CheckConstraint("task_kind IN ('knowledge_report','source_report')", name="ck_scheduled_tasks_kind"),
        CheckConstraint("hour BETWEEN 0 AND 23", name="ck_scheduled_tasks_hour"),
        CheckConstraint("minute BETWEEN 0 AND 59", name="ck_scheduled_tasks_minute"),
        CheckConstraint("weekday IS NULL OR weekday BETWEEN 0 AND 6", name="ck_scheduled_tasks_weekday"),
        Index("idx_scheduled_tasks_due", "status", "next_run_at"),
    )


class ScheduledTaskExecutionModel(Base):
    __tablename__ = "scheduled_task_executions"
    id: Mapped[uuid.UUID] = _uuid_pk()
    scheduled_task_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("scheduled_tasks.id", ondelete="CASCADE"))
    scheduled_for: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(String(20), default="pending")
    task_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("tasks.id", ondelete="SET NULL"), nullable=True)
    run_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("agent_runs.id", ondelete="SET NULL"), nullable=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    error_code: Mapped[str | None] = mapped_column(String(80), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    __table_args__ = (
        CheckConstraint("status IN ('pending','dispatching','dispatched','failed')", name="ck_scheduled_executions_status"),
        UniqueConstraint("scheduled_task_id", "scheduled_for", name="uq_scheduled_execution_slot"),
        Index("idx_scheduled_executions_dispatch", "status", "lease_until"),
    )


class McpServerModel(Base):
    __tablename__ = "mcp_servers"
    id: Mapped[uuid.UUID] = _uuid_pk()
    slug: Mapped[str] = mapped_column(String(80), unique=True)
    name: Mapped[str] = mapped_column(String(200))
    transport: Mapped[str] = mapped_column(String(20))
    command: Mapped[str] = mapped_column(Text)
    args_json: Mapped[list] = mapped_column(JSONB, default=list)
    env_keys_json: Mapped[list] = mapped_column(JSONB, default=list)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    status: Mapped[str] = mapped_column(String(20), default="disconnected")
    last_error_code: Mapped[str | None] = mapped_column(String(80), nullable=True)
    last_connected_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    version: Mapped[int] = mapped_column(Integer, default=1)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    __table_args__ = (
        CheckConstraint("transport IN ('stdio')", name="ck_mcp_servers_transport"),
        CheckConstraint("status IN ('disconnected','connected','error')", name="ck_mcp_servers_status"),
        Index("idx_mcp_servers_enabled", "enabled"),
    )


class McpToolModel(Base):
    __tablename__ = "mcp_tools"
    id: Mapped[uuid.UUID] = _uuid_pk()
    server_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("mcp_servers.id", ondelete="CASCADE")
    )
    original_name: Mapped[str] = mapped_column(String(200))
    internal_name: Mapped[str] = mapped_column(String(300), unique=True)
    description: Mapped[str] = mapped_column(String(500), default="")
    input_schema_json: Mapped[dict] = mapped_column(JSONB, default=dict)
    risk_level: Mapped[str] = mapped_column(String(5), default="L3")
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    discovered_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    __table_args__ = (
        CheckConstraint("risk_level IN ('L0','L1','L2','L3','L4','L5')", name="ck_mcp_tools_risk"),
        UniqueConstraint("server_id", "original_name", name="uq_mcp_tools_server_name"),
        Index("idx_mcp_tools_server_enabled", "server_id", "enabled"),
    )
