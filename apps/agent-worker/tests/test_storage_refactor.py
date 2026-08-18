"""Storage 重构关键不变量测试。"""

from __future__ import annotations

import json
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest

import jarvis_worker.runtime.tasks.service as task_service_module
from jarvis_worker.database.outbox.publisher import (
    EVENT_TO_RUNTIME_STREAM,
    EVENT_TO_STREAM,
    _build_xadd_fields,
)
from jarvis_worker.runtime.events import (
    build_envelope,
    build_runtime_event,
    deterministic_event_id,
    deterministic_step_id,
)
from jarvis_worker.runtime.permissions.service import validate_permission_decision
from jarvis_worker.runtime.service import DURABLE_EVENT_TYPES, RuntimeApplicationService
from jarvis_worker.runtime.tasks.service import CreateTaskInput, TaskApplicationService
from jarvis_worker.runtime.worker import AgentWorker
from jarvis_worker.runtime_bus.messages import RunJobMessage
from jarvis_worker.shared.domain.models import PermissionRequest
from jarvis_worker.shared.errors.application import AppError


class _Consumer:
    consumer_name = "test"
    group = "test"


class _Producer:
    def __init__(self):
        self.events = []

    def publish(self, envelope):
        self.events.append(envelope)
        return "1-0"


class _Runner:
    worker_id = "worker-test"


class _RunService:
    def __init__(self):
        self.claimed = []

    async def claim_job(self, run_id, worker_id, source_event_id):
        self.claimed.append((run_id, worker_id, source_event_id))
        return object(), "execute"


class _RuntimeService:
    def __init__(self):
        self.events = []

    @staticmethod
    def is_durable(event_type):
        return RuntimeApplicationService.is_durable(event_type)

    async def record_envelope(self, envelope):
        self.events.append(envelope)
        return True


def test_runtime_ids_are_deterministic_postgres_uuids():
    run_id = str(uuid4())
    event_id = deterministic_event_id(run_id, "agent.run.started", 1)
    step_id = deterministic_step_id(run_id, 1)
    assert UUID(event_id)
    assert UUID(step_id)
    assert event_id == deterministic_event_id(run_id, "agent.run.started", 1)


def test_model_call_audit_events_are_durable_and_publishable():
    for event_type in (
        "model.call.started",
        "model.call.completed",
        "model.call.failed",
    ):
        assert RuntimeApplicationService.is_durable(event_type)
        assert event_type in EVENT_TO_RUNTIME_STREAM


def test_every_durable_event_has_an_outbox_transport_route():
    routed_event_types = EVENT_TO_RUNTIME_STREAM | set(EVENT_TO_STREAM)

    assert DURABLE_EVENT_TYPES <= routed_event_types
    assert all(
        (event_type in EVENT_TO_RUNTIME_STREAM) + (event_type in EVENT_TO_STREAM) == 1
        for event_type in DURABLE_EVENT_TYPES
    )


def test_worker_persists_durable_event_and_directly_publishes_transient_event():
    producer = _Producer()
    run_service = _RunService()
    runtime_service = _RuntimeService()
    worker = AgentWorker(
        client=object(), consumer=_Consumer(), producer=producer, runner=_Runner(),
        run_service=run_service, event_service=runtime_service,
    )
    task_id, run_id, trace_id = str(uuid4()), str(uuid4()), str(uuid4())
    job = RunJobMessage(
        job_id=str(uuid4()), trace_id=trace_id, task_id=task_id, run_id=run_id,
        user_goal="test", created_at="2026-07-14T00:00:00+00:00",
    )
    durable = build_envelope(
        build_runtime_event(
            "agent.run.started", task_id, run_id,
            event_id=deterministic_event_id(run_id, "agent.run.started", 1),
        ), trace_id, "worker-test",
    )
    transient = build_envelope(
        build_runtime_event("model.delta", task_id, run_id), trace_id, "worker-test"
    )
    model_call = build_envelope(
        build_runtime_event("model.call.started", task_id, run_id), trace_id, "worker-test"
    )
    try:
        assert worker._claim_job(job) == "execute"
        assert worker._publish_or_persist(durable) == "outbox"
        assert worker._publish_or_persist(model_call) == "outbox"
        assert worker._publish_or_persist(transient) == "1-0"
        assert runtime_service.events == [durable, model_call]
        assert producer.events == [transient]
        assert run_service.claimed[0][2] == job.job_id
    finally:
        worker._service_bridge.close()


def test_l4_permission_cannot_be_persistently_allowed():
    req = PermissionRequest(
        id=uuid4(), task_id=uuid4(), run_id=uuid4(), tool_name="shell",
        action_summary="危险操作", risk_level="L4", scope={"type": "workspace"},
        arguments_summary={},
        allowed_decisions=["allow_once", "always_allow_for_workspace", "deny"],
    )
    validate_permission_decision(req, "allow_once")
    with pytest.raises(AppError, match="不允许永久授权"):
        validate_permission_decision(req, "always_allow_for_workspace")


def test_permission_decision_must_be_declared_by_request():
    req = PermissionRequest(
        id=uuid4(), task_id=uuid4(), run_id=uuid4(), tool_name="shell",
        action_summary="执行命令", risk_level="L3", scope={"type": "once"},
        arguments_summary={}, allowed_decisions=["allow_once", "deny"],
    )
    with pytest.raises(AppError, match="不允许决定"):
        validate_permission_decision(req, "allow_for_task")


def test_cancel_outbox_uses_worker_command_type():
    command_id, trace_id, task_id, run_id = (uuid4() for _ in range(4))
    event = SimpleNamespace(
        payload={
            "command_id": str(command_id), "trace_id": str(trace_id),
            "task_id": str(task_id), "run_id": str(run_id),
            "type": "run.cancel", "requested_at": "2026-07-14T00:00:00+00:00",
            "schema_version": "2B-1a.1",
        },
        event_type="run.cancel.requested", schema_version="2B-1a.1",
        event_id=command_id, trace_id=trace_id,
        created_at="2026-07-14T00:00:00+00:00",
    )

    fields = _build_xadd_fields(event)
    assert fields == {
        "schema_version": "2B-1a.1",
        "payload": json.dumps(event.payload, ensure_ascii=False),
        "command_id": str(command_id), "trace_id": str(trace_id),
        "task_id": str(task_id), "run_id": str(run_id),
        "type": "run.cancel", "requested_at": "2026-07-14T00:00:00+00:00",
    }


def test_runtime_event_outbox_uses_complete_transport_routing():
    event_id, trace_id, task_id, run_id = (uuid4() for _ in range(4))
    payload = {
        "event_id": str(event_id), "trace_id": str(trace_id),
        "task_id": str(task_id), "run_id": str(run_id),
        "event_type": "agent.run.completed", "produced_by": "worker-01",
        "schema_version": "2B-1a.1",
        "runtime_event": {
            "id": str(event_id), "type": "agent.run.completed",
            "task_id": str(task_id), "run_id": str(run_id),
            "timestamp": "2026-07-22T00:00:00+00:00", "payload": {},
        },
    }
    event = SimpleNamespace(
        payload=payload, event_type="agent.run.completed",
        schema_version="2B-1a.1", event_id=event_id,
        trace_id=trace_id, created_at="2026-07-22T00:00:00+00:00",
    )

    fields = _build_xadd_fields(event)

    assert fields["event_id"] == str(event_id)
    assert fields["task_id"] == str(task_id)
    assert fields["run_id"] == str(run_id)
    assert fields["type"] == "agent.run.completed"
    assert fields["produced_by"] == "worker-01"


def test_task_created_outbox_uses_run_job_transport_contract():
    job_id, trace_id, task_id, run_id = (uuid4() for _ in range(4))

    event = SimpleNamespace(
        payload={
            "job_id": str(job_id),
            "trace_id": str(trace_id),
            "task_id": str(task_id),
            "run_id": str(run_id),
            "user_goal": "contract test",
            "created_at": "2026-07-22T00:00:00+00:00",
            "schema_version": "2B-1a.1",
        },
        event_type="task.created",
        schema_version="2B-1a.1",
        event_id=job_id,
        trace_id=trace_id,
        created_at="2026-07-22T00:00:00+00:00",
    )

    fields = _build_xadd_fields(event)

    assert fields == {
        "schema_version": "2B-1a.1",
        "payload": json.dumps(event.payload, ensure_ascii=False),
        "job_id": str(job_id),
        "trace_id": str(trace_id),
        "task_id": str(task_id),
        "run_id": str(run_id),
        "type": "run.job",
        "created_at": "2026-07-22T00:00:00+00:00",
    }


def test_run_job_outbox_rejects_missing_routing_fields():
    class Event:
        payload = {"job_id": str(uuid4())}
        event_type = "run.resume.requested"
        schema_version = "2B-1a.1"
        event_id = uuid4()
        trace_id = uuid4()
        created_at = "2026-07-22T00:00:00+00:00"

    with pytest.raises(ValueError, match="RunJob payload 缺少字段"):
        _build_xadd_fields(Event())


@pytest.mark.asyncio
async def test_create_task_flushes_parent_rows_before_child_rows(monkeypatch):
    """防止 Conversation/Task/Run 在 autoflush 时发生 FK 乱序。"""
    operations = []
    created = {}

    class Repo:
        def __init__(self, name):
            self.name = name

        async def create(self, value):
            operations.append(f"create:{self.name}")
            created[self.name] = value
            return value

    class EventRepo(Repo):
        async def get_next_sequence(self, run_id):
            operations.append("next_sequence")
            return 1

        async def append(self, events):
            operations.append("create:event")

    class FakeUow:
        def __init__(self):
            self.conversations = Repo("conversation")
            self.tasks = Repo("task")
            self.messages = Repo("message")
            self.runs = Repo("run")
            self.events = EventRepo("event")
            self.outbox = Repo("outbox")

        def transaction(self):
            return self

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def flush(self):
            operations.append("flush")

        async def commit(self):
            operations.append("commit")

        async def rollback(self):
            operations.append("rollback")

    class SessionContext:
        async def __aenter__(self):
            return object()

        async def __aexit__(self, *args):
            return None

    fake_uow = FakeUow()
    monkeypatch.setattr(task_service_module, "PostgresUnitOfWork", lambda session: fake_uow)
    service = TaskApplicationService(lambda: lambda: SessionContext())

    secret = "sk-proj-1234567890abcdefghijklmnop"
    await service.create_task(CreateTaskInput(user_goal=f"请保存 API key {secret}"))

    assert operations[:7] == [
        "create:conversation", "flush",
        "create:task", "flush",
        "create:message", "create:run", "flush",
    ]
    assert operations[-1] == "commit"
    assert secret not in created["conversation"].title
    assert secret not in created["task"].title
    assert "[已隐藏凭据]" in created["task"].title
    assert secret in created["task"].user_goal
    assert secret in created["message"].content
