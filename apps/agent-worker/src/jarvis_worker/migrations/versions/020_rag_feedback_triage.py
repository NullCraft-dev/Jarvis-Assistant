"""add diagnostic triage to RAG feedback

Revision ID: 020_rag_feedback_triage
Revises: 019_rag_eval_feedback
"""

import sqlalchemy as sa
from alembic import op

revision = "020_rag_feedback_triage"
down_revision = "019_rag_eval_feedback"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "rag_evaluation_feedback",
        sa.Column("failure_category", sa.String(length=30), nullable=True),
    )
    op.create_check_constraint(
        "ck_rag_eval_feedback_failure_category",
        "rag_evaluation_feedback",
        "failure_category IS NULL OR failure_category IN "
        "('candidate_miss','reranker_miss','context_omission','context_truncated',"
        "'citation_mismatch','answer_generation','insufficient_evidence','other')",
    )


def downgrade() -> None:
    op.drop_constraint(
        "ck_rag_eval_feedback_failure_category",
        "rag_evaluation_feedback",
        type_="check",
    )
    op.drop_column("rag_evaluation_feedback", "failure_category")
