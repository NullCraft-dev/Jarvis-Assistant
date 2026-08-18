"""PostgreSQL TaskRepository。"""

from typing import Optional
from uuid import UUID

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from jarvis_worker.shared.domain.models import Task, TaskStatus
from jarvis_worker.database.models import TaskModel
from jarvis_worker.database.repositories.interfaces import TaskRepository


class PostgresTaskRepository(TaskRepository):
    """PostgreSQL 任务持久化 adapter。"""

    def __init__(self, session: AsyncSession):
        self._session = session

    async def create(self, task: Task) -> Task:
        model = TaskModel(
            id=task.id,
            conversation_id=task.conversation_id,
            title=task.title,
            user_goal=task.user_goal,
            status=task.status.value if isinstance(task.status, TaskStatus) else task.status,
            priority=task.priority,
            workspace_path=task.workspace_path,
            workspace_id=task.workspace_id,
            active_run_id=task.active_run_id,
            last_step_summary=task.last_step_summary,
            risk_level=task.risk_level,
            created_at=task.created_at,
            updated_at=task.updated_at,
            completed_at=task.completed_at,
            cancelled_at=task.cancelled_at,
            metadata_json=task.metadata,
            scheduled_execution_id=task.scheduled_execution_id,
        )
        self._session.add(model)
        return task

    async def get(self, task_id: UUID) -> Optional[Task]:
        result = await self._session.execute(select(TaskModel).where(TaskModel.id == task_id))
        model = result.scalar_one_or_none()
        if model is None:
            return None
        return self._to_domain(model)

    async def get_by_scheduled_execution(self, execution_id: UUID) -> Optional[Task]:
        result = await self._session.execute(select(TaskModel).where(TaskModel.scheduled_execution_id == execution_id))
        model = result.scalar_one_or_none()
        return self._to_domain(model) if model else None

    async def list_all(self, limit: int = 50, offset: int = 0) -> list[Task]:
        result = await self._session.execute(
            select(TaskModel)
            .order_by(TaskModel.updated_at.desc())
            .offset(offset)
            .limit(limit)
        )
        return [self._to_domain(m) for m in result.scalars().all()]

    async def update(self, task: Task) -> None:
        await self._session.execute(
            update(TaskModel)
            .where(TaskModel.id == task.id)
            .values(
                status=task.status.value if isinstance(task.status, TaskStatus) else task.status,
                active_run_id=task.active_run_id,
                last_step_summary=task.last_step_summary,
                updated_at=task.updated_at,
                completed_at=task.completed_at,
                cancelled_at=task.cancelled_at,
            )
        )

    @staticmethod
    def _to_domain(m: TaskModel) -> Task:
        return Task(
            id=m.id,
            conversation_id=m.conversation_id,
            title=m.title,
            user_goal=m.user_goal,
            status=TaskStatus(m.status),
            priority=m.priority,
            workspace_path=m.workspace_path,
            workspace_id=m.workspace_id,
            active_run_id=m.active_run_id,
            last_step_summary=m.last_step_summary,
            risk_level=m.risk_level,
            created_at=m.created_at,
            updated_at=m.updated_at,
            completed_at=m.completed_at,
            cancelled_at=m.cancelled_at,
            metadata=m.metadata_json,
            scheduled_execution_id=m.scheduled_execution_id,
        )
