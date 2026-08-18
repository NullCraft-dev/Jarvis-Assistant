"""PostgreSQL MemoryRepository。"""

from uuid import UUID

from sqlalchemy import and_, delete, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from jarvis_worker.shared.domain.models import (
    Memory, MemoryCategory, MemoryScopeType, MemorySourceType, MemoryStatus,
)
from jarvis_worker.database.models import MemoryModel
from jarvis_worker.agent.memory.repository import MemoryRepository


def _to_domain(model: MemoryModel) -> Memory:
    return Memory(
        id=model.id,
        scope_type=MemoryScopeType(model.scope_type),
        workspace_id=model.workspace_id,
        category=MemoryCategory(model.category),
        key=model.key,
        content=model.content,
        status=MemoryStatus(model.status),
        source_type=MemorySourceType(model.source_type),
        source_task_id=model.source_task_id,
        importance=model.importance,
        version=model.version,
        created_at=model.created_at,
        updated_at=model.updated_at,
    )


class PostgresMemoryRepository(MemoryRepository):
    def __init__(self, session: AsyncSession):
        self._session = session

    async def create(self, memory: Memory) -> Memory:
        model = MemoryModel(
            id=memory.id, scope_type=memory.scope_type.value,
            workspace_id=memory.workspace_id, category=memory.category.value,
            key=memory.key, content=memory.content, status=memory.status.value,
            source_type=memory.source_type.value, source_task_id=memory.source_task_id,
            importance=memory.importance, version=memory.version,
            created_at=memory.created_at, updated_at=memory.updated_at,
        )
        self._session.add(model)
        await self._session.flush()
        return _to_domain(model)

    async def get(self, memory_id: UUID) -> Memory | None:
        model = await self._session.get(MemoryModel, memory_id)
        return _to_domain(model) if model else None

    async def get_for_update(self, memory_id: UUID) -> Memory | None:
        result = await self._session.execute(
            select(MemoryModel).where(MemoryModel.id == memory_id).with_for_update()
        )
        model = result.scalar_one_or_none()
        return _to_domain(model) if model else None

    async def get_by_identity(
        self,
        *,
        scope_type: str,
        workspace_id: UUID | None,
        category: str,
        key: str,
    ) -> Memory | None:
        conditions = [
            MemoryModel.scope_type == scope_type,
            MemoryModel.category == category,
            MemoryModel.key == key,
        ]
        if workspace_id is None:
            conditions.append(MemoryModel.workspace_id.is_(None))
        else:
            conditions.append(MemoryModel.workspace_id == workspace_id)
        result = await self._session.execute(select(MemoryModel).where(*conditions))
        model = result.scalar_one_or_none()
        return _to_domain(model) if model else None

    async def list_filtered(
        self, *, scope_type: str | None = None, workspace_id: UUID | None = None,
        status: str | None = None, category: str | None = None,
        query: str | None = None, limit: int = 100,
    ) -> list[Memory]:
        conditions = []
        if scope_type:
            conditions.append(MemoryModel.scope_type == scope_type)
        if workspace_id:
            conditions.append(MemoryModel.workspace_id == workspace_id)
        if status:
            conditions.append(MemoryModel.status == status)
        if category:
            conditions.append(MemoryModel.category == category)
        if query:
            term = f"%{query}%"
            conditions.append(or_(MemoryModel.key.ilike(term), MemoryModel.content.ilike(term)))
        stmt = select(MemoryModel)
        if conditions:
            stmt = stmt.where(*conditions)
        stmt = stmt.order_by(MemoryModel.updated_at.desc(), MemoryModel.id.desc()).limit(limit)
        result = await self._session.execute(stmt)
        return [_to_domain(model) for model in result.scalars().all()]

    async def list_active_for_context(
        self, workspace_id: UUID | None, limit: int = 20
    ) -> list[Memory]:
        scope = MemoryModel.scope_type == "global"
        if workspace_id:
            scope = or_(
                scope,
                and_(MemoryModel.scope_type == "workspace", MemoryModel.workspace_id == workspace_id),
            )
        result = await self._session.execute(
            select(MemoryModel)
            .where(MemoryModel.status == "active", scope)
            .order_by(
                MemoryModel.importance.desc(),
                (MemoryModel.scope_type == "workspace").desc(),
                MemoryModel.updated_at.desc(),
            )
            .limit(limit)
        )
        return [_to_domain(model) for model in result.scalars().all()]

    async def update(self, memory: Memory) -> None:
        model = await self._session.get(MemoryModel, memory.id)
        if model is None:
            raise ValueError("Memory 不存在")
        model.content = memory.content
        model.status = memory.status.value
        model.importance = memory.importance
        model.version = memory.version
        model.updated_at = memory.updated_at
        await self._session.flush()

    async def delete(self, memory_id: UUID) -> bool:
        result = await self._session.execute(delete(MemoryModel).where(MemoryModel.id == memory_id))
        return bool(result.rowcount)
