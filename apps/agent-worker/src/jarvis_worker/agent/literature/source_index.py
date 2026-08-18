"""Read model for sources already committed to scheduled knowledge reports."""

from __future__ import annotations

from uuid import UUID

from sqlalchemy import select

from jarvis_worker.database.models import (
    KnowledgeDocumentModel,
    ScheduledTaskExecutionModel,
    TaskModel,
)


class ScheduledSourceIndex:
    def __init__(self, session_factory):
        self._session_factory = session_factory

    async def known_urls(self, scheduled_task_id: UUID) -> set[str]:
        async with self._session_factory()() as session:
            result = await session.execute(
                select(KnowledgeDocumentModel.source_urls_json)
                .join(TaskModel, KnowledgeDocumentModel.source_task_id == TaskModel.id)
                .join(
                    ScheduledTaskExecutionModel,
                    TaskModel.scheduled_execution_id == ScheduledTaskExecutionModel.id,
                )
                .where(ScheduledTaskExecutionModel.scheduled_task_id == scheduled_task_id)
            )
        return {
            value.strip()
            for values in result.scalars().all()
            for value in (values or [])
            if isinstance(value, str) and value.strip()
        }
