"""PostgreSQL PermissionRepository。"""

from typing import Optional
from uuid import UUID

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from jarvis_worker.database.models import PermissionGrantModel, PermissionRequestModel
from jarvis_worker.database.repositories.interfaces import PermissionRepository
from jarvis_worker.runtime.permissions.policy import permission_request_deadline
from jarvis_worker.shared.domain.models import PermissionGrant, PermissionRequest, PermissionStatus


class PostgresPermissionRepository(PermissionRepository):
    """PostgreSQL Permission 持久化 adapter。"""

    def __init__(self, session: AsyncSession):
        self._session = session

    async def create_request(self, req: PermissionRequest) -> PermissionRequest:
        if req.expires_at is None:
            req.expires_at = permission_request_deadline(req.created_at)
        model = PermissionRequestModel(
            id=req.id,
            task_id=req.task_id,
            run_id=req.run_id,
            step_id=req.step_id,
            tool_call_id=req.tool_call_id,
            tool_name=req.tool_name,
            action_summary=req.action_summary,
            reason=req.reason,
            risk_level=req.risk_level,
            scope_json=req.scope,
            arguments_summary_json=req.arguments_summary,
            allowed_decisions_json=req.allowed_decisions,
            checkpoint_json=req.checkpoint,
            status=req.status.value if isinstance(req.status, PermissionStatus) else req.status,
            created_at=req.created_at,
            expires_at=req.expires_at,
        )
        self._session.add(model)
        return req

    async def update_request(self, req: PermissionRequest) -> None:
        await self._session.execute(
            update(PermissionRequestModel)
            .where(PermissionRequestModel.id == req.id)
            .values(
                status=req.status.value if isinstance(req.status, PermissionStatus) else req.status,
                decision=req.decision,
                decided_at=req.decided_at,
                note=req.note,
                checkpoint_json=req.checkpoint,
            )
        )

    async def get_request(self, request_id: UUID) -> Optional[PermissionRequest]:
        result = await self._session.execute(
            select(PermissionRequestModel).where(PermissionRequestModel.id == request_id)
        )
        model = result.scalar_one_or_none()
        if model is None:
            return None
        return self._req_to_domain(model)

    async def get_request_for_update(self, request_id: UUID) -> Optional[PermissionRequest]:
        result = await self._session.execute(
            select(PermissionRequestModel)
            .where(PermissionRequestModel.id == request_id)
            .with_for_update()
        )
        model = result.scalar_one_or_none()
        if model is None:
            return None
        return self._req_to_domain(model)

    async def list_pending_by_run(self, run_id: UUID) -> list[PermissionRequest]:
        result = await self._session.execute(
            select(PermissionRequestModel)
            .where(PermissionRequestModel.run_id == run_id)
            .where(PermissionRequestModel.status == "pending")
        )
        return [self._req_to_domain(m) for m in result.scalars().all()]

    async def list_expired_pending_for_update(
        self, now, limit: int = 32
    ) -> list[PermissionRequest]:
        result = await self._session.execute(
            select(PermissionRequestModel)
            .where(PermissionRequestModel.status == "pending")
            .where(PermissionRequestModel.expires_at <= now)
            .order_by(PermissionRequestModel.expires_at.asc())
            .limit(limit)
            .with_for_update(skip_locked=True)
        )
        return [self._req_to_domain(m) for m in result.scalars().all()]

    async def create_grant(self, grant: PermissionGrant) -> PermissionGrant:
        model = PermissionGrantModel(
            id=grant.id,
            grant_type=grant.grant_type,
            tool_name=grant.tool_name,
            mcp_server_id=grant.mcp_server_id,
            workspace_path=grant.workspace_path,
            path=grant.path,
            risk_level_max=grant.risk_level_max,
            created_from_request_id=grant.created_from_request_id,
            created_at=grant.created_at,
            expires_at=grant.expires_at,
            revoked_at=grant.revoked_at,
            metadata_json=grant.metadata,
        )
        self._session.add(model)
        return grant

    @staticmethod
    def _req_to_domain(m: PermissionRequestModel) -> PermissionRequest:
        return PermissionRequest(
            id=m.id,
            task_id=m.task_id,
            run_id=m.run_id,
            step_id=m.step_id,
            tool_call_id=m.tool_call_id,
            tool_name=m.tool_name,
            action_summary=m.action_summary,
            reason=m.reason,
            risk_level=m.risk_level,
            scope=m.scope_json,
            arguments_summary=m.arguments_summary_json,
            allowed_decisions=m.allowed_decisions_json,
            checkpoint=m.checkpoint_json or {},
            status=PermissionStatus(m.status),
            decision=m.decision,
            decided_at=m.decided_at,
            note=m.note,
            created_at=m.created_at,
            expires_at=m.expires_at,
        )
