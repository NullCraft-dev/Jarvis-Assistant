"""add bounded source policy to scheduled reports

Revision ID: 013_scheduled_source_reports
Revises: 012_mcp_foundation
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "013_scheduled_source_reports"
down_revision = "012_mcp_foundation"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("scheduled_tasks", sa.Column(
        "task_kind", sa.String(30), nullable=False, server_default="knowledge_report",
    ))
    op.add_column("scheduled_tasks", sa.Column(
        "source_policy_json", postgresql.JSONB(), nullable=False,
        server_default=sa.text("'{}'::jsonb"),
    ))
    op.create_check_constraint(
        "ck_scheduled_tasks_kind", "scheduled_tasks",
        "task_kind IN ('knowledge_report','source_report')",
    )


def downgrade() -> None:
    op.drop_constraint("ck_scheduled_tasks_kind", "scheduled_tasks", type_="check")
    op.drop_column("scheduled_tasks", "source_policy_json")
    op.drop_column("scheduled_tasks", "task_kind")
