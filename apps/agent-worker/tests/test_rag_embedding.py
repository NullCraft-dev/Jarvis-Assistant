from __future__ import annotations

from datetime import datetime, timedelta, timezone
from hashlib import sha256
from uuid import uuid4

import httpx
import pytest

from jarvis_worker.agent.rag.contracts import RagIngestionJob, RagIngestionStatus
from jarvis_worker.agent.rag.embedding.config import RagEmbeddingConfig
from jarvis_worker.agent.rag.embedding.openai import (
    OpenAIEmbeddingError,
    OpenAIEmbeddingProvider,
    create_openai_embedding_provider,
)
from jarvis_worker.database.models import RagChunkEmbeddingModel

NOW = datetime(2026, 7, 27, tzinfo=timezone.utc)


def _embedding_job(
    *, embedding_attempts: int = 0, embedding_max_attempts: int = 3
) -> RagIngestionJob:
    return RagIngestionJob(
        id=uuid4(),
        document_id=uuid4(),
        workspace_id=uuid4(),
        idempotency_key=sha256(b"embedding-job").hexdigest(),
        ingestion_policy_version="rag-v1",
        status=RagIngestionStatus.EMBEDDING,
        attempts=1,
        max_attempts=1,
        embedding_attempts=embedding_attempts,
        embedding_max_attempts=embedding_max_attempts,
        created_at=NOW,
        updated_at=NOW,
    )


@pytest.mark.asyncio
async def test_openai_provider_preserves_response_index_order():
    vector_a = [0.1] * 1536
    vector_b = [0.2] * 1536

    async def handler(request: httpx.Request) -> httpx.Response:
        body = __import__("json").loads(request.content)
        assert request.url.path == "/v1/embeddings"
        assert body == {
            "model": "text-embedding-3-small",
            "input": ["first", "second"],
            "dimensions": 1536,
            "encoding_format": "float",
        }
        return httpx.Response(
            200,
            json={
                "object": "list",
                "data": [
                    {"object": "embedding", "index": 1, "embedding": vector_b},
                    {"object": "embedding", "index": 0, "embedding": vector_a},
                ],
            },
        )

    async with httpx.AsyncClient(
        base_url="https://api.openai.com/v1",
        transport=httpx.MockTransport(handler),
    ) as client:
        provider = OpenAIEmbeddingProvider(api_key="test-only", client=client)
        result = await provider.embed_documents(["first", "second"])
    assert result == [vector_a, vector_b]


@pytest.mark.asyncio
async def test_openai_provider_sanitizes_remote_error():
    async def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, json={"error": {"message": "secret upstream detail"}})

    async with httpx.AsyncClient(
        base_url="https://api.openai.com/v1",
        transport=httpx.MockTransport(handler),
    ) as client:
        provider = OpenAIEmbeddingProvider(api_key="test-only", client=client)
        with pytest.raises(OpenAIEmbeddingError) as caught:
            await provider.embed_query("query")
    assert caught.value.code == "OPENAI_EMBEDDING_UNAVAILABLE"
    assert caught.value.recoverable is True
    assert "secret upstream detail" not in str(caught.value)


def test_embedding_job_retries_without_reparsing_then_completes():
    job = _embedding_job()
    job.claim_embedding(
        worker_id="embedding-1",
        lease_until=NOW + timedelta(minutes=5),
        now=NOW,
    )
    assert job.status is RagIngestionStatus.EMBEDDING
    assert job.attempts == 1
    assert job.embedding_attempts == 1

    retry_at = NOW + timedelta(seconds=30)
    job.fail_embedding(
        worker_id="embedding-1",
        error_code="OPENAI_EMBEDDING_UNAVAILABLE",
        recoverable=True,
        next_retry_at=retry_at,
        now=NOW,
    )
    assert job.status is RagIngestionStatus.EMBEDDING
    assert job.next_retry_at == retry_at
    assert job.claimed_by is None


def test_embedding_job_exhaustion_is_terminal_and_vector_schema_is_fixed():
    job = _embedding_job(embedding_attempts=3, embedding_max_attempts=3)
    job.exhaust_embedding(now=NOW)
    assert job.status is RagIngestionStatus.FAILED
    assert job.error_code == "RAG_EMBEDDING_ATTEMPTS_EXHAUSTED"
    assert RagChunkEmbeddingModel.__table__.c.embedding.type.dim == 1536


def test_provider_factory_resolves_only_named_environment_secret(monkeypatch):
    monkeypatch.setenv("JARVIS_TEST_OPENAI_KEY", "test-only")
    config = RagEmbeddingConfig(rag_embedding_api_key_env="JARVIS_TEST_OPENAI_KEY")
    provider = create_openai_embedding_provider(config)
    assert provider.provider_name == "openai"
    assert provider.model_name == "text-embedding-3-small"
    assert "test-only" not in repr(provider)
