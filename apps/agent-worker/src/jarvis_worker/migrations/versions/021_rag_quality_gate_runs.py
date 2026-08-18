"""persist sanitized RAG quality gate runs

Revision ID: 021_rag_quality_gate_runs
Revises: 020_rag_feedback_triage
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "021_rag_quality_gate_runs"
down_revision = "020_rag_feedback_triage"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "rag_quality_gate_runs",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("gate_id", sa.String(length=100), nullable=False),
        sa.Column("cohort_id", sa.String(length=100), nullable=False),
        sa.Column("baseline_id", sa.String(length=100), nullable=False),
        sa.Column("revision", sa.String(length=40), nullable=False),
        sa.Column("status", sa.String(length=30), nullable=False),
        sa.Column("sample_count", sa.Integer(), nullable=False),
        sa.Column("metrics_json", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("checks_json", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("generated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("sample_count >= 0", name="ck_rag_quality_gate_runs_sample_count"),
        sa.CheckConstraint("status IN ('passed','blocked','insufficient_evidence')", name="ck_rag_quality_gate_runs_status"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("gate_id", "revision", "generated_at", name="uq_rag_quality_gate_runs_execution"),
    )
    op.create_index("idx_rag_quality_gate_runs_created", "rag_quality_gate_runs", ["created_at"])
    op.create_index("idx_rag_quality_gate_runs_status_created", "rag_quality_gate_runs", ["status", "created_at"])


def downgrade() -> None:
    op.drop_index("idx_rag_quality_gate_runs_status_created", table_name="rag_quality_gate_runs")
    op.drop_index("idx_rag_quality_gate_runs_created", table_name="rag_quality_gate_runs")
    op.drop_table("rag_quality_gate_runs")
