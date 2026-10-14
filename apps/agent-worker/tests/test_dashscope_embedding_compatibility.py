"""Patch regressions: provider bounds, safe errors and vector identity isolation."""

import json
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest

from jarvis_worker.agent.rag.contracts import RagDocumentStatus
from jarvis_worker.agent.rag.embedding.config import RagEmbeddingConfig
from jarvis_worker.agent.rag.embedding.dashscope import (
    DashScopeEmbeddingError,
    DashScopeEmbeddingProvider,
)
from jarvis_worker.agent.rag.index_policy import RagIndexTarget, assess_index_freshness
from jarvis_worker.agent.rag.retrieval.contracts import RagQueryPlan, RagRetrievalQuery
from jarvis_worker.agent.rag.retrieval.pipeline import PgVectorCandidateRetriever
from jarvis_worker.shared.errors.application import AppError


@pytest.mark.asyncio
async def test_dashscope_batches_at_twenty_and_restores_each_response_order():
    sizes = []

    async def handler(request):
        inputs = json.loads(request.content)["input"]
        sizes.append(len(inputs))
        return httpx.Response(
            200,
            json={
                "data": [
                    {"index": index, "embedding": [float(text)] * 1536}
                    for index, text in reversed(list(enumerate(inputs)))
                ]
            },
        )

    async with httpx.AsyncClient(
        base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
        transport=httpx.MockTransport(handler),
    ) as client:
        provider = DashScopeEmbeddingProvider(api_key="test-only", client=client)
        result = await provider.embed_documents([str(i) for i in range(41)])
    assert sizes == [20, 20, 1]
    assert [vector[0] for vector in result] == list(range(41))


@pytest.mark.asyncio
@pytest.mark.parametrize("status,recoverable", [(401, False), (429, True), (503, True)])
async def test_dashscope_errors_keep_retry_semantics_through_retrieval(status, recoverable):
    async def handler(request):
        return httpx.Response(status, json={"message": "sensitive upstream response"})

    async with httpx.AsyncClient(
        base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
        transport=httpx.MockTransport(handler),
    ) as client:
        retriever = PgVectorCandidateRetriever(
            DashScopeEmbeddingProvider(api_key="test-only", client=client)
        )
        with pytest.raises(AppError) as caught:
            await retriever.prepare(RagQueryPlan(original_query="query", queries=("query",)))
    suffix = "UNAVAILABLE" if recoverable else "REQUEST_REJECTED"
    assert caught.value.code == f"DASHSCOPE_EMBEDDING_{suffix}"
    assert caught.value.recoverable is recoverable
    assert "sensitive upstream" not in str(caught.value)


@pytest.mark.asyncio
@pytest.mark.parametrize("vector", [[0.1] * 1024, [float("nan")] * 1536])
async def test_dashscope_invalid_vectors_never_enter_index(vector):
    # JSON cannot encode NaN; emulate a malformed response decoder for this case.
    async def malformed_handler(request):
        response = httpx.Response(200, json={"data": []})
        response.json = lambda: {"data": [{"index": 0, "embedding": vector}]}
        return response

    async with httpx.AsyncClient(
        base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
        transport=httpx.MockTransport(malformed_handler),
    ) as client:
        provider = DashScopeEmbeddingProvider(api_key="test-only", client=client)
        with pytest.raises(DashScopeEmbeddingError) as caught:
            await provider.embed_query("query")
    assert caught.value.code == "DASHSCOPE_EMBEDDING_INVALID_RESPONSE"


def test_provider_switch_marks_existing_openai_index_stale_without_mutating_document(monkeypatch):
    monkeypatch.setenv("JARVIS_RAG_EMBEDDING_PROVIDER", "dashscope")
    for key in ("MODEL", "API_KEY_ENV", "BASE_URL", "BATCH_SIZE", "DIMENSIONS"):
        monkeypatch.delenv(f"JARVIS_RAG_EMBEDDING_{key}", raising=False)
    target = RagIndexTarget.current()
    document = SimpleNamespace(
        status=RagDocumentStatus.READY,
        ingestion_policy_version=target.ingestion_policy_version,
        parser_version=target.parser_version,
        chunker_version=target.chunker_version,
        embedding_provider="openai",
        embedding_model="text-embedding-3-small",
        embedding_dimensions=1536,
    )
    freshness = assess_index_freshness(document, target)
    assert freshness.state == "stale"
    assert freshness.stale_reasons == ("embedding_provider", "embedding_model")
    assert document.embedding_provider == "openai"


@pytest.mark.asyncio
async def test_dashscope_retrieval_filters_vectors_by_provider_and_model():
    class Provider:
        provider_name = "dashscope"
        model_name = "qwen3.7-text-embedding"

        async def embed_query(self, text):
            return [0.1] * 1536

    calls = []

    class Repository:
        async def search_candidates(self, **kwargs):
            calls.append(kwargs)
            return []

    retriever = PgVectorCandidateRetriever(Provider())
    plan = RagQueryPlan(original_query="query", queries=("query",))
    prepared = await retriever.prepare(plan)
    workspace_id = uuid4()
    await retriever.retrieve(
        Repository(),
        workspace_id=workspace_id,
        prepared=prepared,
        request=RagRetrievalQuery(query="query"),
    )
    assert calls[0]["provider_name"] == "dashscope"
    assert calls[0]["model_name"] == "qwen3.7-text-embedding"
    assert calls[0]["workspace_id"] == workspace_id


@pytest.mark.parametrize("provider", ["unknown", ""])
def test_unknown_provider_fails_closed(monkeypatch, provider):
    monkeypatch.setenv("JARVIS_RAG_EMBEDDING_PROVIDER", provider)
    with pytest.raises(ValueError, match="仅支持"):
        RagEmbeddingConfig.from_env()
