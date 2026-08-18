"""PostgreSQL RAG ingestion repositories。"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from sqlalchemy import and_, delete, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from jarvis_worker.agent.rag.contracts import (
    RagAsset,
    RagAssetKind,
    RagChunk,
    RagChunkElementLink,
    RagChunkElementRelation,
    RagDocument,
    RagDocumentStatus,
    RagElement,
    RagElementType,
    RagExtractionMethod,
    RagIngestionJob,
    RagIngestionStatus,
    RagJobProgress,
)
from jarvis_worker.agent.rag.repository import (
    RagAssetRepository,
    RagChunkElementLinkRepository,
    RagChunkRepository,
    RagDocumentRepository,
    RagElementRepository,
    RagIngestionJobRepository,
)
from jarvis_worker.database.models import (
    RagAssetModel,
    RagChunkElementLinkModel,
    RagChunkModel,
    RagDocumentModel,
    RagElementModel,
    RagIngestionJobModel,
)


def _document_to_domain(model: RagDocumentModel) -> RagDocument:
    return RagDocument(
        id=model.id,
        workspace_id=model.workspace_id,
        source_artifact_id=model.source_artifact_id,
        title=model.title,
        mime_type=model.mime_type,
        source_content_hash=model.source_content_hash,
        ingestion_policy_version=model.ingestion_policy_version,
        status=RagDocumentStatus(model.status),
        parser_version=model.parser_version,
        chunker_version=model.chunker_version,
        embedding_provider=model.embedding_provider,
        embedding_model=model.embedding_model,
        embedding_dimensions=model.embedding_dimensions,
        chunk_count=model.chunk_count,
        indexed_at=model.indexed_at,
        disabled_at=model.disabled_at,
        version=model.version,
        created_at=model.created_at,
        updated_at=model.updated_at,
    )


def _job_to_domain(model: RagIngestionJobModel) -> RagIngestionJob:
    progress = model.progress_json or {}
    return RagIngestionJob(
        id=model.id,
        document_id=model.document_id,
        workspace_id=model.workspace_id,
        idempotency_key=model.idempotency_key,
        ingestion_policy_version=model.ingestion_policy_version,
        status=RagIngestionStatus(model.status),
        attempts=model.attempts,
        max_attempts=model.max_attempts,
        embedding_attempts=model.embedding_attempts,
        embedding_max_attempts=model.embedding_max_attempts,
        claimed_by=model.claimed_by,
        lease_until=model.lease_until,
        next_retry_at=model.next_retry_at,
        error_code=model.error_code,
        progress=RagJobProgress(
            active_executor=progress.get("active_executor"),
            page_count=int(progress.get("page_count", 0)),
            native_extraction_done=bool(progress.get("native_extraction_done", False)),
            visual_pages_total=int(progress.get("visual_pages_total", 0)),
            visual_pages_completed=int(progress.get("visual_pages_completed", 0)),
            visual_route_counts={
                str(key): int(value)
                for key, value in (progress.get("visual_route_counts") or {}).items()
            },
            chunks_total=int(progress.get("chunks_total", 0)),
            embedding_total=int(progress.get("embedding_total", 0)),
            embedding_completed=int(progress.get("embedding_completed", 0)),
        ),
        started_at=model.started_at,
        completed_at=model.completed_at,
        failed_at=model.failed_at,
        cancelled_at=model.cancelled_at,
        created_at=model.created_at,
        updated_at=model.updated_at,
    )


def _chunk_to_domain(model: RagChunkModel) -> RagChunk:
    return RagChunk(
        id=model.id,
        document_id=model.document_id,
        ingestion_job_id=model.ingestion_job_id,
        workspace_id=model.workspace_id,
        ordinal=model.ordinal,
        content=model.content,
        content_hash=model.content_hash,
        token_count=model.token_count,
        source_locator=model.source_locator_json,
        embedding_key=model.embedding_key,
        created_at=model.created_at,
    )


def _element_to_domain(model: RagElementModel) -> RagElement:
    return RagElement(
        id=model.id,
        document_id=model.document_id,
        workspace_id=model.workspace_id,
        element_type=RagElementType(model.element_type),
        page_number=model.page_number,
        bounding_box=tuple(model.bounding_box_json),
        page_width=model.page_width,
        page_height=model.page_height,
        locator_key=model.locator_key,
        content_hash=model.content_hash,
        extraction_method=RagExtractionMethod(model.extraction_method),
        extraction_version=model.extraction_version,
        confidence=model.confidence,
        caption_text=model.caption_text,
        ocr_text=model.ocr_text,
        structured_data=model.structured_data_json,
        derived_description=model.derived_description,
        created_at=model.created_at,
    )


def _asset_to_domain(model: RagAssetModel) -> RagAsset:
    return RagAsset(
        id=model.id,
        document_id=model.document_id,
        element_id=model.element_id,
        workspace_id=model.workspace_id,
        asset_kind=RagAssetKind(model.asset_kind),
        storage_reference=model.storage_reference,
        mime_type=model.mime_type,
        content_hash=model.content_hash,
        size_bytes=model.size_bytes,
        width=model.width,
        height=model.height,
        created_at=model.created_at,
    )


def _link_to_domain(model: RagChunkElementLinkModel) -> RagChunkElementLink:
    return RagChunkElementLink(
        id=model.id,
        document_id=model.document_id,
        workspace_id=model.workspace_id,
        chunk_id=model.chunk_id,
        element_id=model.element_id,
        relation_type=RagChunkElementRelation(model.relation_type),
        confidence=model.confidence,
        order_index=model.order_index,
        created_at=model.created_at,
    )


def _apply_document(model: RagDocumentModel, document: RagDocument) -> None:
    model.title = document.title
    model.mime_type = document.mime_type
    model.source_content_hash = document.source_content_hash
    model.ingestion_policy_version = document.ingestion_policy_version
    model.status = document.status.value
    model.parser_version = document.parser_version
    model.chunker_version = document.chunker_version
    model.embedding_provider = document.embedding_provider
    model.embedding_model = document.embedding_model
    model.embedding_dimensions = document.embedding_dimensions
    model.chunk_count = document.chunk_count
    model.indexed_at = document.indexed_at
    model.disabled_at = document.disabled_at
    model.version = document.version
    model.updated_at = document.updated_at


def _apply_job(model: RagIngestionJobModel, job: RagIngestionJob) -> None:
    model.status = job.status.value
    model.attempts = job.attempts
    model.max_attempts = job.max_attempts
    model.embedding_attempts = job.embedding_attempts
    model.embedding_max_attempts = job.embedding_max_attempts
    model.claimed_by = job.claimed_by
    model.lease_until = job.lease_until
    model.next_retry_at = job.next_retry_at
    model.error_code = job.error_code
    model.progress_json = _progress_to_dict(job.progress)
    model.started_at = job.started_at
    model.completed_at = job.completed_at
    model.failed_at = job.failed_at
    model.cancelled_at = job.cancelled_at
    model.updated_at = job.updated_at


def _progress_to_dict(progress: RagJobProgress) -> dict:
    return {
        "active_executor": progress.active_executor,
        "page_count": progress.page_count,
        "native_extraction_done": progress.native_extraction_done,
        "visual_pages_total": progress.visual_pages_total,
        "visual_pages_completed": progress.visual_pages_completed,
        "visual_route_counts": progress.visual_route_counts,
        "chunks_total": progress.chunks_total,
        "embedding_total": progress.embedding_total,
        "embedding_completed": progress.embedding_completed,
    }


class PostgresRagDocumentRepository(RagDocumentRepository):
    def __init__(self, session: AsyncSession):
        self._session = session

    async def create(self, document: RagDocument) -> RagDocument:
        model = RagDocumentModel(
            id=document.id,
            workspace_id=document.workspace_id,
            source_artifact_id=document.source_artifact_id,
            title=document.title,
            mime_type=document.mime_type,
            source_content_hash=document.source_content_hash,
            ingestion_policy_version=document.ingestion_policy_version,
            status=document.status.value,
            parser_version=document.parser_version,
            chunker_version=document.chunker_version,
            embedding_provider=document.embedding_provider,
            embedding_model=document.embedding_model,
            embedding_dimensions=document.embedding_dimensions,
            chunk_count=document.chunk_count,
            indexed_at=document.indexed_at,
            disabled_at=document.disabled_at,
            version=document.version,
            created_at=document.created_at,
            updated_at=document.updated_at,
        )
        self._session.add(model)
        await self._session.flush()
        return _document_to_domain(model)

    async def get(self, document_id: UUID) -> RagDocument | None:
        result = await self._session.execute(
            select(RagDocumentModel).where(RagDocumentModel.id == document_id)
        )
        model = result.scalar_one_or_none()
        return _document_to_domain(model) if model else None

    async def get_by_source(
        self, *, workspace_id: UUID, source_artifact_id: UUID, source_content_hash: str
    ) -> RagDocument | None:
        result = await self._session.execute(
            select(RagDocumentModel).where(
                RagDocumentModel.workspace_id == workspace_id,
                RagDocumentModel.source_artifact_id == source_artifact_id,
                RagDocumentModel.source_content_hash == source_content_hash,
            )
        )
        model = result.scalar_one_or_none()
        return _document_to_domain(model) if model else None

    async def list_by_workspace(
        self, *, workspace_id: UUID, include_disabled: bool = False, limit: int = 100
    ) -> list[RagDocument]:
        stmt = select(RagDocumentModel).where(RagDocumentModel.workspace_id == workspace_id)
        if not include_disabled:
            stmt = stmt.where(RagDocumentModel.status != RagDocumentStatus.DISABLED.value)
        stmt = stmt.order_by(RagDocumentModel.created_at.desc(), RagDocumentModel.id.desc()).limit(
            min(max(limit, 1), 100)
        )
        result = await self._session.execute(stmt)
        return [_document_to_domain(model) for model in result.scalars().all()]

    async def update(self, document: RagDocument) -> None:
        model = await self._session.get(RagDocumentModel, document.id)
        if model is None:
            raise ValueError("RAG document 不存在")
        _apply_document(model, document)
        await self._session.flush()

    async def delete(self, *, workspace_id: UUID, document_id: UUID) -> bool:
        result = await self._session.execute(
            delete(RagDocumentModel).where(
                RagDocumentModel.id == document_id,
                RagDocumentModel.workspace_id == workspace_id,
            )
        )
        await self._session.flush()
        return bool(result.rowcount)


class PostgresRagIngestionJobRepository(RagIngestionJobRepository):
    def __init__(self, session: AsyncSession):
        self._session = session

    async def create(self, job: RagIngestionJob) -> RagIngestionJob:
        model = RagIngestionJobModel(
            id=job.id,
            document_id=job.document_id,
            workspace_id=job.workspace_id,
            idempotency_key=job.idempotency_key,
            ingestion_policy_version=job.ingestion_policy_version,
            status=job.status.value,
            attempts=job.attempts,
            max_attempts=job.max_attempts,
            embedding_attempts=job.embedding_attempts,
            embedding_max_attempts=job.embedding_max_attempts,
            claimed_by=job.claimed_by,
            lease_until=job.lease_until,
            next_retry_at=job.next_retry_at,
            error_code=job.error_code,
            progress_json=_progress_to_dict(job.progress),
            started_at=job.started_at,
            completed_at=job.completed_at,
            failed_at=job.failed_at,
            cancelled_at=job.cancelled_at,
            created_at=job.created_at,
            updated_at=job.updated_at,
        )
        self._session.add(model)
        await self._session.flush()
        return _job_to_domain(model)

    async def get(self, job_id: UUID) -> RagIngestionJob | None:
        result = await self._session.execute(
            select(RagIngestionJobModel).where(RagIngestionJobModel.id == job_id)
        )
        model = result.scalar_one_or_none()
        return _job_to_domain(model) if model else None

    async def get_by_idempotency_key(self, idempotency_key: str) -> RagIngestionJob | None:
        result = await self._session.execute(
            select(RagIngestionJobModel).where(
                RagIngestionJobModel.idempotency_key == idempotency_key
            )
        )
        model = result.scalar_one_or_none()
        return _job_to_domain(model) if model else None

    async def list_latest_by_documents(
        self, *, workspace_id: UUID, document_ids: list[UUID]
    ) -> list[RagIngestionJob]:
        if not document_ids:
            return []
        result = await self._session.execute(
            select(RagIngestionJobModel)
            .where(
                RagIngestionJobModel.workspace_id == workspace_id,
                RagIngestionJobModel.document_id.in_(document_ids),
            )
            .order_by(
                RagIngestionJobModel.document_id.asc(),
                RagIngestionJobModel.created_at.desc(),
                RagIngestionJobModel.id.desc(),
            )
        )
        latest: dict[UUID, RagIngestionJob] = {}
        for model in result.scalars().all():
            latest.setdefault(model.document_id, _job_to_domain(model))
        return list(latest.values())

    async def claim_next(
        self,
        *,
        worker_id: str,
        now: datetime,
        lease_until: datetime,
    ) -> RagIngestionJob | None:
        retryable = and_(
            or_(
                and_(
                    RagIngestionJobModel.status == RagIngestionStatus.QUEUED.value,
                    or_(
                        RagIngestionJobModel.next_retry_at.is_(None),
                        RagIngestionJobModel.next_retry_at <= now,
                    ),
                ),
                and_(
                    RagIngestionJobModel.status == RagIngestionStatus.FAILED.value,
                    RagIngestionJobModel.next_retry_at.is_not(None),
                    RagIngestionJobModel.next_retry_at <= now,
                ),
            ),
            RagIngestionJobModel.attempts < RagIngestionJobModel.max_attempts,
        )
        stale = and_(
            RagIngestionJobModel.status.in_(
                [
                    RagIngestionStatus.PARSING.value,
                    RagIngestionStatus.CHUNKING.value,
                ]
            ),
            RagIngestionJobModel.lease_until.is_not(None),
            RagIngestionJobModel.lease_until <= now,
        )
        while True:
            result = await self._session.execute(
                select(RagIngestionJobModel)
                .where(or_(retryable, stale))
                .order_by(
                    RagIngestionJobModel.next_retry_at.asc().nullsfirst(),
                    RagIngestionJobModel.created_at.asc(),
                )
                .with_for_update(skip_locked=True)
                .limit(1)
            )
            model = result.scalar_one_or_none()
            if model is None:
                return None
            job = _job_to_domain(model)
            if job.status in {
                RagIngestionStatus.PARSING,
                RagIngestionStatus.CHUNKING,
                RagIngestionStatus.EMBEDDING,
            }:
                if job.attempts >= job.max_attempts:
                    job.exhaust_ingestion(now=now)
                    _apply_job(model, job)
                    await self._session.flush()
                    # 返回这个终态结果，让 Application Service 在同一事务中同步
                    # RagDocument 与 AuditLog；若继续查找并最终返回 None，上层的
                    # 空闲路径会回滚这次状态收口。
                    return job
                job.recover_stale(worker_id=worker_id, lease_until=lease_until, now=now)
            else:
                job.start(worker_id=worker_id, lease_until=lease_until, now=now)
            _apply_job(model, job)
            await self._session.flush()
            return job

    async def claim_embedding(
        self,
        *,
        worker_id: str,
        now: datetime,
        lease_until: datetime,
    ) -> RagIngestionJob | None:
        claimable = and_(
            RagIngestionJobModel.status == RagIngestionStatus.EMBEDDING.value,
            or_(
                RagIngestionJobModel.next_retry_at.is_(None),
                RagIngestionJobModel.next_retry_at <= now,
            ),
            or_(
                RagIngestionJobModel.claimed_by.is_(None),
                RagIngestionJobModel.lease_until.is_(None),
                RagIngestionJobModel.lease_until <= now,
            ),
        )
        while True:
            result = await self._session.execute(
                select(RagIngestionJobModel)
                .where(claimable)
                .order_by(
                    RagIngestionJobModel.next_retry_at.asc().nullsfirst(),
                    RagIngestionJobModel.created_at.asc(),
                )
                .with_for_update(skip_locked=True)
                .limit(1)
            )
            model = result.scalar_one_or_none()
            if model is None:
                return None
            job = _job_to_domain(model)
            if job.embedding_attempts >= job.embedding_max_attempts:
                job.exhaust_embedding(now=now)
                _apply_job(model, job)
                await self._session.flush()
                return job
            job.claim_embedding(worker_id=worker_id, lease_until=lease_until, now=now)
            _apply_job(model, job)
            await self._session.flush()
            return job

    async def update(self, job: RagIngestionJob) -> None:
        model = await self._session.get(RagIngestionJobModel, job.id)
        if model is None:
            raise ValueError("RAG ingestion job 不存在")
        _apply_job(model, job)
        await self._session.flush()


class PostgresRagChunkRepository(RagChunkRepository):
    def __init__(self, session: AsyncSession):
        self._session = session

    async def create_many(self, chunks: list[RagChunk]) -> list[RagChunk]:
        if not chunks:
            return []
        for chunk in chunks:
            self._session.add(
                RagChunkModel(
                    id=chunk.id,
                    document_id=chunk.document_id,
                    ingestion_job_id=chunk.ingestion_job_id,
                    workspace_id=chunk.workspace_id,
                    ordinal=chunk.ordinal,
                    content=chunk.content,
                    content_hash=chunk.content_hash,
                    token_count=chunk.token_count,
                    source_locator_json=chunk.source_locator,
                    embedding_key=chunk.embedding_key,
                    created_at=chunk.created_at,
                )
            )
        await self._session.flush()
        return chunks

    async def list_by_document(
        self, *, workspace_id: UUID, document_id: UUID, limit: int = 1000
    ) -> list[RagChunk]:
        result = await self._session.execute(
            select(RagChunkModel)
            .where(
                RagChunkModel.workspace_id == workspace_id,
                RagChunkModel.document_id == document_id,
            )
            .order_by(RagChunkModel.ordinal.asc())
            .limit(min(max(limit, 1), 1000))
        )
        return [_chunk_to_domain(model) for model in result.scalars().all()]

    async def list_identity_chunks(
        self, *, workspace_id: UUID, document_ids: list[UUID]
    ) -> list[RagChunk]:
        if not document_ids:
            return []
        result = await self._session.execute(
            select(RagChunkModel)
            .where(
                RagChunkModel.workspace_id == workspace_id,
                RagChunkModel.document_id.in_(document_ids),
                RagChunkModel.ordinal == 0,
            )
            .order_by(
                RagChunkModel.document_id.asc(),
                RagChunkModel.created_at.desc(),
            )
        )
        chunks: list[RagChunk] = []
        seen_documents: set[UUID] = set()
        for model in result.scalars().all():
            if model.document_id in seen_documents:
                continue
            seen_documents.add(model.document_id)
            chunks.append(_chunk_to_domain(model))
        return chunks

    async def list_by_ids(self, *, workspace_id: UUID, chunk_ids: list[UUID]) -> list[RagChunk]:
        if not chunk_ids:
            return []
        result = await self._session.execute(
            select(RagChunkModel).where(
                RagChunkModel.workspace_id == workspace_id,
                RagChunkModel.id.in_(chunk_ids),
            )
        )
        return [_chunk_to_domain(model) for model in result.scalars().all()]

    async def delete_by_job(self, ingestion_job_id: UUID) -> None:
        await self._session.execute(
            delete(RagChunkModel).where(RagChunkModel.ingestion_job_id == ingestion_job_id)
        )
        await self._session.flush()

    async def delete_by_document(self, *, workspace_id: UUID, document_id: UUID) -> None:
        await self._session.execute(
            delete(RagChunkModel).where(
                RagChunkModel.workspace_id == workspace_id,
                RagChunkModel.document_id == document_id,
            )
        )
        await self._session.flush()

    async def mark_embedded(
        self,
        *,
        workspace_id: UUID,
        document_id: UUID,
        embedding_keys: dict[UUID, str],
    ) -> None:
        if not embedding_keys:
            return
        result = await self._session.execute(
            select(RagChunkModel).where(
                RagChunkModel.workspace_id == workspace_id,
                RagChunkModel.document_id == document_id,
                RagChunkModel.id.in_(embedding_keys),
            )
        )
        models = result.scalars().all()
        if len(models) != len(embedding_keys):
            raise ValueError("RAG chunk embedding 回填范围与 Workspace 不匹配")
        for model in models:
            key = embedding_keys[model.id]
            if not key.strip():
                raise ValueError("RAG chunk embedding_key 不能为空")
            model.embedding_key = key
        await self._session.flush()


class PostgresRagElementRepository(RagElementRepository):
    def __init__(self, session: AsyncSession):
        self._session = session

    async def create_many(self, elements: list[RagElement]) -> list[RagElement]:
        if not elements:
            return []
        for element in elements:
            self._session.add(
                RagElementModel(
                    id=element.id,
                    document_id=element.document_id,
                    workspace_id=element.workspace_id,
                    element_type=element.element_type.value,
                    page_number=element.page_number,
                    bounding_box_json=list(element.bounding_box),
                    page_width=element.page_width,
                    page_height=element.page_height,
                    locator_key=element.locator_key,
                    content_hash=element.content_hash,
                    extraction_method=element.extraction_method.value,
                    extraction_version=element.extraction_version,
                    confidence=element.confidence,
                    caption_text=element.caption_text,
                    ocr_text=element.ocr_text,
                    structured_data_json=element.structured_data,
                    derived_description=element.derived_description,
                    created_at=element.created_at,
                )
            )
        await self._session.flush()
        return elements

    async def get(self, element_id: UUID) -> RagElement | None:
        result = await self._session.execute(
            select(RagElementModel).where(RagElementModel.id == element_id)
        )
        model = result.scalar_one_or_none()
        return _element_to_domain(model) if model else None

    async def list_by_document(
        self, *, workspace_id: UUID, document_id: UUID, limit: int = 1000
    ) -> list[RagElement]:
        result = await self._session.execute(
            select(RagElementModel)
            .where(
                RagElementModel.workspace_id == workspace_id,
                RagElementModel.document_id == document_id,
            )
            .order_by(
                RagElementModel.page_number.asc(),
                RagElementModel.locator_key.asc(),
            )
            .limit(min(max(limit, 1), 1000))
        )
        return [_element_to_domain(model) for model in result.scalars().all()]

    async def delete_by_document(self, *, workspace_id: UUID, document_id: UUID) -> None:
        await self._session.execute(
            delete(RagElementModel).where(
                RagElementModel.workspace_id == workspace_id,
                RagElementModel.document_id == document_id,
            )
        )
        await self._session.flush()


class PostgresRagAssetRepository(RagAssetRepository):
    def __init__(self, session: AsyncSession):
        self._session = session

    async def create(self, asset: RagAsset) -> RagAsset:
        model = RagAssetModel(
            id=asset.id,
            document_id=asset.document_id,
            element_id=asset.element_id,
            workspace_id=asset.workspace_id,
            asset_kind=asset.asset_kind.value,
            storage_reference=asset.storage_reference,
            mime_type=asset.mime_type,
            content_hash=asset.content_hash,
            size_bytes=asset.size_bytes,
            width=asset.width,
            height=asset.height,
            created_at=asset.created_at,
        )
        self._session.add(model)
        await self._session.flush()
        return _asset_to_domain(model)

    async def get(self, asset_id: UUID) -> RagAsset | None:
        result = await self._session.execute(
            select(RagAssetModel).where(RagAssetModel.id == asset_id)
        )
        model = result.scalar_one_or_none()
        return _asset_to_domain(model) if model else None

    async def list_by_element(self, *, workspace_id: UUID, element_id: UUID) -> list[RagAsset]:
        result = await self._session.execute(
            select(RagAssetModel)
            .where(
                RagAssetModel.workspace_id == workspace_id,
                RagAssetModel.element_id == element_id,
            )
            .order_by(RagAssetModel.created_at.asc(), RagAssetModel.id.asc())
        )
        return [_asset_to_domain(model) for model in result.scalars().all()]

    async def list_by_document(
        self, *, workspace_id: UUID, document_id: UUID, limit: int = 1000
    ) -> list[RagAsset]:
        result = await self._session.execute(
            select(RagAssetModel)
            .where(
                RagAssetModel.workspace_id == workspace_id,
                RagAssetModel.document_id == document_id,
            )
            .order_by(RagAssetModel.created_at.asc(), RagAssetModel.id.asc())
            .limit(min(max(limit, 1), 10_000))
        )
        return [_asset_to_domain(model) for model in result.scalars().all()]


class PostgresRagChunkElementLinkRepository(RagChunkElementLinkRepository):
    def __init__(self, session: AsyncSession):
        self._session = session

    async def create_many(self, links: list[RagChunkElementLink]) -> list[RagChunkElementLink]:
        if not links:
            return []
        for link in links:
            self._session.add(
                RagChunkElementLinkModel(
                    id=link.id,
                    document_id=link.document_id,
                    workspace_id=link.workspace_id,
                    chunk_id=link.chunk_id,
                    element_id=link.element_id,
                    relation_type=link.relation_type.value,
                    confidence=link.confidence,
                    order_index=link.order_index,
                    created_at=link.created_at,
                )
            )
        await self._session.flush()
        return links

    async def list_by_chunk(
        self, *, workspace_id: UUID, chunk_id: UUID
    ) -> list[RagChunkElementLink]:
        result = await self._session.execute(
            select(RagChunkElementLinkModel)
            .where(
                RagChunkElementLinkModel.workspace_id == workspace_id,
                RagChunkElementLinkModel.chunk_id == chunk_id,
            )
            .order_by(
                RagChunkElementLinkModel.order_index.asc(),
                RagChunkElementLinkModel.id.asc(),
            )
        )
        return [_link_to_domain(model) for model in result.scalars().all()]

    async def list_by_element(
        self, *, workspace_id: UUID, element_id: UUID
    ) -> list[RagChunkElementLink]:
        result = await self._session.execute(
            select(RagChunkElementLinkModel)
            .where(
                RagChunkElementLinkModel.workspace_id == workspace_id,
                RagChunkElementLinkModel.element_id == element_id,
            )
            .order_by(
                RagChunkElementLinkModel.order_index.asc(),
                RagChunkElementLinkModel.id.asc(),
            )
        )
        return [_link_to_domain(model) for model in result.scalars().all()]
