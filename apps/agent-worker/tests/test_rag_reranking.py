from __future__ import annotations

from dataclasses import replace
from uuid import UUID, uuid4

import httpx
import pytest

from jarvis_worker.agent.rag.reranking import (
    CompositeReranker,
    FeatureReranker,
    HardFilter,
    LocalBgeRerankerConfig,
    LocalBgeRerankerProvider,
    PolicySelector,
    QuotaAwareMmrReranker,
    RerankerProvider,
    RerankerProviderError,
    RerankScore,
    SemanticReranker,
    SemanticRerankerConfig,
)
from jarvis_worker.agent.rag.reranking.local_server import (
    CrossEncoderRuntimeConfig,
    build_dynamic_batches,
)
from jarvis_worker.agent.rag.retrieval import RagQueryPlan, RagRetrievalQuery
from jarvis_worker.agent.rag.retrieval.pipeline import PolicyReranker
from jarvis_worker.agent.rag.retrieval.repository import RagCandidateRecord
from jarvis_worker.agent.rag.retrieval.service import RagRetrievalService


class _Provider(RerankerProvider):
    provider_name = "test-cross-encoder"
    model_name = "test-reranker-v1"

    def __init__(self, scores: dict[UUID, float] | None = None, error=None) -> None:
        self.scores = scores or {}
        self.error = error
        self.calls = []

    async def score(self, *, query, documents):
        self.calls.append((query, documents))
        if self.error is not None:
            raise self.error
        return tuple(
            RerankScore(chunk_id=document.chunk_id, score=self.scores[document.chunk_id])
            for document in documents
            if document.chunk_id in self.scores
        )


def _candidate(
    index: int,
    *,
    document_id: UUID | None = None,
    content: str | None = None,
) -> RagCandidateRecord:
    chunk_id = UUID(int=index + 1)
    return RagCandidateRecord(
        chunk_id=chunk_id,
        document_id=document_id or UUID(int=100 + index),
        workspace_id=UUID(int=999),
        source_artifact_id=UUID(int=200 + index),
        document_title=f"Document {index}",
        ordinal=index,
        content=content or f"Evidence {index}",
        content_hash=f"{index + 1:064x}",
        token_count=10,
        source_locator={"heading_path": ["Section", f"Topic {index}"]},
        score=1.0 - index * 0.1,
    )


def _plan() -> RagQueryPlan:
    return RagQueryPlan(original_query="What solves the memory spike?", queries=("query",))


def _request(**kwargs) -> RagRetrievalQuery:
    return RagRetrievalQuery(query="What solves the memory spike?", **kwargs)


@pytest.mark.asyncio
async def test_semantic_reranker_promotes_relevant_candidates_with_rank_blend():
    candidates = [_candidate(0), _candidate(1), _candidate(2)]
    provider = _Provider(
        {
            candidates[0].chunk_id: 0.1,
            candidates[1].chunk_id: 0.5,
            candidates[2].chunk_id: 0.9,
        }
    )

    result = await SemanticReranker(provider).rerank(
        plan=_plan(),
        candidates=candidates,
        request=_request(),
    )

    assert [item.chunk_id for item in result.candidates] == [
        candidates[2].chunk_id,
        candidates[1].chunk_id,
        candidates[0].chunk_id,
    ]
    assert [item.score for item in result.candidates] == [0.75, 0.5, 0.25]
    assert result.steps[0].status == "applied"
    assert result.steps[0].provider == "test-cross-encoder"
    assert result.steps[0].input_count == result.steps[0].output_count == 3


@pytest.mark.asyncio
async def test_semantic_reranker_bounds_provider_input():
    candidate = _candidate(0, content="x" * 1_000)
    provider = _Provider({candidate.chunk_id: 1.0})
    reranker = SemanticReranker(
        provider,
        config=SemanticRerankerConfig(
            max_content_chars=256,
            max_heading_chars=8,
        ),
    )

    await reranker.rerank(plan=_plan(), candidates=[candidate], request=_request())

    query, documents = provider.calls[0]
    assert query == _plan().original_query
    assert len(documents[0].content) == 256
    assert sum(len(value) for value in documents[0].heading_path) <= 8


@pytest.mark.asyncio
async def test_semantic_reranker_compacts_long_input_around_query_and_boundaries():
    content = "A" * 400 + "exact query evidence" + "B" * 400 + "TAIL"
    candidate = _candidate(0, content=content)
    provider = _Provider({candidate.chunk_id: 1.0})
    reranker = SemanticReranker(
        provider,
        config=SemanticRerankerConfig(max_content_chars=256),
    )

    await reranker.rerank(
        plan=RagQueryPlan(original_query="exact query", queries=("exact query",)),
        candidates=[candidate],
        request=_request(),
    )

    compacted = provider.calls[0][1][0].content
    assert len(compacted) == 256
    assert compacted.startswith("A")
    assert "exact query evidence" in compacted
    assert compacted.endswith("TAIL")


@pytest.mark.asyncio
async def test_semantic_reranker_trace_preserves_input_output_direction_when_compressing():
    candidates = [_candidate(index) for index in range(20)]
    provider = _Provider(
        {candidate.chunk_id: float(20 - index) for index, candidate in enumerate(candidates)}
    )

    result = await SemanticReranker(provider).rerank(
        plan=_plan(),
        candidates=candidates,
        request=_request(cross_encoder_limit=16),
    )

    assert len(result.candidates) == 16
    assert result.steps[0].input_count == 20
    assert result.steps[0].output_count == 16


@pytest.mark.asyncio
async def test_semantic_reranker_degrades_to_rrf_order_on_provider_failure():
    candidates = [_candidate(0), _candidate(1)]
    provider = _Provider(error=RerankerProviderError("RERANKER_TIMEOUT"))

    result = await SemanticReranker(provider).rerank(
        plan=_plan(),
        candidates=candidates,
        request=_request(),
    )

    assert list(result.candidates) == candidates
    assert result.steps[0].status == "degraded"
    assert result.steps[0].failure_code == "RERANKER_TIMEOUT"


@pytest.mark.asyncio
async def test_semantic_reranker_degrades_on_provider_contract_violation():
    candidates = [_candidate(0), _candidate(1)]
    provider = _Provider({candidates[0].chunk_id: 1.0})

    result = await SemanticReranker(provider).rerank(
        plan=_plan(),
        candidates=candidates,
        request=_request(),
    )

    assert list(result.candidates) == candidates
    assert result.steps[0].failure_code == "RERANKER_PROVIDER_CONTRACT_VIOLATION"


@pytest.mark.asyncio
async def test_semantic_reranker_skips_provider_for_empty_candidates():
    provider = _Provider()

    result = await SemanticReranker(provider).rerank(
        plan=_plan(),
        candidates=[],
        request=_request(),
    )

    assert result.candidates == ()
    assert result.steps[0].status == "skipped"
    assert provider.calls == []


@pytest.mark.asyncio
async def test_composite_reranker_runs_semantic_before_policy():
    document_id = uuid4()
    candidates = [
        _candidate(0, document_id=document_id),
        _candidate(1, document_id=document_id),
        _candidate(2),
    ]
    provider = _Provider(
        {
            candidates[0].chunk_id: 0.1,
            candidates[1].chunk_id: 0.8,
            candidates[2].chunk_id: 0.9,
        }
    )
    reranker = CompositeReranker(SemanticReranker(provider), PolicyReranker())

    result = await reranker.rerank(
        plan=_plan(),
        candidates=candidates,
        request=_request(top_k=2, max_chunks_per_document=1),
    )

    assert [item.chunk_id for item in result.candidates] == [
        candidates[2].chunk_id,
        candidates[1].chunk_id,
    ]
    assert [step.stage_id for step in result.steps] == [
        "cross-encoder-rank-fusion-v1",
        "policy-score-v1",
    ]


@pytest.mark.asyncio
async def test_composite_reranker_preserves_selected_multi_document_coverage():
    document_a, document_b = uuid4(), uuid4()
    first_a = _candidate(0, document_id=document_a, content="memory queue evidence")
    second_a = _candidate(1, document_id=document_a, content="memory retry evidence")
    only_b = _candidate(2, document_id=document_b, content="independent document evidence")
    provider = _Provider(
        {
            first_a.chunk_id: 0.9,
            second_a.chunk_id: 0.8,
            only_b.chunk_id: 0.1,
        }
    )
    reranker = CompositeReranker(
        HardFilter(),
        FeatureReranker(),
        SemanticReranker(provider),
        QuotaAwareMmrReranker(),
        PolicySelector(),
    )
    request = _request(
        top_k=2,
        candidate_limit=3,
        feature_limit=2,
        cross_encoder_limit=2,
        diversity_limit=2,
        document_ids=(document_a, document_b),
    )

    result = await reranker.rerank(
        plan=_plan(),
        candidates=[first_a, second_a, only_b],
        request=request,
    )

    assert {item.document_id for item in result.candidates} == {
        document_a,
        document_b,
    }
    assert len(result.candidates) == 2


def test_retrieval_service_enables_composite_only_when_provider_is_injected():
    class _EmbeddingProvider:
        provider_name = "openai"
        model_name = "embedding"

    policy_only = RagRetrievalService(
        lambda: None,
        embedding_provider=_EmbeddingProvider(),
    )
    semantic = RagRetrievalService(
        lambda: None,
        embedding_provider=_EmbeddingProvider(),
        reranker_provider=_Provider(),
    )

    assert policy_only.pipeline._reranker.stage_id == "retrieval-rerank-pipeline-v1"
    assert semantic.pipeline._reranker.stage_id == "retrieval-rerank-pipeline-v1"
    assert len(policy_only.pipeline._reranker._rerankers) == 4
    assert len(semantic.pipeline._reranker._rerankers) == 5


@pytest.mark.asyncio
async def test_hard_filter_then_feature_reranker_removes_exact_duplicates_and_bounds_output():
    first = _candidate(0, content="Memory spike solution uses a bounded queue")
    duplicate = _candidate(1, content=first.content)
    duplicate = replace(duplicate, content_hash=first.content_hash)
    weak = _candidate(2, content="Unrelated evidence")
    request = _request(top_k=1, candidate_limit=3, feature_limit=2)

    filtered = await HardFilter().rerank(
        plan=_plan(), candidates=[first, duplicate, weak], request=request
    )
    ranked = await FeatureReranker().rerank(
        plan=_plan(), candidates=list(filtered.candidates), request=request
    )

    assert [item.chunk_id for item in filtered.candidates] == [first.chunk_id, weak.chunk_id]
    assert ranked.candidates[0].chunk_id == first.chunk_id
    assert ranked.candidates[0].trace.feature_score is not None


@pytest.mark.asyncio
async def test_quota_mmr_prefers_diverse_candidate_and_policy_applies_final_top_k():
    document_id = uuid4()
    first = _candidate(0, document_id=document_id, content="alpha beta gamma queue memory")
    near_duplicate = _candidate(
        1, document_id=document_id, content="alpha beta gamma queue memory detail"
    )
    diverse = _candidate(2, content="worker backpressure semaphore bounded execution")
    request = _request(top_k=2, diversity_limit=3, max_chunks_per_document=1)

    mmr = await QuotaAwareMmrReranker().rerank(
        plan=_plan(), candidates=[first, near_duplicate, diverse], request=request
    )
    selected = await PolicySelector().rerank(
        plan=_plan(), candidates=list(mmr.candidates), request=request
    )

    assert [item.chunk_id for item in selected.candidates] == [first.chunk_id, diverse.chunk_id]
    assert all(item.trace.mmr_score is not None for item in selected.candidates)


@pytest.mark.asyncio
async def test_policy_reserves_one_candidate_for_each_selected_document():
    document_a, document_b = uuid4(), uuid4()
    first_a = _candidate(0, document_id=document_a)
    second_a = _candidate(1, document_id=document_a)
    only_b = _candidate(2, document_id=document_b)
    request = _request(
        top_k=2,
        document_ids=(document_a, document_b),
        max_chunks_per_document=2,
    )

    selected = await PolicySelector().rerank(
        plan=_plan(),
        candidates=[first_a, second_a, only_b],
        request=request,
    )

    assert [item.chunk_id for item in selected.candidates] == [
        first_a.chunk_id,
        only_b.chunk_id,
    ]
    assert selected.steps[0].stage_id == "policy-selector-v2"


@pytest.mark.asyncio
async def test_local_bge_provider_maps_bounded_http_contract():
    candidate = _candidate(0)

    def handler(request: httpx.Request) -> httpx.Response:
        payload = __import__("json").loads(request.content)
        assert request.url.path == "/v1/rerank"
        assert payload["model"] == "BAAI/bge-reranker-v2-m3"
        return httpx.Response(
            200,
            json={
                "model": payload["model"],
                "scores": [{"chunk_id": str(candidate.chunk_id), "score": 2.75}],
            },
        )

    client = httpx.AsyncClient(
        base_url="http://127.0.0.1:8121",
        transport=httpx.MockTransport(handler),
    )
    provider = LocalBgeRerankerProvider(LocalBgeRerankerConfig(enabled=True), client=client)
    scores = await provider.score(
        query="memory",
        documents=(provider_document(candidate),),
    )

    assert scores == (RerankScore(chunk_id=candidate.chunk_id, score=2.75),)
    await client.aclose()


@pytest.mark.asyncio
async def test_local_bge_server_preserves_chunk_identity_and_model_contract():
    from jarvis_worker.agent.rag.reranking.local_server import create_app

    class Backend:
        model_name = "BAAI/bge-reranker-v2-m3"

        def score(self, query, passages):
            assert query == "memory"
            assert passages == ["Document\nSection\nEvidence"]
            return [3.5]

    chunk_id = uuid4()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(Backend())),
        base_url="http://test",
    ) as client:
        response = await client.post(
            "/v1/rerank",
            json={
                "model": Backend.model_name,
                "query": "memory",
                "documents": [
                    {
                        "chunk_id": str(chunk_id),
                        "title": "Document",
                        "heading_path": ["Section"],
                        "content": "Evidence",
                    }
                ],
            },
        )

    assert response.status_code == 200
    assert response.json()["scores"] == [{"chunk_id": str(chunk_id), "score": 3.5}]


def test_local_bge_dynamic_batches_respect_size_and_token_budget():
    lengths = [100] * 8 + [640] * 8

    batches = build_dynamic_batches(
        lengths,
        max_batch_size=8,
        max_batch_tokens=4_096,
    )

    assert sorted(index for batch in batches for index in batch) == list(range(16))
    assert all(len(batch) <= 8 for batch in batches)
    assert all(
        len(batch) == 1 or max(lengths[index] for index in batch) * len(batch) <= 4_096
        for batch in batches
    )
    assert batches[0] == list(range(8))


def test_local_bge_runtime_config_reads_optimized_defaults(monkeypatch):
    for name in (
        "JARVIS_RAG_RERANKER_BATCH_SIZE",
        "JARVIS_RAG_RERANKER_MAX_BATCH_TOKENS",
        "JARVIS_RAG_RERANKER_MAX_LENGTH",
        "JARVIS_RAG_RERANKER_WARMUP",
    ):
        monkeypatch.delenv(name, raising=False)

    config = CrossEncoderRuntimeConfig.from_env()

    assert config.max_batch_size == 8
    assert config.max_batch_tokens == 4_096
    assert config.max_length == 640
    assert config.warmup_enabled is True


def provider_document(candidate):
    from jarvis_worker.agent.rag.reranking import RerankDocument

    return RerankDocument(
        chunk_id=candidate.chunk_id,
        document_title=candidate.document_title,
        heading_path=("Section",),
        content=candidate.content,
    )
