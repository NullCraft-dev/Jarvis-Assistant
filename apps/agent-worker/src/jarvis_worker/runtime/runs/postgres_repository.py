"""PostgreSQL RunRepository（含乐观锁条件更新）。"""

from datetime import datetime, timezone
from typing import Optional
from uuid import UUID

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from jarvis_worker.database.models import AgentRunModel
from jarvis_worker.database.repositories.interfaces import RunRepository
from jarvis_worker.shared.domain.models import AgentRun, RunStatus


class PostgresRunRepository(RunRepository):
    """PostgreSQL AgentRun 持久化 adapter。"""

    def __init__(self, session: AsyncSession):
        self._session = session

    async def create(self, run: AgentRun) -> AgentRun:
        model = AgentRunModel(
            id=run.id,
            task_id=run.task_id,
            agent_id=run.agent_id,
            mode=run.mode,
            status=run.status.value if isinstance(run.status, RunStatus) else run.status,
            version=run.version,
            worker_id=run.worker_id,
            lease_until=run.lease_until,
            current_step_id=run.current_step_id,
            final_output_artifact_id=run.final_output_artifact_id,
            max_steps=run.max_steps,
            step_count=run.step_count,
            input_tokens=run.input_tokens,
            output_tokens=run.output_tokens,
            estimated_cost_usd=run.estimated_cost_usd,
            created_at=run.created_at,
            updated_at=run.updated_at,
            started_at=run.started_at,
            completed_at=run.completed_at,
            failed_at=run.failed_at,
            error_json=run.error,
            checkpoint_json=run.checkpoint,
            metadata_json=run.metadata,
        )
        self._session.add(model)
        return run

    async def get(self, run_id: UUID) -> Optional[AgentRun]:
        result = await self._session.execute(select(AgentRunModel).where(AgentRunModel.id == run_id))
        model = result.scalar_one_or_none()
        if model is None:
            return None
        return self._to_domain(model)

    async def list_by_task(self, task_id: UUID) -> list[AgentRun]:
        result = await self._session.execute(
            select(AgentRunModel)
            .where(AgentRunModel.task_id == task_id)
            .order_by(AgentRunModel.created_at.desc())
        )
        return [self._to_domain(m) for m in result.scalars().all()]

    async def list_recent(self, limit: int = 50) -> list[AgentRun]:
        result = await self._session.execute(
            select(AgentRunModel)
            .order_by(AgentRunModel.updated_at.desc(), AgentRunModel.id.desc())
            .limit(limit)
        )
        return [self._to_domain(m) for m in result.scalars().all()]

    async def list_expired_running(
        self, now: datetime, limit: int = 32
    ) -> list[AgentRun]:
        """锁定一批 lease 已过期的 running Run，供应用层 reconciliation。"""
        result = await self._session.execute(
            select(AgentRunModel)
            .where(AgentRunModel.status.in_([
                RunStatus.RUNNING.value,
                RunStatus.PAUSE_REQUESTED.value,
            ]))
            .where(AgentRunModel.lease_until.is_not(None))
            .where(AgentRunModel.lease_until <= now)
            .order_by(AgentRunModel.lease_until.asc())
            .limit(limit)
            .with_for_update(skip_locked=True)
        )
        return [self._to_domain(m) for m in result.scalars().all()]

    async def list_stale_queued(
        self, updated_before: datetime, limit: int = 32
    ) -> list[AgentRun]:
        """锁定长期未被 Worker claim 的 queued Run，供投递对账。"""
        result = await self._session.execute(
            select(AgentRunModel)
            .where(AgentRunModel.status == RunStatus.QUEUED.value)
            .where(AgentRunModel.updated_at <= updated_before)
            .order_by(AgentRunModel.updated_at.asc(), AgentRunModel.id.asc())
            .limit(limit)
            .with_for_update(skip_locked=True)
        )
        return [self._to_domain(m) for m in result.scalars().all()]

    async def renew_lease(
        self, run_id: UUID, worker_id: str, lease_until: datetime
    ) -> bool:
        result = await self._session.execute(
            update(AgentRunModel)
            .where(AgentRunModel.id == run_id)
            .where(AgentRunModel.status.in_([
                RunStatus.RUNNING.value,
                RunStatus.PAUSE_REQUESTED.value,
            ]))
            .where(AgentRunModel.worker_id == worker_id)
            .values(lease_until=lease_until, updated_at=datetime.now(timezone.utc))
        )
        return result.rowcount > 0

    async def update(self, run: AgentRun) -> None:
        await self._session.execute(
            update(AgentRunModel)
            .where(AgentRunModel.id == run.id)
            .values(
                status=run.status.value if isinstance(run.status, RunStatus) else run.status,
                version=run.version,
                worker_id=run.worker_id,
                lease_until=run.lease_until,
                step_count=run.step_count,
                updated_at=run.updated_at,
                started_at=run.started_at,
                completed_at=run.completed_at,
                failed_at=run.failed_at,
                error_json=run.error,
                checkpoint_json=run.checkpoint,
                current_step_id=run.current_step_id,
                final_output_artifact_id=run.final_output_artifact_id,
            )
        )

    async def update_with_lock(
        self,
        run_id: UUID,
        new_status: str,
        expected_version: int,
        expected_status: Optional[str] = None,
        **extra_fields,
    ) -> bool:
        """乐观锁条件更新。

        Args:
            run_id: Run ID。
            new_status: 目标状态。
            expected_version: 期望版本号。
            expected_status: 期望的当前状态（可选，为空时只检查 version）。
            **extra_fields: 额外更新字段。

        Returns:
            True 表示更新成功（affected rows > 0）。
        """
        values = {
            "status": new_status,
            "version": expected_version + 1,
            "updated_at": datetime.now(timezone.utc),
            **extra_fields,
        }

        stmt = (
            update(AgentRunModel)
            .where(AgentRunModel.id == run_id)
            .where(AgentRunModel.version == expected_version)
        )
        if expected_status is not None:
            stmt = stmt.where(AgentRunModel.status == expected_status)

        stmt = stmt.values(**values)
        result = await self._session.execute(stmt)
        return result.rowcount > 0

    @staticmethod
    def _to_domain(m: AgentRunModel) -> AgentRun:
        return AgentRun(
            id=m.id,
            task_id=m.task_id,
            agent_id=m.agent_id,
            mode=m.mode,
            status=RunStatus(m.status),
            version=m.version,
            worker_id=m.worker_id,
            lease_until=m.lease_until,
            current_step_id=m.current_step_id,
            final_output_artifact_id=m.final_output_artifact_id,
            max_steps=m.max_steps,
            step_count=m.step_count,
            input_tokens=m.input_tokens,
            output_tokens=m.output_tokens,
            estimated_cost_usd=m.estimated_cost_usd,
            created_at=m.created_at,
            updated_at=m.updated_at,
            started_at=m.started_at,
            completed_at=m.completed_at,
            failed_at=m.failed_at,
            error=m.error_json,
            checkpoint=m.checkpoint_json or {},
            metadata=m.metadata_json,
        )
