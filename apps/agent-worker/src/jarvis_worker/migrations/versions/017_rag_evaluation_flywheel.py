"""add production RAG evaluation traces and confirmed labels

Revision ID: 017_rag_evaluation_flywheel
Revises: 016_rag_job_progress
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "017_rag_evaluation_flywheel"
down_revision = "016_rag_job_progress"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "rag_evaluation_traces",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("workspace_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("task_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("run_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("step_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("query_text", sa.Text(), nullable=False),
        sa.Column("query_hash", sa.String(length=64), nullable=False),
        sa.Column("request_json", postgresql.JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("pipeline_versions_json", postgresql.JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("candidate_ranking_json", postgresql.JSONB(), nullable=False, server_default=sa.text("'[]'::jsonb")),
        sa.Column("reranked_ranking_json", postgresql.JSONB(), nullable=False, server_default=sa.text("'[]'::jsonb")),
        sa.Column("context_chunk_ids_json", postgresql.JSONB(), nullable=False, server_default=sa.text("'[]'::jsonb")),
        sa.Column("context_truncated", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("result_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("privacy_status", sa.String(length=20), nullable=False, server_default="pending"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("length(query_text) BETWEEN 1 AND 2000", name="ck_rag_eval_traces_query"),
        sa.CheckConstraint("result_count >= 0", name="ck_rag_eval_traces_result_count"),
        sa.CheckConstraint("privacy_status IN ('pending','approved','rejected')", name="ck_rag_eval_traces_privacy"),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["task_id"], ["tasks.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["run_id"], ["agent_runs.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["step_id"], ["execution_steps.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("idx_rag_eval_traces_workspace_created", "rag_evaluation_traces", ["workspace_id", "created_at"])
    op.create_index("idx_rag_eval_traces_privacy_created", "rag_evaluation_traces", ["privacy_status", "created_at"])
    op.create_index("idx_rag_eval_traces_run", "rag_evaluation_traces", ["run_id"])

    op.create_table(
        "rag_evaluation_labels",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("trace_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("positive_chunk_ids_json", postgresql.JSONB(), nullable=False, server_default=sa.text("'[]'::jsonb")),
        sa.Column("hard_negative_chunk_ids_json", postgresql.JSONB(), nullable=False, server_default=sa.text("'[]'::jsonb")),
        sa.Column("source", sa.String(length=30), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False, server_default="draft"),
        sa.Column("notes", sa.Text(), nullable=False, server_default=""),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("source IN ('user_feedback','human_review','citation_validator','judge')", name="ck_rag_eval_labels_source"),
        sa.CheckConstraint("status IN ('draft','confirmed','rejected','promoted')", name="ck_rag_eval_labels_status"),
        sa.CheckConstraint("jsonb_typeof(positive_chunk_ids_json) = 'array' AND jsonb_array_length(positive_chunk_ids_json) > 0", name="ck_rag_eval_labels_positive_chunks"),
        sa.ForeignKeyConstraint(["trace_id"], ["rag_evaluation_traces.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("trace_id"),
    )
    op.create_index("idx_rag_eval_labels_status", "rag_evaluation_labels", ["status", "updated_at"])


def downgrade() -> None:
    op.drop_index("idx_rag_eval_labels_status", table_name="rag_evaluation_labels")
    op.drop_table("rag_evaluation_labels")
    op.drop_index("idx_rag_eval_traces_run", table_name="rag_evaluation_traces")
    op.drop_index("idx_rag_eval_traces_privacy_created", table_name="rag_evaluation_traces")
    op.drop_index("idx_rag_eval_traces_workspace_created", table_name="rag_evaluation_traces")
    op.drop_table("rag_evaluation_traces")
