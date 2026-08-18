from __future__ import annotations

import pytest

import jarvis_worker.database.outbox.reconciliation as reconciliation_module
from jarvis_worker.database.outbox.reconciliation import ReconciliationJob


class _Context:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return None

    def begin(self):
        return self


class _OutboxRepository:
    def __init__(self, _session):
        pass

    async def reset_stale_dispatching(self, *, stale_seconds):
        assert stale_seconds == 60
        return 2


class _RunService:
    async def reconcile_stale_queued_runs(self, *, queue_event_exists, stale_seconds):
        assert stale_seconds == 60
        assert await queue_event_exists("event") is True
        return {"queued_runs_requeued": 4, "queued_runs_failed_closed": 0}

    async def reconcile_expired_runs(self):
        return {"runs_rescheduled": 1, "runs_failed_closed": 0}


class _PermissionService:
    def __init__(self):
        self.calls = 0

    async def expire_pending_requests(self):
        self.calls += 1
        return 3


class _Redis:
    def __init__(self, exists):
        self.exists_result = exists
        self.keys = []

    async def exists(self, key):
        self.keys.append(key)
        return self.exists_result


@pytest.mark.asyncio
async def test_reconciliation_includes_durable_permission_expiry(monkeypatch):
    permission_service = _PermissionService()
    monkeypatch.setattr(reconciliation_module, "get_session_factory", lambda: lambda: _Context())
    monkeypatch.setattr(reconciliation_module, "PostgresOutboxRepository", _OutboxRepository)
    job = ReconciliationJob(
        run_service=_RunService(),
        permission_service=permission_service,
    )

    result = await job.run_once()

    assert result == {
        "stale_dispatching_reset": 2,
        "queued_runs_requeued": 4,
        "queued_runs_failed_closed": 0,
        "runs_rescheduled": 1,
        "runs_failed_closed": 0,
        "permissions_expired": 3,
    }
    assert permission_service.calls == 1


@pytest.mark.asyncio
async def test_queue_event_evidence_uses_outbox_dedupe_key():
    redis = _Redis(False)
    job = ReconciliationJob(redis_client=redis)

    exists = await job._queue_event_exists("event-123")

    assert exists is False
    assert redis.keys == ["jarvis:outbox:dedupe:event-123"]
