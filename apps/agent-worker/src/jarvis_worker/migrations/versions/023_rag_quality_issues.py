"""add governed RAG quality issues

Revision ID: 023_rag_quality_issues
Revises: 022_rag_quality_failure_targets
"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "023_rag_quality_issues"
down_revision = "022_rag_quality_failure_targets"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "rag_quality_issues",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("candidate_id", sa.String(64), nullable=False),
        sa.Column("trace_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("gate_id", sa.String(100), nullable=False),
        sa.Column("cohort_id", sa.String(100), nullable=False),
        sa.Column("failure_type", sa.String(80), nullable=False),
        sa.Column("owner", sa.String(40), nullable=False),
        sa.Column("status", sa.String(30), nullable=False),
        sa.Column("occurrence_count", sa.Integer(), nullable=False),
        sa.Column("first_seen_run_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("last_seen_run_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("verified_run_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("resolution_note", sa.String(500), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("status IN ('open','in_progress','resolved','verified','dismissed')", name="ck_rag_quality_issues_status"),
        sa.CheckConstraint("owner IN ('data_quality','candidate_recall','reranker','context_assembly')", name="ck_rag_quality_issues_owner"),
        sa.CheckConstraint("occurrence_count >= 1", name="ck_rag_quality_issues_occurrences"),
        sa.CheckConstraint("version >= 1", name="ck_rag_quality_issues_version"),
        sa.ForeignKeyConstraint(["trace_id"], ["rag_evaluation_traces.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["first_seen_run_id"], ["rag_quality_gate_runs.id"]),
        sa.ForeignKeyConstraint(["last_seen_run_id"], ["rag_quality_gate_runs.id"]),
        sa.ForeignKeyConstraint(["verified_run_id"], ["rag_quality_gate_runs.id"]),
        sa.PrimaryKeyConstraint("id"), sa.UniqueConstraint("candidate_id"),
    )
    op.create_index("idx_rag_quality_issues_status_updated", "rag_quality_issues", ["status", "updated_at"])
    op.create_index("idx_rag_quality_issues_gate_cohort", "rag_quality_issues", ["gate_id", "cohort_id"])


def downgrade() -> None:
    op.drop_index("idx_rag_quality_issues_gate_cohort", table_name="rag_quality_issues")
    op.drop_index("idx_rag_quality_issues_status_updated", table_name="rag_quality_issues")
    op.drop_table("rag_quality_issues")
