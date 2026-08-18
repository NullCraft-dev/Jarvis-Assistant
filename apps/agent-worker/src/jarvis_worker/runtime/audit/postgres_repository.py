"""PostgreSQL AuditRepository。"""

from datetime import datetime
from uuid import UUID

from sqlalchemy import and_, delete, or_, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from jarvis_worker.database.models import AuditLogModel
from jarvis_worker.database.repositories.interfaces import AuditRepository
from jarvis_worker.shared.domain.models import AuditLog


class PostgresAuditRepository(AuditRepository):
    """PostgreSQL AuditLog 持久化 adapter。"""

    def __init__(self, session: AsyncSession):
        self._session = session

    async def create(self, log: AuditLog) -> AuditLog:
        model = AuditLogModel(
            id=log.id,
            task_id=log.task_id,
            run_id=log.run_id,
            step_id=log.step_id,
            tool_call_id=log.tool_call_id,
            event_type=log.event_type,
            actor=log.actor,
            risk_level=log.risk_level,
            permission_decision=log.permission_decision,
            action_summary=log.action_summary,
            details_json=log.details,
            result_summary=log.result_summary,
            error_json=log.error,
            created_at=log.created_at,
        )
        self._session.add(model)
        return log

    async def list_by_task(self, task_id: UUID) -> list[AuditLog]:
        result = await self._session.execute(
            select(AuditLogModel)
            .where(AuditLogModel.task_id == task_id)
            .order_by(AuditLogModel.created_at.desc())
        )
        return [self._to_domain(m) for m in result.scalars().all()]

    async def list_by_run(self, run_id: UUID) -> list[AuditLog]:
        result = await self._session.execute(
            select(AuditLogModel)
            .where(AuditLogModel.run_id == run_id)
            .order_by(AuditLogModel.created_at.asc())
        )
        return [self._to_domain(m) for m in result.scalars().all()]

    async def list_page(
        self,
        *,
        limit: int,
        event_type: str | None = None,
        actor: str | None = None,
        task_id: UUID | None = None,
        run_id: UUID | None = None,
        before_created_at: datetime | None = None,
        before_id: UUID | None = None,
    ) -> list[AuditLog]:
        """按最新优先查询有界审计页；cursor 使用 (created_at, id) 保证稳定翻页。"""
        conditions = []
        if event_type:
            conditions.append(AuditLogModel.event_type == event_type)
        if actor:
            conditions.append(AuditLogModel.actor == actor)
        if task_id:
            conditions.append(AuditLogModel.task_id == task_id)
        if run_id:
            conditions.append(AuditLogModel.run_id == run_id)
        if before_created_at and before_id:
            conditions.append(
                or_(
                    AuditLogModel.created_at < before_created_at,
                    and_(AuditLogModel.created_at == before_created_at, AuditLogModel.id < before_id),
                )
            )

        statement = select(AuditLogModel)
        if conditions:
            statement = statement.where(*conditions)
        statement = statement.order_by(AuditLogModel.created_at.desc(), AuditLogModel.id.desc()).limit(limit)
        result = await self._session.execute(statement)
        return [self._to_domain(m) for m in result.scalars().all()]

    async def list_oldest_page(
        self,
        *,
        limit: int,
        created_before: datetime,
        after_created_at: datetime | None = None,
        after_id: UUID | None = None,
    ) -> list[AuditLog]:
        """通用时间窗口扫描；保留分类语义仍由 Application Service 拥有。"""
        conditions = [AuditLogModel.created_at < created_before]
        if after_created_at and after_id:
            conditions.append(
                or_(
                    AuditLogModel.created_at > after_created_at,
                    and_(
                        AuditLogModel.created_at == after_created_at,
                        AuditLogModel.id > after_id,
                    ),
                )
            )
        statement = (
            select(AuditLogModel)
            .where(*conditions)
            .order_by(AuditLogModel.created_at.asc(), AuditLogModel.id.asc())
            .limit(limit)
        )
        result = await self._session.execute(statement)
        return [self._to_domain(m) for m in result.scalars().all()]

    async def acquire_retention_execution_lock(self) -> None:
        """固定 advisory xact lock，防止不同确认请求并发清理同一批日志。"""
        await self._session.execute(
            text("SELECT pg_advisory_xact_lock(:lock_key)"),
            {"lock_key": 4_741_838_377_101},
        )

    async def delete_by_ids(self, audit_log_ids: list[UUID]) -> int:
        if not audit_log_ids:
            return 0
        result = await self._session.execute(
            delete(AuditLogModel)
            .where(AuditLogModel.id.in_(audit_log_ids))
            .returning(AuditLogModel.id)
        )
        return len(result.scalars().all())

    @staticmethod
    def _to_domain(m: AuditLogModel) -> AuditLog:
        return AuditLog(
            id=m.id,
            task_id=m.task_id,
            run_id=m.run_id,
            step_id=m.step_id,
            tool_call_id=m.tool_call_id,
            event_type=m.event_type,
            actor=m.actor,
            risk_level=m.risk_level,
            permission_decision=m.permission_decision,
            action_summary=m.action_summary,
            details=m.details_json,
            result_summary=m.result_summary,
            error=m.error_json,
            created_at=m.created_at,
        )
