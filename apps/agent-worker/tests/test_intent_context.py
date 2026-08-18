from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from uuid import uuid4

import pytest

import jarvis_worker.agent.intents.context as context_module
from jarvis_worker.agent.intents import PostgresIntentContextProvider
from jarvis_worker.agent.rag.contracts import RagDocument, RagDocumentStatus
from jarvis_worker.shared.domain.models import WorkspaceStatus


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
    def __init__(self, values):
        self.values = values

    async def list_by_workspace(self, *, workspace_id, include_disabled, limit):
        assert include_disabled is False
        return [value for value in self.values if value.workspace_id == workspace_id][:limit]


class _Chunks:
    def __init__(self, values):
        self.values = values

    async def list_identity_chunks(self, *, workspace_id, document_ids):
        return [
            value
            for value in self.values
            if value.workspace_id == workspace_id and value.document_id in document_ids
        ]


def _document(workspace_id, title, status, created_at):
    return RagDocument(
        id=uuid4(),
        workspace_id=workspace_id,
        source_artifact_id=uuid4(),
        title=title,
        mime_type="application/pdf",
        source_content_hash="a" * 64,
        ingestion_policy_version="rag-v1",
        status=status,
        created_at=created_at,
        updated_at=created_at,
    )


def _provider(monkeypatch, uow):
    monkeypatch.setattr(context_module, "PostgresUnitOfWork", lambda _session: uow)
    return PostgresIntentContextProvider(lambda: lambda: _Session(), async_bridge=None)


@pytest.mark.asyncio
async def test_intent_catalog_is_task_workspace_scoped_ready_only_and_anonymous(monkeypatch):
    now = datetime(2026, 7, 30, tzinfo=timezone.utc)
    task_id, workspace_id, other_workspace_id = uuid4(), uuid4(), uuid4()
    older = _document(workspace_id, "旧资料", RagDocumentStatus.READY, now)
    newer = _document(workspace_id, "新资料", RagDocumentStatus.READY, now + timedelta(hours=1))
    indexing = _document(
        workspace_id,
        "尚未完成资料",
        RagDocumentStatus.INDEXING,
        now + timedelta(hours=2),
    )
    foreign = _document(
        other_workspace_id,
        "其他工作区资料",
        RagDocumentStatus.READY,
        now + timedelta(hours=3),
    )
    uow = SimpleNamespace(
        tasks=_Repo({task_id: SimpleNamespace(workspace_id=workspace_id)}),
        workspaces=_Repo({workspace_id: SimpleNamespace(status=WorkspaceStatus.ACTIVE)}),
        rag_documents=_Documents([older, newer, indexing, foreign]),
        rag_chunks=_Chunks(
            [
                SimpleNamespace(
                    workspace_id=workspace_id,
                    document_id=newer.id,
                    content="  New   Paper\nidentity  ",
                ),
                SimpleNamespace(
                    workspace_id=workspace_id,
                    document_id=older.id,
                    content="Old Paper identity",
                ),
            ]
        ),
    )

    context = await _provider(monkeypatch, uow)._load(task_id)

    assert [item.key for item in context.documents] == ["doc_1", "doc_2"]
    assert [item.title for item in context.documents] == ["新资料", "旧资料"]
    assert [item.document_id for item in context.documents] == [str(newer.id), str(older.id)]
    assert [item.identity_excerpt for item in context.documents] == [
        "New Paper identity",
        "Old Paper identity",
    ]
    assert all(item.title != "其他工作区资料" for item in context.documents)
    prompt_values = [item.to_prompt_dict() for item in context.documents]
    assert all("document_id" not in item for item in prompt_values)


@pytest.mark.asyncio
async def test_intent_catalog_fails_closed_for_revoked_workspace(monkeypatch):
    task_id, workspace_id = uuid4(), uuid4()
    uow = SimpleNamespace(
        tasks=_Repo({task_id: SimpleNamespace(workspace_id=workspace_id)}),
        workspaces=_Repo({workspace_id: SimpleNamespace(status=WorkspaceStatus.REVOKED)}),
        rag_documents=_Documents([]),
        rag_chunks=_Chunks([]),
    )

    context = await _provider(monkeypatch, uow)._load(task_id)

    assert context.documents == ()
