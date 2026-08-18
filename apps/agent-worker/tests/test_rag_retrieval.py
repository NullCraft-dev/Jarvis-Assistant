from __future__ import annotations

from dataclasses import replace
from uuid import UUID, uuid4

import pytest

from jarvis_worker.agent.rag.retrieval import RagRetrievalQuery, RagRetrievalService
from jarvis_worker.agent.rag.retrieval.fusion import reciprocal_rank_fuse
from jarvis_worker.agent.rag.retrieval.keyword import build_keyword_terms
from jarvis_worker.agent.rag.retrieval.pipeline import (
    BoundedQueryPlanner,
    HybridRrfCandidateRetriever,
    PgVectorCandidateRetriever,
    PostgresKeywordCandidateRetriever,
    RagRetrievalPipeline,
)
from jarvis_worker.agent.rag.retrieval.repository import (
    RagCandidateRecord,
    RagElementEvidenceRecord,
    RagNeighborRecord,
)
from jarvis_worker.agent.rag.retrieval.stages import build_context_items
from jarvis_worker.shared.domain.models import Task, Workspace, WorkspaceStatus
from jarvis_worker.shared.errors.application import AppError


class _Session:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return None


class _SessionFactory:
    def __call__(self):
        return _Session()


class _Lookup:
    def __init__(self, values):
        self._values = values

    async def get(self, item_id):
        return self._values.get(item_id)


class _RetrievalRepository:
    def __init__(self, candidates, neighbors=(), elements=(), keyword_candidates=()):
        self._candidates = list(candidates)
        self._keyword_candidates = list(keyword_candidates)
        self._neighbors = list(neighbors)
        self._elements = list(elements)
        self.search_kwargs = None
        self.keyword_search_kwargs = None

    async def search_candidates(self, **kwargs):
        self.search_kwargs = kwargs
        return list(self._candidates)

    async def search_keyword_candidates(self, **kwargs):
        self.keyword_search_kwargs = kwargs
        return list(self._keyword_candidates)

    async def load_neighbors(self, **_kwargs):
        return list(self._neighbors)

    async def load_elements(self, **_kwargs):
        return list(self._elements)


class _FakeUow:
    def __init__(self, *, tasks, workspaces, retrieval):
        self.tasks = _Lookup(tasks)
        self.workspaces = _Lookup(workspaces)
        self.rag_retrieval = retrieval

    def transaction(self):
        return self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return None


class _EmbeddingProvider:
    provider_name = "openai"
    model_name = "text-embedding-3-small"
    dimensions = 1_536

    def __init__(self):
        self.queries = []

    async def embed_query(self, text):
        self.queries.append(text)
        return [0.1] * self.dimensions


@pytest.mark.asyncio
async def test_bounded_query_planner_keeps_original_and_adds_meaningful_clauses():
    planner = BoundedQueryPlanner(max_queries=3, min_clause_chars=8)

    plan = await planner.rewrite(
        RagRetrievalQuery(
            query="比较两种检索架构的召回质量；分析它们的延迟与成本；给出适用边界"
        )
    )

    assert plan.original_query == "比较两种检索架构的召回质量；分析它们的延迟与成本；给出适用边界"
    assert plan.queries == (
        plan.original_query,
        "比较两种检索架构的召回质量",
        "分析它们的延迟与成本",
    )


@pytest.mark.asyncio
async def test_bounded_query_planner_expands_chinese_comparison_dimensions() -> None:
    planner = BoundedQueryPlanner()
    query = "比较两份规范在风险治理、生命周期、组织责任和验证活动上的差异"

    plan = await planner.rewrite(RagRetrievalQuery(query=query))

    assert plan.queries == (
        query,
        "风险治理",
        "生命周期",
        "组织责任",
        "验证活动",
    )


@pytest.mark.asyncio
async def test_bounded_query_planner_keeps_dimension_expansion_bounded() -> None:
    planner = BoundedQueryPlanner(max_queries=3)
    query = "比较两份规范在风险治理、生命周期、组织责任和验证活动上的差异"

    plan = await planner.rewrite(RagRetrievalQuery(query=query))

    assert plan.queries == (query, "风险治理", "生命周期")


@pytest.mark.asyncio
async def test_bounded_query_planner_does_not_expand_short_or_duplicate_clauses():
    planner = BoundedQueryPlanner()

    plan = await planner.rewrite(RagRetrievalQuery(query="什么是混合检索？"))

    assert plan.queries == ("什么是混合检索？",)


def _candidate(
    workspace_id: UUID,
    document_id: UUID,
    *,
    ordinal: int,
    score: float,
    content_hash: str,
    token_count: int = 100,
):
    return RagCandidateRecord(
        chunk_id=uuid4(),
        document_id=document_id,
        workspace_id=workspace_id,
        source_artifact_id=uuid4(),
        document_title=f"Document {str(document_id)[:8]}",
        ordinal=ordinal,
        content=f"Evidence chunk {ordinal}",
        content_hash=content_hash,
        token_count=token_count,
        source_locator={"page_start": ordinal + 1, "heading_path": ["Section"]},
        score=score,
    )


def test_context_assembler_reserves_budget_for_multiple_primary_evidence():
    workspace_id, document_a, document_b = uuid4(), uuid4(), uuid4()
    first = replace(
        _candidate(
            workspace_id,
            document_a,
            ordinal=0,
            score=0.9,
            content_hash="a" * 64,
            token_count=700,
        ),
        content="alpha evidence " * 700,
    )
    second = replace(
        _candidate(
            workspace_id,
            document_b,
            ordinal=0,
            score=0.8,
            content_hash="b" * 64,
            token_count=700,
        ),
        content="beta evidence " * 700,
    )

    items, total_tokens, was_truncated = build_context_items(
        [first, second], [], [], query="evidence", token_budget=256
    )

    assert [item.chunk_id for item in items] == [first.chunk_id, second.chunk_id]
    assert all(item.token_count <= 128 for item in items)
    assert total_tokens <= 256
    assert was_truncated is True


def test_context_assembler_truncates_around_query_match():
    workspace_id, document_id = uuid4(), uuid4()
    query = "关键结论在这里"
    candidate = replace(
        _candidate(
            workspace_id,
            document_id,
            ordinal=0,
            score=0.9,
            content_hash="c" * 64,
            token_count=1_000,
        ),
        content=("无关背景。" * 600) + query + ("后续附录。" * 600),
    )

    items, total_tokens, was_truncated = build_context_items(
        [candidate], [], [], query=query, token_budget=96
    )

    assert query in items[0].chunks[0].content
    assert total_tokens <= 96
    assert was_truncated is True


def test_context_assembler_removes_neighbor_overlap():
    workspace_id, document_id = uuid4(), uuid4()
    overlap = "这是主分块和下一分块共同携带的重叠上下文，用于避免边界信息丢失。"
    candidate = replace(
        _candidate(
            workspace_id,
            document_id,
            ordinal=0,
            score=0.9,
            content_hash="d" * 64,
            token_count=40,
        ),
        content="主分块内容。" + overlap,
    )
    neighbor = RagNeighborRecord(
        chunk_id=uuid4(),
        document_id=document_id,
        ordinal=1,
        content=overlap + "下一分块独有内容。",
        token_count=40,
        source_locator={"page_start": 2},
    )

    items, total_tokens, _ = build_context_items(
        [candidate], [neighbor], [], query="", token_budget=256
    )

    assert len(items[0].chunks) == 2
    assert items[0].chunks[1].content == "下一分块独有内容。"
    assert total_tokens == items[0].token_count


@pytest.mark.asyncio
async def test_retrieval_uses_trusted_task_workspace_and_expands_evidence(monkeypatch):
    workspace_id, task_id = uuid4(), uuid4()
    document_a, document_b = uuid4(), uuid4()
    first = _candidate(workspace_id, document_a, ordinal=2, score=0.94, content_hash="a" * 64)
    duplicate = _candidate(workspace_id, document_b, ordinal=0, score=0.90, content_hash="a" * 64)
    second = _candidate(workspace_id, document_b, ordinal=4, score=0.85, content_hash="b" * 64)
    neighbor = RagNeighborRecord(
        chunk_id=uuid4(),
        document_id=document_a,
        ordinal=1,
        content="Previous context",
        token_count=20,
        source_locator={"page_start": 2},
    )
    asset_id = uuid4()
    element = RagElementEvidenceRecord(
        chunk_id=first.chunk_id,
        element_id=uuid4(),
        element_type="table",
        page_number=3,
        caption_text="Benchmark results",
        ocr_text="Model A 92%",
        structured_data={"columns": ["model", "score"]},
        derived_description="",
        confidence=0.98,
        asset_ids=(asset_id,),
    )
    repository = _RetrievalRepository([first, duplicate, second], [neighbor], [element])
    workspace = Workspace(
        id=workspace_id,
        name="Project",
        root_path="/tmp/project",
        canonical_path="/tmp/project",
    )
    task = Task(
        id=task_id,
        title="Question",
        user_goal="Find evidence",
        conversation_id=uuid4(),
        workspace_id=workspace_id,
    )
    uow = _FakeUow(
        tasks={task_id: task},
        workspaces={workspace_id: workspace},
        retrieval=repository,
    )
    monkeypatch.setattr(
        "jarvis_worker.agent.rag.retrieval.service.PostgresUnitOfWork",
        lambda _session: uow,
    )
    provider = _EmbeddingProvider()
    service = RagRetrievalService(lambda: _SessionFactory(), embedding_provider=provider)

    package = await service.search_for_task(
        task_id=task_id,
        request=RagRetrievalQuery(
            query="  benchmark score  ",
            top_k=3,
            candidate_limit=10,
            token_budget=1_000,
        ),
    )

    assert provider.queries == ["benchmark score"]
    assert package.workspace_id == workspace_id
    assert package.candidate_count == 3
    assert [item.chunk_id for item in package.items] == [first.chunk_id, second.chunk_id]
    assert package.items[0].chunks[1].role == "previous"
    assert package.items[0].elements[0].asset_ids == (asset_id,)
    assert repository.search_kwargs["workspace_id"] == workspace_id
    assert repository.search_kwargs["provider_name"] == "openai"
    assert repository.search_kwargs["model_name"] == "text-embedding-3-small"
    assert repository.keyword_search_kwargs["query_terms"] == (
        "benchmark",
        "score",
    )
    assert package.pipeline.retriever == "semantic-keyword-rrf-v1"
    assert [item.chunk_id for item in package.pipeline.retrieved_candidates] == [
        first.chunk_id,
        duplicate.chunk_id,
        second.chunk_id,
    ]
    assert [item.chunk_id for item in package.pipeline.reranked_candidates] == [
        first.chunk_id,
        second.chunk_id,
    ]
    assert package.pipeline.context_chunk_ids == (
        first.chunk_id,
        neighbor.chunk_id,
        second.chunk_id,
    )
    assert package.pipeline.retrieved_candidates[0].rank == 1
    assert package.pipeline.retrieved_candidates[0].content_hash == first.content_hash
    assert [step.stage_id for step in package.pipeline.reranker_steps] == [
        "hard-filter-v1",
        "feature-rank-v1",
        "quota-mmr-v1",
        "policy-selector-v2",
    ]
    assert all(step.status == "applied" for step in package.pipeline.reranker_steps)


@pytest.mark.asyncio
async def test_retrieval_rejects_revoked_workspace_before_embedding(monkeypatch):
    workspace_id = uuid4()
    workspace = Workspace(
        id=workspace_id,
        name="Revoked",
        root_path="/tmp/revoked",
        canonical_path="/tmp/revoked",
        status=WorkspaceStatus.REVOKED,
    )
    repository = _RetrievalRepository([])
    uow = _FakeUow(tasks={}, workspaces={workspace_id: workspace}, retrieval=repository)
    monkeypatch.setattr(
        "jarvis_worker.agent.rag.retrieval.service.PostgresUnitOfWork",
        lambda _session: uow,
    )
    provider = _EmbeddingProvider()
    service = RagRetrievalService(lambda: _SessionFactory(), embedding_provider=provider)

    with pytest.raises(AppError) as caught:
        await service.search(
            workspace_id=workspace_id,
            request=RagRetrievalQuery(query="secret project"),
        )

    assert caught.value.code == "RAG_WORKSPACE_UNAVAILABLE"
    assert provider.queries == []


@pytest.mark.asyncio
async def test_retrieval_requires_task_workspace(monkeypatch):
    task_id = uuid4()
    task = Task(
        id=task_id,
        title="No workspace",
        user_goal="Find evidence",
        conversation_id=uuid4(),
    )
    uow = _FakeUow(tasks={task_id: task}, workspaces={}, retrieval=_RetrievalRepository([]))
    monkeypatch.setattr(
        "jarvis_worker.agent.rag.retrieval.service.PostgresUnitOfWork",
        lambda _session: uow,
    )
    service = RagRetrievalService(
        lambda: _SessionFactory(), embedding_provider=_EmbeddingProvider()
    )

    with pytest.raises(AppError) as caught:
        await service.search_for_task(
            task_id=task_id,
            request=RagRetrievalQuery(query="question"),
        )

    assert caught.value.code == "RAG_WORKSPACE_REQUIRED"


@pytest.mark.asyncio
async def test_retrieval_truncates_primary_chunk_to_token_budget(monkeypatch):
    workspace_id, document_id = uuid4(), uuid4()
    candidate = replace(
        _candidate(
            workspace_id,
            document_id,
            ordinal=0,
            score=0.9,
            content_hash="c" * 64,
            token_count=700,
        ),
        content="evidence " * 700,
    )
    workspace = Workspace(
        id=workspace_id,
        name="Budget",
        root_path="/tmp/budget",
        canonical_path="/tmp/budget",
    )
    repository = _RetrievalRepository([candidate])
    uow = _FakeUow(
        tasks={},
        workspaces={workspace_id: workspace},
        retrieval=repository,
    )
    monkeypatch.setattr(
        "jarvis_worker.agent.rag.retrieval.service.PostgresUnitOfWork",
        lambda _session: uow,
    )
    service = RagRetrievalService(
        lambda: _SessionFactory(), embedding_provider=_EmbeddingProvider()
    )

    package = await service.search(
        workspace_id=workspace_id,
        request=RagRetrievalQuery(query="question", token_budget=256),
    )

    assert package.total_tokens == 256
    assert package.truncated is True
    assert package.items[0].chunks[0].truncated is True


def test_retrieval_query_enforces_bounded_contract():
    with pytest.raises(ValueError, match="candidate_limit"):
        RagRetrievalQuery(query="question", top_k=8, candidate_limit=4)
    with pytest.raises(ValueError, match="document_ids"):
        document_id = uuid4()
        RagRetrievalQuery(query="question", document_ids=(document_id, document_id))


def test_keyword_terms_are_bounded_and_support_chinese_and_identifiers():
    terms = build_keyword_terms("Transformer 为什么使用多头注意力？GPT-4o 与 RAG_v2")

    assert "transformer" in terms
    assert "gpt-4o" in terms
    assert "rag_v2" in terms
    assert "为什么使用多头注意力" in terms
    assert "用多头注" in terms
    assert len(terms) <= 16


def test_rrf_fuses_routes_without_comparing_raw_scores():
    workspace_id, document_id = uuid4(), uuid4()
    both = _candidate(
        workspace_id,
        document_id,
        ordinal=0,
        score=0.51,
        content_hash="d" * 64,
    )
    semantic_only = _candidate(
        workspace_id,
        document_id,
        ordinal=1,
        score=0.99,
        content_hash="e" * 64,
    )
    keyword_only = _candidate(
        workspace_id,
        document_id,
        ordinal=2,
        score=0.30,
        content_hash="f" * 64,
    )

    fused = reciprocal_rank_fuse(
        {
            "semantic": [semantic_only, both],
            "keyword": [replace(both, score=0.30), keyword_only],
        }
    )

    assert fused[0].chunk_id == both.chunk_id
    assert fused[0].trace.sources == ("semantic", "keyword")
    assert fused[0].trace.semantic_rank == 2
    assert fused[0].trace.keyword_rank == 1
    assert fused[0].trace.semantic_score == 0.51
    assert fused[0].trace.keyword_score == 0.30
    assert 0 < fused[0].score <= 1


@pytest.mark.asyncio
async def test_hybrid_retriever_preserves_document_scope_for_both_routes():
    workspace_id, document_id = uuid4(), uuid4()
    semantic = _candidate(
        workspace_id,
        document_id,
        ordinal=0,
        score=0.8,
        content_hash="1" * 64,
    )
    keyword = replace(semantic, score=0.6)
    repository = _RetrievalRepository([semantic], keyword_candidates=[keyword])
    provider = _EmbeddingProvider()
    retriever = HybridRrfCandidateRetriever(
        PgVectorCandidateRetriever(provider),
        PostgresKeywordCandidateRetriever(),
    )
    request = RagRetrievalQuery(
        query="PaddleOCR-VL 表格识别",
        document_ids=(document_id,),
    )
    from jarvis_worker.agent.rag.retrieval import RagQueryPlan

    prepared = await retriever.prepare(
        RagQueryPlan(original_query=request.query, queries=(request.query,))
    )
    candidates = await retriever.retrieve(
        repository,
        workspace_id=workspace_id,
        prepared=prepared,
        request=request,
    )

    assert candidates[0].trace.sources == ("semantic", "keyword")
    assert repository.search_kwargs["document_ids"] == (document_id,)
    assert repository.keyword_search_kwargs["document_ids"] == (document_id,)
    assert repository.keyword_search_kwargs["workspace_id"] == workspace_id


@pytest.mark.asyncio
async def test_hybrid_retriever_backfills_missing_selected_document_candidates():
    workspace_id, document_a, document_b = uuid4(), uuid4(), uuid4()
    first = _candidate(
        workspace_id,
        document_a,
        ordinal=0,
        score=0.9,
        content_hash="a" * 64,
    )
    second = _candidate(
        workspace_id,
        document_b,
        ordinal=0,
        score=0.8,
        content_hash="b" * 64,
    )

    class _ScopedRepository(_RetrievalRepository):
        def __init__(self):
            super().__init__([])
            self.semantic_scopes = []
            self.keyword_scopes = []

        async def search_candidates(self, **kwargs):
            scope = kwargs["document_ids"]
            self.semantic_scopes.append(scope)
            return [second] if scope == (document_b,) else [first]

        async def search_keyword_candidates(self, **kwargs):
            scope = kwargs["document_ids"]
            self.keyword_scopes.append(scope)
            return []

    repository = _ScopedRepository()
    retriever = HybridRrfCandidateRetriever(
        PgVectorCandidateRetriever(_EmbeddingProvider()),
        PostgresKeywordCandidateRetriever(),
    )
    request = RagRetrievalQuery(
        query="比较两份资料",
        document_ids=(document_a, document_b),
    )
    from jarvis_worker.agent.rag.retrieval import RagQueryPlan

    prepared = await retriever.prepare(
        RagQueryPlan(original_query=request.query, queries=(request.query,))
    )
    candidates = await retriever.retrieve(
        repository,
        workspace_id=workspace_id,
        prepared=prepared,
        request=request,
    )

    assert [item.document_id for item in candidates[:2]] == [document_a, document_b]
    assert repository.semantic_scopes == [
        (document_a, document_b),
        (document_b,),
    ]
    assert repository.keyword_scopes == [
        (document_a, document_b),
        (document_b,),
    ]


@pytest.mark.asyncio
async def test_hybrid_retriever_filters_weak_routes_before_rrf():
    workspace_id, document_id = uuid4(), uuid4()
    weak = _candidate(
        workspace_id,
        document_id,
        ordinal=0,
        score=0.14,
        content_hash="2" * 64,
    )
    repository = _RetrievalRepository([weak], keyword_candidates=[replace(weak, score=0.1)])
    retriever = HybridRrfCandidateRetriever(
        PgVectorCandidateRetriever(_EmbeddingProvider()),
        PostgresKeywordCandidateRetriever(),
    )
    request = RagRetrievalQuery(query="weak evidence", min_score=0.15)
    from jarvis_worker.agent.rag.retrieval import RagQueryPlan

    prepared = await retriever.prepare(
        RagQueryPlan(original_query=request.query, queries=(request.query,))
    )

    assert (
        await retriever.retrieve(
            repository,
            workspace_id=workspace_id,
            prepared=prepared,
            request=request,
        )
        == []
    )


@pytest.mark.asyncio
async def test_pipeline_stages_are_replaceable_and_expose_trace():
    workspace_id = uuid4()
    calls: list[str] = []
    workspace = Workspace(
        id=workspace_id,
        name="Pipeline",
        root_path="/tmp/pipeline",
        canonical_path="/tmp/pipeline",
    )
    uow = _FakeUow(
        tasks={},
        workspaces={workspace_id: workspace},
        retrieval=_RetrievalRepository([]),
    )

    class Rewriter:
        stage_id = "test-rewriter"

        async def rewrite(self, request):
            from jarvis_worker.agent.rag.retrieval import RagQueryPlan

            calls.append("rewrite")
            return RagQueryPlan(
                original_query=request.query,
                queries=(request.query, f"{request.query} expanded"),
            )

    class Retriever:
        stage_id = "test-hybrid-retriever"

        async def prepare(self, plan):
            from jarvis_worker.agent.rag.retrieval import RagPreparedQuery

            calls.append("prepare")
            return RagPreparedQuery(plan=plan)

        async def retrieve(self, repository, **_kwargs):
            assert repository is uow.rag_retrieval
            calls.append("retrieve")
            return []

    class Reranker:
        stage_id = "test-reranker"

        async def rerank(self, **kwargs):
            from jarvis_worker.agent.rag.reranking import RagRerankResult

            calls.append("rerank")
            return RagRerankResult(candidates=tuple(kwargs["candidates"]), steps=())

    class Assembler:
        stage_id = "test-assembler"

        async def assemble(self, repository, **_kwargs):
            assert repository is uow.rag_retrieval
            calls.append("assemble")
            return (), 0, False

    pipeline = RagRetrievalPipeline(
        lambda: _SessionFactory(),
        query_rewriter=Rewriter(),
        retriever=Retriever(),
        reranker=Reranker(),
        context_assembler=Assembler(),
        unit_of_work=lambda _session: uow,
    )

    package = await pipeline.run(
        workspace_id=workspace_id,
        request=RagRetrievalQuery(query="question"),
    )

    assert calls == ["rewrite", "prepare", "retrieve", "rerank", "assemble"]
    assert package.pipeline.query_rewriter == "test-rewriter"
    assert package.pipeline.retriever == "test-hybrid-retriever"
    assert package.pipeline.reranker == "test-reranker"
    assert package.pipeline.context_assembler == "test-assembler"
    assert package.pipeline.queries == ("question", "question expanded")


@pytest.mark.asyncio
async def test_pipeline_rejects_cross_workspace_candidate_from_retriever():
    workspace_id = uuid4()
    workspace = Workspace(
        id=workspace_id,
        name="Pipeline",
        root_path="/tmp/pipeline",
        canonical_path="/tmp/pipeline",
    )
    uow = _FakeUow(
        tasks={},
        workspaces={workspace_id: workspace},
        retrieval=_RetrievalRepository([]),
    )
    foreign = _candidate(
        uuid4(),
        uuid4(),
        ordinal=0,
        score=0.9,
        content_hash="f" * 64,
    )

    class Retriever:
        stage_id = "unsafe-retriever"

        async def prepare(self, plan):
            from jarvis_worker.agent.rag.retrieval import RagPreparedQuery

            return RagPreparedQuery(plan=plan)

        async def retrieve(self, repository, **_kwargs):
            return [foreign]

    from jarvis_worker.agent.rag.retrieval.pipeline import (
        EvidenceContextAssembler,
        IdentityQueryRewriter,
        PolicyReranker,
    )

    pipeline = RagRetrievalPipeline(
        lambda: _SessionFactory(),
        query_rewriter=IdentityQueryRewriter(),
        retriever=Retriever(),
        reranker=PolicyReranker(),
        context_assembler=EvidenceContextAssembler(),
        unit_of_work=lambda _session: uow,
    )

    with pytest.raises(AppError) as caught:
        await pipeline.run(
            workspace_id=workspace_id,
            request=RagRetrievalQuery(query="question"),
        )

    assert caught.value.code == "RAG_RETRIEVER_SCOPE_VIOLATION"
