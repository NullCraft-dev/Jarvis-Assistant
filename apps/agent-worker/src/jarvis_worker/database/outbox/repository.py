"""PostgreSQL OutboxRepository（Transactional Outbox）。

使用 FOR UPDATE SKIP LOCKED 安全并发 claim 事件。
"""

from datetime import datetime, timezone
from uuid import UUID

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from jarvis_worker.database.models import OutboxEventModel
from jarvis_worker.database.repositories.interfaces import OutboxRepository
from jarvis_worker.shared.domain.models import OutboxEvent, OutboxStatus


class PostgresOutboxRepository(OutboxRepository):
    """PostgreSQL Outbox 持久化 adapter。"""

    def __init__(self, session: AsyncSession):
        self._session = session

    async def create(self, events: list[OutboxEvent]) -> None:
        """批量创建 OutboxEvent。"""
        if not events:
            return
        models = [
            OutboxEventModel(
                id=e.id,
                event_id=e.event_id,
                aggregate_type=e.aggregate_type,
                aggregate_id=e.aggregate_id,
                event_type=e.event_type,
                schema_version=e.schema_version,
                payload=e.payload,
                trace_id=e.trace_id,
                correlation_id=e.correlation_id,
                causation_id=e.causation_id,
                status=e.status.value if isinstance(e.status, OutboxStatus) else e.status,
                retry_count=e.retry_count,
                max_retries=e.max_retries,
                next_retry_at=e.next_retry_at,
                created_at=e.created_at,
            )
            for e in events
        ]
        self._session.add_all(models)

    async def claim_pending(
        self, batch_size: int = 32, lease_seconds: int = 30, claimed_by: str = "publisher-01"
    ) -> list[OutboxEvent]:
        """使用 FOR UPDATE SKIP LOCKED claim 一批 pending 事件。

        在短事务中：SELECT + UPDATE，提交后释放锁。
        """
        now = datetime.now(timezone.utc)
        lease_until = datetime.fromtimestamp(now.timestamp() + lease_seconds, tz=timezone.utc)

        # SELECT ... FOR UPDATE SKIP LOCKED
        result = await self._session.execute(
            select(OutboxEventModel)
            .where(OutboxEventModel.status == "pending")
            .where(OutboxEventModel.next_retry_at <= now)
            .order_by(OutboxEventModel.created_at.asc())
            .limit(batch_size)
            .with_for_update(skip_locked=True)
        )
        rows = result.scalars().all()
        if not rows:
            return []

        ids = [r.id for r in rows]
        # UPDATE 为 dispatching
        await self._session.execute(
            update(OutboxEventModel)
            .where(OutboxEventModel.id.in_(ids))
            .values(
                status="dispatching",
                claimed_by=claimed_by,
                claimed_at=now,
                lease_until=lease_until,
            )
        )
        return [self._to_domain(r) for r in rows]

    async def mark_delivered(self, event_ids: list[UUID]) -> None:
        """标记事件为 delivered。"""
        if not event_ids:
            return
        now = datetime.now(timezone.utc)
        await self._session.execute(
            update(OutboxEventModel)
            .where(OutboxEventModel.id.in_(event_ids))
            .values(status="delivered", delivered_at=now, claimed_by=None, lease_until=None)
        )

    async def mark_failed(
        self, event_ids: list[UUID], error_code: str, error_message: str = ""
    ) -> None:
        """标记事件失败。根据重试次数决定 pending 还是 dead。"""
        if not event_ids:
            return
        now = datetime.now(timezone.utc)

        # 对每个事件单独处理
        for eid in event_ids:
            event = await self._session.get(OutboxEventModel, eid)
            if event is None:
                continue
            new_retry_count = event.retry_count + 1
            event.retry_count = new_retry_count
            event.claimed_by = None
            event.claimed_at = None
            event.lease_until = None
            if new_retry_count >= event.max_retries:
                # 进入 dead
                event.status = "dead"
                event.error_code = error_code
                event.error_message = error_message
            else:
                # 计算退避：min(100ms * 2^retry, 60s)
                backoff_seconds = min(0.1 * (2 ** new_retry_count), 60)
                event.status = "pending"
                event.next_retry_at = datetime.fromtimestamp(
                    now.timestamp() + backoff_seconds, tz=timezone.utc
                )
                event.error_code = error_code
                event.error_message = error_message

    async def reset_stale_dispatching(self, stale_seconds: int = 60) -> int:
        """重置 lease 过期的 dispatching 事件为 pending。"""
        now = datetime.now(timezone.utc)
        result = await self._session.execute(
            update(OutboxEventModel)
            .where(OutboxEventModel.status == "dispatching")
            .where(OutboxEventModel.lease_until <= now)
            .values(
                status="pending",
                claimed_by=None,
                claimed_at=None,
                lease_until=None,
            )
        )
        return result.rowcount

    async def get_latest_run_job(self, run_id: UUID) -> OutboxEvent | None:
        """返回指定 Run 最近一次 durable queue job。"""
        result = await self._session.execute(
            select(OutboxEventModel)
            .where(OutboxEventModel.aggregate_type == "AgentRun")
            .where(OutboxEventModel.aggregate_id == run_id)
            .where(
                OutboxEventModel.event_type.in_((
                    "task.created",
                    "run.resume.requested",
                    "run.retry.requested",
                    "run.step_retry.requested",
                    "run.queue.reconciled",
                ))
            )
            .order_by(OutboxEventModel.created_at.desc(), OutboxEventModel.id.desc())
            .limit(1)
        )
        model = result.scalar_one_or_none()
        return self._to_domain(model) if model is not None else None

    @staticmethod
    def _to_domain(m: OutboxEventModel) -> OutboxEvent:
        return OutboxEvent(
            id=m.id,
            event_id=m.event_id,
            aggregate_type=m.aggregate_type,
            aggregate_id=m.aggregate_id,
            event_type=m.event_type,
            schema_version=m.schema_version,
            payload=m.payload,
            trace_id=m.trace_id,
            correlation_id=m.correlation_id,
            causation_id=m.causation_id,
            status=OutboxStatus(m.status),
            retry_count=m.retry_count,
            max_retries=m.max_retries,
            next_retry_at=m.next_retry_at,
            claimed_by=m.claimed_by,
            claimed_at=m.claimed_at,
            lease_until=m.lease_until,
            error_code=m.error_code,
            error_message=m.error_message,
            created_at=m.created_at,
            delivered_at=m.delivered_at,
        )
