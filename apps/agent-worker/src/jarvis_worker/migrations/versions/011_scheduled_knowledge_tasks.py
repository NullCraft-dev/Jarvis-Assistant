"""add scheduled knowledge tasks and source provenance

Revision ID: 011_scheduled_knowledge_tasks
Revises: 010_obsidian_knowledge_vault
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "011_scheduled_knowledge_tasks"
down_revision = "010_obsidian_knowledge_vault"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("knowledge_documents", sa.Column("source_urls_json", postgresql.JSONB(), nullable=False, server_default=sa.text("'[]'::jsonb")))
    op.add_column("tasks", sa.Column("scheduled_execution_id", postgresql.UUID(as_uuid=True), nullable=True))
    op.create_unique_constraint("uq_tasks_scheduled_execution", "tasks", ["scheduled_execution_id"])
    op.create_table(
        "scheduled_tasks",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("user_goal", sa.Text(), nullable=False),
        sa.Column("recurrence", sa.String(20), nullable=False),
        sa.Column("timezone", sa.String(100), nullable=False),
        sa.Column("hour", sa.Integer(), nullable=False),
        sa.Column("minute", sa.Integer(), nullable=False),
        sa.Column("weekday", sa.Integer(), nullable=True),
        sa.Column("workspace_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("workspaces.id", ondelete="SET NULL"), nullable=True),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("authorized_tools_json", postgresql.JSONB(), nullable=False),
        sa.Column("next_run_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_run_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_task_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("tasks.id", ondelete="SET NULL"), nullable=True),
        sa.Column("last_run_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("agent_runs.id", ondelete="SET NULL"), nullable=True),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("recurrence IN ('daily','weekly')", name="ck_scheduled_tasks_recurrence"),
        sa.CheckConstraint("status IN ('active','paused')", name="ck_scheduled_tasks_status"),
        sa.CheckConstraint("hour BETWEEN 0 AND 23", name="ck_scheduled_tasks_hour"),
        sa.CheckConstraint("minute BETWEEN 0 AND 59", name="ck_scheduled_tasks_minute"),
        sa.CheckConstraint("weekday IS NULL OR weekday BETWEEN 0 AND 6", name="ck_scheduled_tasks_weekday"),
    )
    op.create_index("idx_scheduled_tasks_due", "scheduled_tasks", ["status", "next_run_at"])
    op.create_table(
        "scheduled_task_executions",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("scheduled_task_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("scheduled_tasks.id", ondelete="CASCADE"), nullable=False),
        sa.Column("scheduled_for", sa.DateTime(timezone=True), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("task_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("tasks.id", ondelete="SET NULL"), nullable=True),
        sa.Column("run_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("agent_runs.id", ondelete="SET NULL"), nullable=True),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("lease_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("error_code", sa.String(80), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("status IN ('pending','dispatching','dispatched','failed')", name="ck_scheduled_executions_status"),
        sa.UniqueConstraint("scheduled_task_id", "scheduled_for", name="uq_scheduled_execution_slot"),
    )
    op.create_index("idx_scheduled_executions_dispatch", "scheduled_task_executions", ["status", "lease_until"])


def downgrade() -> None:
    op.drop_table("scheduled_task_executions")
    op.drop_table("scheduled_tasks")
    op.drop_constraint("uq_tasks_scheduled_execution", "tasks", type_="unique")
    op.drop_column("tasks", "scheduled_execution_id")
    op.drop_column("knowledge_documents", "source_urls_json")
