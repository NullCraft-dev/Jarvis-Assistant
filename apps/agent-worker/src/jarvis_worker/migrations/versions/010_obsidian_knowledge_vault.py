"""add isolated Obsidian knowledge vault metadata

Revision ID: 010_obsidian_knowledge_vault
Revises: 009_memory_candidate_maintenance
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "010_obsidian_knowledge_vault"
down_revision = "009_memory_candidate_maintenance"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "knowledge_vaults",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("root_path", sa.Text(), nullable=False),
        sa.Column("canonical_path", sa.Text(), nullable=False, unique=True),
        sa.Column("status", sa.String(20), nullable=False, server_default="active"),
        sa.Column("source", sa.String(30), nullable=False, server_default="jarvis_managed"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("status IN ('active','revoked')", name="ck_knowledge_vaults_status"),
        sa.CheckConstraint("source IN ('jarvis_managed')", name="ck_knowledge_vaults_source"),
    )
    op.create_index("idx_knowledge_vaults_status", "knowledge_vaults", ["status"])
    op.create_table(
        "knowledge_documents",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("vault_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("knowledge_vaults.id", ondelete="CASCADE"), nullable=False),
        sa.Column("title", sa.String(500), nullable=False),
        sa.Column("kind", sa.String(20), nullable=False),
        sa.Column("relative_path", sa.Text(), nullable=False),
        sa.Column("content_hash", sa.String(64), nullable=False),
        sa.Column("size_bytes", sa.BigInteger(), nullable=False),
        sa.Column("tags_json", postgresql.JSONB(), nullable=False, server_default=sa.text("'[]'::jsonb")),
        sa.Column("source_task_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("tasks.id", ondelete="SET NULL"), nullable=True),
        sa.Column("source_run_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("agent_runs.id", ondelete="SET NULL"), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("kind IN ('report','note','source')", name="ck_knowledge_documents_kind"),
        sa.CheckConstraint("size_bytes >= 0", name="ck_knowledge_documents_size"),
        sa.UniqueConstraint("vault_id", "relative_path", name="uq_knowledge_documents_path"),
    )
    op.create_index("idx_knowledge_documents_vault_created", "knowledge_documents", ["vault_id", "created_at"])


def downgrade() -> None:
    op.drop_table("knowledge_documents")
    op.drop_table("knowledge_vaults")
