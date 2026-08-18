from datetime import datetime, timezone
from types import SimpleNamespace
from uuid import uuid4

import pytest

import jarvis_worker.agent.rag.query.service as service_module
from jarvis_worker.agent.rag.contracts import (
    RagDocument,
    RagDocumentStatus,
    RagIngestionJob,
    RagIngestionStatus,
)
from jarvis_worker.agent.rag.query import RagDocumentQueryService
from jarvis_worker.agent.rag.index_policy import RagIndexTarget
from jarvis_worker.shared.domain.models import WorkspaceStatus
from jarvis_worker.shared.errors.application import AppError

NOW = datetime(2026, 7, 28, tzinfo=timezone.utc)


class _Session:
    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, traceback):
        return None


class _Repo:
    def __init__(self, values):
        self.values = values

    async def get(self, value_id):
        return self.values.get(value_id)


class _Documents:
    def __init__(self, documents):
        self.documents = documents

    async def list_by_workspace(self, *, workspace_id, include_disabled, limit):
        return [
            document
            for document in self.documents
            if document.workspace_id == workspace_id
            and (include_disabled or document.status is not RagDocumentStatus.DISABLED)
        ][:limit]


class _Jobs:
    def __init__(self, jobs):
        self.jobs = jobs

    async def list_latest_by_documents(self, *, workspace_id, document_ids):
        return [
            job
            for job in self.jobs
            if job.workspace_id == workspace_id and job.document_id in document_ids
        ]


def _service(monkeypatch, uow):
    monkeypatch.setattr(service_module, "PostgresUnitOfWork", lambda session: uow)
    return RagDocumentQueryService(
        lambda: lambda: _Session(),
        index_target=RagIndexTarget(
            ingestion_policy_version="rag-v1",
            parser_version="pymupdf-v1",
            chunker_version="structure-v1",
            embedding_provider="openai",
            embedding_model="text-embedding-3-small",
            embedding_dimensions=1536,
        ),
    )


@pytest.mark.asyncio
async def test_lists_documents_with_latest_job_inside_workspace(monkeypatch):
    workspace_id = uuid4()
    document = RagDocument(
        id=uuid4(),
        workspace_id=workspace_id,
        source_artifact_id=uuid4(),
        title="Attention Is All You Need",
        mime_type="application/pdf",
        source_content_hash="a" * 64,
        ingestion_policy_version="rag-v1",
        status=RagDocumentStatus.READY,
        parser_version="pymupdf-v1",
        chunker_version="structure-v1",
        embedding_provider="openai",
        embedding_model="text-embedding-3-small",
        embedding_dimensions=1536,
        chunk_count=48,
        created_at=NOW,
        updated_at=NOW,
    )
    job = RagIngestionJob(
        id=uuid4(),
        document_id=document.id,
        workspace_id=workspace_id,
        idempotency_key="b" * 64,
        ingestion_policy_version="rag-v1",
        status=RagIngestionStatus.COMPLETED,
        attempts=1,
        embedding_attempts=1,
        created_at=NOW,
        updated_at=NOW,
    )
    uow = SimpleNamespace(
        workspaces=_Repo({workspace_id: SimpleNamespace(status=WorkspaceStatus.ACTIVE)}),
        rag_documents=_Documents([document]),
        rag_ingestion_jobs=_Jobs([job]),
    )

    items = await _service(monkeypatch, uow).list_documents(workspace_id=workspace_id)

    assert len(items) == 1
    assert items[0].document.chunk_count == 48
    assert items[0].latest_job is job
    assert items[0].index_freshness.state == "current"

    document.embedding_model = "outdated-model"
    stale = await _service(monkeypatch, uow).list_documents(workspace_id=workspace_id)
    assert stale[0].index_freshness.state == "stale"
    assert stale[0].index_freshness.stale_reasons == ("embedding_model",)


@pytest.mark.asyncio
async def test_rejects_missing_or_revoked_workspace(monkeypatch):
    workspace_id = uuid4()
    uow = SimpleNamespace(
        workspaces=_Repo({workspace_id: SimpleNamespace(status=WorkspaceStatus.REVOKED)}),
        rag_documents=_Documents([]),
        rag_ingestion_jobs=_Jobs([]),
    )

    with pytest.raises(AppError) as error:
        await _service(monkeypatch, uow).list_documents(workspace_id=workspace_id)

    assert error.value.code == "WORKSPACE_NOT_FOUND"
