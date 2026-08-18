from __future__ import annotations

import asyncio
from collections import deque

import pytest

from jarvis_worker.agent.rag.worker import RagWorker, RagWorkerConfig


class _Stage:
    def __init__(self, name: str, responses, calls: list[str]) -> None:
        self._name = name
        self._responses = deque(responses)
        self._calls = calls

    async def process_next(self, *, worker_id: str):
        self._calls.append(f"{self._name}:{worker_id}")
        if not self._responses:
            return None
        response = self._responses.popleft()
        if isinstance(response, BaseException):
            raise response
        return response


async def _wait_until(predicate, *, timeout: float = 1.0) -> None:
    async with asyncio.timeout(timeout):
        while not predicate():
            await asyncio.sleep(0.005)


@pytest.mark.asyncio
async def test_rag_worker_fairly_drains_ingestion_and_embedding() -> None:
    calls: list[str] = []
    ingestion = _Stage("ingestion", [object(), object(), None], calls)
    embedding = _Stage("embedding", [object(), object(), None], calls)
    worker = RagWorker(
        worker_id="rag-worker-test",
        ingestion_service=ingestion,
        embedding_service=embedding,
        poll_interval=0.01,
        error_backoff=0.01,
    )

    task = asyncio.create_task(worker.run_forever())
    await _wait_until(lambda: worker.stats.embedding_processed == 2)
    worker.stop()
    await task

    assert calls[:4] == [
        "ingestion:rag-worker-test",
        "embedding:rag-worker-test",
        "ingestion:rag-worker-test",
        "embedding:rag-worker-test",
    ]
    assert worker.stats.ingestion_processed == 2
    assert worker.stats.embedding_processed == 2
    assert worker.stats.cycle_errors == 0


@pytest.mark.asyncio
async def test_rag_worker_recovers_after_cycle_error() -> None:
    calls: list[str] = []
    ingestion = _Stage("ingestion", [RuntimeError("temporary"), object(), None], calls)
    embedding = _Stage("embedding", [object(), None], calls)
    worker = RagWorker(
        worker_id="rag-worker-test",
        ingestion_service=ingestion,
        embedding_service=embedding,
        poll_interval=0.01,
        error_backoff=0.01,
    )

    task = asyncio.create_task(worker.run_forever())
    await _wait_until(
        lambda: worker.stats.ingestion_processed == 1 and worker.stats.embedding_processed == 1
    )
    worker.stop()
    await task

    assert worker.stats.cycle_errors == 1
    assert worker.stats.ingestion_processed == 1
    assert worker.stats.embedding_processed == 1


@pytest.mark.asyncio
async def test_rag_worker_stop_interrupts_idle_wait() -> None:
    calls: list[str] = []
    worker = RagWorker(
        worker_id="rag-worker-test",
        ingestion_service=_Stage("ingestion", [], calls),
        embedding_service=_Stage("embedding", [], calls),
        poll_interval=60,
        error_backoff=60,
    )

    task = asyncio.create_task(worker.run_forever())
    await _wait_until(lambda: len(calls) >= 2)
    worker.stop()
    await asyncio.wait_for(task, timeout=0.2)


@pytest.mark.asyncio
async def test_rag_worker_reports_runtime_status_transitions() -> None:
    calls: list[str] = []
    statuses: list[str] = []
    worker = RagWorker(
        worker_id="rag-worker-test",
        ingestion_service=_Stage("ingestion", [object(), None], calls),
        embedding_service=_Stage("embedding", [None, None], calls),
        poll_interval=0.01,
        error_backoff=0.01,
        status_callback=statuses.append,
    )

    task = asyncio.create_task(worker.run_forever())
    await _wait_until(lambda: worker.stats.ingestion_processed == 1)
    worker.stop()
    await task

    assert statuses[0] == "idle"
    assert "busy" in statuses
    assert statuses.index("busy") < statuses.index("draining")
    assert statuses[-1] == "draining"


def test_rag_worker_config_is_independent_from_agent_model(monkeypatch) -> None:
    monkeypatch.delenv("JARVIS_MODEL_PROVIDER", raising=False)
    monkeypatch.delenv("JARVIS_MODEL_NAME", raising=False)
    monkeypatch.setenv("JARVIS_RAG_WORKER_ID", "rag-worker-local")
    monkeypatch.setenv("JARVIS_RAG_STRUCTURE_PROVIDER", "native-only")
    monkeypatch.setenv("JARVIS_RAG_PADDLEOCR_SITE_PACKAGES", "/tmp/paddle-client")
    monkeypatch.setenv("JARVIS_RAG_WORKER_POLL_INTERVAL_MS", "250")
    monkeypatch.setenv("JARVIS_RAG_JOB_LEASE_SECONDS", "15")

    config = RagWorkerConfig.from_env()

    assert config.worker_id == "rag-worker-local"
    assert config.redis_addr == "127.0.0.1:6379"
    assert config.structure_provider == "native-only"
    assert config.paddleocr_site_packages == "/tmp/paddle-client"
    assert config.poll_interval_ms == 250
    assert config.job_lease_seconds == 15


def test_rag_worker_uses_shared_redis_database_and_hides_password(monkeypatch) -> None:
    monkeypatch.setenv("JARVIS_REDIS_PASSWORD", "redis-secret")
    monkeypatch.setenv("JARVIS_REDIS_DB", "9")

    config = RagWorkerConfig.from_env()

    assert config.redis_password == "redis-secret"
    assert config.redis_db == 9
    assert "redis-secret" not in repr(config)


def test_rag_worker_config_rejects_unknown_structure_provider(monkeypatch) -> None:
    monkeypatch.setenv("JARVIS_RAG_STRUCTURE_PROVIDER", "remote-cloud")

    with pytest.raises(ValueError, match="JARVIS_RAG_STRUCTURE_PROVIDER"):
        RagWorkerConfig.from_env()


def test_rag_worker_config_rejects_unsafe_worker_id(monkeypatch) -> None:
    monkeypatch.setenv("JARVIS_RAG_WORKER_ID", "../../agent-worker")

    with pytest.raises(ValueError, match="JARVIS_RAG_WORKER_ID"):
        RagWorkerConfig.from_env()


def test_rag_worker_config_loads_asset_total_capacity(monkeypatch) -> None:
    monkeypatch.setenv("JARVIS_RAG_ASSET_MAX_FILE_BYTES", "1024")
    monkeypatch.setenv("JARVIS_RAG_ASSET_MAX_TOTAL_BYTES", "2048")

    config = RagWorkerConfig.from_env()

    assert config.asset_max_file_bytes == 1024
    assert config.asset_max_total_bytes == 2048


def test_rag_worker_config_rejects_inverted_asset_capacity(monkeypatch) -> None:
    monkeypatch.setenv("JARVIS_RAG_ASSET_MAX_FILE_BYTES", "2048")
    monkeypatch.setenv("JARVIS_RAG_ASSET_MAX_TOTAL_BYTES", "1024")

    with pytest.raises(ValueError, match="file <= total"):
        RagWorkerConfig.from_env()


@pytest.mark.parametrize("value", ["0", "4", "1801"])
def test_rag_worker_config_rejects_unbounded_job_lease(
    monkeypatch, value: str
) -> None:
    monkeypatch.setenv("JARVIS_RAG_JOB_LEASE_SECONDS", value)

    with pytest.raises(ValueError, match="JARVIS_RAG_JOB_LEASE_SECONDS"):
        RagWorkerConfig.from_env()
