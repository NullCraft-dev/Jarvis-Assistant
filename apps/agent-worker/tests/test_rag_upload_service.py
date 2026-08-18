import hashlib
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from jarvis_worker.agent.artifacts.file_store import LocalArtifactFileStore
from jarvis_worker.agent.rag.contracts import RagIngestionStatus
from jarvis_worker.agent.rag.ingestion import upload_service as upload_module
from jarvis_worker.agent.rag.ingestion.service import RagIngestionEnqueueResult
from jarvis_worker.agent.rag.ingestion.source import is_user_upload_artifact
from jarvis_worker.agent.rag.ingestion.upload_service import RagUploadApplicationService
from jarvis_worker.shared.domain.models import (
    Artifact,
    PermissionRequest,
    PermissionStatus,
    WorkspaceStatus,
)
from jarvis_worker.shared.errors.application import AppError


class _Ingestion:
    def __init__(self):
        self.enqueue_pdf = AsyncMock()


class _SessionContext:
    async def __aenter__(self):
        return object()

    async def __aexit__(self, *_args):
        return None


class _SessionFactory:
    def __call__(self):
        return _SessionContext()


class _MemoryRepo:
    def __init__(self, values=None):
        self.values = dict(values or {})

    async def get(self, item_id):
        return self.values.get(item_id)

    async def create(self, item):
        self.values[item.id] = item
        return item


class _PermissionRepo(_MemoryRepo):
    async def get_request(self, item_id):
        return await self.get(item_id)

    async def get_request_for_update(self, item_id):
        return await self.get(item_id)

    async def create_request(self, item):
        return await self.create(item)


class _EventRepo:
    def __init__(self):
        self.values = []

    async def get_next_sequence(self, _run_id):
        return len(self.values) + 1

    async def append(self, events):
        self.values.extend(events)


class _UploadUow:
    def __init__(self, workspace_id):
        workspace = SimpleNamespace(
            id=workspace_id,
            status=WorkspaceStatus.ACTIVE,
            canonical_path="/workspace",
        )
        self.workspaces = _MemoryRepo({workspace_id: workspace})
        self.permissions = _PermissionRepo()
        self.conversations = _MemoryRepo()
        self.tasks = _MemoryRepo()
        self.runs = _MemoryRepo()
        self.messages = _MemoryRepo()
        self.events = _EventRepo()
        self.audits = _MemoryRepo()

    def transaction(self):
        return self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return None

    async def flush(self):
        return None

    async def commit(self):
        return None


def test_upload_attempt_ids_are_stable_and_terminal_failures_are_not_reused() -> None:
    artifact_id = uuid4()
    first = upload_module._upload_attempt_ids(artifact_id, 1)
    retry = upload_module._upload_attempt_ids(artifact_id, 2)
    base = PermissionRequest(
        id=first[2],
        task_id=first[0],
        run_id=first[1],
        tool_name="rag.upload_pdf",
        action_summary="upload",
        risk_level="L2",
        scope={"type": "once"},
        arguments_summary={},
        allowed_decisions=["allow_once", "deny"],
    )

    assert first == upload_module._upload_attempt_ids(artifact_id, 1)
    assert retry == upload_module._upload_attempt_ids(artifact_id, 2)
    assert len(set(first + retry)) == 6
    base.status = PermissionStatus.PENDING
    assert upload_module._upload_permission_is_reusable(base) is True
    base.status = PermissionStatus.EXPIRED
    assert upload_module._upload_permission_is_reusable(base) is False
    base.status = PermissionStatus.DENIED
    assert upload_module._upload_permission_is_reusable(base) is False
    base.status = PermissionStatus.CONSUMED
    base.checkpoint = {"error_code": "RAG_UPLOAD_PDF_INVALID"}
    assert upload_module._upload_permission_is_reusable(base) is False


@pytest.mark.asyncio
async def test_create_upload_request_retries_terminal_attempt_and_reuses_active_retry(
    tmp_path,
    monkeypatch,
) -> None:
    workspace_id = uuid4()
    uow = _UploadUow(workspace_id)
    monkeypatch.setattr(upload_module, "PostgresUnitOfWork", lambda _session: uow)
    service = RagUploadApplicationService(
        lambda: _SessionFactory(),
        artifact_file_store=LocalArtifactFileStore(tmp_path, max_bytes=1024),
        ingestion_service=_Ingestion(),
    )
    payload = {
        "workspace_id": workspace_id,
        "filename": "paper.pdf",
        "size_bytes": 12,
        "content_sha256": "a" * 64,
    }

    first = await service.create_upload_request(**payload)
    first.status = PermissionStatus.EXPIRED
    retry = await service.create_upload_request(**payload)
    repeated = await service.create_upload_request(**payload)

    assert retry.id != first.id
    assert retry.task_id != first.task_id
    assert retry.run_id != first.run_id
    assert retry.checkpoint["attempt"] == 2
    assert retry.checkpoint["root_request_id"] == str(first.id)
    assert repeated.id == retry.id


@pytest.mark.asyncio
async def test_upload_writes_controlled_artifact_and_enqueues(tmp_path):
    workspace_id = uuid4()
    request_id = uuid4()
    task_id = uuid4()
    run_id = uuid4()
    ingestion = _Ingestion()
    ingestion.enqueue_pdf.return_value = RagIngestionEnqueueResult(
        document_id=uuid4(),
        job_id=uuid4(),
        status=RagIngestionStatus.QUEUED,
        created=True,
    )
    service = RagUploadApplicationService(
        lambda: None,
        artifact_file_store=LocalArtifactFileStore(tmp_path, max_bytes=1024),
        ingestion_service=ingestion,
    )
    service._get_artifact = AsyncMock(return_value=None)
    service._persist_upload_operation = AsyncMock()
    service._validate_upload_permission = AsyncMock(
        return_value=type(
            "Permission",
            (),
            {
                "id": request_id,
                "task_id": task_id,
                "run_id": run_id,
                "status": PermissionStatus.APPROVED,
            },
        )()
    )
    service._consume_upload_permission = AsyncMock()

    result = await service.upload_pdf(
        workspace_id=workspace_id,
        filename="../paper.pdf",
        content=b"%PDF-1.7\nfixture",
        permission_request_id=request_id,
    )

    assert result.uploaded is True
    stored = list(tmp_path.rglob("*.pdf"))
    assert len(stored) == 1
    assert stored[0].name == f"{result.artifact_id}.pdf"
    persisted = service._persist_upload_operation.await_args.kwargs
    assert persisted["title"] == "paper.pdf"
    assert persisted["task_id"] == task_id
    assert persisted["run_id"] == run_id
    assert persisted["permission_request_id"] == request_id
    ingestion.enqueue_pdf.assert_awaited_once_with(
        workspace_id=workspace_id,
        source_artifact_id=result.artifact_id,
    )
    service._consume_upload_permission.assert_awaited_once_with(request_id, result.artifact_id)


@pytest.mark.asyncio
async def test_upload_rejects_non_pdf_before_writing(tmp_path):
    service = RagUploadApplicationService(
        lambda: None,
        artifact_file_store=LocalArtifactFileStore(tmp_path, max_bytes=1024),
        ingestion_service=_Ingestion(),
    )
    request_id = uuid4()
    service._validate_upload_permission = AsyncMock(
        return_value=SimpleNamespace(
            id=request_id,
            task_id=uuid4(),
            run_id=uuid4(),
            status=PermissionStatus.APPROVED,
        )
    )
    service._get_artifact = AsyncMock(return_value=None)
    service._fail_upload_permission = AsyncMock()
    with pytest.raises(AppError) as error:
        await service.upload_pdf(
            workspace_id=uuid4(),
            filename="notes.pdf",
            content=b"not a pdf",
            permission_request_id=request_id,
        )
    assert error.value.code == "RAG_UPLOAD_PDF_INVALID"
    assert list(tmp_path.rglob("*.pdf")) == []
    service._fail_upload_permission.assert_awaited_once_with(
        request_id,
        code="RAG_UPLOAD_PDF_INVALID",
        message="上传内容不是有效 PDF",
    )


@pytest.mark.asyncio
async def test_upload_maps_total_capacity_failure_to_stable_app_error(tmp_path):
    store = LocalArtifactFileStore(
        tmp_path,
        max_bytes=16,
        max_run_bytes=16,
        max_workspace_bytes=16,
        max_total_bytes=16,
    )
    store.write_bytes(
        uuid4(),
        b"%PDF-1.7\n123456",
        run_id=uuid4(),
        workspace_id=uuid4(),
        suffix=".pdf",
        mime_type="application/pdf",
    )
    ingestion = _Ingestion()
    service = RagUploadApplicationService(
        lambda: None,
        artifact_file_store=store,
        ingestion_service=ingestion,
    )
    service._get_artifact = AsyncMock(return_value=None)
    service._persist_upload_operation = AsyncMock()
    request_id = uuid4()
    task_id = uuid4()
    run_id = uuid4()
    service._validate_upload_permission = AsyncMock(
        return_value=type(
            "Permission",
            (),
            {
                "id": request_id,
                "task_id": task_id,
                "run_id": run_id,
                "status": PermissionStatus.APPROVED,
            },
        )()
    )
    service._consume_upload_permission = AsyncMock()

    with pytest.raises(AppError) as error:
        await service.upload_pdf(
            workspace_id=uuid4(),
            filename="paper.pdf",
            content=b"%PDF-1.7\nx",
            permission_request_id=request_id,
        )

    assert error.value.code == "ARTIFACT_TOTAL_CAPACITY_EXCEEDED"
    assert error.value.category == "storage"
    assert error.value.recoverable is True
    service._persist_upload_operation.assert_not_awaited()
    ingestion.enqueue_pdf.assert_not_awaited()


def test_user_upload_source_marker_requires_explicit_runtime_lineage():
    artifact = Artifact(
        id=uuid4(),
        task_id=uuid4(),
        run_id=uuid4(),
        kind="file",
        title="paper.pdf",
        purpose="deliverable",
        producer_type="runtime",
        file_path="aa/paper.pdf",
        file_size_bytes=10,
        mime_type="application/pdf",
        content_hash="a" * 64,
        metadata={"storage": "local_file", "source": "user_upload", "explicit_user_action": True},
    )
    assert is_user_upload_artifact(artifact) is True
    artifact.metadata["explicit_user_action"] = False
    assert is_user_upload_artifact(artifact) is False


@pytest.mark.asyncio
async def test_approved_upload_permission_is_bound_to_exact_file_metadata(tmp_path, monkeypatch):
    workspace_id = uuid4()
    request = PermissionRequest(
        id=uuid4(),
        task_id=uuid4(),
        run_id=uuid4(),
        tool_name="rag.upload_pdf",
        action_summary="upload",
        risk_level="L2",
        scope={"type": "once"},
        arguments_summary={},
        checkpoint={
            "version": 1,
            "action": "rag_upload_pdf",
            "workspace_id": str(workspace_id),
            "filename": "paper.pdf",
            "size_bytes": 8,
            "sha256": "a" * 64,
        },
        status=PermissionStatus.APPROVED,
        decision="allow_once",
    )
    permissions = SimpleNamespace(get_request=AsyncMock(return_value=request))
    monkeypatch.setattr(
        upload_module,
        "PostgresUnitOfWork",
        lambda _session: SimpleNamespace(permissions=permissions),
    )
    service = RagUploadApplicationService(
        lambda: _SessionFactory(),
        artifact_file_store=LocalArtifactFileStore(tmp_path, max_bytes=1024),
        ingestion_service=_Ingestion(),
    )

    with pytest.raises(AppError) as error:
        await service._validate_upload_permission(
            permission_request_id=request.id,
            workspace_id=workspace_id,
            filename="different.pdf",
            size_bytes=8,
            content_sha256="a" * 64,
        )

    assert error.value.code == "RAG_UPLOAD_PERMISSION_MISMATCH"


@pytest.mark.asyncio
async def test_consumed_permission_allows_existing_content_under_renamed_client_file(
    tmp_path, monkeypatch
):
    workspace_id = uuid4()
    content = b"%PDF-1.7"
    digest = hashlib.sha256(content).hexdigest()
    artifact_id = upload_module.uuid5(
        upload_module.NAMESPACE_URL,
        f"jarvis:rag-user-upload:{workspace_id}:{digest}",
    )
    request = PermissionRequest(
        id=uuid4(),
        task_id=uuid4(),
        run_id=uuid4(),
        tool_name="rag.upload_pdf",
        action_summary="upload",
        risk_level="L2",
        scope={"type": "once"},
        arguments_summary={},
        checkpoint={
            "version": 1,
            "action": "rag_upload_pdf",
            "artifact_id": str(artifact_id),
            "workspace_id": str(workspace_id),
            "filename": "original.pdf",
            "size_bytes": len(content),
            "sha256": digest,
        },
        status=PermissionStatus.CONSUMED,
        decision="allow_once",
    )
    permissions = SimpleNamespace(get_request=AsyncMock(return_value=request))
    monkeypatch.setattr(
        upload_module,
        "PostgresUnitOfWork",
        lambda _session: SimpleNamespace(permissions=permissions),
    )
    service = RagUploadApplicationService(
        lambda: _SessionFactory(),
        artifact_file_store=LocalArtifactFileStore(tmp_path, max_bytes=1024),
        ingestion_service=_Ingestion(),
    )

    validated = await service._validate_upload_permission(
        permission_request_id=request.id,
        workspace_id=workspace_id,
        filename="renamed.pdf",
        size_bytes=len(content),
        content_sha256=digest,
        allow_consumed_filename_alias=True,
    )

    assert validated is request


@pytest.mark.asyncio
async def test_consumed_permission_does_not_allow_renamed_file_without_existing_artifact(
    tmp_path, monkeypatch
):
    workspace_id = uuid4()
    content = b"%PDF-1.7"
    digest = hashlib.sha256(content).hexdigest()
    artifact_id = upload_module.uuid5(
        upload_module.NAMESPACE_URL,
        f"jarvis:rag-user-upload:{workspace_id}:{digest}",
    )
    request = PermissionRequest(
        id=uuid4(),
        task_id=uuid4(),
        run_id=uuid4(),
        tool_name="rag.upload_pdf",
        action_summary="upload",
        risk_level="L2",
        scope={"type": "once"},
        arguments_summary={},
        checkpoint={
            "version": 1,
            "action": "rag_upload_pdf",
            "artifact_id": str(artifact_id),
            "workspace_id": str(workspace_id),
            "filename": "original.pdf",
            "size_bytes": len(content),
            "sha256": digest,
        },
        status=PermissionStatus.CONSUMED,
        decision="allow_once",
    )
    permissions = SimpleNamespace(get_request=AsyncMock(return_value=request))
    monkeypatch.setattr(
        upload_module,
        "PostgresUnitOfWork",
        lambda _session: SimpleNamespace(permissions=permissions),
    )
    service = RagUploadApplicationService(
        lambda: _SessionFactory(),
        artifact_file_store=LocalArtifactFileStore(tmp_path, max_bytes=1024),
        ingestion_service=_Ingestion(),
    )

    with pytest.raises(AppError) as error:
        await service._validate_upload_permission(
            permission_request_id=request.id,
            workspace_id=workspace_id,
            filename="renamed.pdf",
            size_bytes=len(content),
            content_sha256=digest,
            allow_consumed_filename_alias=False,
        )

    assert error.value.code == "RAG_UPLOAD_PERMISSION_MISMATCH"


@pytest.mark.asyncio
async def test_pending_or_denied_upload_permission_cannot_write(tmp_path, monkeypatch):
    workspace_id = uuid4()
    request = PermissionRequest(
        id=uuid4(),
        task_id=uuid4(),
        run_id=uuid4(),
        tool_name="rag.upload_pdf",
        action_summary="upload",
        risk_level="L2",
        scope={"type": "once"},
        arguments_summary={},
        status=PermissionStatus.DENIED,
        decision="deny",
    )
    permissions = SimpleNamespace(get_request=AsyncMock(return_value=request))
    monkeypatch.setattr(
        upload_module,
        "PostgresUnitOfWork",
        lambda _session: SimpleNamespace(permissions=permissions),
    )
    service = RagUploadApplicationService(
        lambda: _SessionFactory(),
        artifact_file_store=LocalArtifactFileStore(tmp_path, max_bytes=1024),
        ingestion_service=_Ingestion(),
    )
    service._get_artifact = AsyncMock()

    with pytest.raises(AppError) as error:
        await service.upload_pdf(
            workspace_id=workspace_id,
            filename="paper.pdf",
            content=b"%PDF-1.7",
            permission_request_id=request.id,
        )

    assert error.value.code == "PERMISSION_DENIED"
    service._get_artifact.assert_not_awaited()
    assert list(tmp_path.rglob("*.pdf")) == []


@pytest.mark.asyncio
async def test_existing_content_alias_is_only_resolved_after_exact_permission_mismatch(tmp_path):
    workspace_id = uuid4()
    request_id = uuid4()
    content = b"%PDF-1.7\nexisting"
    digest = hashlib.sha256(content).hexdigest()
    artifact = SimpleNamespace(id=uuid4())
    ingestion = _Ingestion()
    ingestion.enqueue_pdf.return_value = RagIngestionEnqueueResult(
        document_id=uuid4(),
        job_id=uuid4(),
        status=RagIngestionStatus.COMPLETED,
        created=False,
    )
    service = RagUploadApplicationService(
        lambda: None,
        artifact_file_store=LocalArtifactFileStore(tmp_path, max_bytes=1024),
        ingestion_service=ingestion,
    )
    service._validate_upload_permission = AsyncMock(
        side_effect=[
            AppError(
                "RAG_UPLOAD_PERMISSION_MISMATCH",
                "上传文件与已批准的文件摘要不一致",
                "permission",
            ),
            SimpleNamespace(id=request_id, status=PermissionStatus.CONSUMED),
        ]
    )
    service._get_artifact = AsyncMock(return_value=artifact)
    service._validate_existing = AsyncMock()
    service._consume_upload_permission = AsyncMock()

    result = await service.upload_pdf(
        workspace_id=workspace_id,
        filename="renamed.pdf",
        content=content,
        permission_request_id=request_id,
    )

    assert result.uploaded is False
    assert service._validate_upload_permission.await_count == 2
    first, second = service._validate_upload_permission.await_args_list
    assert "allow_consumed_filename_alias" not in first.kwargs
    assert second.kwargs["allow_consumed_filename_alias"] is True
    service._get_artifact.assert_awaited_once()
    service._validate_existing.assert_awaited_once_with(
        artifact, workspace_id, digest, len(content)
    )


@pytest.mark.asyncio
async def test_consumed_permission_never_recreates_a_missing_artifact(tmp_path):
    workspace_id = uuid4()
    request_id = uuid4()
    ingestion = _Ingestion()
    service = RagUploadApplicationService(
        lambda: None,
        artifact_file_store=LocalArtifactFileStore(tmp_path, max_bytes=1024),
        ingestion_service=ingestion,
    )
    service._validate_upload_permission = AsyncMock(
        return_value=SimpleNamespace(id=request_id, status=PermissionStatus.CONSUMED)
    )
    service._get_artifact = AsyncMock(return_value=None)

    with pytest.raises(AppError) as error:
        await service.upload_pdf(
            workspace_id=workspace_id,
            filename="paper.pdf",
            content=b"%PDF-1.7\nmissing",
            permission_request_id=request_id,
        )

    assert error.value.code == "RAG_UPLOAD_INTEGRITY_ERROR"
    assert list(tmp_path.rglob("*.pdf")) == []
    ingestion.enqueue_pdf.assert_not_awaited()
