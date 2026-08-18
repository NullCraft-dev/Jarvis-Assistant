
"""PostgreSQL StepRepository。"""

from typing import Optional
from uuid import UUID

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from jarvis_worker.shared.domain.models import ExecutionStep
from jarvis_worker.database.models import ExecutionStepModel
from jarvis_worker.database.repositories.interfaces import StepRepository


class PostgresStepRepository(StepRepository):
    """PostgreSQL ExecutionStep 持久化 adapter。"""

    def __init__(self, session: AsyncSession):
        self._session = session

    async def create(self, step: ExecutionStep) -> ExecutionStep:
        model = ExecutionStepModel(
            id=step.id, run_id=step.run_id, task_id=step.task_id,
            parent_step_id=step.parent_step_id, type=step.type.value if hasattr(step.type, "value") else step.type,
            status=step.status.value if hasattr(step.status, "value") else step.status,
            title=step.title, summary=step.summary,
            input_json=step.input_data, output_json=step.output_data,
            error_json=step.error,
            started_at=step.started_at, completed_at=step.completed_at,
            duration_ms=step.duration_ms, order_index=step.order_index,
            metadata_json=step.metadata,
        )
        self._session.add(model)
        return step

    async def update(self, step: ExecutionStep) -> None:
        await self._session.execute(
            update(ExecutionStepModel)
            .where(ExecutionStepModel.id == step.id)
            .values(
                status=step.status.value if hasattr(step.status, "value") else step.status,
                summary=step.summary, output_json=step.output_data,
                error_json=step.error, completed_at=step.completed_at,
                duration_ms=step.duration_ms,
            )
        )

    async def get(self, step_id: UUID) -> Optional[ExecutionStep]:
        result = await self._session.execute(
            select(ExecutionStepModel).where(ExecutionStepModel.id == step_id)
        )
        model = result.scalar_one_or_none()
        if model is None: return None
        return self._to_domain(model)

    async def list_by_run(self, run_id: UUID) -> list[ExecutionStep]:
        result = await self._session.execute(
            select(ExecutionStepModel)
            .where(ExecutionStepModel.run_id == run_id)
            .order_by(ExecutionStepModel.order_index.asc())
        )
        return [self._to_domain(m) for m in result.scalars().all()]

    @staticmethod
    def _to_domain(m: ExecutionStepModel):
        from jarvis_worker.shared.domain.models import StepType, StepStatus
        return ExecutionStep(
            id=m.id, run_id=m.run_id, task_id=m.task_id, parent_step_id=m.parent_step_id,
            type=StepType(m.type), status=StepStatus(m.status),
            title=m.title, summary=m.summary,
            input_data=m.input_json, output_data=m.output_json, error=m.error_json,
            started_at=m.started_at, completed_at=m.completed_at,
            duration_ms=m.duration_ms, order_index=m.order_index, metadata=m.metadata_json,
        )
