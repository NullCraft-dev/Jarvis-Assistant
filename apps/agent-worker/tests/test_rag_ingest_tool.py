from __future__ import annotations

import asyncio
from dataclasses import dataclass
from uuid import UUID, uuid4

from jarvis_worker.agent.permissions.manager import PermissionManager
from jarvis_worker.agent.rag.contracts import RagDocumentStatus, RagIngestionStatus
from jarvis_worker.agent.rag.query import RagIngestionCompletion
from jarvis_worker.agent.tool_gateway.contracts import ToolRequest
from jarvis_worker.agent.tools.rag import (
    RagAwaitIngestionToolExecutor,
    RagIngestArtifactToolExecutor,
    create_rag_capability,
)


class _Bridge:
    def run(self, coroutine, *, timeout):
        assert timeout == 30
        return asyncio.run(coroutine)


@dataclass(frozen=True)
class _Result:
    document_id: UUID
    job_id: UUID
    status: RagIngestionStatus
    created: bool


class _Service:
    def __init__(self) -> None:
        self.calls = []

    async def enqueue_pdf_for_task(self, **kwargs):
        self.calls.append(kwargs)
        return _Result(uuid4(), uuid4(), RagIngestionStatus.QUEUED, True)


def test_rag_ingest_tool_derives_scope_from_task_and_returns_job() -> None:
    service = _Service()
    executor = RagIngestArtifactToolExecutor(service, _Bridge())
    task_id, artifact_id = uuid4(), uuid4()

    result = executor(
        ToolRequest(
            task_id=str(task_id),
            run_id=str(uuid4()),
            tool_name="rag.ingest_artifact",
            arguments={"artifact_id": str(artifact_id)},
        )
    )

    assert result.ok is True
    assert result.data["artifact_id"] == str(artifact_id)
    assert result.data["status"] == "queued"
    assert f"document_id={result.data['document_id']}" in result.summary
    assert f"job_id={result.data['job_id']}" in result.summary
    assert service.calls == [{"task_id": task_id, "source_artifact_id": artifact_id}]


def test_rag_ingest_tool_is_l2_and_never_accepts_workspace_id() -> None:
    def search_executor(_request):
        return None

    ingest_executor = RagIngestArtifactToolExecutor(_Service(), _Bridge())
    manifest = (
        create_rag_capability(
            search_executor,
            ingest_executor,
        )
        .tool_bindings[1]
        .manifest
    )
    decision = PermissionManager().check(
        manifest,
        ToolRequest(
            task_id=str(uuid4()),
            run_id=str(uuid4()),
            tool_name="rag.ingest_artifact",
            arguments={"artifact_id": str(uuid4())},
        ),
    )

    assert manifest.risk_level_default == "L2"
    assert "workspace_id" not in manifest.input_schema["properties"]
    assert decision.needs_user_approval is True


class _AwaitBridge:
    def run(self, coroutine, *, timeout):
        assert timeout == 910
        return asyncio.run(coroutine)


class _AwaitService:
    def __init__(self, completion):
        self.completion = completion
        self.calls = []

    async def wait_for_task_job(self, **kwargs):
        self.calls.append(kwargs)
        return self.completion


def test_rag_await_ingestion_returns_real_ready_counts() -> None:
    task_id, job_id, document_id = uuid4(), uuid4(), uuid4()
    service = _AwaitService(
        RagIngestionCompletion(
            job_id=job_id,
            document_id=document_id,
            status=RagIngestionStatus.COMPLETED,
            document_status=RagDocumentStatus.READY,
            chunk_count=189,
            embedding_completed=189,
        )
    )
    executor = RagAwaitIngestionToolExecutor(service, _AwaitBridge())

    result = executor(ToolRequest(
        task_id=str(task_id),
        run_id=str(uuid4()),
        tool_name="rag.await_ingestion",
        arguments={"job_id": str(job_id)},
    ))

    assert result.ok is True
    assert result.data["ready"] is True
    assert result.data["chunk_count"] == 189
    assert result.data["embedding_completed"] == 189
    assert service.calls == [{"task_id": task_id, "job_id": job_id}]


def test_rag_await_ingestion_manifest_is_l0_and_workspace_scoped() -> None:
    manifest = create_rag_capability(
        lambda _request: None,
        _Service(),
        RagAwaitIngestionToolExecutor(_AwaitService(None), _AwaitBridge()),
    ).tool_bindings[2].manifest

    assert manifest.name == "rag.await_ingestion"
    assert manifest.risk_level_default == "L0"
    assert manifest.permission_scope == "current_workspace_rag"
    assert list(manifest.input_schema["properties"]) == ["job_id"]
    decision = PermissionManager().check(
        manifest,
        ToolRequest(
            task_id=str(uuid4()),
            run_id=str(uuid4()),
            tool_name="rag.await_ingestion",
            arguments={"job_id": str(uuid4())},
        ),
    )
    assert decision.allowed is True
    assert decision.needs_user_approval is False
