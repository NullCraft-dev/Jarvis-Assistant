"""长期记忆持久化端口。

Memory 业务只依赖该接口；PostgreSQL 是由 bootstrap 注入的实现细节。
"""

from abc import ABC, abstractmethod
from typing import Optional
from uuid import UUID

from jarvis_worker.shared.domain.models import Memory


class MemoryRepository(ABC):
    """长期记忆持久化。"""

    @abstractmethod
    async def create(self, memory: Memory) -> Memory: ...

    @abstractmethod
    async def get(self, memory_id: UUID) -> Optional[Memory]: ...

    @abstractmethod
    async def get_for_update(self, memory_id: UUID) -> Optional[Memory]: ...

    @abstractmethod
    async def get_by_identity(
        self,
        *,
        scope_type: str,
        workspace_id: UUID | None,
        category: str,
        key: str,
    ) -> Optional[Memory]: ...

    @abstractmethod
    async def list_filtered(
        self,
        *,
        scope_type: str | None = None,
        workspace_id: UUID | None = None,
        status: str | None = None,
        category: str | None = None,
        query: str | None = None,
        limit: int = 100,
    ) -> list[Memory]: ...

    @abstractmethod
    async def list_active_for_context(
        self,
        workspace_id: UUID | None,
        limit: int = 20,
    ) -> list[Memory]: ...

    @abstractmethod
    async def update(self, memory: Memory) -> None: ...

    @abstractmethod
    async def delete(self, memory_id: UUID) -> bool: ...
