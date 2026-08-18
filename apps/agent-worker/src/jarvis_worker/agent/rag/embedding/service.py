"""RAG Embedding 阶段：OpenAI 向量化与 pgvector 原子落库。"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from typing import Callable
from uuid import UUID, uuid4

from jarvis_worker.agent.rag.contracts import (
    EmbeddingProvider,
    RagIngestionStatus,
    RagVectorRecord,
)
from jarvis_worker.agent.rag.embedding.openai import OpenAIEmbeddingError
from jarvis_worker.database.unit_of_work import PostgresUnitOfWork
from jarvis_worker.shared.domain.models import AuditLog, utcnow


@dataclass(frozen=True, slots=True)
class RagEmbeddingProcessResult:
    document_id: UUID
    job_id: UUID
    status: RagIngestionStatus
    chunk_count: int
    error_code: str | None = None


class RagEmbeddingService:
    def __init__(
        self,
        uow_factory,
        *,
        provider: EmbeddingProvider,
        now: Callable[[], datetime] = utcnow,
        lease_duration: timedelta = timedelta(minutes=5),
        retry_base_delay: timedelta = timedelta(seconds=30),
    ) -> None:
        if lease_duration <= timedelta(0) or retry_base_delay <= timedelta(0):
            raise ValueError("RAG embedding lease/retry delay 必须大于 0")
        self._uow_factory = uow_factory
        self._provider = provider
        self._now = now
        self._lease_duration = lease_duration
        self._retry_base_delay = retry_base_delay

    async def process_next(self, *, worker_id: str) -> RagEmbeddingProcessResult | None:
        if not worker_id.strip():
            raise ValueError("RAG embedding worker_id 不能为空")
        now = self._now()
        session_factory = self._uow_factory()
        async with session_factory() as session:
            async with PostgresUnitOfWork(session).transaction() as tx:
                job = await tx.rag_ingestion_jobs.claim_embedding(
                    worker_id=worker_id,
                    now=now,
                    lease_until=now + self._lease_duration,
                )
                if job is None:
                    return None
                document = await tx.rag_documents.get(job.document_id)
                if job.status is RagIngestionStatus.FAILED:
                    if document is not None:
                        document.mark_failed(now=now)
                        await tx.rag_documents.update(document)
                    await tx.audits.create(
                        _audit(
                            event_type="rag.embedding.failed",
                            action_summary="RAG Embedding 已耗尽重试预算",
                            details={
                                "document_id": str(job.document_id),
                                "job_id": str(job.id),
                                "error_code": job.error_code,
                                "retry_scheduled": False,
                            },
                            now=now,
                        )
                    )
                    await tx.commit()
                    return RagEmbeddingProcessResult(
                        document_id=job.document_id,
                        job_id=job.id,
                        status=job.status,
                        chunk_count=0,
                        error_code=job.error_code,
                    )
                chunks = await tx.rag_chunks.list_by_document(
                    workspace_id=job.workspace_id,
                    document_id=job.document_id,
                )
                if document is None or document.workspace_id != job.workspace_id or not chunks:
                    raise ValueError("RAG embedding 文档或 chunks 不存在")
                job.report_progress(
                    progress=replace(
                        job.progress,
                        active_executor="openai-embedding",
                        embedding_total=len(chunks),
                        embedding_completed=0,
                    ),
                    worker_id=worker_id,
                    now=now,
                )
                await tx.rag_ingestion_jobs.update(job)
                await tx.commit()

        try:
            vectors = await self._provider.embed_documents([chunk.content for chunk in chunks])
            if len(vectors) != len(chunks):
                raise OpenAIEmbeddingError(
                    "OPENAI_EMBEDDING_INVALID_RESPONSE",
                    "Embedding 数量与 chunks 不匹配",
                    recoverable=True,
                )
            return await self._complete(
                worker_id=worker_id,
                job_id=job.id,
                chunks=chunks,
                vectors=vectors,
            )
        except asyncio.CancelledError:
            raise
        except OpenAIEmbeddingError as exc:
            return await self._fail(
                worker_id=worker_id,
                job_id=job.id,
                error_code=exc.code,
                recoverable=exc.recoverable,
                chunk_count=len(chunks),
            )
        except Exception:
            return await self._fail(
                worker_id=worker_id,
                job_id=job.id,
                error_code="RAG_EMBEDDING_INTERNAL_ERROR",
                recoverable=True,
                chunk_count=len(chunks),
            )

    async def _complete(self, *, worker_id, job_id, chunks, vectors):
        now = self._now()
        session_factory = self._uow_factory()
        async with session_factory() as session:
            async with PostgresUnitOfWork(session).transaction() as tx:
                job = await tx.rag_ingestion_jobs.get(job_id)
                if (
                    job is None
                    or job.status is not RagIngestionStatus.EMBEDDING
                    or job.claimed_by != worker_id
                    or job.lease_until is None
                    or job.lease_until <= now
                ):
                    raise RuntimeError("RAG embedding lease 已丢失")
                document = await tx.rag_documents.get(job.document_id)
                if document is None or document.workspace_id != job.workspace_id:
                    raise RuntimeError("RAG embedding document scope 不匹配")
                records = [
                    RagVectorRecord(
                        chunk_id=chunk.id,
                        document_id=chunk.document_id,
                        workspace_id=chunk.workspace_id,
                        embedding=vector,
                        content_hash=chunk.content_hash,
                        provider_name=self._provider.provider_name,
                        model_name=self._provider.model_name,
                    )
                    for chunk, vector in zip(chunks, vectors, strict=True)
                ]
                await tx.rag_vector_index.upsert(records)
                embedding_keys = {
                    chunk.id: (
                        f"{self._provider.provider_name}:{self._provider.model_name}:"
                        f"{chunk.content_hash}"
                    )
                    for chunk in chunks
                }
                await tx.rag_chunks.mark_embedded(
                    workspace_id=job.workspace_id,
                    document_id=job.document_id,
                    embedding_keys=embedding_keys,
                )
                document.mark_ready(
                    parser_version=document.parser_version,
                    chunker_version=document.chunker_version,
                    embedding_provider=self._provider.provider_name,
                    embedding_model=self._provider.model_name,
                    embedding_dimensions=self._provider.dimensions,
                    chunk_count=len(chunks),
                    now=now,
                )
                job.progress = replace(
                    job.progress,
                    active_executor=None,
                    embedding_total=len(chunks),
                    embedding_completed=len(chunks),
                )
                job.advance(RagIngestionStatus.COMPLETED, now=now)
                await tx.rag_documents.update(document)
                await tx.rag_ingestion_jobs.update(job)
                await tx.audits.create(
                    _audit(
                        event_type="rag.embedding.completed",
                        action_summary="RAG chunks 已完成向量化并进入可检索状态",
                        details={
                            "document_id": str(job.document_id),
                            "job_id": str(job.id),
                            "chunk_count": len(chunks),
                            "provider": self._provider.provider_name,
                            "model": self._provider.model_name,
                            "dimensions": self._provider.dimensions,
                        },
                        now=now,
                    )
                )
                await tx.commit()
        return RagEmbeddingProcessResult(
            document_id=job.document_id,
            job_id=job.id,
            status=job.status,
            chunk_count=len(chunks),
        )

    async def _fail(
        self, *, worker_id, job_id, error_code, recoverable, chunk_count
    ) -> RagEmbeddingProcessResult:
        now = self._now()
        session_factory = self._uow_factory()
        async with session_factory() as session:
            async with PostgresUnitOfWork(session).transaction() as tx:
                job = await tx.rag_ingestion_jobs.get(job_id)
                if job is None:
                    raise RuntimeError("RAG embedding 失败状态无法安全回填")
                if (
                    job.status is not RagIngestionStatus.EMBEDDING
                    or job.claimed_by != worker_id
                ):
                    return RagEmbeddingProcessResult(
                        document_id=job.document_id,
                        job_id=job.id,
                        status=job.status,
                        chunk_count=chunk_count,
                        error_code="RAG_JOB_LEASE_LOST",
                    )
                can_retry = recoverable and job.embedding_attempts < job.embedding_max_attempts
                next_retry_at = (
                    now + self._retry_base_delay * (2 ** max(job.embedding_attempts - 1, 0))
                    if can_retry
                    else None
                )
                job.fail_embedding(
                    worker_id=worker_id,
                    error_code=error_code,
                    recoverable=can_retry,
                    next_retry_at=next_retry_at,
                    now=now,
                )
                await tx.rag_ingestion_jobs.update(job)
                if job.status is RagIngestionStatus.FAILED:
                    document = await tx.rag_documents.get(job.document_id)
                    if document is not None:
                        document.mark_failed(now=now)
                        await tx.rag_documents.update(document)
                await tx.audits.create(
                    _audit(
                        event_type="rag.embedding.failed",
                        action_summary="RAG Embedding 阶段执行失败",
                        details={
                            "document_id": str(job.document_id),
                            "job_id": str(job.id),
                            "error_code": error_code,
                            "retry_scheduled": job.next_retry_at is not None,
                        },
                        now=now,
                    )
                )
                await tx.commit()
        return RagEmbeddingProcessResult(
            document_id=job.document_id,
            job_id=job.id,
            status=job.status,
            chunk_count=chunk_count,
            error_code=error_code,
        )


def _audit(*, event_type: str, action_summary: str, details: dict, now: datetime) -> AuditLog:
    return AuditLog(
        id=uuid4(),
        event_type=event_type,
        actor="rag-embedding-service",
        risk_level="L1",
        permission_decision="not_required",
        action_summary=action_summary,
        details=details,
        created_at=now,
    )
