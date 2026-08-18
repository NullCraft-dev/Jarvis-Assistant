"""RAG 检索读取模型端口。"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Literal, Sequence
from uuid import UUID

RagRetrievalSource = Literal["semantic", "keyword"]


@dataclass(frozen=True, slots=True)
class RagCandidateTrace:
    """仅在检索流水线内部使用的候选来源与排名解释。"""

    sources: tuple[RagRetrievalSource, ...] = ()
    semantic_rank: int | None = None
    semantic_score: float | None = None
    keyword_rank: int | None = None
    keyword_score: float | None = None
    rrf_score: float | None = None
    feature_score: float | None = None
    cross_encoder_score: float | None = None
    fused_score: float | None = None
    mmr_score: float | None = None


@dataclass(frozen=True, slots=True)
class RagCandidateRecord:
    chunk_id: UUID
    document_id: UUID
    workspace_id: UUID
    source_artifact_id: UUID
    document_title: str
    ordinal: int
    content: str
    content_hash: str
    token_count: int
    source_locator: dict
    score: float
    trace: RagCandidateTrace = field(default_factory=RagCandidateTrace)


@dataclass(frozen=True, slots=True)
class RagNeighborRecord:
    chunk_id: UUID
    document_id: UUID
    ordinal: int
    content: str
    token_count: int
    source_locator: dict


@dataclass(frozen=True, slots=True)
class RagElementEvidenceRecord:
    chunk_id: UUID
    element_id: UUID
    element_type: str
    page_number: int
    caption_text: str
    ocr_text: str
    structured_data: dict
    derived_description: str
    confidence: float
    asset_ids: tuple[UUID, ...] = field(default_factory=tuple)


class RagRetrievalRepository(ABC):
    @abstractmethod
    async def search_candidates(
        self,
        *,
        workspace_id: UUID,
        query_vector: Sequence[float],
        provider_name: str,
        model_name: str,
        document_ids: tuple[UUID, ...],
        limit: int,
    ) -> list[RagCandidateRecord]: ...

    @abstractmethod
    async def search_keyword_candidates(
        self,
        *,
        workspace_id: UUID,
        query_terms: tuple[str, ...],
        document_ids: tuple[UUID, ...],
        limit: int,
    ) -> list[RagCandidateRecord]: ...

    @abstractmethod
    async def load_neighbors(
        self,
        *,
        workspace_id: UUID,
        centers: tuple[tuple[UUID, int], ...],
        radius: int,
    ) -> list[RagNeighborRecord]: ...

    @abstractmethod
    async def load_elements(
        self, *, workspace_id: UUID, chunk_ids: tuple[UUID, ...]
    ) -> list[RagElementEvidenceRecord]: ...
