"""persist redacted RAG quality failure targets

Revision ID: 022_rag_quality_failure_targets
Revises: 021_rag_quality_gate_runs
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "022_rag_quality_failure_targets"
down_revision = "021_rag_quality_gate_runs"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "rag_quality_gate_runs",
        sa.Column(
            "failure_targets_json",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
    )
    op.alter_column("rag_quality_gate_runs", "failure_targets_json", server_default=None)


def downgrade() -> None:
    op.drop_column("rag_quality_gate_runs", "failure_targets_json")
