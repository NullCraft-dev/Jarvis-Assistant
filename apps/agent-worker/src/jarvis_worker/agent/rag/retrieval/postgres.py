"""PostgreSQL/pgvector RAG 在线检索读取适配器。"""

from __future__ import annotations

import math
from collections import defaultdict
from typing import Sequence
from uuid import UUID

from sqlalchemy import Float, and_, case, cast, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from jarvis_worker.agent.rag.retrieval.config import HnswSearchConfig
from jarvis_worker.agent.rag.retrieval.repository import (
    RagCandidateRecord,
    RagCandidateTrace,
    RagElementEvidenceRecord,
    RagNeighborRecord,
    RagRetrievalRepository,
)
from jarvis_worker.database.models import (
    RagAssetModel,
    RagChunkElementLinkModel,
    RagChunkEmbeddingModel,
    RagChunkModel,
    RagDocumentModel,
    RagElementModel,
)

_VECTOR_DIMENSIONS = 1_536


class PostgresRagRetrievalRepository(RagRetrievalRepository):
    def __init__(
        self,
        session: AsyncSession,
        *,
        hnsw_config: HnswSearchConfig | None = None,
    ) -> None:
        self._session = session
        self._hnsw_config = hnsw_config or HnswSearchConfig.from_env()

    async def search_candidates(
        self,
        *,
        workspace_id: UUID,
        query_vector: Sequence[float],
        provider_name: str,
        model_name: str,
        document_ids: tuple[UUID, ...],
        limit: int,
    ) -> list[RagCandidateRecord]:
        vector = _validate_vector(query_vector)
        await self._apply_hnsw_search_config()
        distance = RagChunkEmbeddingModel.embedding.cosine_distance(vector)
        statement = (
            select(RagChunkModel, RagDocumentModel, distance.label("distance"))
            .join(
                RagChunkModel,
                and_(
                    RagChunkModel.id == RagChunkEmbeddingModel.chunk_id,
                    RagChunkModel.document_id == RagChunkEmbeddingModel.document_id,
                    RagChunkModel.workspace_id == RagChunkEmbeddingModel.workspace_id,
                ),
            )
            .join(
                RagDocumentModel,
                and_(
                    RagDocumentModel.id == RagChunkModel.document_id,
                    RagDocumentModel.workspace_id == RagChunkModel.workspace_id,
                ),
            )
            .where(
                RagChunkEmbeddingModel.workspace_id == workspace_id,
                RagChunkEmbeddingModel.provider_name == provider_name,
                RagChunkEmbeddingModel.model_name == model_name,
                RagDocumentModel.status == "ready",
                RagChunkModel.embedding_key.is_not(None),
            )
            # HNSW 只能直接提供距离顺序；稳定 tie-break 在有限候选返回后由
            # PgVectorCandidateRetriever 统一完成，避免二级排序阻断 KNN Index Scan。
            .order_by(distance.asc())
            .limit(min(max(limit, 1), 100))
        )
        if document_ids:
            statement = statement.where(RagDocumentModel.id.in_(document_ids))
        result = await self._session.execute(statement)
        return [
            RagCandidateRecord(
                chunk_id=chunk.id,
                document_id=chunk.document_id,
                workspace_id=chunk.workspace_id,
                source_artifact_id=document.source_artifact_id,
                document_title=document.title,
                ordinal=chunk.ordinal,
                content=chunk.content,
                content_hash=chunk.content_hash,
                token_count=chunk.token_count,
                source_locator=dict(chunk.source_locator_json or {}),
                score=max(0.0, min(1.0, 1.0 - float(distance_value))),
                trace=RagCandidateTrace(sources=("semantic",)),
            )
            for chunk, document, distance_value in result.all()
        ]

    async def _apply_hnsw_search_config(self) -> None:
        settings = self._hnsw_config.transaction_settings()
        await self._session.execute(
            select(
                *(
                    func.set_config(name, value, True).label(f"hnsw_setting_{index}")
                    for index, (name, value) in enumerate(settings)
                )
            )
        )

    async def search_keyword_candidates(
        self,
        *,
        workspace_id: UUID,
        query_terms: tuple[str, ...],
        document_ids: tuple[UUID, ...],
        limit: int,
    ) -> list[RagCandidateRecord]:
        terms = _validate_query_terms(query_terms)
        if not terms:
            return []
        normalized_content = func.lower(RagChunkModel.content)
        normalized_title = func.lower(RagDocumentModel.title)
        matches = []
        weighted_scores = []
        maximum_score = 0.0
        for term in terms:
            content_match = func.strpos(normalized_content, term) > 0
            title_match = func.strpos(normalized_title, term) > 0
            weight = min(len(term), 12) / 12
            matches.extend((content_match, title_match))
            weighted_scores.append(
                case((content_match, weight), else_=0.0)
                + case((title_match, weight * 0.35), else_=0.0)
            )
            maximum_score += weight * 1.35
        coverage_score = cast(sum(weighted_scores), Float) / maximum_score
        strongest_score = func.greatest(*weighted_scores) / 1.35
        score = func.least(1.0, coverage_score * 0.3 + strongest_score * 0.7)
        statement = (
            select(RagChunkModel, RagDocumentModel, score.label("keyword_score"))
            .join(
                RagDocumentModel,
                and_(
                    RagDocumentModel.id == RagChunkModel.document_id,
                    RagDocumentModel.workspace_id == RagChunkModel.workspace_id,
                ),
            )
            .where(
                RagChunkModel.workspace_id == workspace_id,
                RagDocumentModel.status == "ready",
                or_(*matches),
            )
            .order_by(score.desc(), RagChunkModel.id.asc())
            .limit(min(max(limit, 1), 100))
        )
        if document_ids:
            statement = statement.where(RagDocumentModel.id.in_(document_ids))
        result = await self._session.execute(statement)
        return [
            RagCandidateRecord(
                chunk_id=chunk.id,
                document_id=chunk.document_id,
                workspace_id=chunk.workspace_id,
                source_artifact_id=document.source_artifact_id,
                document_title=document.title,
                ordinal=chunk.ordinal,
                content=chunk.content,
                content_hash=chunk.content_hash,
                token_count=chunk.token_count,
                source_locator=dict(chunk.source_locator_json or {}),
                score=max(0.0, min(1.0, float(keyword_score))),
                trace=RagCandidateTrace(sources=("keyword",)),
            )
            for chunk, document, keyword_score in result.all()
        ]

    async def load_neighbors(
        self,
        *,
        workspace_id: UUID,
        centers: tuple[tuple[UUID, int], ...],
        radius: int,
    ) -> list[RagNeighborRecord]:
        if not centers or radius < 1:
            return []
        windows = [
            and_(
                RagChunkModel.document_id == document_id,
                RagChunkModel.ordinal.between(ordinal - radius, ordinal + radius),
            )
            for document_id, ordinal in centers
        ]
        result = await self._session.execute(
            select(RagChunkModel)
            .join(
                RagDocumentModel,
                and_(
                    RagDocumentModel.id == RagChunkModel.document_id,
                    RagDocumentModel.workspace_id == RagChunkModel.workspace_id,
                ),
            )
            .where(
                RagChunkModel.workspace_id == workspace_id,
                RagDocumentModel.status == "ready",
                or_(*windows),
            )
            .order_by(RagChunkModel.document_id.asc(), RagChunkModel.ordinal.asc())
        )
        return [
            RagNeighborRecord(
                chunk_id=chunk.id,
                document_id=chunk.document_id,
                ordinal=chunk.ordinal,
                content=chunk.content,
                token_count=chunk.token_count,
                source_locator=dict(chunk.source_locator_json or {}),
            )
            for chunk in result.scalars().all()
        ]

    async def load_elements(
        self, *, workspace_id: UUID, chunk_ids: tuple[UUID, ...]
    ) -> list[RagElementEvidenceRecord]:
        if not chunk_ids:
            return []
        result = await self._session.execute(
            select(RagChunkElementLinkModel, RagElementModel)
            .join(
                RagElementModel,
                and_(
                    RagElementModel.id == RagChunkElementLinkModel.element_id,
                    RagElementModel.document_id == RagChunkElementLinkModel.document_id,
                    RagElementModel.workspace_id == RagChunkElementLinkModel.workspace_id,
                ),
            )
            .where(
                RagChunkElementLinkModel.workspace_id == workspace_id,
                RagChunkElementLinkModel.chunk_id.in_(chunk_ids),
            )
            .order_by(
                RagChunkElementLinkModel.chunk_id.asc(),
                RagChunkElementLinkModel.order_index.asc(),
            )
            .limit(min(len(chunk_ids) * 20, 400))
        )
        rows = result.all()
        element_ids = tuple({element.id for _, element in rows})
        asset_ids_by_element: dict[UUID, list[UUID]] = defaultdict(list)
        if element_ids:
            assets = await self._session.execute(
                select(RagAssetModel.element_id, RagAssetModel.id)
                .where(
                    RagAssetModel.workspace_id == workspace_id,
                    RagAssetModel.element_id.in_(element_ids),
                )
                .order_by(RagAssetModel.element_id.asc(), RagAssetModel.id.asc())
            )
            for element_id, asset_id in assets.all():
                asset_ids_by_element[element_id].append(asset_id)
        return [
            RagElementEvidenceRecord(
                chunk_id=link.chunk_id,
                element_id=element.id,
                element_type=element.element_type,
                page_number=element.page_number,
                caption_text=element.caption_text,
                ocr_text=element.ocr_text,
                structured_data=dict(element.structured_data_json or {}),
                derived_description=element.derived_description,
                confidence=element.confidence,
                asset_ids=tuple(asset_ids_by_element[element.id]),
            )
            for link, element in rows
        ]


def _validate_vector(vector: Sequence[float]) -> list[float]:
    if len(vector) != _VECTOR_DIMENSIONS:
        raise ValueError(f"Embedding 维度必须为 {_VECTOR_DIMENSIONS}")
    normalized = [float(value) for value in vector]
    if not all(math.isfinite(value) for value in normalized):
        raise ValueError("Embedding 包含非有限数值")
    return normalized


def _validate_query_terms(query_terms: tuple[str, ...]) -> tuple[str, ...]:
    if len(query_terms) > 16:
        raise ValueError("关键词词项不能超过 16 个")
    normalized = tuple(term.strip().casefold() for term in query_terms if term.strip())
    if any(len(term) > 64 for term in normalized):
        raise ValueError("单个关键词长度不能超过 64")
    return tuple(dict.fromkeys(normalized))
