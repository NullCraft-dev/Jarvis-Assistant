from __future__ import annotations

import hashlib
from datetime import datetime, timedelta, timezone
from uuid import UUID, uuid4

import pytest

import jarvis_worker.agent.rag.ingestion.service as service_module
from jarvis_worker.agent.artifacts.file_store import LocalArtifactFileStore
from jarvis_worker.agent.rag.contracts import (
    RagDocumentStatus,
    RagIngestionStatus,
)
from jarvis_worker.agent.rag.ingestion import (
    LocalRagAssetFileStore,
    RagIngestionError,
    RagIngestionService,
)
from jarvis_worker.agent.rag.ingestion.source import (
    RAG_UPLOAD_OPERATION_TYPE,
    RAG_UPLOAD_TOOL_NAME,
    user_upload_permission_request_id,
)
from jarvis_worker.agent.rag.preprocessing import (
    DocumentNode,
    DocumentNodeType,
    NodeExtractionMethod,
    PreprocessedDocument,
)
from jarvis_worker.shared.domain.models import (
    AgentRun,
    Artifact,
    PermissionRequest,
    PermissionStatus,
    RunStatus,
    Task,
    TaskStatus,
    ToolCall,
)
from jarvis_worker.shared.storage_capacity import StorageCapacityExceeded

NOW = datetime(2026, 7, 27, 4, 0, tzinfo=timezone.utc)


class _GetRepository:
    def __init__(self, values: dict[UUID, object]):
        self.values = values

    async def get(self, value_id: UUID):
        return self.values.get(value_id)


class _PermissionRepository(_GetRepository):
    async def get_request(self, request_id: UUID):
        return self.values.get(request_id)


class _DocumentRepository:
    def __init__(self):
        self.values = {}

    async def create(self, document):
        self.values[document.id] = document
        return document

    async def get(self, document_id):
        return self.values.get(document_id)

    async def get_by_source(self, *, workspace_id, source_artifact_id, source_content_hash):
        return next(
            (
                document
                for document in self.values.values()
                if document.workspace_id == workspace_id
                and document.source_artifact_id == source_artifact_id
                and document.source_content_hash == source_content_hash
            ),
            None,
        )

    async def update(self, document):
        self.values[document.id] = document


class _JobRepository:
    def __init__(self, now):
        self.values = {}
        self._now = now

    async def create(self, job):
        self.values[job.id] = job
        return job

    async def get(self, job_id):
        return self.values.get(job_id)

    async def get_by_idempotency_key(self, key):
        return next((job for job in self.values.values() if job.idempotency_key == key), None)

    async def list_latest_by_documents(self, *, workspace_id, document_ids):
        return [
            job
            for job in self.values.values()
            if job.workspace_id == workspace_id and job.document_id in document_ids
        ]

    async def claim_next(self, *, worker_id, now, lease_until):
        for job in self.values.values():
            if job.status is RagIngestionStatus.QUEUED or (
                job.status is RagIngestionStatus.FAILED
                and job.next_retry_at is not None
                and job.next_retry_at <= now
            ):
                job.start(worker_id=worker_id, now=now, lease_until=lease_until)
                return job
            if (
                job.status in {RagIngestionStatus.PARSING, RagIngestionStatus.CHUNKING}
                and job.lease_until is not None
                and job.lease_until <= now
            ):
                if job.attempts >= job.max_attempts:
                    job.exhaust_ingestion(now=now)
                    return job
                job.recover_stale(worker_id=worker_id, now=now, lease_until=lease_until)
                return job
        return None

    async def update(self, job):
        self.values[job.id] = job


class _CollectionRepository:
    def __init__(self):
        self.values = {}

    async def create(self, value):
        self.values[value.id] = value
        return value

    async def create_many(self, values):
        for value in values:
            self.values[value.id] = value
        return values


class _ChunkRepository(_CollectionRepository):
    async def delete_by_document(self, *, workspace_id, document_id):
        self.values = {
            key: value
            for key, value in self.values.items()
            if not (value.workspace_id == workspace_id and value.document_id == document_id)
        }


class _ElementRepository(_ChunkRepository):
    pass


class _AssetRepository(_CollectionRepository):
    async def list_by_document(self, *, workspace_id, document_id, limit=1000):
        return [
            value
            for value in self.values.values()
            if value.workspace_id == workspace_id and value.document_id == document_id
        ][:limit]


class _AuditRepository:
    def __init__(self):
        self.values = []

    async def create(self, audit):
        self.values.append(audit)
        return audit


class _FakeUow:
    def __init__(self, *, artifacts, tasks, tool_calls, now, runs=None, permissions=None):
        self.artifacts = _GetRepository(artifacts)
        self.tasks = _GetRepository(tasks)
        self.runs = _GetRepository(runs or {})
        self.tool_calls = _GetRepository(tool_calls)
        self.permissions = _PermissionRepository(permissions or {})
        self.rag_documents = _DocumentRepository()
        self.rag_ingestion_jobs = _JobRepository(now)
        self.rag_chunks = _ChunkRepository()
        self.rag_elements = _ElementRepository()
        self.rag_assets = _AssetRepository()
        self.rag_chunk_element_links = _CollectionRepository()
        self.audits = _AuditRepository()

    def transaction(self):
        return self

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, traceback):
        return None

    async def commit(self):
        return None


class _Session:
    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, traceback):
        return None


class _SessionFactory:
    def __call__(self):
        return _Session()


class _Preprocessor:
    def __init__(self, document: PreprocessedDocument | None = None, error=None):
        self.document = document or _preprocessed_document()
        self.error = error
        self.calls = 0

    async def preprocess_pdf(self, content: bytes, *, progress_callback=None):
        self.calls += 1
        if self.error is not None:
            raise self.error
        if progress_callback is not None:
            from jarvis_worker.agent.rag.preprocessing import PreprocessingProgress

            await progress_callback(
                PreprocessingProgress(
                    page_count=self.document.page_count,
                    native_extraction_done=True,
                    visual_pages_total=0,
                    visual_pages_completed=0,
                    active_executor="pymupdf",
                )
            )
        return self.document


def _preprocessed_document() -> PreprocessedDocument:
    paragraph = DocumentNode(
        node_id="a" * 64,
        node_type=DocumentNodeType.PARAGRAPH,
        page_number=1,
        order_index=0,
        bounding_box=(10.0, 10.0, 400.0, 80.0),
        page_width=500.0,
        page_height=700.0,
        text="A searchable paragraph with enough content for deterministic RAG ingestion.",
        extraction_method=NodeExtractionMethod.NATIVE,
        extraction_version="pymupdf-native-v1",
        confidence=1.0,
    )
    chart = DocumentNode(
        node_id="b" * 64,
        node_type=DocumentNodeType.CHART,
        page_number=1,
        order_index=1,
        bounding_box=(20.0, 100.0, 450.0, 400.0),
        page_width=500.0,
        page_height=700.0,
        text="| Quarter | Value |\n| --- | --- |\n| Q1 | 10 |",
        structured_data={"source_format": "markdown"},
        asset_bytes=b"controlled-image-bytes",
        asset_mime_type="image/png",
        extraction_method=NodeExtractionMethod.HYBRID,
        extraction_version="native+vl-v1",
        confidence=0.9,
    )
    return PreprocessedDocument(
        page_count=1,
        nodes=(paragraph, chart),
        native_parser_version="pymupdf-native-v1",
        preprocessing_policy_version="preprocess-v1",
        structure_provider="paddleocr-vl-local",
        structure_provider_version="v1.6",
        pages_processed_by_structure_model=(1,),
    )


def _fixture(tmp_path, monkeypatch, *, preprocessor=None, workspace_id=None):
    workspace_id = workspace_id or uuid4()
    task_id, run_id, step_id, tool_call_id, artifact_id = (uuid4() for _ in range(5))
    artifact_store = LocalArtifactFileStore(tmp_path / "artifacts")
    content = b"%PDF-1.4\ncontrolled fixture"
    stored = artifact_store.write_bytes(
        artifact_id,
        content,
        run_id=run_id,
        workspace_id=workspace_id,
        suffix=".pdf",
        mime_type="application/pdf",
    )
    task = Task(
        id=task_id,
        title="Download paper",
        user_goal="Download a paper",
        conversation_id=uuid4(),
        workspace_id=workspace_id,
    )
    artifact = Artifact(
        id=artifact_id,
        task_id=task_id,
        run_id=run_id,
        step_id=step_id,
        kind="file",
        title="paper.pdf",
        purpose="deliverable",
        producer_type="tool",
        source_tool_call_id=tool_call_id,
        file_path=stored.relative_path,
        file_size_bytes=stored.size_bytes,
        mime_type=stored.mime_type,
        content_hash=stored.sha256,
        metadata={"storage": "local_file", "source": "arxiv"},
    )
    result = {
        "artifact_ids": [str(artifact_id)],
        "data": {
            "downloaded": True,
            "path": stored.relative_path,
            "size_bytes": stored.size_bytes,
            "sha256": stored.sha256,
        },
        "deliverables": [
            {
                "kind": "file",
                "path": stored.relative_path,
                "size_bytes": stored.size_bytes,
                "mime_type": stored.mime_type,
                "content_hash": stored.sha256,
            }
        ],
    }
    tool_call = ToolCall(
        id=tool_call_id,
        task_id=task_id,
        run_id=run_id,
        step_id=step_id,
        provider="native",
        tool_name="literature.download_arxiv_pdf",
        risk_level="L2",
        arguments={"arxiv_id": "2401.12345"},
        status="completed",
        result=result,
    )
    uow = _FakeUow(
        artifacts={artifact_id: artifact},
        tasks={task_id: task},
        tool_calls={tool_call_id: tool_call},
        now=lambda: NOW,
    )
    monkeypatch.setattr(service_module, "PostgresUnitOfWork", lambda session: uow)
    service = RagIngestionService(
        lambda: _SessionFactory(),
        artifact_file_store=artifact_store,
        asset_file_store=LocalRagAssetFileStore(tmp_path / "rag-assets"),
        preprocessor=preprocessor or _Preprocessor(),
        now=lambda: NOW,
    )
    return service, uow, workspace_id, artifact


def _staged_user_upload_fixture(tmp_path, monkeypatch, *, upload_attempt: int = 1):
    workspace_id = uuid4()
    artifact_id, task_id, run_id = (uuid4() for _ in range(3))
    artifact_store = LocalArtifactFileStore(tmp_path / "upload-artifacts")
    content = b"%PDF-1.4\napproved user upload"
    stored = artifact_store.write_bytes(
        artifact_id,
        content,
        run_id=run_id,
        workspace_id=workspace_id,
        suffix=".pdf",
        mime_type="application/pdf",
    )
    metadata = {
        "operation_type": RAG_UPLOAD_OPERATION_TYPE,
        "artifact_id": str(artifact_id),
    }
    task = Task(
        id=task_id,
        title="Upload paper",
        user_goal="Upload paper to RAG",
        conversation_id=uuid4(),
        workspace_id=workspace_id,
        status=TaskStatus.WAITING_FOR_USER,
        active_run_id=run_id,
        metadata=dict(metadata),
    )
    run = AgentRun(
        id=run_id,
        task_id=task_id,
        status=RunStatus.WAITING_PERMISSION,
        version=1,
        metadata=dict(metadata),
    )
    permission_id = user_upload_permission_request_id(artifact_id, upload_attempt)
    artifact = Artifact(
        id=artifact_id,
        task_id=task_id,
        run_id=run_id,
        kind="file",
        title="paper.pdf",
        purpose="deliverable",
        producer_type="runtime",
        file_path=stored.relative_path,
        file_size_bytes=stored.size_bytes,
        mime_type=stored.mime_type,
        content_hash=stored.sha256,
        metadata={
            "storage": "local_file",
            "source": "user_upload",
            "explicit_user_action": True,
            "permission_request_id": str(permission_id),
        },
    )
    permission = PermissionRequest(
        id=permission_id,
        task_id=task_id,
        run_id=run_id,
        tool_name=RAG_UPLOAD_TOOL_NAME,
        action_summary="Upload paper",
        risk_level="L2",
        scope={
            "type": "once",
            "workspace_id": str(workspace_id),
            "artifact_id": str(artifact_id),
        },
        arguments_summary={},
        checkpoint={
            "version": 1,
            "action": "rag_upload_pdf",
            "workspace_id": str(workspace_id),
            "artifact_id": str(artifact_id),
            "filename": artifact.title,
            "size_bytes": artifact.file_size_bytes,
            "sha256": artifact.content_hash,
            "attempt": upload_attempt,
            "root_request_id": str(user_upload_permission_request_id(artifact_id)),
        },
        status=PermissionStatus.APPROVED,
        decision="allow_once",
    )
    uow = _FakeUow(
        artifacts={artifact_id: artifact},
        tasks={task_id: task},
        runs={run_id: run},
        tool_calls={},
        permissions={permission_id: permission},
        now=lambda: NOW,
    )
    monkeypatch.setattr(service_module, "PostgresUnitOfWork", lambda session: uow)
    service = RagIngestionService(
        lambda: _SessionFactory(),
        artifact_file_store=artifact_store,
        asset_file_store=LocalRagAssetFileStore(tmp_path / "upload-rag-assets"),
        preprocessor=_Preprocessor(),
        now=lambda: NOW,
    )
    return service, uow, workspace_id, artifact, permission


@pytest.mark.asyncio
async def test_enqueue_for_task_derives_workspace_from_persisted_task(
    tmp_path, monkeypatch
) -> None:
    service, uow, workspace_id, artifact = _fixture(tmp_path, monkeypatch)

    queued = await service.enqueue_pdf_for_task(
        task_id=artifact.task_id,
        source_artifact_id=artifact.id,
    )

    assert queued.created is True
    assert uow.rag_documents.values[queued.document_id].workspace_id == workspace_id


@pytest.mark.asyncio
async def test_ingestion_persists_prepared_multimodal_projection(tmp_path, monkeypatch):
    service, uow, workspace_id, artifact = _fixture(tmp_path, monkeypatch)

    queued = await service.enqueue_pdf(workspace_id=workspace_id, source_artifact_id=artifact.id)
    duplicate = await service.enqueue_pdf(workspace_id=workspace_id, source_artifact_id=artifact.id)
    processed = await service.process_next(worker_id="rag-worker-1")

    assert queued.created is True
    assert duplicate.created is False
    assert duplicate.job_id == queued.job_id
    assert processed is not None
    assert processed.status is RagIngestionStatus.EMBEDDING
    assert processed.chunk_count == 2
    assert processed.element_count == 1
    assert processed.asset_count == 1
    job = uow.rag_ingestion_jobs.values[queued.job_id]
    document = uow.rag_documents.values[queued.document_id]
    assert job.claimed_by is None and job.lease_until is None
    assert job.progress.active_executor is None
    assert job.progress.page_count == 1
    assert job.progress.native_extraction_done is True
    assert job.progress.chunks_total == 2
    assert job.progress.embedding_total == 2
    assert job.progress.embedding_completed == 0
    assert document.status is RagDocumentStatus.INDEXING
    assert document.parser_version == "pymupdf-native-v1"
    assert document.chunk_count == 2
    assert len(uow.rag_chunks.values) == 2
    assert len(uow.rag_elements.values) == 1
    assert len(uow.rag_assets.values) == 1
    assert len(uow.rag_chunk_element_links.values) == 1
    assert [audit.event_type for audit in uow.audits.values] == [
        "rag.ingestion.queued",
        "rag.ingestion.prepared",
    ]


@pytest.mark.asyncio
async def test_ingestion_sanitizes_nul_from_chunks_and_structured_elements(tmp_path, monkeypatch):
    paragraph = DocumentNode(
        node_id="c" * 64,
        node_type=DocumentNodeType.PARAGRAPH,
        page_number=1,
        order_index=0,
        bounding_box=(10.0, 10.0, 400.0, 80.0),
        page_width=500.0,
        page_height=700.0,
        text="Batch\x00 Normalization remains searchable.",
        extraction_method=NodeExtractionMethod.NATIVE,
        extraction_version="pymupdf-native-v1",
        confidence=1.0,
    )
    table = DocumentNode(
        node_id="d" * 64,
        node_type=DocumentNodeType.TABLE,
        page_number=1,
        order_index=1,
        bounding_box=(20.0, 100.0, 450.0, 300.0),
        page_width=500.0,
        page_height=700.0,
        text="| Name | Value |\n| --- | --- |\n| BN\x00 | 1 |",
        structured_data={"caption": "Batch\x00 Normalization"},
        extraction_method=NodeExtractionMethod.NATIVE,
        extraction_version="pymupdf-native-v1",
        confidence=1.0,
    )
    document = PreprocessedDocument(
        page_count=1,
        nodes=(paragraph, table),
        native_parser_version="pymupdf-native-v1",
        preprocessing_policy_version="preprocess-v1",
    )
    service, uow, workspace_id, artifact = _fixture(
        tmp_path,
        monkeypatch,
        preprocessor=_Preprocessor(document),
    )

    await service.enqueue_pdf(workspace_id=workspace_id, source_artifact_id=artifact.id)
    processed = await service.process_next(worker_id="rag-worker-1")

    assert processed is not None
    assert processed.status is RagIngestionStatus.EMBEDDING
    assert all("\x00" not in chunk.content for chunk in uow.rag_chunks.values.values())
    element = next(iter(uow.rag_elements.values.values()))
    assert "\x00" not in element.ocr_text
    assert element.structured_data == {"caption": "Batch Normalization"}


@pytest.mark.asyncio
async def test_restart_reuses_artifact_and_resets_same_job(tmp_path, monkeypatch):
    service, uow, workspace_id, artifact = _fixture(tmp_path, monkeypatch)
    queued = await service.enqueue_pdf(workspace_id=workspace_id, source_artifact_id=artifact.id)
    job = uow.rag_ingestion_jobs.values[queued.job_id]
    job.start(
        worker_id="old-rag-worker",
        lease_until=NOW + timedelta(minutes=5),
        now=NOW,
    )

    restarted = await service.restart_document(
        workspace_id=workspace_id,
        document_id=queued.document_id,
        expected_version=uow.rag_documents.values[queued.document_id].version,
    )

    assert restarted.job_id == queued.job_id
    assert restarted.status is RagIngestionStatus.QUEUED
    assert job.status is RagIngestionStatus.QUEUED
    assert job.attempts == 0
    assert job.claimed_by is None and job.lease_until is None
    assert job.progress.page_count == 0
    assert uow.rag_documents.values[queued.document_id].status is RagDocumentStatus.INDEXING
    assert [audit.event_type for audit in uow.audits.values] == [
        "rag.ingestion.queued",
        "rag.ingestion.restarted",
    ]


@pytest.mark.asyncio
async def test_document_lifecycle_disables_and_enables_ready_index(tmp_path, monkeypatch):
    service, uow, workspace_id, artifact = _fixture(tmp_path, monkeypatch)
    queued = await service.enqueue_pdf(
        workspace_id=workspace_id,
        source_artifact_id=artifact.id,
    )
    document = uow.rag_documents.values[queued.document_id]
    job = uow.rag_ingestion_jobs.values[queued.job_id]
    document.mark_ready(
        parser_version="pymupdf-v1",
        chunker_version="semantic-v1",
        embedding_provider="openai",
        embedding_model="text-embedding-3-small",
        embedding_dimensions=1536,
        chunk_count=4,
        now=NOW,
    )
    job.status = RagIngestionStatus.COMPLETED

    disabled = await service.set_document_enabled(
        workspace_id=workspace_id,
        document_id=document.id,
        expected_version=document.version,
        enabled=False,
    )
    enabled = await service.set_document_enabled(
        workspace_id=workspace_id,
        document_id=document.id,
        expected_version=disabled.version,
        enabled=True,
    )

    assert disabled.status is RagDocumentStatus.DISABLED
    assert enabled.status is RagDocumentStatus.READY
    assert document.chunk_count == 4
    assert [audit.event_type for audit in uow.audits.values][-2:] == [
        "rag.document.disabled",
        "rag.document.enabled",
    ]


@pytest.mark.asyncio
async def test_document_lifecycle_cancels_active_job_with_version_guard(tmp_path, monkeypatch):
    service, uow, workspace_id, artifact = _fixture(tmp_path, monkeypatch)
    queued = await service.enqueue_pdf(
        workspace_id=workspace_id,
        source_artifact_id=artifact.id,
    )
    document = uow.rag_documents.values[queued.document_id]

    with pytest.raises(RagIngestionError) as stale:
        await service.cancel_document(
            workspace_id=workspace_id,
            document_id=document.id,
            expected_version=document.version + 1,
        )
    assert stale.value.code == "RAG_DOCUMENT_VERSION_CONFLICT"

    cancelled = await service.cancel_document(
        workspace_id=workspace_id,
        document_id=document.id,
        expected_version=document.version,
    )

    assert cancelled.status is RagDocumentStatus.FAILED
    assert cancelled.job_status is RagIngestionStatus.CANCELLED
    assert uow.rag_ingestion_jobs.values[queued.job_id].claimed_by is None
    assert uow.audits.values[-1].event_type == "rag.ingestion.cancelled"


@pytest.mark.asyncio
async def test_ingestion_rejects_cross_workspace_source(tmp_path, monkeypatch):
    service, _, _, artifact = _fixture(tmp_path, monkeypatch)

    with pytest.raises(RagIngestionError) as captured:
        await service.enqueue_pdf(workspace_id=uuid4(), source_artifact_id=artifact.id)

    assert captured.value.code == "RAG_SOURCE_INTEGRITY_ERROR"
    assert captured.value.recoverable is False


@pytest.mark.asyncio
async def test_ingestion_accepts_exactly_approved_staged_user_upload(tmp_path, monkeypatch):
    service, uow, workspace_id, artifact, _permission = _staged_user_upload_fixture(
        tmp_path, monkeypatch
    )

    queued = await service.enqueue_pdf(
        workspace_id=workspace_id,
        source_artifact_id=artifact.id,
    )

    assert queued.created is True
    document = uow.rag_documents.values[queued.document_id]
    assert document.source_artifact_id == artifact.id
    assert document.workspace_id == workspace_id


@pytest.mark.asyncio
async def test_ingestion_accepts_approved_retry_upload_lineage(tmp_path, monkeypatch):
    service, uow, workspace_id, artifact, permission = _staged_user_upload_fixture(
        tmp_path,
        monkeypatch,
        upload_attempt=2,
    )

    queued = await service.enqueue_pdf(
        workspace_id=workspace_id,
        source_artifact_id=artifact.id,
    )

    assert queued.created is True
    assert permission.id == user_upload_permission_request_id(artifact.id, 2)
    assert uow.rag_documents.values[queued.document_id].source_artifact_id == artifact.id


@pytest.mark.asyncio
async def test_ingestion_rejects_staged_user_upload_without_permission(tmp_path, monkeypatch):
    service, uow, workspace_id, artifact, permission = _staged_user_upload_fixture(
        tmp_path, monkeypatch
    )
    uow.permissions.values.pop(permission.id)

    with pytest.raises(RagIngestionError) as captured:
        await service.enqueue_pdf(
            workspace_id=workspace_id,
            source_artifact_id=artifact.id,
        )

    assert captured.value.code == "RAG_SOURCE_INTEGRITY_ERROR"


@pytest.mark.asyncio
async def test_ingestion_rejects_staged_user_upload_with_mismatched_file_approval(
    tmp_path, monkeypatch
):
    service, _uow, workspace_id, artifact, permission = _staged_user_upload_fixture(
        tmp_path, monkeypatch
    )
    permission.checkpoint["sha256"] = "f" * 64

    with pytest.raises(RagIngestionError) as captured:
        await service.enqueue_pdf(
            workspace_id=workspace_id,
            source_artifact_id=artifact.id,
        )

    assert captured.value.code == "RAG_SOURCE_INTEGRITY_ERROR"


@pytest.mark.asyncio
async def test_ingestion_accepts_any_verified_pdf_tool_deliverable(tmp_path, monkeypatch):
    service, uow, workspace_id, artifact = _fixture(tmp_path, monkeypatch)
    tool_call = next(iter(uow.tool_calls.values.values()))
    tool_call.tool_name = "documents.upload_pdf"

    queued = await service.enqueue_pdf(workspace_id=workspace_id, source_artifact_id=artifact.id)

    assert queued.created is True


@pytest.mark.asyncio
async def test_ingestion_rejects_tampered_artifact(tmp_path, monkeypatch):
    service, _, workspace_id, artifact = _fixture(tmp_path, monkeypatch)
    target = service._artifact_file_store.root / artifact.file_path
    target.write_bytes(b"%PDF-1.4\ntampered")

    with pytest.raises(RagIngestionError) as captured:
        await service.enqueue_pdf(workspace_id=workspace_id, source_artifact_id=artifact.id)

    assert captured.value.code == "RAG_SOURCE_INTEGRITY_ERROR"


@pytest.mark.asyncio
async def test_recoverable_preprocessor_failure_schedules_bounded_retry(tmp_path, monkeypatch):
    service, uow, workspace_id, artifact = _fixture(
        tmp_path,
        monkeypatch,
        preprocessor=_Preprocessor(error=RuntimeError("provider unavailable")),
    )
    queued = await service.enqueue_pdf(
        workspace_id=workspace_id, source_artifact_id=artifact.id, max_attempts=2
    )

    processed = await service.process_next(worker_id="rag-worker-1")

    assert processed is not None
    assert processed.status is RagIngestionStatus.FAILED
    assert processed.error_code == "RAG_INGESTION_FAILED"
    job = uow.rag_ingestion_jobs.values[queued.job_id]
    assert job.next_retry_at == NOW + timedelta(seconds=30)
    assert job.is_terminal is False
    assert uow.rag_documents.values[queued.document_id].status is RagDocumentStatus.INDEXING


@pytest.mark.asyncio
async def test_current_worker_final_failure_normalizes_exhausted_attempts(tmp_path, monkeypatch):
    service, uow, workspace_id, artifact = _fixture(
        tmp_path,
        monkeypatch,
        preprocessor=_Preprocessor(error=RuntimeError("provider unavailable")),
    )
    queued = await service.enqueue_pdf(
        workspace_id=workspace_id, source_artifact_id=artifact.id, max_attempts=1
    )

    processed = await service.process_next(worker_id="rag-worker-1")

    assert processed is not None
    assert processed.status is RagIngestionStatus.FAILED
    assert processed.error_code == "RAG_INGESTION_ATTEMPTS_EXHAUSTED"
    job = uow.rag_ingestion_jobs.values[queued.job_id]
    assert job.error_code == "RAG_INGESTION_ATTEMPTS_EXHAUSTED"
    assert job.next_retry_at is None and job.is_terminal is True
    assert uow.rag_documents.values[queued.document_id].status is RagDocumentStatus.FAILED
    assert uow.audits.values[-1].event_type == "rag.ingestion.failed"
    assert uow.audits.values[-1].details["attempts"] == 1


@pytest.mark.asyncio
async def test_expired_ingestion_lease_at_attempt_limit_reconciles_terminal_state(
    tmp_path, monkeypatch
):
    preprocessor = _Preprocessor()
    service, uow, workspace_id, artifact = _fixture(
        tmp_path,
        monkeypatch,
        preprocessor=preprocessor,
    )
    queued = await service.enqueue_pdf(
        workspace_id=workspace_id,
        source_artifact_id=artifact.id,
        max_attempts=1,
    )
    job = uow.rag_ingestion_jobs.values[queued.job_id]
    job.start(
        worker_id="crashed-rag-worker",
        lease_until=NOW - timedelta(minutes=1),
        now=NOW - timedelta(minutes=2),
    )

    processed = await service.process_next(worker_id="replacement-rag-worker")

    assert processed is not None
    assert processed.status is RagIngestionStatus.FAILED
    assert processed.error_code == "RAG_INGESTION_ATTEMPTS_EXHAUSTED"
    assert job.status is RagIngestionStatus.FAILED
    assert job.claimed_by is None and job.lease_until is None
    assert uow.rag_documents.values[queued.document_id].status is RagDocumentStatus.FAILED
    assert preprocessor.calls == 0
    assert uow.audits.values[-1].event_type == "rag.ingestion.failed"
    assert uow.audits.values[-1].details["retry_scheduled"] is False


@pytest.mark.asyncio
async def test_cancel_marks_job_terminal_and_document_failed(tmp_path, monkeypatch):
    service, uow, workspace_id, artifact = _fixture(tmp_path, monkeypatch)
    queued = await service.enqueue_pdf(workspace_id=workspace_id, source_artifact_id=artifact.id)

    job = await service.cancel(workspace_id=workspace_id, job_id=queued.job_id)

    assert job.status is RagIngestionStatus.CANCELLED
    assert job.is_terminal is True
    assert uow.rag_documents.values[queued.document_id].status is RagDocumentStatus.FAILED


def test_local_rag_asset_store_checks_hash_and_reference(tmp_path):
    store = LocalRagAssetFileStore(tmp_path / "rag-assets")
    content = b"image-bytes"
    digest = hashlib.sha256(content).hexdigest()
    asset_id = uuid4()

    reference = store.write(
        asset_id=asset_id,
        content=content,
        expected_hash=digest,
        mime_type="image/png",
    )

    assert reference.endswith(f"{asset_id}.png")
    with pytest.raises(ValueError, match="哈希"):
        store.write(
            asset_id=uuid4(),
            content=content,
            expected_hash="0" * 64,
            mime_type="image/png",
        )


def test_local_rag_asset_store_enforces_object_and_total_capacity(tmp_path):
    object_store = LocalRagAssetFileStore(
        tmp_path / "object",
        max_bytes=4,
        max_total_bytes=10,
    )
    content = b"12345"
    with pytest.raises(StorageCapacityExceeded) as object_error:
        object_store.write(
            asset_id=uuid4(),
            content=content,
            expected_hash=hashlib.sha256(content).hexdigest(),
            mime_type="image/png",
        )
    assert object_error.value.code == "RAG_ASSET_OBJECT_CAPACITY_EXCEEDED"

    total_store = LocalRagAssetFileStore(
        tmp_path / "total",
        max_bytes=6,
        max_total_bytes=10,
    )
    first = b"123456"
    second = b"abcde"
    total_store.write(
        asset_id=uuid4(),
        content=first,
        expected_hash=hashlib.sha256(first).hexdigest(),
        mime_type="image/png",
    )
    with pytest.raises(StorageCapacityExceeded) as total_error:
        total_store.write(
            asset_id=uuid4(),
            content=second,
            expected_hash=hashlib.sha256(second).hexdigest(),
            mime_type="image/png",
        )
    assert total_error.value.code == "RAG_ASSET_TOTAL_CAPACITY_EXCEEDED"
