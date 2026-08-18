from types import SimpleNamespace
from uuid import uuid4

import pytest

import jarvis_worker.agent.rag.query.ingestion_status as monitor_module
from jarvis_worker.agent.rag.contracts import (
    RagDocumentStatus,
    RagIngestionStatus,
    RagJobProgress,
)
from jarvis_worker.agent.rag.query import (
    RagIngestionMonitorError,
    RagIngestionMonitorService,
)


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


def _service(monkeypatch, *, task, job, document):
    uow = SimpleNamespace(
        tasks=_Repo({task.id: task}),
        rag_ingestion_jobs=_Repo({job.id: job}),
        rag_documents=_Repo({document.id: document}),
    )
    monkeypatch.setattr(monitor_module, "PostgresUnitOfWork", lambda _session: uow)
    return RagIngestionMonitorService(
        lambda: lambda: _Session(),
        max_wait_seconds=0.01,
        poll_interval_seconds=0.001,
    )


@pytest.mark.asyncio
async def test_wait_returns_only_real_completed_ready_state(monkeypatch):
    workspace_id, task_id, job_id, document_id = uuid4(), uuid4(), uuid4(), uuid4()
    task = SimpleNamespace(id=task_id, workspace_id=workspace_id)
    job = SimpleNamespace(
        id=job_id,
        document_id=document_id,
        workspace_id=workspace_id,
        status=RagIngestionStatus.COMPLETED,
        progress=RagJobProgress(embedding_total=42, embedding_completed=42),
    )
    document = SimpleNamespace(
        id=document_id,
        workspace_id=workspace_id,
        status=RagDocumentStatus.READY,
        chunk_count=42,
    )

    result = await _service(monkeypatch, task=task, job=job, document=document).wait_for_task_job(
        task_id=task_id, job_id=job_id
    )

    assert result.ready is True
    assert result.chunk_count == 42
    assert result.embedding_completed == 42


@pytest.mark.asyncio
async def test_wait_rejects_cross_workspace_job(monkeypatch):
    workspace_id, task_id, job_id, document_id = uuid4(), uuid4(), uuid4(), uuid4()
    task = SimpleNamespace(id=task_id, workspace_id=workspace_id)
    job = SimpleNamespace(
        id=job_id,
        document_id=document_id,
        workspace_id=uuid4(),
        status=RagIngestionStatus.QUEUED,
        progress=RagJobProgress(),
    )
    document = SimpleNamespace(
        id=document_id,
        workspace_id=job.workspace_id,
        status=RagDocumentStatus.INDEXING,
        chunk_count=0,
    )

    with pytest.raises(RagIngestionMonitorError) as exc:
        await _service(monkeypatch, task=task, job=job, document=document).wait_for_task_job(
            task_id=task_id, job_id=job_id
        )

    assert exc.value.code == "RAG_JOB_NOT_FOUND"
    assert exc.value.recoverable is False
