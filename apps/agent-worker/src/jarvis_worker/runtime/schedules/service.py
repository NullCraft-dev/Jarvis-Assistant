from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from uuid import UUID
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from jarvis_worker.database.unit_of_work import PostgresUnitOfWork
from jarvis_worker.runtime.tasks.service import CreateTaskInput
from jarvis_worker.shared.domain.models import (
    AuditLog, ScheduleRecurrence, ScheduledExecutionStatus, ScheduledTask,
    ScheduledTaskExecution, ScheduledTaskStatus, WorkspaceStatus, new_id, utcnow,
)
from jarvis_worker.shared.errors.application import AppError

log = logging.getLogger("jarvis_worker.schedules")


@dataclass(frozen=True)
class CreateScheduledTaskInput:
    name: str
    user_goal: str
    recurrence: str
    timezone: str
    hour: int
    minute: int
    weekday: int | None = None
    workspace_id: UUID | None = None
    task_kind: str = "knowledge_report"
    source_query: str | None = None
    source_max_results: int = 5


class ScheduledTaskApplicationService:
    def __init__(self, uow_factory, task_service):
        self._uow_factory = uow_factory
        self._task_service = task_service

    async def list_tasks(self) -> list[ScheduledTask]:
        async with self._uow_factory()() as session:
            return await PostgresUnitOfWork(session).scheduled_tasks.list_all()

    async def create(self, data: CreateScheduledTaskInput) -> ScheduledTask:
        name, goal = data.name.strip(), data.user_goal.strip()
        if not name or not goal: raise AppError("VALIDATION_ERROR", "名称和任务目标不能为空", "validation")
        try: recurrence = ScheduleRecurrence(data.recurrence); zone = ZoneInfo(data.timezone)
        except (ValueError, ZoneInfoNotFoundError): raise AppError("VALIDATION_ERROR", "无效的重复规则或时区", "validation")
        if not 0 <= data.hour <= 23 or not 0 <= data.minute <= 59: raise AppError("VALIDATION_ERROR", "执行时间无效", "validation")
        if recurrence is ScheduleRecurrence.WEEKLY and (data.weekday is None or not 0 <= data.weekday <= 6): raise AppError("VALIDATION_ERROR", "每周任务必须指定星期", "validation")
        if recurrence is ScheduleRecurrence.DAILY and data.weekday is not None: raise AppError("VALIDATION_ERROR", "每日任务不能指定星期", "validation")
        if data.task_kind not in ("knowledge_report", "source_report"):
            raise AppError("VALIDATION_ERROR", "无效的定期任务类型", "validation")
        source_policy: dict = {}
        authorized_tools = ["knowledge.create_document"]
        if data.task_kind == "source_report":
            query = " ".join((data.source_query or "").split())
            if not query or len(query) > 300:
                raise AppError("VALIDATION_ERROR", "arXiv 检索词长度必须为 1 到 300", "validation")
            if not 1 <= data.source_max_results <= 10:
                raise AppError("VALIDATION_ERROR", "每次文献数量必须在 1 到 10 之间", "validation")
            source_policy = {"provider": "arxiv", "query": query, "max_results": data.source_max_results}
            authorized_tools.insert(0, "literature.search_arxiv")
        now = utcnow()
        item = ScheduledTask(id=new_id(), name=name[:200], user_goal=goal[:10000], recurrence=recurrence,
            timezone=data.timezone, hour=data.hour, minute=data.minute, weekday=data.weekday,
            workspace_id=data.workspace_id, next_run_at=_next_run(recurrence, zone, data.hour, data.minute, data.weekday, now),
            task_kind=data.task_kind, source_policy=source_policy, authorized_tools=authorized_tools,
            created_at=now, updated_at=now)
        async with self._uow_factory()() as session:
            async with PostgresUnitOfWork(session).transaction() as tx:
                if data.workspace_id:
                    workspace = await tx.workspaces.get(data.workspace_id)
                    if workspace is None or workspace.status is not WorkspaceStatus.ACTIVE: raise AppError("WORKSPACE_NOT_FOUND", "工作区不存在或已撤销", "not_found")
                item = await tx.scheduled_tasks.create(item)
                await tx.audits.create(_audit("schedule.created", f"创建定期任务：{item.name}", item.id))
                await tx.commit()
        return item

    async def set_status(self, item_id: UUID, status: str, expected_version: int) -> ScheduledTask:
        try: target = ScheduledTaskStatus(status)
        except ValueError: raise AppError("VALIDATION_ERROR", "无效的定期任务状态", "validation")
        async with self._uow_factory()() as session:
            async with PostgresUnitOfWork(session).transaction() as tx:
                item = await tx.scheduled_tasks.get(item_id, for_update=True)
                if item is None: raise AppError("SCHEDULE_NOT_FOUND", "定期任务不存在", "not_found")
                if item.version != expected_version: raise AppError("SCHEDULE_VERSION_CONFLICT", "定期任务已被修改，请刷新后重试", "runtime", True)
                item.status=target; item.version += 1; item.updated_at=utcnow()
                if target is ScheduledTaskStatus.ACTIVE and item.next_run_at <= item.updated_at:
                    item.next_run_at = _next_run(item.recurrence, ZoneInfo(item.timezone), item.hour, item.minute, item.weekday, item.updated_at)
                await tx.scheduled_tasks.update(item)
                await tx.audits.create(_audit(f"schedule.{target.value}", f"{target.value} 定期任务：{item.name}", item.id))
                await tx.commit(); return item

    async def trigger_now(self, item_id: UUID) -> ScheduledTaskExecution:
        now = utcnow()
        async with self._uow_factory()() as session:
            async with PostgresUnitOfWork(session).transaction() as tx:
                item = await tx.scheduled_tasks.get(item_id, for_update=True)
                if item is None: raise AppError("SCHEDULE_NOT_FOUND", "定期任务不存在", "not_found")
                execution = await tx.scheduled_executions.create(ScheduledTaskExecution(id=new_id(), scheduled_task_id=item.id, scheduled_for=now, created_at=now, updated_at=now))
                await tx.audits.create(_audit("schedule.triggered", f"手动触发定期任务：{item.name}", item.id))
                await tx.commit()
        await self._dispatch(execution.id)
        async with self._uow_factory()() as session:
            return await PostgresUnitOfWork(session).scheduled_executions.get(execution.id)

    async def tick(self) -> int:
        now = utcnow(); claimed = 0
        async with self._uow_factory()() as session:
            async with PostgresUnitOfWork(session).transaction() as tx:
                for item in await tx.scheduled_tasks.list_due_for_update(now, 16):
                    scheduled_for = item.next_run_at
                    await tx.scheduled_executions.create(ScheduledTaskExecution(id=new_id(), scheduled_task_id=item.id, scheduled_for=scheduled_for, created_at=now, updated_at=now))
                    item.last_run_at=scheduled_for; item.next_run_at=_next_run(item.recurrence, ZoneInfo(item.timezone), item.hour, item.minute, item.weekday, scheduled_for + timedelta(seconds=1)); item.updated_at=now; item.version += 1
                    await tx.scheduled_tasks.update(item); claimed += 1
                await tx.commit()
        await self.dispatch_pending()
        return claimed

    async def dispatch_pending(self, limit: int = 16) -> None:
        now = utcnow()
        async with self._uow_factory()() as session:
            candidates = await PostgresUnitOfWork(session).scheduled_executions.list_dispatchable(now, limit)
        for candidate in candidates:
            await self._dispatch(candidate.id)

    async def _dispatch(self, execution_id: UUID) -> None:
        now = utcnow()
        async with self._uow_factory()() as session:
            async with PostgresUnitOfWork(session).transaction() as tx:
                execution = await tx.scheduled_executions.get(execution_id, for_update=True)
                if execution is None or execution.status not in (ScheduledExecutionStatus.PENDING, ScheduledExecutionStatus.DISPATCHING): return
                if execution.status is ScheduledExecutionStatus.DISPATCHING and execution.lease_until and execution.lease_until > now: return
                if execution.attempts >= 3:
                    execution.status=ScheduledExecutionStatus.FAILED; execution.error_code="SCHEDULE_DISPATCH_RETRY_EXHAUSTED"; execution.updated_at=now; await tx.scheduled_executions.update(execution); await tx.commit(); return
                execution.status=ScheduledExecutionStatus.DISPATCHING; execution.attempts += 1; execution.lease_until=now + timedelta(seconds=60); execution.updated_at=now
                await tx.scheduled_executions.update(execution); await tx.commit()
        try:
            async with self._uow_factory()() as session:
                uow = PostgresUnitOfWork(session)
                schedule = await uow.scheduled_tasks.get(execution.scheduled_task_id)
                existing = await uow.tasks.get_by_scheduled_execution(execution.id)
            if schedule is None: raise AppError("SCHEDULE_NOT_FOUND", "定期任务不存在", "not_found")
            if existing is not None:
                task_id, run_id = existing.id, existing.active_run_id
            else:
                result = await self._task_service.create_task(CreateTaskInput(
                    user_goal=_execution_goal(schedule), title=f"[定期] {schedule.name}", workspace_id=schedule.workspace_id,
                    scheduled_execution_id=execution.id,
                    metadata={"scheduled_task_id": str(schedule.id), "authorized_tools": list(schedule.authorized_tools), "source_policy": dict(schedule.source_policy)},
                ))
                task_id, run_id = result.task.id, result.run.id
            async with self._uow_factory()() as session:
                async with PostgresUnitOfWork(session).transaction() as tx:
                    current = await tx.scheduled_executions.get(execution.id, for_update=True); current.status=ScheduledExecutionStatus.DISPATCHED; current.task_id=task_id; current.run_id=run_id; current.lease_until=None; current.error_code=None; current.updated_at=utcnow(); await tx.scheduled_executions.update(current)
                    schedule = await tx.scheduled_tasks.get(current.scheduled_task_id, for_update=True); schedule.last_task_id=task_id; schedule.last_run_id=run_id; schedule.updated_at=utcnow(); await tx.scheduled_tasks.update(schedule); await tx.commit()
        except Exception as exc:
            log.warning("定期任务派发失败: execution=%s error=%s", execution_id, type(exc).__name__)


class ScheduledTaskWorker:
    def __init__(self, service: ScheduledTaskApplicationService, poll_interval: float = 30): self._service=service; self._interval=max(5, poll_interval); self._task=None
    async def start(self):
        if self._task is None: self._task=asyncio.create_task(self._loop(), name="scheduled-task-worker")
    async def stop(self):
        if self._task: self._task.cancel(); await asyncio.gather(self._task, return_exceptions=True); self._task=None
    async def _loop(self):
        while True:
            try: await self._service.tick()
            except Exception: log.exception("定期任务扫描失败")
            await asyncio.sleep(self._interval)


def _next_run(recurrence: ScheduleRecurrence, zone: ZoneInfo, hour: int, minute: int, weekday: int | None, after: datetime) -> datetime:
    local = after.astimezone(zone)
    candidate = local.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if recurrence is ScheduleRecurrence.DAILY:
        if candidate <= local: candidate += timedelta(days=1)
    else:
        delta = (int(weekday) - local.weekday()) % 7
        candidate += timedelta(days=delta)
        if candidate <= local: candidate += timedelta(days=7)
    return candidate.astimezone(timezone.utc)


def _audit(event: str, summary: str, schedule_id: UUID) -> AuditLog:
    return AuditLog(id=new_id(), event_type=event, actor="user" if event in ("schedule.created", "schedule.triggered") else "system", risk_level="L2", permission_decision="user_explicit", action_summary=summary, details={"scheduled_task_id": str(schedule_id)}, result_summary="操作已保存")


def _execution_goal(schedule: ScheduledTask) -> str:
    if schedule.task_kind != "source_report":
        return schedule.user_goal
    policy = schedule.source_policy
    return (
        f"{schedule.user_goal}\n\n"
        "这是经过用户持久授权的定期来源报告。必须先调用 literature.search_arxiv，"
        f"参数必须严格为 query={policy['query']!r}、max_results={policy['max_results']}。"
        "仅总结工具返回的未收录结果，并调用 knowledge.create_document 保存 report；"
        "source_urls 必须列出每一条实际采用结果的 abstract_url。若没有新结果，也要保存一份简短报告说明本期无新增来源。"
    )
