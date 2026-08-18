"""PostgreSQL EventRepository。"""

from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from jarvis_worker.shared.domain.models import RuntimeEvent
from jarvis_worker.database.models import RuntimeEventModel
from jarvis_worker.database.repositories.interfaces import EventRepository


class PostgresEventRepository(EventRepository):
    """PostgreSQL RuntimeEvent 持久化 adapter。仅追加，不修改。"""

    def __init__(self, session: AsyncSession):
        self._session = session

    async def append(self, events: list[RuntimeEvent]) -> None:
        """批量追加事件。使用 ON CONFLICT DO NOTHING 保证幂等。"""
        if not events:
            return
        values = [
            {
                "id": e.id,
                "event_id": e.event_id,
                "task_id": e.task_id,
                "run_id": e.run_id,
                "step_id": e.step_id,
                "type": e.type,
                "event_sequence": e.event_sequence,
                "payload_json": e.payload,
                "created_at": e.created_at,
            }
            for e in events
        ]
        stmt = pg_insert(RuntimeEventModel).values(values).on_conflict_do_nothing(index_elements=["event_id"])
        await self._session.execute(stmt)

    async def list_by_run(self, run_id: UUID) -> list[RuntimeEvent]:
        result = await self._session.execute(
            select(RuntimeEventModel)
            .where(RuntimeEventModel.run_id == run_id)
            .order_by(RuntimeEventModel.event_sequence.asc())
        )
        return [self._to_domain(m) for m in result.scalars().all()]

    async def get_next_sequence(self, run_id: UUID) -> int:
        # 对 agent_runs 行加锁，序列化同一 Run 的 sequence 分配
        from jarvis_worker.database.models import AgentRunModel
        await self._session.execute(
            select(AgentRunModel.id).where(AgentRunModel.id == run_id).with_for_update()
        )
        result = await self._session.execute(
            select(func.coalesce(func.max(RuntimeEventModel.event_sequence), 0))
            .where(RuntimeEventModel.run_id == run_id)
        )
        max_seq = result.scalar() or 0
        return max_seq + 1

    @staticmethod
    def _to_domain(m: RuntimeEventModel) -> RuntimeEvent:
        return RuntimeEvent(
            id=m.id,
            event_id=m.event_id,
            task_id=m.task_id,
            run_id=m.run_id,
            step_id=m.step_id,
            type=m.type,
            event_sequence=m.event_sequence,
            payload=m.payload_json,
            created_at=m.created_at,
        )
