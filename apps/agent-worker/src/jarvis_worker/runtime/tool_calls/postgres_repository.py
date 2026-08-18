"""PostgreSQL ToolCallRepository。"""

from uuid import UUID

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from jarvis_worker.shared.domain.models import ToolCall
from jarvis_worker.database.models import ToolCallModel
from jarvis_worker.database.repositories.interfaces import ToolCallRepository


class PostgresToolCallRepository(ToolCallRepository):
    """PostgreSQL ToolCall 持久化 adapter。"""

    def __init__(self, session: AsyncSession):
        self._session = session

    async def create(self, tc: ToolCall) -> ToolCall:
        model = ToolCallModel(
            id=tc.id,
            task_id=tc.task_id,
            run_id=tc.run_id,
            step_id=tc.step_id,
            provider=tc.provider,
            tool_name=tc.tool_name,
            mcp_server_id=tc.mcp_server_id,
            risk_level=tc.risk_level,
            arguments_json=tc.arguments,
            arguments_summary_json=tc.arguments_summary,
            result_json=tc.result,
            result_summary=tc.result_summary,
            permission_request_id=tc.permission_request_id,
            permission_status=tc.permission_status,
            status=tc.status,
            error_json=tc.error,
            started_at=tc.started_at,
            completed_at=tc.completed_at,
            duration_ms=tc.duration_ms,
        )
        self._session.add(model)
        return tc

    async def get(self, tool_call_id: UUID) -> ToolCall | None:
        result = await self._session.execute(
            select(ToolCallModel).where(ToolCallModel.id == tool_call_id)
        )
        model = result.scalar_one_or_none()
        return self._to_domain(model) if model is not None else None

    async def update(self, tc: ToolCall) -> None:
        await self._session.execute(
            update(ToolCallModel)
            .where(ToolCallModel.id == tc.id)
            .values(
                result_json=tc.result,
                result_summary=tc.result_summary,
                status=tc.status,
                error_json=tc.error,
                completed_at=tc.completed_at,
                duration_ms=tc.duration_ms,
                permission_status=tc.permission_status,
                permission_request_id=tc.permission_request_id,
            )
        )

    async def list_by_run(self, run_id: UUID) -> list[ToolCall]:
        result = await self._session.execute(
            select(ToolCallModel)
            .where(ToolCallModel.run_id == run_id)
            .order_by(ToolCallModel.started_at.asc())
        )
        return [self._to_domain(m) for m in result.scalars().all()]

    @staticmethod
    def _to_domain(m: ToolCallModel) -> ToolCall:
        return ToolCall(
            id=m.id,
            task_id=m.task_id,
            run_id=m.run_id,
            step_id=m.step_id,
            provider=m.provider,
            tool_name=m.tool_name,
            mcp_server_id=m.mcp_server_id,
            risk_level=m.risk_level,
            arguments=m.arguments_json,
            arguments_summary=m.arguments_summary_json,
            result=m.result_json,
            result_summary=m.result_summary,
            permission_request_id=m.permission_request_id,
            permission_status=m.permission_status,
            status=m.status,
            error=m.error_json,
            started_at=m.started_at,
            completed_at=m.completed_at,
            duration_ms=m.duration_ms,
        )
