from datetime import datetime, timezone
from types import SimpleNamespace
from uuid import uuid4

import pytest

from jarvis_worker.agent.rag.contracts import RagDocument, RagDocumentStatus
from jarvis_worker.agent.rag.lifecycle import service as lifecycle_module
from jarvis_worker.agent.rag.lifecycle import RagDocumentLifecycleService
from jarvis_worker.shared.domain.models import Artifact, PermissionStatus, Workspace

NOW = datetime(2026, 7, 29, tzinfo=timezone.utc)


class _Session:
    async def __aenter__(self): return self
    async def __aexit__(self, *_args): return None


class _Repo:
    def __init__(self, items=()): self.items = {item.id: item for item in items}
    async def get(self, item_id): return self.items.get(item_id)
    async def create(self, item): self.items[item.id] = item; return item


class _Documents(_Repo):
    async def delete(self, *, workspace_id, document_id):
        document = self.items.get(document_id)
        if document is None or document.workspace_id != workspace_id: return False
        del self.items[document_id]
        return True


class _Permissions(_Repo):
    async def get_request(self, item_id): return await self.get(item_id)
    async def get_request_for_update(self, item_id): return await self.get(item_id)
    async def create_request(self, item): return await self.create(item)
    async def update_request(self, item): self.items[item.id] = item


class _Jobs:
    async def list_latest_by_documents(self, **_kwargs): return []


class _Assets:
    async def list_by_document(self, **_kwargs):
        return [SimpleNamespace(storage_reference="ab/asset.png")]


class _Audits:
    def __init__(self): self.items = []
    async def create(self, item): self.items.append(item); return item


class _Uow:
    def __init__(self, workspace, artifact, document):
        self.workspaces = _Repo([workspace])
        self.artifacts = _Repo([artifact])
        self.rag_documents = _Documents([document])
        self.rag_ingestion_jobs = _Jobs()
        self.rag_assets = _Assets()
        self.permissions = _Permissions()
        self.audits = _Audits()
    def transaction(self): return self
    async def __aenter__(self): return self
    async def __aexit__(self, *_args): return None
    async def commit(self): return None


class _Files:
    def __init__(self): self.deleted = []
    def delete_reference(self, reference): self.deleted.append(reference)


def _fixture(monkeypatch):
    workspace = Workspace(id=uuid4(), name="test", root_path="/tmp/test", canonical_path="/tmp/test")
    artifact = Artifact(id=uuid4(), task_id=uuid4(), run_id=uuid4(), kind="file", title="paper.pdf", purpose="rag", producer_type="user")
    document = RagDocument(
        id=uuid4(), workspace_id=workspace.id, source_artifact_id=artifact.id,
        title="paper.pdf", mime_type="application/pdf", source_content_hash="a" * 64,
        ingestion_policy_version="rag-v1", status=RagDocumentStatus.READY,
    )
    uow, files = _Uow(workspace, artifact, document), _Files()
    monkeypatch.setattr(lifecycle_module, "PostgresUnitOfWork", lambda _session: uow)
    return RagDocumentLifecycleService(lambda: lambda: _Session(), asset_file_store=files, now=lambda: NOW), uow, files, document


@pytest.mark.asyncio
async def test_delete_requires_l4_confirmation_and_retains_source_artifact(monkeypatch):
    service, uow, files, document = _fixture(monkeypatch)
    request = await service.create_delete_request(
        workspace_id=document.workspace_id, document_id=document.id, expected_version=document.version,
    )
    assert request.risk_level == "L4"
    assert request.allowed_decisions == ["allow_once", "deny"]
    assert document.id in uow.rag_documents.items

    result = await service.resolve_delete_request(request.id, "allow_once")

    assert result.deleted is True
    assert result.source_artifact_retained is True
    assert document.id not in uow.rag_documents.items
    assert document.source_artifact_id in uow.artifacts.items
    assert files.deleted == ["ab/asset.png"]
    assert result.request.status is PermissionStatus.CONSUMED
    assert uow.audits.items[-1].event_type == "rag.document.delete.cleanup_completed"


@pytest.mark.asyncio
async def test_denied_delete_is_audited_without_mutation(monkeypatch):
    service, uow, files, document = _fixture(monkeypatch)
    request = await service.create_delete_request(
        workspace_id=document.workspace_id, document_id=document.id, expected_version=document.version,
    )
    result = await service.resolve_delete_request(request.id, "deny", "keep it")
    assert result.deleted is False
    assert document.id in uow.rag_documents.items
    assert files.deleted == []
    assert uow.audits.items[-1].permission_decision == "deny"
