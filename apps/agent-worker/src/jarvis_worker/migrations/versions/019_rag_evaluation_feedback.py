"""add user RAG feedback review queue

Revision ID: 019_rag_eval_feedback
Revises: 018_outbox_redis_recovery
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "019_rag_eval_feedback"
down_revision = "018_outbox_redis_recovery"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "rag_evaluation_feedback",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("trace_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("workspace_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("task_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("run_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("message_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("kind", sa.String(length=30), nullable=False),
        sa.Column("citation_chunk_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("status", sa.String(length=20), nullable=False, server_default="pending"),
        sa.Column("fingerprint", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "kind IN ('helpful','unhelpful','citation_incorrect','evidence_insufficient')",
            name="ck_rag_eval_feedback_kind",
        ),
        sa.CheckConstraint(
            "status IN ('pending','reviewed','dismissed')", name="ck_rag_eval_feedback_status"
        ),
        sa.CheckConstraint(
            "kind = 'citation_incorrect' OR citation_chunk_id IS NULL",
            name="ck_rag_eval_feedback_citation_scope",
        ),
        sa.ForeignKeyConstraint(["trace_id"], ["rag_evaluation_traces.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["task_id"], ["tasks.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["run_id"], ["agent_runs.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["message_id"], ["messages.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["citation_chunk_id"], ["rag_chunks.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("fingerprint"),
    )
    op.create_index(
        "idx_rag_eval_feedback_workspace_status",
        "rag_evaluation_feedback",
        ["workspace_id", "status", "created_at"],
    )
    op.create_index("idx_rag_eval_feedback_trace", "rag_evaluation_feedback", ["trace_id"])


def downgrade() -> None:
    op.drop_index("idx_rag_eval_feedback_trace", table_name="rag_evaluation_feedback")
    op.drop_index("idx_rag_eval_feedback_workspace_status", table_name="rag_evaluation_feedback")
    op.drop_table("rag_evaluation_feedback")
