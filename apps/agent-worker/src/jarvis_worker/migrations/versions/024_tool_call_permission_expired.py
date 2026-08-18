"""add expired tool call permission status

Revision ID: 024_tool_permission_expired
Revises: 023_rag_quality_issues
"""

from alembic import op

revision = "024_tool_permission_expired"
down_revision = "023_rag_quality_issues"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_constraint(
        "ck_tool_calls_permission_status",
        "tool_calls",
        type_="check",
    )
    op.create_check_constraint(
        "ck_tool_calls_permission_status",
        "tool_calls",
        "permission_status IN ('not_required','pending','approved','denied','expired')",
    )


def downgrade() -> None:
    op.execute(
        "UPDATE tool_calls SET permission_status = 'pending' "
        "WHERE permission_status = 'expired'"
    )
    op.drop_constraint(
        "ck_tool_calls_permission_status",
        "tool_calls",
        type_="check",
    )
    op.create_check_constraint(
        "ck_tool_calls_permission_status",
        "tool_calls",
        "permission_status IN ('not_required','pending','approved','denied')",
    )
