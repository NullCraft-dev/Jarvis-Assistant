from __future__ import annotations

from uuid import uuid4

import pytest
from sqlalchemy.dialects import postgresql

from jarvis_worker.agent.rag.retrieval.config import HnswSearchConfig
from jarvis_worker.agent.rag.retrieval.postgres import (
    PostgresRagRetrievalRepository,
)


class _EmptyResult:
    def all(self):
        return []


class _RecordingSession:
    def __init__(self) -> None:
        self.statements = []

    async def execute(self, statement):
        self.statements.append(statement)
        return _EmptyResult()


def test_hnsw_search_config_reads_bounded_environment(monkeypatch):
    monkeypatch.setenv("JARVIS_RAG_HNSW_EF_SEARCH", "120")
    monkeypatch.setenv("JARVIS_RAG_HNSW_ITERATIVE_SCAN", "strict_order")
    monkeypatch.setenv("JARVIS_RAG_HNSW_MAX_SCAN_TUPLES", "30000")
    monkeypatch.setenv("JARVIS_RAG_HNSW_SCAN_MEM_MULTIPLIER", "2")

    config = HnswSearchConfig.from_env()

    assert config.transaction_settings() == (
        ("hnsw.ef_search", "120"),
        ("hnsw.iterative_scan", "strict_order"),
        ("hnsw.max_scan_tuples", "30000"),
        ("hnsw.scan_mem_multiplier", "2"),
    )


@pytest.mark.parametrize(
    ("name", "value", "message"),
    [
        ("JARVIS_RAG_HNSW_EF_SEARCH", "0", "1..1000"),
        ("JARVIS_RAG_HNSW_ITERATIVE_SCAN", "invalid", "仅支持"),
        ("JARVIS_RAG_HNSW_MAX_SCAN_TUPLES", "many", "必须是整数"),
        ("JARVIS_RAG_HNSW_SCAN_MEM_MULTIPLIER", "0", "1..1000"),
    ],
)
def test_hnsw_search_config_rejects_invalid_environment(monkeypatch, name, value, message):
    monkeypatch.setenv(name, value)

    with pytest.raises(ValueError, match=message):
        HnswSearchConfig.from_env()


@pytest.mark.asyncio
async def test_semantic_search_applies_transaction_local_hnsw_settings_and_knn_order():
    session = _RecordingSession()
    repository = PostgresRagRetrievalRepository(
        session,
        hnsw_config=HnswSearchConfig(
            ef_search=80,
            iterative_scan="relaxed_order",
            max_scan_tuples=10_000,
            scan_mem_multiplier=2,
        ),
    )
    vector = [0.0] * 1_536
    vector[0] = 1.0

    matches = await repository.search_candidates(
        workspace_id=uuid4(),
        query_vector=vector,
        provider_name="openai",
        model_name="text-embedding-3-small",
        document_ids=(),
        limit=30,
    )

    assert matches == []
    assert len(session.statements) == 2
    settings = session.statements[0].compile(dialect=postgresql.dialect())
    assert str(settings).count("set_config(") == 4
    assert set(settings.params.values()) >= {
        "hnsw.ef_search",
        "80",
        "hnsw.iterative_scan",
        "relaxed_order",
        "hnsw.max_scan_tuples",
        "10000",
        "hnsw.scan_mem_multiplier",
        "2",
        True,
    }

    query = session.statements[1]
    assert len(query._order_by_clauses) == 1
    compiled_query = str(query.compile(dialect=postgresql.dialect()))
    order_by = compiled_query.split(" ORDER BY ", maxsplit=1)[1]
    assert "rag_chunk_embeddings.embedding <=>" in order_by
    assert "rag_chunks.id" not in order_by
