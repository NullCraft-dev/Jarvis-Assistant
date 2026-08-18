"""PostgreSQL + pgvector 的 Workspace 隔离向量索引适配器。"""

from __future__ import annotations

import math
from datetime import datetime, timezone
from typing import Sequence
from uuid import UUID

from sqlalchemy import delete, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from jarvis_worker.agent.rag.contracts import (
    RagSearchMatch,
    RagVectorRecord,
    VectorIndex,
)
from jarvis_worker.database.models import RagChunkEmbeddingModel

OPENAI_VECTOR_DIMENSIONS = 1536


class PostgresPgVectorIndex(VectorIndex):
    def __init__(self, session: AsyncSession, *, dimensions: int = OPENAI_VECTOR_DIMENSIONS):
        if dimensions != OPENAI_VECTOR_DIMENSIONS:
            raise ValueError("当前 RAG pgvector 索引固定为 1536 维")
        self._session = session
        self._dimensions = dimensions

    async def upsert(self, records: Sequence[RagVectorRecord]) -> None:
        if not records:
            return
        now = datetime.now(timezone.utc)
        values = []
        for record in records:
            vector = _validate_vector(record.embedding, self._dimensions)
            values.append(
                {
                    "chunk_id": record.chunk_id,
                    "document_id": record.document_id,
                    "workspace_id": record.workspace_id,
                    "content_hash": record.content_hash,
                    "provider_name": record.provider_name,
                    "model_name": record.model_name,
                    "dimensions": self._dimensions,
                    "embedding": vector,
                    "updated_at": now,
                }
            )
        statement = insert(RagChunkEmbeddingModel).values(values)
        statement = statement.on_conflict_do_update(
            index_elements=[RagChunkEmbeddingModel.chunk_id],
            set_={
                "document_id": statement.excluded.document_id,
                "workspace_id": statement.excluded.workspace_id,
                "content_hash": statement.excluded.content_hash,
                "provider_name": statement.excluded.provider_name,
                "model_name": statement.excluded.model_name,
                "dimensions": statement.excluded.dimensions,
                "embedding": statement.excluded.embedding,
                "updated_at": statement.excluded.updated_at,
            },
        )
        await self._session.execute(statement)
        await self._session.flush()

    async def search(
        self, *, workspace_id: UUID, query_vector: Sequence[float], limit: int
    ) -> list[RagSearchMatch]:
        vector = _validate_vector(query_vector, self._dimensions)
        bounded_limit = min(max(limit, 1), 100)
        distance = RagChunkEmbeddingModel.embedding.cosine_distance(vector)
        result = await self._session.execute(
            select(RagChunkEmbeddingModel, distance.label("distance"))
            .where(RagChunkEmbeddingModel.workspace_id == workspace_id)
            .order_by(distance.asc())
            .limit(bounded_limit)
        )
        return [
            RagSearchMatch(
                chunk_id=model.chunk_id,
                document_id=model.document_id,
                workspace_id=model.workspace_id,
                score=max(0.0, min(1.0, 1.0 - float(distance_value))),
            )
            for model, distance_value in result.all()
        ]

    async def delete_document(self, *, workspace_id: UUID, document_id: UUID) -> None:
        await self._session.execute(
            delete(RagChunkEmbeddingModel).where(
                RagChunkEmbeddingModel.workspace_id == workspace_id,
                RagChunkEmbeddingModel.document_id == document_id,
            )
        )
        await self._session.flush()


def _validate_vector(vector: Sequence[float], dimensions: int) -> list[float]:
    if len(vector) != dimensions:
        raise ValueError(f"Embedding 维度必须为 {dimensions}，当前为 {len(vector)}")
    normalized = [float(value) for value in vector]
    if not all(math.isfinite(value) for value in normalized):
        raise ValueError("Embedding 包含非有限数值")
    return normalized
