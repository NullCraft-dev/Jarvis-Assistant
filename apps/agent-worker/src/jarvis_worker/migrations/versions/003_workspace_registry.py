"""add workspace registry

Revision ID: 003_workspace_registry
Revises: 002_permission_resume_checkpoint
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "003_workspace_registry"
down_revision = "002_permission_resume_checkpoint"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ── workspaces ──
    op.create_table(
        "workspaces",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("name", sa.String(500), nullable=False),
        sa.Column("root_path", sa.Text, nullable=False),
        sa.Column("canonical_path", sa.Text, nullable=False),
        sa.Column("status", sa.String(20), nullable=False, server_default="active"),
        sa.Column("source", sa.String(20), nullable=False, server_default="user_picker"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("metadata_json", postgresql.JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.CheckConstraint("status IN ('active','revoked')", name="ck_workspaces_status"),
        sa.CheckConstraint("source IN ('configured','user_picker')", name="ck_workspaces_source"),
    )
    op.create_index("idx_workspaces_status", "workspaces", ["status"])
    op.create_unique_constraint("uq_workspaces_canonical_path", "workspaces", ["canonical_path"])

    # ── tasks.workspace_id ──
    op.add_column(
        "tasks",
        sa.Column("workspace_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.create_foreign_key(
        "fk_tasks_workspace_id",
        "tasks", "workspaces",
        ["workspace_id"], ["id"],
        ondelete="SET NULL",
    )
    op.create_index("idx_tasks_workspace_id", "tasks", ["workspace_id"])


def downgrade() -> None:
    op.drop_index("idx_tasks_workspace_id", table_name="tasks")
    op.drop_constraint("fk_tasks_workspace_id", "tasks", type_="foreignkey")
    op.drop_column("tasks", "workspace_id")
    op.drop_table("workspaces")
