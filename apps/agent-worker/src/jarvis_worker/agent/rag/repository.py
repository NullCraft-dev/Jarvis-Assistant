"""RAG 文档、入库作业和分块的持久化端口。"""

from abc import ABC, abstractmethod
from datetime import datetime
from uuid import UUID

from jarvis_worker.agent.rag.contracts import (
    RagAsset,
    RagChunk,
    RagChunkElementLink,
    RagDocument,
    RagElement,
    RagIngestionJob,
)


class RagDocumentRepository(ABC):
    @abstractmethod
    async def create(self, document: RagDocument) -> RagDocument: ...

    @abstractmethod
    async def get(self, document_id: UUID) -> RagDocument | None: ...

    @abstractmethod
    async def get_by_source(
        self, *, workspace_id: UUID, source_artifact_id: UUID, source_content_hash: str
    ) -> RagDocument | None: ...

    @abstractmethod
    async def list_by_workspace(
        self, *, workspace_id: UUID, include_disabled: bool = False, limit: int = 100
    ) -> list[RagDocument]: ...

    @abstractmethod
    async def update(self, document: RagDocument) -> None: ...

    @abstractmethod
    async def delete(self, *, workspace_id: UUID, document_id: UUID) -> bool: ...


class RagIngestionJobRepository(ABC):
    @abstractmethod
    async def create(self, job: RagIngestionJob) -> RagIngestionJob: ...

    @abstractmethod
    async def get(self, job_id: UUID) -> RagIngestionJob | None: ...

    @abstractmethod
    async def get_by_idempotency_key(self, idempotency_key: str) -> RagIngestionJob | None: ...

    @abstractmethod
    async def list_latest_by_documents(
        self, *, workspace_id: UUID, document_ids: list[UUID]
    ) -> list[RagIngestionJob]: ...

    @abstractmethod
    async def claim_next(
        self,
        *,
        worker_id: str,
        now: datetime,
        lease_until: datetime,
    ) -> RagIngestionJob | None: ...

    @abstractmethod
    async def claim_embedding(
        self,
        *,
        worker_id: str,
        now: datetime,
        lease_until: datetime,
    ) -> RagIngestionJob | None: ...

    @abstractmethod
    async def update(self, job: RagIngestionJob) -> None: ...


class RagChunkRepository(ABC):
    @abstractmethod
    async def create_many(self, chunks: list[RagChunk]) -> list[RagChunk]: ...

    @abstractmethod
    async def list_by_document(
        self, *, workspace_id: UUID, document_id: UUID, limit: int = 1000
    ) -> list[RagChunk]: ...

    @abstractmethod
    async def list_identity_chunks(
        self, *, workspace_id: UUID, document_ids: list[UUID]
    ) -> list[RagChunk]: ...

    @abstractmethod
    async def list_by_ids(
        self, *, workspace_id: UUID, chunk_ids: list[UUID]
    ) -> list[RagChunk]: ...

    @abstractmethod
    async def delete_by_job(self, ingestion_job_id: UUID) -> None: ...

    @abstractmethod
    async def delete_by_document(
        self, *, workspace_id: UUID, document_id: UUID
    ) -> None: ...

    @abstractmethod
    async def mark_embedded(
        self,
        *,
        workspace_id: UUID,
        document_id: UUID,
        embedding_keys: dict[UUID, str],
    ) -> None: ...


class RagElementRepository(ABC):
    @abstractmethod
    async def create_many(self, elements: list[RagElement]) -> list[RagElement]: ...

    @abstractmethod
    async def get(self, element_id: UUID) -> RagElement | None: ...

    @abstractmethod
    async def list_by_document(
        self, *, workspace_id: UUID, document_id: UUID, limit: int = 1000
    ) -> list[RagElement]: ...

    @abstractmethod
    async def delete_by_document(
        self, *, workspace_id: UUID, document_id: UUID
    ) -> None: ...


class RagAssetRepository(ABC):
    @abstractmethod
    async def create(self, asset: RagAsset) -> RagAsset: ...

    @abstractmethod
    async def get(self, asset_id: UUID) -> RagAsset | None: ...

    @abstractmethod
    async def list_by_element(
        self, *, workspace_id: UUID, element_id: UUID
    ) -> list[RagAsset]: ...

    @abstractmethod
    async def list_by_document(
        self, *, workspace_id: UUID, document_id: UUID, limit: int = 1000
    ) -> list[RagAsset]: ...


class RagChunkElementLinkRepository(ABC):
    @abstractmethod
    async def create_many(
        self, links: list[RagChunkElementLink]
    ) -> list[RagChunkElementLink]: ...

    @abstractmethod
    async def list_by_chunk(
        self, *, workspace_id: UUID, chunk_id: UUID
    ) -> list[RagChunkElementLink]: ...

    @abstractmethod
    async def list_by_element(
        self, *, workspace_id: UUID, element_id: UUID
    ) -> list[RagChunkElementLink]: ...
