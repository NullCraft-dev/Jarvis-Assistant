"""extend bounded Outbox transport retry window

Revision ID: 018_outbox_redis_recovery
Revises: 017_rag_evaluation_flywheel
"""

import sqlalchemy as sa
from alembic import op

revision = "018_outbox_redis_recovery"
down_revision = "017_rag_evaluation_flywheel"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.alter_column(
        "outbox_events",
        "max_retries",
        existing_type=sa.Integer(),
        server_default="20",
        existing_nullable=False,
    )
    op.create_check_constraint(
        "ck_outbox_retry_budget",
        "outbox_events",
        "retry_count >= 0 AND max_retries BETWEEN 1 AND 100",
    )
    # 尚未完成的旧事件继承新的有限预算。只重新激活明确由 Redis 传输
    # 中断导致、且仍低于新预算的 dead 事件；未知/契约错误继续保持 dead。
    op.execute(
        """
        UPDATE outbox_events
        SET max_retries = 20
        WHERE max_retries < 20
          AND status IN ('pending', 'dispatching')
        """
    )
    op.execute(
        """
        UPDATE outbox_events
        SET status = 'pending',
            max_retries = 20,
            next_retry_at = now(),
            claimed_by = NULL,
            claimed_at = NULL,
            lease_until = NULL
        WHERE status = 'dead'
          AND error_code = 'REDIS_PUBLISH_ERROR'
          AND retry_count < 20
        """
    )


def downgrade() -> None:
    op.drop_constraint("ck_outbox_retry_budget", "outbox_events", type_="check")
    op.alter_column(
        "outbox_events",
        "max_retries",
        existing_type=sa.Integer(),
        server_default="5",
        existing_nullable=False,
    )
