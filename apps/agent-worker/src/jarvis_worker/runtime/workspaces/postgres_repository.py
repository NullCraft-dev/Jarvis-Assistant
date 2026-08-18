"""WorkspaceRepository PostgreSQL 实现。"""

from typing import Optional
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from jarvis_worker.shared.domain.models import Workspace, WorkspaceStatus, WorkspaceSource
from jarvis_worker.database.models import WorkspaceModel


def _model_to_domain(m: WorkspaceModel) -> Workspace:
    return Workspace(
        id=m.id,
        name=m.name,
        root_path=m.root_path,
        canonical_path=m.canonical_path,
        status=WorkspaceStatus(m.status),
        source=WorkspaceSource(m.source),
        created_at=m.created_at,
        updated_at=m.updated_at,
        revoked_at=m.revoked_at,
        metadata=m.metadata_json,
    )


def _domain_to_model(w: Workspace) -> WorkspaceModel:
    return WorkspaceModel(
        id=w.id,
        name=w.name,
        root_path=w.root_path,
        canonical_path=w.canonical_path,
        status=w.status.value,
        source=w.source.value,
        created_at=w.created_at,
        updated_at=w.updated_at,
        revoked_at=w.revoked_at,
        metadata_json=w.metadata,
    )


class PostgresWorkspaceRepository:
    """WorkspaceRepository PostgreSQL adapter。"""

    def __init__(self, session: AsyncSession):
        self._session = session

    async def create(self, workspace: Workspace) -> Workspace:
        model = _domain_to_model(workspace)
        self._session.add(model)
        await self._session.flush()
        return _model_to_domain(model)

    async def insert_if_absent(self, workspace: Workspace) -> bool:
        values = {
            "id": workspace.id,
            "name": workspace.name,
            "root_path": workspace.root_path,
            "canonical_path": workspace.canonical_path,
            "status": workspace.status.value,
            "source": workspace.source.value,
            "created_at": workspace.created_at,
            "updated_at": workspace.updated_at,
            "revoked_at": workspace.revoked_at,
            "metadata_json": workspace.metadata,
        }
        stmt = (
            insert(WorkspaceModel)
            .values(**values)
            .on_conflict_do_nothing(index_elements=[WorkspaceModel.canonical_path])
            .returning(WorkspaceModel.id)
        )
        result = await self._session.execute(stmt)
        return result.scalar_one_or_none() is not None

    async def get(self, workspace_id: UUID) -> Optional[Workspace]:
        model = await self._session.get(WorkspaceModel, workspace_id)
        return _model_to_domain(model) if model else None

    async def get_for_update(self, workspace_id: UUID) -> Optional[Workspace]:
        stmt = select(WorkspaceModel).where(WorkspaceModel.id == workspace_id).with_for_update()
        result = await self._session.execute(stmt)
        model = result.scalar_one_or_none()
        return _model_to_domain(model) if model else None

    async def get_by_canonical_path(self, canonical_path: str) -> Optional[Workspace]:
        stmt = select(WorkspaceModel).where(WorkspaceModel.canonical_path == canonical_path)
        result = await self._session.execute(stmt)
        model = result.scalar_one_or_none()
        return _model_to_domain(model) if model else None

    async def get_by_canonical_path_for_update(self, canonical_path: str) -> Optional[Workspace]:
        stmt = (
            select(WorkspaceModel)
            .where(WorkspaceModel.canonical_path == canonical_path)
            .with_for_update()
        )
        result = await self._session.execute(stmt)
        model = result.scalar_one_or_none()
        return _model_to_domain(model) if model else None

    async def list_active(self) -> list[Workspace]:
        stmt = select(WorkspaceModel).where(WorkspaceModel.status == "active").order_by(WorkspaceModel.created_at)
        result = await self._session.execute(stmt)
        return [_model_to_domain(m) for m in result.scalars().all()]

    async def list_all(self) -> list[Workspace]:
        stmt = select(WorkspaceModel).order_by(WorkspaceModel.created_at)
        result = await self._session.execute(stmt)
        return [_model_to_domain(m) for m in result.scalars().all()]

    async def update(self, workspace: Workspace) -> None:
        model = await self._session.get(WorkspaceModel, workspace.id)
        if model is None:
            raise ValueError(f"Workspace 不存在: {workspace.id}")
        model.name = workspace.name
        model.root_path = workspace.root_path
        model.canonical_path = workspace.canonical_path
        model.status = workspace.status.value
        model.source = workspace.source.value
        model.updated_at = workspace.updated_at
        model.revoked_at = workspace.revoked_at
        model.metadata_json = workspace.metadata
        await self._session.flush()
