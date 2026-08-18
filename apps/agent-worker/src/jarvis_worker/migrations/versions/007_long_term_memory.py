"""add reusable long-term memory records

Revision ID: 007_long_term_memory
Revises: 006_artifact_v2_contract
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "007_long_term_memory"
down_revision = "006_artifact_v2_contract"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "memories",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("scope_type", sa.String(20), nullable=False),
        sa.Column("workspace_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("category", sa.String(30), nullable=False),
        sa.Column("key", sa.String(128), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("status", sa.String(20), nullable=False, server_default="active"),
        sa.Column("source_type", sa.String(30), nullable=False, server_default="user_explicit"),
        sa.Column("source_task_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("importance", sa.Integer(), nullable=False, server_default="50"),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["source_task_id"], ["tasks.id"], ondelete="SET NULL"),
        sa.CheckConstraint("scope_type IN ('global','workspace')", name="ck_memories_scope_type"),
        sa.CheckConstraint(
            "(scope_type = 'global' AND workspace_id IS NULL) OR "
            "(scope_type = 'workspace' AND workspace_id IS NOT NULL)",
            name="ck_memories_scope_owner",
        ),
        sa.CheckConstraint(
            "category IN ('preference','user_fact','project_fact','rule')",
            name="ck_memories_category",
        ),
        sa.CheckConstraint("status IN ('active','disabled')", name="ck_memories_status"),
        sa.CheckConstraint("source_type IN ('user_explicit')", name="ck_memories_source_type"),
        sa.CheckConstraint("importance BETWEEN 0 AND 100", name="ck_memories_importance"),
    )
    op.create_index("idx_memories_context", "memories", ["status", "scope_type", "workspace_id", "importance"])
    op.create_index("idx_memories_updated", "memories", ["updated_at"])
    op.create_index(
        "uq_memories_global_category_key",
        "memories",
        ["category", "key"],
        unique=True,
        postgresql_where=sa.text("scope_type = 'global'"),
    )
    op.create_index(
        "uq_memories_workspace_category_key",
        "memories",
        ["workspace_id", "category", "key"],
        unique=True,
        postgresql_where=sa.text("scope_type = 'workspace'"),
    )


def downgrade() -> None:
    op.drop_table("memories")
