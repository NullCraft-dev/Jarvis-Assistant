"""Trusted Task/Workspace document catalog for LLM intent resolution."""

from __future__ import annotations

import re
from uuid import UUID

from jarvis_worker.agent.intents.contracts import (
    IntentDocument,
    IntentRuntimeContext,
)
from jarvis_worker.agent.rag.contracts import RagDocumentStatus
from jarvis_worker.database.unit_of_work import PostgresUnitOfWork
from jarvis_worker.shared.domain.models import WorkspaceStatus

_MAX_DOCUMENTS = 50
_MAX_IDENTITY_EXCERPT_CHARS = 600


class PostgresIntentContextProvider:
    """Expose bounded anonymous document keys; never expose UUIDs to the LLM."""

    def __init__(self, uow_factory, async_bridge) -> None:
        self._uow_factory = uow_factory
        self._bridge = async_bridge

    def load(self, task_id: str) -> IntentRuntimeContext:
        return self._bridge.run(self._load(UUID(task_id)), timeout=10)

    async def _load(self, task_id: UUID) -> IntentRuntimeContext:
        session_factory = self._uow_factory()
        async with session_factory() as session:
            uow = PostgresUnitOfWork(session)
            task = await uow.tasks.get(task_id)
            if task is None or task.workspace_id is None:
                return IntentRuntimeContext()
            workspace = await uow.workspaces.get(task.workspace_id)
            if workspace is None or workspace.status is not WorkspaceStatus.ACTIVE:
                return IntentRuntimeContext()
            documents = await uow.rag_documents.list_by_workspace(
                workspace_id=task.workspace_id,
                include_disabled=False,
                limit=100,
            )
            ready = [item for item in documents if item.status is RagDocumentStatus.READY]
            ready.sort(key=lambda item: (item.created_at, str(item.id)), reverse=True)
            selected = ready[:_MAX_DOCUMENTS]
            identity_chunks = await uow.rag_chunks.list_identity_chunks(
                workspace_id=task.workspace_id,
                document_ids=[document.id for document in selected],
            )
            identity_by_document = {
                chunk.document_id: _identity_excerpt(chunk.content)
                for chunk in identity_chunks
            }
            return IntentRuntimeContext(
                tuple(
                    IntentDocument(
                        key=f"doc_{index}",
                        document_id=str(document.id),
                        title=document.title[:500],
                        created_at=document.created_at.isoformat(),
                        identity_excerpt=identity_by_document.get(document.id, ""),
                    )
                    for index, document in enumerate(selected, 1)
                )
            )


def _identity_excerpt(value: str) -> str:
    """Create a bounded identity hint from persisted, trusted-scope RAG text."""

    return re.sub(r"\s+", " ", value).strip()[:_MAX_IDENTITY_EXCERPT_CHARS]
