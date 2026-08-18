from datetime import datetime, timezone
from uuid import uuid4

import pytest

from jarvis_worker.runtime.runs import service as run_service_module
from jarvis_worker.shared.errors.application import AppError
from jarvis_worker.runtime.runs.service import RunApplicationService
from jarvis_worker.shared.domain.models import (
    AgentRun,
    PermissionStatus,
    RunStatus,
    RuntimeEvent,
    Task,
    TaskStatus,
)


class _SessionContext:
    async def __aenter__(self):
        return object()

    async def __aexit__(self, *_args):
        return None


class _Repo:
    def __init__(self, item=None):
        self.items = {} if item is None else {item.id: item}

    async def get(self, item_id):
        return self.items.get(item_id)

    async def create(self, item):
        self.items[item.id] = item
        return item


class _Permissions(_Repo):
    async def get_request(self, request_id):
        return await self.get(request_id)

    async def get_request_for_update(self, request_id):
        return await self.get(request_id)

    async def create_request(self, request):
        return await self.create(request)

    async def update_request(self, request):
        self.items[request.id] = request


class _Events:
    def __init__(self, run_id):
        self.run_id = run_id
        self.items = [
            RuntimeEvent(
                id=uuid4(), event_id=uuid4(), type="agent.run.started",
                payload={}, run_id=run_id, event_sequence=1,
            )
        ]

    async def list_by_run(self, _run_id):
        return list(self.items)

    async def get_next_sequence(self, _run_id):
        return len(self.items) + 1

    async def append(self, events):
        self.items.extend(events)


class _Collector:
    def __init__(self):
        self.items = []

    async def create(self, value):
        if isinstance(value, list):
            self.items.extend(value)
        else:
            self.items.append(value)
        return value


class _FakeUow:
    def __init__(self, task, run):
        self.tasks = _Repo(task)
        self.runs = _Repo(run)
        self.permissions = _Permissions()
        self.events = _Events(run.id)
        self.audits = _Collector()
        self.outbox = _Collector()

    def transaction(self):
        return self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return None

    async def commit(self):
        return None


def _service(monkeypatch):
    task_id, run_id = uuid4(), uuid4()
    task = Task(
        id=task_id, conversation_id=uuid4(), title="repair",
        user_goal="repair safely", status=TaskStatus.FAILED, active_run_id=run_id,
    )
    run = AgentRun(
        id=run_id, task_id=task_id, status=RunStatus.FAILED,
        failed_at=datetime.now(timezone.utc),
        error={
            "code": "MODEL_PROVIDER_ERROR",
            "message": "模型调用失败",
            "category": "model",
            "recoverable": False,
        },
    )
    uow = _FakeUow(task, run)
    monkeypatch.setattr(run_service_module, "PostgresUnitOfWork", lambda _session: uow)
    return RunApplicationService(lambda: lambda: _SessionContext()), uow, run


@pytest.mark.asyncio
async def test_terminal_event_repair_requires_l3_and_appends_audited_outbox(monkeypatch):
    service, uow, run = _service(monkeypatch)
    inspection = await service.inspect_terminal_event_repair(run.id)
    assert inspection.eligible is True
    request = await service.create_terminal_event_repair_request(run.id)
    assert request.risk_level == "L3"
    assert request.allowed_decisions == ["allow_once", "deny"]

    resolution = await service.resolve_terminal_event_repair_request(
        request.id, "allow_once", "repair observed inconsistency"
    )

    assert resolution.request.status == PermissionStatus.CONSUMED
    assert resolution.repaired_event_type == "agent.run.failed"
    assert [event.type for event in uow.events.items].count("agent.run.failed") == 1
    assert uow.events.items[-1].event_sequence == 2
    assert uow.outbox.items[-1].event_type == "agent.run.failed"
    assert uow.audits.items[-1].event_type == "runtime.repair.applied"
    assert uow.audits.items[-1].permission_decision == "allow_once"

    repeated = await service.resolve_terminal_event_repair_request(
        request.id, "allow_once"
    )
    assert repeated.repaired_event_id == resolution.repaired_event_id
    assert [event.type for event in uow.events.items].count("agent.run.failed") == 1


@pytest.mark.asyncio
async def test_terminal_event_repair_deny_is_audited_without_event(monkeypatch):
    service, uow, run = _service(monkeypatch)
    request = await service.create_terminal_event_repair_request(run.id)
    resolution = await service.resolve_terminal_event_repair_request(
        request.id, "deny", "preserve history"
    )
    assert resolution.request.status == PermissionStatus.DENIED
    assert len(uow.events.items) == 1
    assert uow.audits.items[-1].permission_decision == "deny"


@pytest.mark.asyncio
async def test_terminal_event_repair_revalidates_before_approval(monkeypatch):
    service, uow, run = _service(monkeypatch)
    request = await service.create_terminal_event_repair_request(run.id)
    uow.events.items.append(RuntimeEvent(
        id=uuid4(), event_id=uuid4(), type="agent.run.failed",
        payload={}, run_id=run.id, event_sequence=2,
    ))
    with pytest.raises(AppError) as exc:
        await service.resolve_terminal_event_repair_request(request.id, "allow_once")
    assert exc.value.code == "REPAIR_ALREADY_APPLIED"
