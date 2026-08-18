"""EventApplicationService — RuntimeEvent 追加与查询。"""

from uuid import UUID

from jarvis_worker.shared.domain.models import RuntimeEvent
from jarvis_worker.database.unit_of_work import PostgresUnitOfWork


class EventApplicationService:
    """RuntimeEvent Application Service。仅追加，不修改。"""

    def __init__(self, uow_factory):
        self._uow_factory = uow_factory

    async def get_events_by_run(self, run_id: UUID) -> list[RuntimeEvent]:
        """查询 Run 的所有事件（按 event_sequence 排序）。"""
        session_factory = self._uow_factory()
        async with session_factory() as session:
            uow = PostgresUnitOfWork(session)
            return await uow.events.list_by_run(run_id)

    async def append_events(
        self, run_id: UUID, events: list[RuntimeEvent]
    ) -> None:
        """增量追加事件到 Run。

        在每个事件上设置递增的 event_sequence。
        使用 ON CONFLICT DO NOTHING 保证幂等。
        """
        if not events:
            return

        session_factory = self._uow_factory()
        async with session_factory() as session:
            uow = PostgresUnitOfWork(session)
            async with uow.transaction() as tx:
                next_seq = await tx.events.get_next_sequence(run_id)
                for i, event in enumerate(events):
                    event.event_sequence = next_seq + i
                await tx.events.append(events)
                await tx.commit()

    async def get_messages_by_task(self, task_id: UUID) -> list:
        session_factory = self._uow_factory()
        async with session_factory() as session:
            uow = PostgresUnitOfWork(session)
            return await uow.messages.list_by_task(task_id)
