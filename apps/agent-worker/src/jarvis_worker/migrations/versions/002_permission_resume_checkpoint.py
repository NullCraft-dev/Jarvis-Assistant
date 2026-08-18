"""add durable permission resume checkpoint

Revision ID: 002_permission_resume_checkpoint
Revises: 001_initial_schema
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "002_permission_resume_checkpoint"
down_revision = "001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "permission_requests",
        sa.Column(
            "checkpoint_json",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
    )
    op.alter_column("permission_requests", "checkpoint_json", server_default=None)


def downgrade() -> None:
    op.drop_column("permission_requests", "checkpoint_json")
