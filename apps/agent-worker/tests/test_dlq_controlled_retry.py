from __future__ import annotations

from uuid import uuid4

import pytest

from jarvis_worker.runtime.runs import service as run_service_module
from jarvis_worker.shared.errors.application import AppError
from jarvis_worker.runtime.runs.service import (
    DLQ_RETRY_ERROR_CODE,
    DlqRetryEvidence,
    RunApplicationService,
)
from jarvis_worker.shared.domain.models import (
    AgentRun,
    PermissionStatus,
    RunStatus,
    Task,
    TaskStatus,
)
from jarvis_worker.database.outbox.publisher import _build_xadd_fields


class _SessionContext:
    async def __aenter__(self):
        return object()

    async def __aexit__(self, *_args):
        return None


class _Runs:
    def __init__(self, run):
        self.items = {run.id: run}

    async def get(self, run_id):
        return self.items.get(run_id)

    async def create(self, run):
        self.items[run.id] = run
        return run


class _Tasks:
    def __init__(self, task):
        self.items = {task.id: task}

    async def get(self, task_id):
        return self.items.get(task_id)

    async def update(self, _task):
        return None


class _Permissions:
    def __init__(self):
        self.items = {}

    async def get_request(self, request_id):
        return self.items.get(request_id)

    async def get_request_for_update(self, request_id):
        return self.items.get(request_id)

    async def create_request(self, request):
        self.items[request.id] = request
        return request

    async def update_request(self, request):
        self.items[request.id] = request


class _Collector:
    def __init__(self):
        self.items = []

    async def create(self, value):
        self.items.append(value)
        return value

    async def append(self, values):
        self.items.extend(values)


class _OutboxCollector(_Collector):
    async def create(self, values):
        self.items.extend(values)


class _Workspaces:
    async def get(self, _workspace_id):
        return None


class _FakeUow:
    def __init__(self, task, run):
        self.tasks = _Tasks(task)
        self.runs = _Runs(run)
        self.permissions = _Permissions()
        self.audits = _Collector()
        self.events = _Collector()
        self.outbox = _OutboxCollector()
        self.workspaces = _Workspaces()
        self.committed = 0

    def transaction(self):
        return self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return None

    async def commit(self):
        self.committed += 1

    async def flush(self):
        return None


def _service(monkeypatch):
    task_id, run_id = uuid4(), uuid4()
    task = Task(
        id=task_id,
        conversation_id=uuid4(),
        title="retry task",
        user_goal="retry safely",
        status=TaskStatus.FAILED,
        active_run_id=run_id,
    )
    run = AgentRun(
        id=run_id,
        task_id=task_id,
        status=RunStatus.FAILED,
        error={"code": DLQ_RETRY_ERROR_CODE},
    )
    uow = _FakeUow(task, run)
    monkeypatch.setattr(run_service_module, "PostgresUnitOfWork", lambda _session: uow)
    service = RunApplicationService(lambda: lambda: _SessionContext())
    evidence = DlqRetryEvidence(
        source="run_queue",
        record_id="10-0",
        original_message_id="9-0",
        error_code=DLQ_RETRY_ERROR_CODE,
        task_id=task_id,
        run_id=run_id,
        payload_sha256="a" * 64,
    )
    return service, uow, task, run, evidence


@pytest.mark.asyncio
async def test_dlq_retry_inspection_fails_closed_for_non_run_queue(monkeypatch):
    service, _uow, _task, _run, evidence = _service(monkeypatch)
    inspection = await service.inspect_dlq_retry(
        DlqRetryEvidence(**{**evidence.__dict__, "source": "worker_command"})
    )
    assert inspection.eligible is False
    assert inspection.reason_code == "DLQ_SOURCE_NOT_RETRYABLE"


@pytest.mark.asyncio
async def test_dlq_retry_deny_is_persisted_and_audited(monkeypatch):
    service, uow, _task, run, evidence = _service(monkeypatch)
    request = await service.create_dlq_retry_request(evidence)
    assert request.status == PermissionStatus.PENDING
    assert request.allowed_decisions == ["allow_once", "deny"]
    resolution = await service.resolve_dlq_retry_request(request.id, "deny", "not now")
    assert resolution.request.status == PermissionStatus.DENIED
    assert len(uow.runs.items) == 1
    assert uow.audits.items[-1].permission_decision == "deny"
    assert resolution.previous_run_id == run.id


@pytest.mark.asyncio
async def test_dlq_retry_approval_creates_new_run_without_replaying_payload(monkeypatch):
    service, uow, task, previous_run, evidence = _service(monkeypatch)
    request = await service.create_dlq_retry_request(evidence)
    resolution = await service.resolve_dlq_retry_request(request.id, "allow_once")

    assert resolution.request.status == PermissionStatus.CONSUMED
    assert resolution.new_run is not None
    assert resolution.new_run.id != previous_run.id
    assert resolution.new_run.status == RunStatus.QUEUED
    assert task.active_run_id == resolution.new_run.id
    assert task.status == TaskStatus.RUNNING
    assert uow.outbox.items[0].event_type == "run.retry.requested"
    assert uow.outbox.items[0].payload["user_goal"] == task.user_goal
    assert "payload" not in uow.outbox.items[0].payload
    fields = _build_xadd_fields(uow.outbox.items[0])
    assert fields["type"] == "run.job"
    assert fields["run_id"] == str(resolution.new_run.id)
    assert uow.audits.items[-1].permission_decision == "allow_once"
    repeated = await service.resolve_dlq_retry_request(request.id, "allow_once")
    assert repeated.new_run is not None
    assert repeated.new_run.id == resolution.new_run.id
    assert len(uow.runs.items) == 2


@pytest.mark.asyncio
async def test_dlq_retry_revalidates_authority_before_approval(monkeypatch):
    service, _uow, task, _run, evidence = _service(monkeypatch)
    request = await service.create_dlq_retry_request(evidence)
    task.active_run_id = uuid4()
    with pytest.raises(AppError) as exc:
        await service.resolve_dlq_retry_request(request.id, "allow_once")
    assert exc.value.code == "DLQ_TASK_STATE_CHANGED"
