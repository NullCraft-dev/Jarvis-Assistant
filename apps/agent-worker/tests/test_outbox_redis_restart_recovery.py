"""Outbox transport retry budget and Redis restart recovery regression tests."""

from datetime import datetime, timezone
from importlib import import_module
from types import SimpleNamespace
from uuid import uuid4

import pytest
from redis.exceptions import NoScriptError

from jarvis_worker.database.models import OutboxEventModel
from jarvis_worker.database.outbox.publisher import OutboxPublisher
from jarvis_worker.database.outbox.repository import PostgresOutboxRepository
from jarvis_worker.shared.domain.models import OutboxEvent


class _Session:
    def __init__(self, event):
        self.event = event

    async def get(self, _model, _event_id):
        return self.event


def _event(*, retry_count: int, max_retries: int = 20):
    return SimpleNamespace(
        retry_count=retry_count,
        max_retries=max_retries,
        status="dispatching",
        claimed_by="publisher-test",
        claimed_at=datetime.now(timezone.utc),
        lease_until=datetime.now(timezone.utc),
        next_retry_at=datetime.now(timezone.utc),
        error_code=None,
        error_message=None,
    )


def test_outbox_defaults_cover_bounded_redis_restart_window():
    event = OutboxEvent(
        id=uuid4(),
        event_id=uuid4(),
        aggregate_type="agent_run",
        aggregate_id=uuid4(),
        event_type="task.created",
        schema_version="v1",
        payload={},
        trace_id=uuid4(),
    )
    assert event.max_retries == 20
    assert OutboxEventModel.__table__.c.max_retries.default.arg == 20


@pytest.mark.asyncio
async def test_transient_redis_failure_remains_pending_within_new_budget():
    event = _event(retry_count=4)
    await PostgresOutboxRepository(_Session(event)).mark_failed(
        [uuid4()], "REDIS_PUBLISH_ERROR", "connection reset"
    )
    assert event.retry_count == 5
    assert event.status == "pending"
    assert event.next_retry_at > datetime.now(timezone.utc)
    assert event.claimed_by is None
    assert event.lease_until is None


@pytest.mark.asyncio
async def test_outbox_retry_budget_is_still_finite():
    event = _event(retry_count=19)
    await PostgresOutboxRepository(_Session(event)).mark_failed(
        [uuid4()], "REDIS_PUBLISH_ERROR", "connection reset"
    )
    assert event.retry_count == 20
    assert event.status == "dead"
    assert event.error_code == "REDIS_PUBLISH_ERROR"


def test_migration_requeues_only_redis_transport_dead_events(monkeypatch):
    migration = import_module("jarvis_worker.migrations.versions.018_outbox_redis_restart_recovery")
    assert len(migration.revision) <= 32
    assert migration.down_revision == "017_rag_evaluation_flywheel"
    statements: list[str] = []
    monkeypatch.setattr(migration.op, "alter_column", lambda *args, **kwargs: None)
    monkeypatch.setattr(migration.op, "create_check_constraint", lambda *args, **kwargs: None)
    monkeypatch.setattr(migration.op, "execute", statements.append)

    migration.upgrade()

    normalized = " ".join(statements[-1].split())
    assert "status = 'dead'" in normalized
    assert "error_code = 'REDIS_PUBLISH_ERROR'" in normalized
    assert "retry_count < 20" in normalized
    assert "status = 'pending'" in normalized


class _RestartedAsyncRedis:
    def __init__(self):
        self.evalsha_calls = 0
        self.script_load_calls = 0

    async def evalsha(self, _sha, _numkeys, *_args):
        self.evalsha_calls += 1
        if self.evalsha_calls == 1:
            raise NoScriptError("No matching script. Please use EVAL.")
        return "redis-stream-id"

    async def script_load(self, _script):
        self.script_load_calls += 1
        return "reloaded-sha"


@pytest.mark.asyncio
async def test_outbox_reloads_lua_script_after_redis_restart():
    redis_client = _RestartedAsyncRedis()
    publisher = OutboxPublisher(redis_client)
    publisher._lua_sha = "cached-before-restart"

    result = await publisher._eval_atomic_xadd(["dedupe", "stream"])

    assert result == "redis-stream-id"
    assert redis_client.script_load_calls == 1
    assert redis_client.evalsha_calls == 2
