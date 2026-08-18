"""初始 PostgreSQL schema。

创建全部 13 张表（含约束、索引、CHECK、外键）。
此 migration 替代旧的 Go SQLite 4 张表 schema。

Revision ID: 001
Create Date: 2026-07-14
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers
revision: str = "001"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # ── conversations ──
    op.create_table(
        "conversations",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("title", sa.String(500), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )

    # ── tasks ──
    op.create_table(
        "tasks",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("conversation_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("conversations.id"), nullable=False),
        sa.Column("title", sa.String(500), nullable=False),
        sa.Column("user_goal", sa.Text, nullable=False),
        sa.Column("status", sa.String(30), nullable=False),
        sa.Column("priority", sa.Integer, nullable=False, server_default="0"),
        sa.Column("workspace_path", sa.Text, nullable=True),
        sa.Column("active_run_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("last_step_summary", sa.Text, nullable=True),
        sa.Column("risk_level", sa.String(5), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("cancelled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("metadata_json", postgresql.JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.CheckConstraint(
            "status IN ('pending','running','waiting_for_user','blocked','failed','completed','cancelled')",
            name="ck_tasks_status",
        ),
    )
    op.create_index("idx_tasks_status_updated", "tasks", ["status", "updated_at"])

    # ── agent_runs ──
    op.create_table(
        "agent_runs",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("task_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("tasks.id"), nullable=False),
        sa.Column("agent_id", sa.String(100), nullable=False, server_default="default"),
        sa.Column("mode", sa.String(20), nullable=False, server_default="single_agent"),
        sa.Column("status", sa.String(30), nullable=False),
        sa.Column("version", sa.Integer, nullable=False, server_default="1"),
        sa.Column("worker_id", sa.String(100), nullable=True),
        sa.Column("lease_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("current_step_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("final_output_artifact_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("max_steps", sa.Integer, nullable=False, server_default="20"),
        sa.Column("step_count", sa.Integer, nullable=False, server_default="0"),
        sa.Column("input_tokens", sa.Integer, nullable=False, server_default="0"),
        sa.Column("output_tokens", sa.Integer, nullable=False, server_default="0"),
        sa.Column("estimated_cost_usd", sa.Float, nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("failed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("error_json", postgresql.JSONB, nullable=True),
        sa.Column("metadata_json", postgresql.JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.CheckConstraint("mode IN ('single_agent','multi_agent')", name="ck_agent_runs_mode"),
        sa.CheckConstraint(
            "status IN ('queued','running','waiting_permission','paused','cancel_requested','cancelling','completed','failed','cancelled')",
            name="ck_agent_runs_status",
        ),
    )
    op.create_index("idx_agent_runs_task_id", "agent_runs", ["task_id"])
    op.create_index("idx_agent_runs_status", "agent_runs", ["status"])
    op.create_index("idx_agent_runs_worker_id", "agent_runs", ["worker_id"])

    # ── execution_steps ──
    op.create_table(
        "execution_steps",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("run_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("agent_runs.id"), nullable=False),
        sa.Column("task_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("tasks.id"), nullable=False),
        sa.Column("parent_step_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("type", sa.String(30), nullable=False),
        sa.Column("status", sa.String(30), nullable=False),
        sa.Column("title", sa.String(500), nullable=False),
        sa.Column("summary", sa.Text, nullable=True),
        sa.Column("input_json", postgresql.JSONB, nullable=True),
        sa.Column("output_json", postgresql.JSONB, nullable=True),
        sa.Column("error_json", postgresql.JSONB, nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("duration_ms", sa.Integer, nullable=True),
        sa.Column("order_index", sa.Integer, nullable=False, server_default="0"),
        sa.Column("metadata_json", postgresql.JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.CheckConstraint(
            "type IN ('user_message','system_event','model_call','plan_created','tool_call','mcp_call','observation','permission_request','review','final_output')",
            name="ck_execution_steps_type",
        ),
        sa.CheckConstraint(
            "status IN ('pending','running','waiting_for_permission','completed','failed','cancelled','skipped')",
            name="ck_execution_steps_status",
        ),
    )
    op.create_index("idx_execution_steps_run_order", "execution_steps", ["run_id", "order_index"])

    # ── runtime_events ──
    op.create_table(
        "runtime_events",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("event_id", postgresql.UUID(as_uuid=True), nullable=False, unique=True),
        sa.Column("task_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("run_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("step_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("type", sa.String(50), nullable=False),
        sa.Column("event_sequence", sa.BigInteger, nullable=False, server_default="0"),
        sa.Column("payload_json", postgresql.JSONB, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_unique_constraint("uq_runtime_events_run_seq", "runtime_events", ["run_id", "event_sequence"])
    op.create_index("idx_runtime_events_run_created", "runtime_events", ["run_id", "created_at"])
    op.create_index("idx_runtime_events_type", "runtime_events", ["type"])

    # ── tool_calls ──
    op.create_table(
        "tool_calls",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("task_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("tasks.id"), nullable=False),
        sa.Column("run_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("agent_runs.id"), nullable=False),
        sa.Column("step_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("execution_steps.id"), nullable=False),
        sa.Column("provider", sa.String(20), nullable=False),
        sa.Column("tool_name", sa.String(200), nullable=False),
        sa.Column("mcp_server_id", sa.String(200), nullable=True),
        sa.Column("risk_level", sa.String(5), nullable=False),
        sa.Column("arguments_json", postgresql.JSONB, nullable=False),
        sa.Column("arguments_summary_json", postgresql.JSONB, nullable=True),
        sa.Column("result_json", postgresql.JSONB, nullable=True),
        sa.Column("result_summary", sa.Text, nullable=True),
        sa.Column("permission_request_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("permission_status", sa.String(20), nullable=False, server_default="not_required"),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("error_json", postgresql.JSONB, nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("duration_ms", sa.Integer, nullable=True),
        sa.CheckConstraint("provider IN ('native','mcp','system')", name="ck_tool_calls_provider"),
        sa.CheckConstraint(
            "permission_status IN ('not_required','pending','approved','denied','expired')",
            name="ck_tool_calls_permission_status",
        ),
        sa.CheckConstraint(
            "status IN ('pending','running','completed','failed','cancelled')",
            name="ck_tool_calls_status",
        ),
    )
    op.create_index("idx_tool_calls_run_id", "tool_calls", ["run_id"])
    op.create_index("idx_tool_calls_tool_name", "tool_calls", ["tool_name"])
    op.create_index("idx_tool_calls_status", "tool_calls", ["status"])

    # ── permission_requests ──
    op.create_table(
        "permission_requests",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("task_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("tasks.id"), nullable=False),
        sa.Column("run_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("agent_runs.id"), nullable=False),
        sa.Column("step_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("tool_call_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("tool_name", sa.String(200), nullable=False),
        sa.Column("action_summary", sa.Text, nullable=False),
        sa.Column("reason", sa.Text, nullable=True),
        sa.Column("risk_level", sa.String(5), nullable=False),
        sa.Column("scope_json", postgresql.JSONB, nullable=False),
        sa.Column("arguments_summary_json", postgresql.JSONB, nullable=False),
        sa.Column("allowed_decisions_json", postgresql.JSONB, nullable=False, server_default=sa.text("'[]'::jsonb")),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("decision", sa.String(30), nullable=True),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("note", sa.Text, nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint(
            "status IN ('pending','approved','denied','expired','consumed')",
            name="ck_perm_req_status",
        ),
    )
    op.create_index("idx_permission_requests_status", "permission_requests", ["status"])
    op.create_index("idx_permission_requests_run_id", "permission_requests", ["run_id"])

    # ── permission_grants ──
    op.create_table(
        "permission_grants",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("grant_type", sa.String(30), nullable=False),
        sa.Column("tool_name", sa.String(200), nullable=True),
        sa.Column("mcp_server_id", sa.String(200), nullable=True),
        sa.Column("workspace_path", sa.Text, nullable=True),
        sa.Column("path", sa.Text, nullable=True),
        sa.Column("risk_level_max", sa.String(5), nullable=False),
        sa.Column("created_from_request_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("permission_requests.id"), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("metadata_json", postgresql.JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.CheckConstraint(
            "grant_type IN ('once','task','tool_path','workspace','global')",
            name="ck_perm_grant_type",
        ),
    )
    op.create_index("idx_permission_grants_tool", "permission_grants", ["tool_name"])

    # ── audit_logs ──
    op.create_table(
        "audit_logs",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("task_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("run_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("step_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("tool_call_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("event_type", sa.String(50), nullable=False),
        sa.Column("actor", sa.String(100), nullable=False),
        sa.Column("risk_level", sa.String(5), nullable=True),
        sa.Column("permission_decision", sa.String(30), nullable=True),
        sa.Column("action_summary", sa.Text, nullable=False),
        sa.Column("details_json", postgresql.JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("result_summary", sa.Text, nullable=True),
        sa.Column("error_json", postgresql.JSONB, nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("idx_audit_logs_task_run", "audit_logs", ["task_id", "run_id"])
    op.create_index("idx_audit_logs_event_type", "audit_logs", ["event_type"])
    op.create_index("idx_audit_logs_created_at", "audit_logs", ["created_at"])

    # ── artifacts ──
    op.create_table(
        "artifacts",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("task_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("tasks.id"), nullable=False),
        sa.Column("run_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("agent_runs.id"), nullable=False),
        sa.Column("step_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("kind", sa.String(30), nullable=False),
        sa.Column("title", sa.String(500), nullable=False),
        sa.Column("content", sa.Text, nullable=True),
        sa.Column("file_path", sa.Text, nullable=True),
        sa.Column("file_size_bytes", sa.BigInteger, nullable=True),
        sa.Column("mime_type", sa.String(100), nullable=True),
        sa.Column("content_hash", sa.String(128), nullable=True),
        sa.Column("metadata_json", postgresql.JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint(
            "kind IN ('markdown','text','json','file','diff','screenshot')",
            name="ck_artifacts_kind",
        ),
    )
    op.create_index("idx_artifacts_task_id", "artifacts", ["task_id"])
    op.create_index("idx_artifacts_run_id", "artifacts", ["run_id"])

    # ── outbox_events ──
    op.create_table(
        "outbox_events",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("event_id", postgresql.UUID(as_uuid=True), nullable=False, unique=True),
        sa.Column("aggregate_type", sa.String(50), nullable=False),
        sa.Column("aggregate_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("event_type", sa.String(100), nullable=False),
        sa.Column("schema_version", sa.String(20), nullable=False),
        sa.Column("payload", postgresql.JSONB, nullable=False),
        sa.Column("trace_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("correlation_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("causation_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("status", sa.String(20), nullable=False, server_default="pending"),
        sa.Column("retry_count", sa.Integer, nullable=False, server_default="0"),
        sa.Column("max_retries", sa.Integer, nullable=False, server_default="5"),
        sa.Column("next_retry_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("claimed_by", sa.String(100), nullable=True),
        sa.Column("claimed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("lease_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("error_code", sa.String(50), nullable=True),
        sa.Column("error_message", sa.Text, nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("delivered_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "status IN ('pending','dispatching','delivered','failed','dead')",
            name="ck_outbox_status",
        ),
    )
    op.create_index("idx_outbox_status_next_retry", "outbox_events", ["status", "next_retry_at"])

    # ── inbox_events ──
    op.create_table(
        "inbox_events",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("source", sa.String(100), nullable=False),
        sa.Column("source_event_id", sa.String(200), nullable=False),
        sa.Column("status", sa.String(20), nullable=False, server_default="processing"),
        sa.Column("processed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint(
            "status IN ('processing','processed','failed')",
            name="ck_inbox_status",
        ),
    )
    op.create_unique_constraint("uq_inbox_source_event", "inbox_events", ["source", "source_event_id"])
    op.create_index("idx_inbox_source_event", "inbox_events", ["source", "source_event_id"])

    # ── messages (needs conversations + tasks) ──
    op.create_table(
        "messages",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("conversation_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("conversations.id"), nullable=False),
        sa.Column("task_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("tasks.id"), nullable=True),
        sa.Column("run_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("role", sa.String(20), nullable=False),
        sa.Column("content", sa.Text, nullable=False),
        sa.Column("tool_call_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("metadata_json", postgresql.JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.CheckConstraint(
            "role IN ('system','user','assistant','tool')",
            name="ck_messages_role",
        ),
    )
    op.create_index("idx_messages_conv_created", "messages", ["conversation_id", "created_at"])
    op.create_index("idx_messages_task_id", "messages", ["task_id"])


def downgrade() -> None:
    op.drop_table("messages")
    op.drop_table("inbox_events")
    op.drop_table("outbox_events")
    op.drop_table("artifacts")
    op.drop_table("audit_logs")
    op.drop_table("permission_grants")
    op.drop_table("permission_requests")
    op.drop_table("tool_calls")
    op.drop_table("runtime_events")
    op.drop_table("execution_steps")
    op.drop_table("agent_runs")
    op.drop_table("tasks")
    op.drop_table("conversations")
