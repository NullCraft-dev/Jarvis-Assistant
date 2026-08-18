from __future__ import annotations

import asyncio
import importlib
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest

from jarvis_worker.agent.rag.contracts import (
    OcrResult,
    OcrSpan,
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
    VisualDescription,
)
from jarvis_worker.agent.rag.identifiers import (
    build_element_locator_key,
    deterministic_asset_id,
    deterministic_chunk_element_link_id,
    deterministic_element_id,
)
from jarvis_worker.agent.rag.postgres_repository import (
    PostgresRagAssetRepository,
    PostgresRagChunkElementLinkRepository,
    PostgresRagChunkRepository,
    PostgresRagDocumentRepository,
    PostgresRagElementRepository,
    PostgresRagIngestionJobRepository,
)
from jarvis_worker.database.models import (
    RagAssetModel,
    RagChunkElementLinkModel,
    RagChunkModel,
    RagDocumentModel,
    RagElementModel,
    RagIngestionJobModel,
)
from jarvis_worker.database.unit_of_work import PostgresUnitOfWork

NOW = datetime(2026, 7, 27, 9, 0, tzinfo=timezone.utc)
HASH_A = "a" * 64
HASH_B = "b" * 64


def _document() -> RagDocument:
    return RagDocument(
        id=uuid4(),
        workspace_id=uuid4(),
        source_artifact_id=uuid4(),
        title="RAG contract",
        mime_type="application/pdf",
        source_content_hash=HASH_A,
        ingestion_policy_version="rag-ingestion-v1",
        created_at=NOW,
        updated_at=NOW,
    )


def _job(*, document: RagDocument | None = None, max_attempts: int = 3) -> RagIngestionJob:
    document = document or _document()
    return RagIngestionJob(
        id=uuid4(),
        document_id=document.id,
        workspace_id=document.workspace_id,
        idempotency_key=HASH_B,
        ingestion_policy_version=document.ingestion_policy_version,
        max_attempts=max_attempts,
        created_at=NOW,
        updated_at=NOW,
    )


def _element(document: RagDocument) -> RagElement:
    return RagElement(
        id=uuid4(),
        document_id=document.id,
        workspace_id=document.workspace_id,
        element_type=RagElementType.CHART,
        page_number=3,
        bounding_box=(10.0, 20.0, 300.0, 400.0),
        page_width=612.0,
        page_height=792.0,
        locator_key="c" * 64,
        content_hash="d" * 64,
        extraction_method=RagExtractionMethod.HYBRID,
        extraction_version="pdf-elements-v1",
        confidence=0.92,
        caption_text="Figure 3",
        ocr_text="Accuracy",
        derived_description="Accuracy rises with more data.",
        created_at=NOW,
    )


def test_ingestion_happy_path_has_ordered_recoverable_states():
    job = _job()
    job.start(worker_id="rag-worker-01", lease_until=NOW + timedelta(minutes=2), now=NOW)
    assert job.status is RagIngestionStatus.PARSING
    assert job.attempts == 1
    assert job.claimed_by == "rag-worker-01"

    job.advance(RagIngestionStatus.CHUNKING, now=NOW + timedelta(seconds=1))
    job.advance(RagIngestionStatus.EMBEDDING, now=NOW + timedelta(seconds=2))
    job.advance(RagIngestionStatus.COMPLETED, now=NOW + timedelta(seconds=3))

    assert job.is_terminal is True
    assert job.completed_at == NOW + timedelta(seconds=3)
    assert job.claimed_by is None
    assert job.lease_until is None


def test_ingestion_rejects_skipped_state_and_terminal_mutation():
    job = _job()
    with pytest.raises(ValueError, match="非法 RAG ingestion 状态转换"):
        job.advance(RagIngestionStatus.EMBEDDING, now=NOW)

    job.cancel(now=NOW)
    with pytest.raises(ValueError, match="终态"):
        job.cancel(now=NOW)


def test_failed_ingestion_can_retry_with_bounded_attempts():
    job = _job(max_attempts=2)
    job.start(worker_id="rag-worker-01", lease_until=NOW + timedelta(minutes=1), now=NOW)
    retry_at = NOW + timedelta(minutes=5)
    job.fail(error_code="RAG_PARSE_FAILED", next_retry_at=retry_at, now=NOW)

    with pytest.raises(ValueError, match="尚未到重试时间"):
        job.start(
            worker_id="rag-worker-01",
            lease_until=retry_at + timedelta(minutes=1),
            now=retry_at - timedelta(seconds=1),
        )

    job.start(
        worker_id="rag-worker-02",
        lease_until=retry_at + timedelta(minutes=1),
        now=retry_at,
    )
    assert job.status is RagIngestionStatus.PARSING
    assert job.attempts == 2
    with pytest.raises(ValueError, match="最大尝试次数"):
        job.fail(
            error_code="RAG_PARSE_FAILED",
            next_retry_at=retry_at + timedelta(minutes=2),
            now=retry_at + timedelta(minutes=1),
        )


def test_failed_ingestion_without_retry_is_terminal():
    job = _job()
    job.start(worker_id="rag-worker-01", lease_until=NOW + timedelta(minutes=1), now=NOW)
    job.fail(error_code="RAG_PARSE_FAILED", next_retry_at=None, now=NOW)
    assert job.is_terminal is True
    with pytest.raises(ValueError, match="未安排重试"):
        job.start(
            worker_id="rag-worker-02",
            lease_until=NOW + timedelta(minutes=2),
            now=NOW + timedelta(minutes=1),
        )


def test_stale_lease_restarts_from_parsing_with_new_attempt():
    job = _job()
    job.start(worker_id="rag-worker-01", lease_until=NOW + timedelta(minutes=1), now=NOW)
    recovery_time = NOW + timedelta(minutes=2)
    job.recover_stale(
        worker_id="rag-worker-02",
        lease_until=recovery_time + timedelta(minutes=1),
        now=recovery_time,
    )
    assert job.status is RagIngestionStatus.PARSING
    assert job.attempts == 2
    assert job.claimed_by == "rag-worker-02"


def test_stale_ingestion_lease_exhaustion_is_terminal():
    job = _job(max_attempts=1)
    job.start(
        worker_id="rag-worker-01",
        lease_until=NOW + timedelta(minutes=1),
        now=NOW,
    )
    exhaustion_time = NOW + timedelta(minutes=2)

    job.exhaust_ingestion(now=exhaustion_time)

    assert job.status is RagIngestionStatus.FAILED
    assert job.error_code == "RAG_INGESTION_ATTEMPTS_EXHAUSTED"
    assert job.is_terminal is True
    assert job.claimed_by is None and job.lease_until is None


def test_active_ingestion_lease_can_only_be_renewed_by_owner():
    job = _job()
    first_lease = NOW + timedelta(minutes=1)
    job.start(worker_id="rag-worker-01", lease_until=first_lease, now=NOW)

    job.renew_lease(
        worker_id="rag-worker-01",
        lease_until=NOW + timedelta(minutes=2),
        now=NOW + timedelta(seconds=10),
    )

    assert job.lease_until == NOW + timedelta(minutes=2)
    with pytest.raises(ValueError, match="不属于"):
        job.renew_lease(
            worker_id="rag-worker-02",
            lease_until=NOW + timedelta(minutes=3),
            now=NOW + timedelta(seconds=20),
        )


def test_document_only_becomes_ready_with_complete_index_metadata():
    document = _document()
    with pytest.raises(ValueError, match="至少一个 chunk"):
        document.mark_ready(
            parser_version="pdf-v1",
            chunker_version="fixed-v1",
            embedding_provider="provider",
            embedding_model="model",
            embedding_dimensions=1024,
            chunk_count=0,
            now=NOW,
        )

    document.mark_ready(
        parser_version="pdf-v1",
        chunker_version="fixed-v1",
        embedding_provider="provider",
        embedding_model="model",
        embedding_dimensions=1024,
        chunk_count=3,
        now=NOW,
    )
    assert document.status is RagDocumentStatus.READY
    assert document.chunk_count == 3
    assert document.version == 2

    document.begin_indexing(ingestion_policy_version="rag-ingestion-v2", now=NOW)
    assert document.status is RagDocumentStatus.INDEXING
    assert document.ingestion_policy_version == "rag-ingestion-v2"
    assert document.embedding_model == ""
    assert document.chunk_count == 0


def test_ready_document_can_be_disabled_and_restored_without_losing_index_metadata():
    document = _document()
    document.mark_ready(
        parser_version="pdf-v1",
        chunker_version="semantic-v1",
        embedding_provider="openai",
        embedding_model="text-embedding-3-small",
        embedding_dimensions=1536,
        chunk_count=8,
        now=NOW,
    )

    document.disable(now=NOW + timedelta(seconds=1))
    assert document.status is RagDocumentStatus.DISABLED
    assert document.embedding_model == "text-embedding-3-small"

    document.enable(now=NOW + timedelta(seconds=2))
    assert document.status is RagDocumentStatus.READY
    assert document.disabled_at is None
    assert document.chunk_count == 8


def test_indexing_document_must_be_cancelled_instead_of_disabled():
    with pytest.raises(ValueError, match="只有 ready"):
        _document().disable(now=NOW)


def test_document_and_chunk_require_content_hashes_and_bounded_metadata():
    document = _document()
    with pytest.raises(ValueError, match="64 位小写 SHA-256"):
        RagDocument(
            id=uuid4(),
            workspace_id=uuid4(),
            source_artifact_id=uuid4(),
            title="bad",
            mime_type="application/pdf",
            source_content_hash="BAD",
            ingestion_policy_version="v1",
        )
    with pytest.raises(ValueError, match="token_count"):
        RagChunk(
            id=uuid4(),
            document_id=document.id,
            ingestion_job_id=uuid4(),
            workspace_id=document.workspace_id,
            ordinal=0,
            content="text",
            content_hash=HASH_B,
            token_count=0,
        )


def test_visual_element_requires_page_bounded_locator_and_confidence():
    document = _document()
    with pytest.raises(ValueError, match="页面范围"):
        RagElement(
            id=uuid4(),
            document_id=document.id,
            workspace_id=document.workspace_id,
            element_type=RagElementType.FIGURE,
            page_number=1,
            bounding_box=(0.0, 0.0, 700.0, 20.0),
            page_width=612.0,
            page_height=792.0,
            locator_key="c" * 64,
            content_hash="d" * 64,
            extraction_method=RagExtractionMethod.NATIVE,
            extraction_version="v1",
            confidence=1.0,
        )
    with pytest.raises(ValueError, match="0..1"):
        element = _element(document)
        RagElement(**{**element.__dict__, "confidence": 1.1})


def test_rag_asset_uses_safe_internal_reference_and_separate_binary_metadata():
    document = _document()
    element = _element(document)
    asset = RagAsset(
        id=uuid4(),
        document_id=document.id,
        element_id=element.id,
        workspace_id=document.workspace_id,
        asset_kind=RagAssetKind.CROP,
        storage_reference=f"{document.id}/{element.id}/crop.png",
        mime_type="image/png",
        content_hash="e" * 64,
        size_bytes=1024,
        width=800,
        height=600,
        created_at=NOW,
    )
    assert asset.element_id == element.id
    with pytest.raises(ValueError, match="安全的内部相对引用"):
        RagAsset(**{**asset.__dict__, "storage_reference": "../outside.png"})
    with pytest.raises(ValueError, match="同时提供"):
        RagAsset(**{**asset.__dict__, "height": None})
    with pytest.raises(ValueError, match="安全的内部相对引用"):
        RagAsset(**{**asset.__dict__, "storage_reference": "C:/outside.png"})


def test_ocr_and_visual_results_keep_bounds_confidence_and_provider_provenance():
    span = OcrSpan(text="Accuracy", bounding_box=(1.0, 2.0, 30.0, 12.0), confidence=0.9)
    result = OcrResult(
        text="Accuracy",
        spans=(span,),
        language="en",
        provider="local-ocr",
        model_version="v1",
    )
    description = VisualDescription(
        text="The curve rises.",
        confidence=0.8,
        provider="vision-provider",
        model_version="v1",
    )
    assert result.spans == (span,)
    assert description.confidence == 0.8
    with pytest.raises(ValueError, match="confidence"):
        OcrSpan(text="bad", bounding_box=(1.0, 2.0, 3.0, 4.0), confidence=1.1)


def test_chunk_element_relation_is_explicit_and_bounded():
    document = _document()
    link = RagChunkElementLink(
        id=uuid4(),
        document_id=document.id,
        workspace_id=document.workspace_id,
        chunk_id=uuid4(),
        element_id=uuid4(),
        relation_type=RagChunkElementRelation.EXPLAINS,
        confidence=0.8,
        order_index=1,
        created_at=NOW,
    )
    assert link.relation_type is RagChunkElementRelation.EXPLAINS
    with pytest.raises(ValueError, match="order_index"):
        RagChunkElementLink(**{**link.__dict__, "order_index": -1})


def test_multimodal_identifiers_are_deterministic_and_policy_versioned():
    document_id = uuid4()
    locator = build_element_locator_key(
        page_number=3,
        element_type=RagElementType.CHART,
        bounding_box=(10.123456, 20.0, 300.0, 400.0),
        extraction_version="pdf-elements-v1",
    )
    assert locator == build_element_locator_key(
        page_number=3,
        element_type=RagElementType.CHART,
        bounding_box=(10.123456, 20.0, 300.0, 400.0),
        extraction_version="pdf-elements-v1",
    )
    element_id = deterministic_element_id(document_id, locator)
    assert element_id == deterministic_element_id(document_id, locator)
    assert element_id != deterministic_element_id(document_id, "f" * 64)

    asset_id = deterministic_asset_id(
        element_id, asset_kind=RagAssetKind.CROP, content_hash="e" * 64
    )
    assert asset_id == deterministic_asset_id(
        element_id, asset_kind=RagAssetKind.CROP, content_hash="e" * 64
    )
    chunk_id = uuid4()
    assert deterministic_chunk_element_link_id(
        chunk_id,
        element_id=element_id,
        relation_type=RagChunkElementRelation.REFERENCES,
    ) == deterministic_chunk_element_link_id(
        chunk_id,
        element_id=element_id,
        relation_type=RagChunkElementRelation.REFERENCES,
    )


def test_database_models_expose_workspace_and_idempotency_constraints():
    assert RagDocumentModel.__table__.c.workspace_id.nullable is False
    assert RagIngestionJobModel.__table__.c.workspace_id.nullable is False
    assert RagChunkModel.__table__.c.workspace_id.nullable is False
    assert RagElementModel.__table__.c.workspace_id.nullable is False
    assert RagAssetModel.__table__.c.workspace_id.nullable is False
    assert RagChunkElementLinkModel.__table__.c.workspace_id.nullable is False

    document_constraints = {item.name for item in RagDocumentModel.__table__.constraints}
    job_constraints = {item.name for item in RagIngestionJobModel.__table__.constraints}
    chunk_constraints = {item.name for item in RagChunkModel.__table__.constraints}
    element_constraints = {item.name for item in RagElementModel.__table__.constraints}
    asset_constraints = {item.name for item in RagAssetModel.__table__.constraints}
    link_constraints = {item.name for item in RagChunkElementLinkModel.__table__.constraints}
    assert "uq_rag_documents_workspace_source" in document_constraints
    assert "uq_rag_documents_id_workspace" in document_constraints
    assert "uq_rag_ingestion_jobs_document_policy" in job_constraints
    assert "fk_rag_ingestion_jobs_document_workspace" in job_constraints
    assert "uq_rag_chunks_job_ordinal" in chunk_constraints
    assert "fk_rag_chunks_document_workspace" in chunk_constraints
    assert "fk_rag_chunks_job_workspace" in chunk_constraints
    assert "uq_rag_chunks_id_document_workspace" in chunk_constraints
    assert "fk_rag_elements_document_workspace" in element_constraints
    assert "fk_rag_assets_element_document_workspace" in asset_constraints
    assert "fk_rag_chunk_element_links_chunk_document_workspace" in link_constraints
    assert "fk_rag_chunk_element_links_element_document_workspace" in link_constraints
    assert RagIngestionJobModel.__table__.c.idempotency_key.unique is True


def test_migration_extends_current_head_without_vector_backend_dependency():
    migration = importlib.import_module(
        "jarvis_worker.migrations.versions.014_rag_ingestion_foundation"
    )
    assert migration.down_revision == "013_scheduled_source_reports"
    assert migration.revision == "014_rag_ingestion_foundation"
    assert "embedding" not in RagChunkModel.__table__.c
    assert "embedding_key" in RagChunkModel.__table__.c


def test_unit_of_work_exposes_only_repository_ports_for_rag():
    uow = PostgresUnitOfWork(object())
    assert isinstance(uow.rag_documents, PostgresRagDocumentRepository)
    assert isinstance(uow.rag_ingestion_jobs, PostgresRagIngestionJobRepository)
    assert isinstance(uow.rag_chunks, PostgresRagChunkRepository)
    assert isinstance(uow.rag_elements, PostgresRagElementRepository)
    assert isinstance(uow.rag_assets, PostgresRagAssetRepository)
    assert isinstance(uow.rag_chunk_element_links, PostgresRagChunkElementLinkRepository)


class _WriteSession:
    def __init__(self):
        self.added: list[object] = []
        self.flushes = 0

    def add(self, value: object) -> None:
        self.added.append(value)

    async def flush(self) -> None:
        self.flushes += 1


def test_postgres_repositories_create_domain_objects_without_committing():
    session = _WriteSession()
    document = _document()
    job = _job(document=document)
    chunk = RagChunk(
        id=uuid4(),
        document_id=document.id,
        ingestion_job_id=job.id,
        workspace_id=document.workspace_id,
        ordinal=0,
        content="bounded chunk",
        content_hash=HASH_B,
        token_count=2,
        source_locator={"page": 1},
        embedding_key="vector/chunk-0",
        created_at=NOW,
    )
    element = _element(document)
    asset = RagAsset(
        id=uuid4(),
        document_id=document.id,
        element_id=element.id,
        workspace_id=document.workspace_id,
        asset_kind=RagAssetKind.CROP,
        storage_reference=f"{document.id}/{element.id}/crop.png",
        mime_type="image/png",
        content_hash="e" * 64,
        size_bytes=1024,
        width=800,
        height=600,
        created_at=NOW,
    )
    link = RagChunkElementLink(
        id=uuid4(),
        document_id=document.id,
        workspace_id=document.workspace_id,
        chunk_id=chunk.id,
        element_id=element.id,
        relation_type=RagChunkElementRelation.REFERENCES,
        confidence=1.0,
        created_at=NOW,
    )

    async def exercise() -> None:
        created_document = await PostgresRagDocumentRepository(session).create(document)
        created_job = await PostgresRagIngestionJobRepository(session).create(job)
        created_chunks = await PostgresRagChunkRepository(session).create_many([chunk])
        created_elements = await PostgresRagElementRepository(session).create_many([element])
        created_asset = await PostgresRagAssetRepository(session).create(asset)
        created_links = await PostgresRagChunkElementLinkRepository(session).create_many([link])
        assert created_document.id == document.id
        assert created_job.id == job.id
        assert created_chunks == [chunk]
        assert created_elements == [element]
        assert created_asset == asset
        assert created_links == [link]

    asyncio.run(exercise())
    assert [type(value) for value in session.added] == [
        RagDocumentModel,
        RagIngestionJobModel,
        RagChunkModel,
        RagElementModel,
        RagAssetModel,
        RagChunkElementLinkModel,
    ]
    assert session.flushes == 6
    assert not hasattr(session, "commit")
