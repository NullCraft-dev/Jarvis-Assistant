"""add durable run recovery checkpoint

Revision ID: 004_run_recovery_checkpoint
Revises: 003_workspace_registry
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "004_run_recovery_checkpoint"
down_revision = "003_workspace_registry"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "agent_runs",
        sa.Column(
            "checkpoint_json",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
    )
    op.alter_column("agent_runs", "checkpoint_json", server_default=None)
    op.create_index(
        "idx_agent_runs_recovery_lease",
        "agent_runs",
        ["lease_until"],
        postgresql_where=sa.text("status = 'running'"),
    )


def downgrade() -> None:
    op.drop_index("idx_agent_runs_recovery_lease", table_name="agent_runs")
    op.drop_column("agent_runs", "checkpoint_json")
