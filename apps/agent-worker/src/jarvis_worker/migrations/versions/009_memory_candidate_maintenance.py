"""close memory candidate deduplication and expiration maintenance

Revision ID: 009_memory_candidate_maintenance
Revises: 008_memory_candidates
"""

from alembic import op
import sqlalchemy as sa


revision = "009_memory_candidate_maintenance"
down_revision = "008_memory_candidates"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 旧版本允许不同 Run 生成完全相同的 pending 候选。保留最早一条供用户决定，
    # 其余只转换为 expired，不删除历史来源。
    op.execute(
        """
        WITH ranked AS (
            SELECT id,
                   row_number() OVER (
                       PARTITION BY deduplication_key
                       ORDER BY created_at ASC, id ASC
                   ) AS duplicate_rank
            FROM memory_candidates
            WHERE status = 'pending'
        )
        UPDATE memory_candidates AS candidate
        SET status = 'expired',
            resolved_at = COALESCE(candidate.resolved_at, CURRENT_TIMESTAMP),
            updated_at = CURRENT_TIMESTAMP,
            version = candidate.version + 1,
            resolution_note = CASE
                WHEN candidate.resolution_note = ''
                    THEN 'migration: duplicate pending candidate'
                ELSE candidate.resolution_note
            END
        FROM ranked
        WHERE candidate.id = ranked.id AND ranked.duplicate_rank > 1
        """
    )
    op.create_index(
        "idx_memory_candidates_status_expires",
        "memory_candidates",
        ["status", "expires_at"],
    )
    op.create_index(
        "uq_memory_candidates_pending_dedup",
        "memory_candidates",
        ["deduplication_key"],
        unique=True,
        postgresql_where=sa.text("status = 'pending'"),
    )


def downgrade() -> None:
    op.drop_index(
        "uq_memory_candidates_pending_dedup", table_name="memory_candidates"
    )
    op.drop_index(
        "idx_memory_candidates_status_expires", table_name="memory_candidates"
    )
