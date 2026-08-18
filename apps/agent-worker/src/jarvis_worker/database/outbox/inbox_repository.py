"""PostgreSQL InboxRepository（幂等消费记录）。"""

from datetime import datetime, timezone

from sqlalchemy import update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from jarvis_worker.database.models import InboxEventModel
from jarvis_worker.database.repositories.interfaces import InboxRepository


class PostgresInboxRepository(InboxRepository):
    """PostgreSQL Inbox 持久化 adapter。"""

    def __init__(self, session: AsyncSession):
        self._session = session

    async def try_insert(self, source: str, source_event_id: str) -> bool:
        """尝试插入去重记录。

        使用 ON CONFLICT DO NOTHING 保证幂等。

        Returns:
            True 表示首次处理（插入成功），False 表示重复。
        """
        stmt = pg_insert(InboxEventModel).values(
            source=source,
            source_event_id=source_event_id,
            status="processing",
        ).on_conflict_do_nothing(index_elements=["source", "source_event_id"])

        result = await self._session.execute(stmt)
        return result.rowcount > 0

    async def mark_processed(self, source: str, source_event_id: str) -> None:
        await self._session.execute(
            update(InboxEventModel)
            .where(InboxEventModel.source == source)
            .where(InboxEventModel.source_event_id == source_event_id)
            .values(status="processed", processed_at=datetime.now(timezone.utc))
        )
