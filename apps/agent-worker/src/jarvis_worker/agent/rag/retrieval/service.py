"""可信 Task/Workspace 入口与可插拔 RAG Pipeline 装配。"""

from __future__ import annotations

from uuid import UUID

from jarvis_worker.agent.rag.reranking import (
    CompositeReranker,
    FeatureReranker,
    HardFilter,
    PolicySelector,
    QuotaAwareMmrReranker,
    SemanticReranker,
)
from jarvis_worker.agent.rag.retrieval.contracts import (
    RagContextPackage,
    RagRetrievalQuery,
)
from jarvis_worker.agent.rag.retrieval.pipeline import (
    BoundedQueryPlanner,
    EvidenceContextAssembler,
    HybridRrfCandidateRetriever,
    PgVectorCandidateRetriever,
    PostgresKeywordCandidateRetriever,
    RagRetrievalPipeline,
)
from jarvis_worker.database.unit_of_work import PostgresUnitOfWork
from jarvis_worker.shared.errors.application import AppError


class RagRetrievalService:
    def __init__(
        self,
        uow_factory,
        *,
        embedding_provider=None,
        reranker_provider=None,
        pipeline: RagRetrievalPipeline | None = None,
    ) -> None:
        if pipeline is None:
            if embedding_provider is None:
                raise ValueError("默认 RAG Pipeline 必须提供 embedding_provider")
            stages = [HardFilter(), FeatureReranker()]
            if reranker_provider is not None:
                stages.append(SemanticReranker(reranker_provider))
            stages.extend((QuotaAwareMmrReranker(), PolicySelector()))
            reranker = CompositeReranker(*stages)
            pipeline = RagRetrievalPipeline(
                uow_factory,
                query_rewriter=BoundedQueryPlanner(),
                retriever=HybridRrfCandidateRetriever(
                    PgVectorCandidateRetriever(embedding_provider),
                    PostgresKeywordCandidateRetriever(),
                ),
                reranker=reranker,
                context_assembler=EvidenceContextAssembler(),
                unit_of_work=PostgresUnitOfWork,
            )
        self._uow_factory = uow_factory
        self._pipeline = pipeline

    @property
    def pipeline(self) -> RagRetrievalPipeline:
        return self._pipeline

    async def search_for_task(
        self, *, task_id: UUID, request: RagRetrievalQuery
    ) -> RagContextPackage:
        session_factory = self._uow_factory()
        async with session_factory() as session:
            async with PostgresUnitOfWork(session).transaction() as tx:
                task = await tx.tasks.get(task_id)
                if task is None:
                    raise _error("RAG_TASK_NOT_FOUND", "检索来源任务不存在", "not_found")
                if task.workspace_id is None:
                    raise _error(
                        "RAG_WORKSPACE_REQUIRED",
                        "当前任务未绑定 Workspace",
                        "validation",
                    )
                workspace_id = task.workspace_id
        return await self.search(workspace_id=workspace_id, request=request)

    async def search(self, *, workspace_id: UUID, request: RagRetrievalQuery) -> RagContextPackage:
        return await self._pipeline.run(workspace_id=workspace_id, request=request)


def _error(code: str, message: str, category: str, recoverable: bool = False) -> AppError:
    return AppError(
        code=code,
        message=message,
        category=category,
        recoverable=recoverable,
    )
