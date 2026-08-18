from datetime import datetime
from uuid import UUID

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from jarvis_worker.database.models import ScheduledTaskExecutionModel, ScheduledTaskModel
from jarvis_worker.shared.domain.models import (
    ScheduleRecurrence, ScheduledExecutionStatus, ScheduledTask,
    ScheduledTaskExecution, ScheduledTaskStatus,
)


def _schedule(m: ScheduledTaskModel) -> ScheduledTask:
    return ScheduledTask(
        id=m.id, name=m.name, user_goal=m.user_goal, recurrence=ScheduleRecurrence(m.recurrence),
        timezone=m.timezone, hour=m.hour, minute=m.minute, weekday=m.weekday,
        workspace_id=m.workspace_id, status=ScheduledTaskStatus(m.status),
        authorized_tools=list(m.authorized_tools_json or []), next_run_at=m.next_run_at,
        task_kind=m.task_kind, source_policy=dict(m.source_policy_json or {}),
        last_run_at=m.last_run_at, last_task_id=m.last_task_id, last_run_id=m.last_run_id,
        version=m.version, created_at=m.created_at, updated_at=m.updated_at,
    )


def _execution(m: ScheduledTaskExecutionModel) -> ScheduledTaskExecution:
    return ScheduledTaskExecution(
        id=m.id, scheduled_task_id=m.scheduled_task_id, scheduled_for=m.scheduled_for,
        status=ScheduledExecutionStatus(m.status), task_id=m.task_id, run_id=m.run_id,
        attempts=m.attempts, lease_until=m.lease_until, error_code=m.error_code,
        created_at=m.created_at, updated_at=m.updated_at,
    )


class PostgresScheduledTaskRepository:
    def __init__(self, session: AsyncSession): self._session = session
    async def create(self, item: ScheduledTask) -> ScheduledTask:
        model = ScheduledTaskModel(id=item.id, name=item.name, user_goal=item.user_goal, recurrence=item.recurrence.value,
            timezone=item.timezone, hour=item.hour, minute=item.minute, weekday=item.weekday, workspace_id=item.workspace_id,
            status=item.status.value, authorized_tools_json=item.authorized_tools, next_run_at=item.next_run_at,
            task_kind=item.task_kind, source_policy_json=item.source_policy,
            last_run_at=item.last_run_at, last_task_id=item.last_task_id, last_run_id=item.last_run_id,
            version=item.version, created_at=item.created_at, updated_at=item.updated_at)
        self._session.add(model); await self._session.flush(); return _schedule(model)
    async def get(self, item_id: UUID, *, for_update: bool = False) -> ScheduledTask | None:
        stmt = select(ScheduledTaskModel).where(ScheduledTaskModel.id == item_id)
        if for_update: stmt = stmt.with_for_update()
        model = (await self._session.execute(stmt)).scalar_one_or_none()
        return _schedule(model) if model else None
    async def list_all(self) -> list[ScheduledTask]:
        result = await self._session.execute(select(ScheduledTaskModel).order_by(ScheduledTaskModel.created_at.desc()).limit(100))
        return [_schedule(item) for item in result.scalars().all()]
    async def list_due_for_update(self, now: datetime, limit: int) -> list[ScheduledTask]:
        result = await self._session.execute(select(ScheduledTaskModel).where(
            ScheduledTaskModel.status == "active", ScheduledTaskModel.next_run_at <= now
        ).order_by(ScheduledTaskModel.next_run_at).limit(limit).with_for_update(skip_locked=True))
        return [_schedule(item) for item in result.scalars().all()]
    async def update(self, item: ScheduledTask) -> None:
        model = await self._session.get(ScheduledTaskModel, item.id)
        model.status=item.status.value; model.next_run_at=item.next_run_at; model.last_run_at=item.last_run_at
        model.last_task_id=item.last_task_id; model.last_run_id=item.last_run_id; model.version=item.version; model.updated_at=item.updated_at
        await self._session.flush()


class PostgresScheduledExecutionRepository:
    def __init__(self, session: AsyncSession): self._session = session
    async def create(self, item: ScheduledTaskExecution) -> ScheduledTaskExecution:
        model = ScheduledTaskExecutionModel(id=item.id, scheduled_task_id=item.scheduled_task_id, scheduled_for=item.scheduled_for,
            status=item.status.value, task_id=item.task_id, run_id=item.run_id, attempts=item.attempts,
            lease_until=item.lease_until, error_code=item.error_code, created_at=item.created_at, updated_at=item.updated_at)
        self._session.add(model); await self._session.flush(); return _execution(model)
    async def get(self, item_id: UUID, *, for_update: bool = False) -> ScheduledTaskExecution | None:
        stmt = select(ScheduledTaskExecutionModel).where(ScheduledTaskExecutionModel.id == item_id)
        if for_update: stmt = stmt.with_for_update()
        model = (await self._session.execute(stmt)).scalar_one_or_none()
        return _execution(model) if model else None
    async def list_dispatchable(self, now: datetime, limit: int = 16) -> list[ScheduledTaskExecution]:
        result = await self._session.execute(select(ScheduledTaskExecutionModel).where(or_(
            ScheduledTaskExecutionModel.status == "pending",
            (ScheduledTaskExecutionModel.status == "dispatching") & (ScheduledTaskExecutionModel.lease_until <= now),
        )).order_by(ScheduledTaskExecutionModel.created_at).limit(limit))
        return [_execution(item) for item in result.scalars().all()]
    async def update(self, item: ScheduledTaskExecution) -> None:
        model = await self._session.get(ScheduledTaskExecutionModel, item.id)
        model.status=item.status.value; model.task_id=item.task_id; model.run_id=item.run_id; model.attempts=item.attempts
        model.lease_until=item.lease_until; model.error_code=item.error_code; model.updated_at=item.updated_at
        await self._session.flush()
