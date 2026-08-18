from datetime import timedelta
from uuid import uuid4

import pytest

import jarvis_worker.runtime.permissions.service as permission_service_module
from jarvis_worker.runtime.permissions.service import PermissionApplicationService
from jarvis_worker.shared.domain.models import (
    AgentRun,
    PermissionRequest,
    PermissionStatus,
    RunStatus,
    Task,
    TaskStatus,
    ToolCall,
    utcnow,
)
from jarvis_worker.shared.errors.application import AppError


class _SessionContext:
    async def __aenter__(self):
        return object()

    async def __aexit__(self, *_args):
        return None


class _CollectingRepo:
    def __init__(self):
        self.values = []

    async def create(self, values):
        self.values.extend(values if isinstance(values, list) else [values])


class _Permissions:
    def __init__(self, request):
        self.request = request

    async def get_request(self, request_id):
        return self.request if request_id == self.request.id else None

    async def get_request_for_update(self, request_id):
        return await self.get_request(request_id)

    async def update_request(self, request):
        self.request = request

    async def create_grant(self, grant):
        return grant

    async def list_expired_pending_for_update(self, now, limit=32):
        if (
            self.request.status is PermissionStatus.PENDING
            and self.request.expires_at is not None
            and self.request.expires_at <= now
        ):
            return [self.request]
        return []


class _Runs:
    def __init__(self, run):
        self.run = run

    async def get(self, run_id):
        return self.run if run_id == self.run.id else None

    async def update_with_lock(self, **values):
        if self.run.version != values["expected_version"]:
            return False
        if self.run.status.value != values["expected_status"]:
            return False
        self.run.status = RunStatus(values["new_status"])
        self.run.version += 1
        self.run.error = values.get("error_json")
        self.run.failed_at = values.get("failed_at")
        self.run.checkpoint = values.get("checkpoint_json", self.run.checkpoint)
        return True


class _ToolCalls:
    def __init__(self, tool_call):
        self.tool_call = tool_call

    async def get(self, tool_call_id):
        return self.tool_call if tool_call_id == self.tool_call.id else None

    async def update(self, tool_call):
        self.tool_call = tool_call


class _Tasks:
    def __init__(self, task):
        self.task = task

    async def get(self, task_id):
        return self.task if task_id == self.task.id else None

    async def update(self, task):
        self.task = task


class _Steps:
    async def get(self, _step_id):
        return None


class _Events:
    def __init__(self):
        self.values = []

    async def get_next_sequence(self, _run_id):
        return len(self.values) + 1

    async def append(self, events):
        self.values.extend(events)


class _Uow:
    def __init__(self, request, run, tool_call, task=None):
        self.permissions = _Permissions(request)
        self.runs = _Runs(run)
        self.tool_calls = _ToolCalls(tool_call)
        self.tasks = _Tasks(task or Task(
            id=request.task_id,
            title="permission test",
            user_goal="test",
            conversation_id=uuid4(),
            status=TaskStatus.WAITING_FOR_USER,
        ))
        self.steps = _Steps()
        self.events = _Events()
        self.outbox = _CollectingRepo()
        self.audits = _CollectingRepo()

    def transaction(self):
        return self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return None

    async def commit(self):
        return None


@pytest.mark.asyncio
async def test_permission_decision_atomically_updates_tool_call_projection(monkeypatch):
    task_id, run_id, step_id, tool_call_id, request_id = (
        uuid4(), uuid4(), uuid4(), uuid4(), uuid4()
    )
    run = AgentRun(id=run_id, task_id=task_id, status=RunStatus.WAITING_PERMISSION)
    request = PermissionRequest(
        id=request_id, task_id=task_id, run_id=run_id, step_id=step_id,
        tool_call_id=tool_call_id, tool_name="literature.download_arxiv_pdf",
        action_summary="下载 PDF", risk_level="L2", scope={"type": "once"},
        arguments_summary={}, allowed_decisions=["allow_once", "deny"],
        status=PermissionStatus.PENDING,
        expires_at=run.created_at + timedelta(minutes=15),
    )
    tool_call = ToolCall(
        id=tool_call_id, task_id=task_id, run_id=run_id, step_id=step_id,
        provider="native", tool_name=request.tool_name, risk_level="L2",
        arguments={}, status="running", permission_status="pending",
    )
    uow = _Uow(request, run, tool_call)
    monkeypatch.setattr(
        permission_service_module, "PostgresUnitOfWork", lambda _session: uow
    )
    service = PermissionApplicationService(lambda: lambda: _SessionContext())

    decided = await service.decide(request_id, "allow_once")

    assert decided.status == PermissionStatus.APPROVED
    assert tool_call.permission_request_id == request_id
    assert tool_call.permission_status == "approved"


@pytest.mark.asyncio
async def test_expired_permission_click_fails_closed_and_emits_terminal_events(monkeypatch):
    task_id, run_id, tool_call_id, request_id = (uuid4() for _ in range(4))
    now = utcnow()
    run = AgentRun(id=run_id, task_id=task_id, status=RunStatus.WAITING_PERMISSION)
    request = PermissionRequest(
        id=request_id,
        task_id=task_id,
        run_id=run_id,
        tool_call_id=tool_call_id,
        tool_name="workspace.create_file",
        action_summary="创建文件",
        risk_level="L2",
        scope={"type": "once"},
        arguments_summary={},
        allowed_decisions=["allow_once", "deny"],
        status=PermissionStatus.PENDING,
        created_at=now - timedelta(minutes=2),
        expires_at=now - timedelta(minutes=1),
    )
    tool_call = ToolCall(
        id=tool_call_id,
        task_id=task_id,
        run_id=run_id,
        step_id=uuid4(),
        provider="native",
        tool_name=request.tool_name,
        risk_level="L2",
        arguments={},
        status="running",
        permission_status="pending",
    )
    uow = _Uow(request, run, tool_call)
    monkeypatch.setattr(
        permission_service_module, "PostgresUnitOfWork", lambda _session: uow
    )
    service = PermissionApplicationService(lambda: lambda: _SessionContext())

    with pytest.raises(AppError) as captured:
        await service.decide(request_id, "allow_once")

    assert captured.value.code == "PERMISSION_NOT_PENDING"
    assert request.status is PermissionStatus.EXPIRED
    assert request.decision is None
    assert tool_call.permission_status == "expired"
    assert tool_call.status == "failed"
    assert tool_call.error["code"] == "PERMISSION_REQUEST_EXPIRED"
    assert run.status is RunStatus.FAILED
    assert uow.tasks.task.status is TaskStatus.FAILED
    assert [event.type for event in uow.events.values] == [
        "permission.expired",
        "agent.run.failed",
    ]
    assert len(uow.outbox.values) == 2
    assert uow.audits.values[0].result_summary == "expired"


@pytest.mark.asyncio
async def test_expiry_reconciliation_is_idempotent(monkeypatch):
    task_id, run_id, tool_call_id, request_id = (uuid4() for _ in range(4))
    now = utcnow()
    run = AgentRun(id=run_id, task_id=task_id, status=RunStatus.WAITING_PERMISSION)
    request = PermissionRequest(
        id=request_id,
        task_id=task_id,
        run_id=run_id,
        tool_call_id=tool_call_id,
        tool_name="workspace.create_file",
        action_summary="创建文件",
        risk_level="L2",
        scope={"type": "once"},
        arguments_summary={},
        status=PermissionStatus.PENDING,
        expires_at=now - timedelta(seconds=1),
    )
    tool_call = ToolCall(
        id=tool_call_id,
        task_id=task_id,
        run_id=run_id,
        step_id=uuid4(),
        provider="native",
        tool_name=request.tool_name,
        risk_level="L2",
        arguments={},
        status="running",
        permission_status="pending",
    )
    uow = _Uow(request, run, tool_call)
    monkeypatch.setattr(
        permission_service_module, "PostgresUnitOfWork", lambda _session: uow
    )
    service = PermissionApplicationService(lambda: lambda: _SessionContext())

    assert await service.expire_pending_requests() == 1
    assert await service.expire_pending_requests() == 0
    assert len(uow.events.values) == 2
    assert len(uow.audits.values) == 1
