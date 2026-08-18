"""MemoryCandidate 与异步提取作业的持久化端口。"""

from abc import ABC, abstractmethod
from datetime import datetime
from uuid import UUID

from jarvis_worker.shared.domain.models import MemoryCandidate, MemoryExtractionJob


class MemoryCandidateRepository(ABC):
    @abstractmethod
    async def create(self, candidate: MemoryCandidate) -> MemoryCandidate: ...

    @abstractmethod
    async def get_for_update(self, candidate_id: UUID) -> MemoryCandidate | None: ...

    @abstractmethod
    async def get_pending_by_deduplication_key(
        self, deduplication_key: str
    ) -> MemoryCandidate | None: ...

    @abstractmethod
    async def list_filtered(
        self,
        *,
        status: str | None = None,
        workspace_id: UUID | None = None,
        limit: int = 100,
    ) -> list[MemoryCandidate]: ...

    @abstractmethod
    async def update(self, candidate: MemoryCandidate) -> None: ...

    @abstractmethod
    async def list_due_for_update(
        self, *, now: datetime, limit: int = 100
    ) -> list[MemoryCandidate]: ...


class MemoryExtractionJobRepository(ABC):
    @abstractmethod
    async def create(self, job: MemoryExtractionJob) -> MemoryExtractionJob: ...

    @abstractmethod
    async def get_by_run_policy(
        self, source_run_id: UUID, extraction_policy_version: str
    ) -> MemoryExtractionJob | None: ...

    @abstractmethod
    async def claim_next(
        self, *, now: datetime, stale_before: datetime
    ) -> MemoryExtractionJob | None: ...

    @abstractmethod
    async def mark_completed(self, job_id: UUID, *, now: datetime) -> None: ...

    @abstractmethod
    async def mark_failed(
        self,
        job_id: UUID,
        *,
        error_code: str,
        next_retry_at: datetime | None,
        now: datetime,
    ) -> None: ...
