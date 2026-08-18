"""RAG 文档与最近入库作业的只读应用层聚合。"""

from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

from jarvis_worker.agent.rag.contracts import RagDocument, RagIngestionJob
from jarvis_worker.agent.rag.index_policy import (
    RagIndexFreshness,
    RagIndexTarget,
    assess_index_freshness,
)
from jarvis_worker.database.unit_of_work import PostgresUnitOfWork
from jarvis_worker.shared.domain.models import WorkspaceStatus
from jarvis_worker.shared.errors.application import AppError


@dataclass(frozen=True)
class RagDocumentQueryItem:
    document: RagDocument
    latest_job: RagIngestionJob | None
    index_freshness: RagIndexFreshness


class RagDocumentQueryService:
    """按 Workspace 边界返回 RAG 文档管理读模型。"""

    def __init__(self, uow_factory, *, index_target: RagIndexTarget | None = None):
        self._uow_factory = uow_factory
        self._index_target = index_target or RagIndexTarget.current()

    async def list_documents(
        self,
        *,
        workspace_id: UUID,
        include_disabled: bool = False,
        limit: int = 100,
    ) -> list[RagDocumentQueryItem]:
        session_factory = self._uow_factory()
        async with session_factory() as session:
            uow = PostgresUnitOfWork(session)
            workspace = await uow.workspaces.get(workspace_id)
            if workspace is None or workspace.status is not WorkspaceStatus.ACTIVE:
                raise AppError(
                    code="WORKSPACE_NOT_FOUND",
                    message="工作区不存在或已撤销",
                    category="validation",
                )
            documents = await uow.rag_documents.list_by_workspace(
                workspace_id=workspace_id,
                include_disabled=include_disabled,
                limit=min(max(limit, 1), 100),
            )
            jobs = await uow.rag_ingestion_jobs.list_latest_by_documents(
                workspace_id=workspace_id,
                document_ids=[document.id for document in documents],
            )
            latest_by_document = {job.document_id: job for job in jobs}
            return [
                RagDocumentQueryItem(
                    document=document,
                    latest_job=latest_by_document.get(document.id),
                    index_freshness=assess_index_freshness(document, self._index_target),
                )
                for document in documents
            ]
