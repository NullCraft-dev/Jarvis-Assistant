"""add durable permission request expiry

Revision ID: 025_permission_request_expiry
Revises: 024_tool_permission_expired
"""

import sqlalchemy as sa
from alembic import op

revision = "025_permission_request_expiry"
down_revision = "024_tool_permission_expired"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "permission_requests",
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.execute(
        "UPDATE permission_requests "
        "SET expires_at = created_at + interval '15 minutes' "
        "WHERE expires_at IS NULL"
    )
    op.alter_column("permission_requests", "expires_at", nullable=False)
    op.create_index(
        "idx_permission_requests_status_expires",
        "permission_requests",
        ["status", "expires_at"],
    )


def downgrade() -> None:
    op.drop_index(
        "idx_permission_requests_status_expires",
        table_name="permission_requests",
    )
    op.drop_column("permission_requests", "expires_at")
