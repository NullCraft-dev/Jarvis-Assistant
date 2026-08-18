"""add memory candidate approval and extraction job contracts

Revision ID: 008_memory_candidates
Revises: 007_long_term_memory
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "008_memory_candidates"
down_revision = "007_long_term_memory"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_constraint("ck_memories_source_type", "memories", type_="check")
    op.create_check_constraint(
        "ck_memories_source_type", "memories",
        "source_type IN ('user_explicit','candidate_approved')",
    )
    op.create_table(
        "memory_candidates",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("scope_type", sa.String(20), nullable=False),
        sa.Column("workspace_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("category", sa.String(30), nullable=False),
        sa.Column("suggested_key", sa.String(128), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("status", sa.String(20), nullable=False, server_default="pending"),
        sa.Column("source_task_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("source_run_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("source_message_ids", postgresql.JSONB(), nullable=False, server_default="[]"),
        sa.Column("extraction_input_fingerprint", sa.String(64), nullable=False),
        sa.Column("confidence", sa.Float(), nullable=False),
        sa.Column("importance", sa.Integer(), nullable=False),
        sa.Column("sensitivity", sa.String(20), nullable=False, server_default="normal"),
        sa.Column("deduplication_key", sa.String(64), nullable=False),
        sa.Column("extraction_policy_version", sa.String(80), nullable=False),
        sa.Column("extractor_provider", sa.String(80), nullable=False, server_default=""),
        sa.Column("extractor_model", sa.String(160), nullable=False, server_default=""),
        sa.Column("conflict_memory_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("approved_memory_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("resolution_note", sa.String(500), nullable=False, server_default=""),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["source_task_id"], ["tasks.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["source_run_id"], ["agent_runs.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["conflict_memory_id"], ["memories.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["approved_memory_id"], ["memories.id"], ondelete="SET NULL"),
        sa.CheckConstraint("scope_type IN ('global','workspace')", name="ck_memory_candidates_scope"),
        sa.CheckConstraint(
            "(scope_type = 'global' AND workspace_id IS NULL) OR "
            "(scope_type = 'workspace' AND workspace_id IS NOT NULL)",
            name="ck_memory_candidates_scope_owner",
        ),
        sa.CheckConstraint(
            "category IN ('preference','user_fact','project_fact','rule')",
            name="ck_memory_candidates_category",
        ),
        sa.CheckConstraint(
            "status IN ('pending','approved','rejected','expired')",
            name="ck_memory_candidates_status",
        ),
        sa.CheckConstraint("confidence BETWEEN 0 AND 1", name="ck_memory_candidates_confidence"),
        sa.CheckConstraint("importance BETWEEN 0 AND 100", name="ck_memory_candidates_importance"),
        sa.CheckConstraint("sensitivity IN ('normal','sensitive')", name="ck_memory_candidates_sensitivity"),
        sa.UniqueConstraint(
            "source_run_id", "extraction_policy_version", "deduplication_key",
            name="uq_memory_candidates_run_policy_dedup",
        ),
    )
    op.create_index("idx_memory_candidates_status_created", "memory_candidates", ["status", "created_at"])
    op.create_index("idx_memory_candidates_workspace_status", "memory_candidates", ["workspace_id", "status"])

    op.create_table(
        "memory_extraction_jobs",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("source_task_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("source_run_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("extraction_policy_version", sa.String(80), nullable=False),
        sa.Column("status", sa.String(20), nullable=False, server_default="queued"),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("next_retry_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("error_code", sa.String(80), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["source_task_id"], ["tasks.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["source_run_id"], ["agent_runs.id"], ondelete="CASCADE"),
        sa.CheckConstraint(
            "status IN ('queued','running','completed','failed')",
            name="ck_memory_extraction_jobs_status",
        ),
        sa.CheckConstraint("attempts BETWEEN 0 AND 10", name="ck_memory_extraction_jobs_attempts"),
        sa.UniqueConstraint(
            "source_run_id", "extraction_policy_version",
            name="uq_memory_extraction_jobs_run_policy",
        ),
    )
    op.create_index(
        "idx_memory_extraction_jobs_retry", "memory_extraction_jobs",
        ["status", "next_retry_at"],
    )


def downgrade() -> None:
    op.drop_table("memory_extraction_jobs")
    op.drop_table("memory_candidates")
    op.drop_constraint("ck_memories_source_type", "memories", type_="check")
    op.create_check_constraint(
        "ck_memories_source_type", "memories", "source_type IN ('user_explicit')"
    )
