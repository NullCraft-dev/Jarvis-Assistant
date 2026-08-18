"""AgentRun checkpoint reconciliation 与故障收口测试。"""

from __future__ import annotations

from datetime import timedelta
from uuid import uuid4

import pytest

import jarvis_worker.runtime.runs.service as run_service_module
from jarvis_worker.agent.core.checkpoint import (
    NON_RESUMABLE_NODES,
    RESUMABLE_NODES,
    build_run_checkpoint,
    restore_agent_state,
    validate_run_checkpoint,
)
from jarvis_worker.agent.core.state import AgentState
from jarvis_worker.agent.harness import RunBudget, RunSupervisor
from jarvis_worker.agent.intents import IntentExtraction, IntentRuntimeContext, RetrievalIntent
from jarvis_worker.agent.loop import (
    CompletionContract,
    LoopController,
    LoopProgressSnapshot,
    StopDecision,
)
from jarvis_worker.agent.permissions.manager import PermissionManager
from jarvis_worker.agent.tool_gateway.gateway import ToolGateway
from jarvis_worker.agent.tool_gateway.registry import ToolRegistry
from jarvis_worker.runtime.runs.service import RunApplicationService
from jarvis_worker.runtime_bus.messages import RunJobMessage
from jarvis_worker.shared.domain.models import (
    AgentRun,
    ExecutionStep,
    OutboxEvent,
    OutboxStatus,
    RunStatus,
    StepStatus,
    StepType,
    Task,
    TaskStatus,
    ToolCall,
    utcnow,
)


class _Runs:
    def __init__(self, run):
        self.run = run
        self.created = {}
        self.updates = []

    async def list_expired_running(self, now, limit=32):
        return [self.run]

    async def list_stale_queued(self, updated_before, limit=32):
        if self.run.status == RunStatus.QUEUED and self.run.updated_at <= updated_before:
            return [self.run]
        return []

    async def get(self, run_id):
        return self.run if self.run.id == run_id else self.created.get(run_id)

    async def create(self, run):
        self.created[run.id] = run
        return run

    async def update_with_lock(
        self, run_id, new_status, expected_version, expected_status=None, **fields
    ):
        if self.run.version != expected_version:
            return False
        if expected_status and self.run.status.value != expected_status:
            return False
        self.updates.append((new_status, fields))
        self.run.status = RunStatus(new_status)
        self.run.version += 1
        for key, value in fields.items():
            domain_key = {
                "error_json": "error", "checkpoint_json": "checkpoint",
                "metadata_json": "metadata",
            }.get(
                key, key
            )
            setattr(self.run, domain_key, value)
        return True


class _Tasks:
    def __init__(self, task):
        self.task = task

    async def get(self, task_id):
        return self.task if self.task.id == task_id else None

    async def update(self, task):
        self.task = task


class _CollectingRepo:
    def __init__(self):
        self.values = []
        self.latest_run_job = None

    async def create(self, values):
        if isinstance(values, list):
            self.values.extend(values)
        else:
            self.values.append(values)
        return values

    async def get_latest_run_job(self, run_id):
        return self.latest_run_job


class _Events(_CollectingRepo):
    async def get_next_sequence(self, run_id):
        return len(self.values) + 1

    async def append(self, values):
        self.values.extend(values)


class _Inbox:
    def __init__(self):
        self.values = set()

    async def try_insert(self, source, source_event_id):
        key = (source, source_event_id)
        if key in self.values:
            return False
        self.values.add(key)
        return True

    async def mark_processed(self, source, source_event_id):
        return None


class _FakeUow:
    def __init__(self, run, task):
        self.runs = _Runs(run)
        self.tasks = _Tasks(task)
        self.events = _Events()
        self.outbox = _CollectingRepo()
        self.audits = _CollectingRepo()
        self.inbox = _Inbox()
        self.steps = _StepsRepo()
        self.tool_calls = _ToolCallsRepo()

    def transaction(self):
        return self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return None

    async def commit(self):
        return None

    async def flush(self):
        return None


class _StepsRepo:
    def __init__(self):
        self.values = {}

    async def get(self, step_id):
        return self.values.get(step_id)

    async def list_by_run(self, run_id):
        return [item for item in self.values.values() if item.run_id == run_id]

    async def update(self, step):
        self.values[step.id] = step


class _ToolCallsRepo:
    def __init__(self):
        self.values = {}

    async def get(self, tool_call_id):
        return self.values.get(tool_call_id)

    async def list_by_run(self, run_id):
        return [item for item in self.values.values() if item.run_id == run_id]

    async def update(self, tool_call):
        self.values[tool_call.id] = tool_call


class _SessionContext:
    async def __aenter__(self):
        return object()

    async def __aexit__(self, *args):
        return None


async def _queue_event_missing(_event_id):
    return False


def _checkpoint(resume_node: str):
    task_id, run_id, trace_id = uuid4(), uuid4(), uuid4()
    job = RunJobMessage(
        job_id=str(uuid4()), trace_id=str(trace_id), task_id=str(task_id),
        run_id=str(run_id), user_goal="recovery test",
        created_at="2026-07-21T00:00:00+00:00",
    )
    state = AgentState(
        task_id=str(task_id), run_id=str(run_id), user_goal=job.user_goal
    )
    if resume_node != "extract_intent":
        state.intent = IntentExtraction(
            primary_intent="task",
            retrieval=RetrievalIntent(
                mode="skip",
                query=job.user_goal,
                confidence=1.0,
                reason="测试恢复状态",
                document_scope="none",
            ),
            source="rule",
        ).to_state_dict()
        state.intent_context = IntentRuntimeContext().to_state_dict()
        state.completion_contract = CompletionContract().to_state_dict()
        state.loop_progress = LoopProgressSnapshot().to_state_dict()
        state.stop_decision = StopDecision(
            disposition="continue",
            reason_code="LOOP_INITIALIZED",
        ).to_state_dict()
    RunSupervisor(RunBudget()).ensure_run_control(state)
    checkpoint = build_run_checkpoint(
        job=job, state=state, next_step_seq=3, resume_node=resume_node
    )
    run = AgentRun(
        id=run_id, task_id=task_id, status=RunStatus.RUNNING,
        version=4, worker_id="dead-worker",
        lease_until=utcnow() - timedelta(seconds=1), checkpoint=checkpoint,
        metadata={"trace_id": str(trace_id)},
    )
    task = Task(
        id=task_id, conversation_id=uuid4(), title="recovery",
        user_goal=job.user_goal, status=TaskStatus.RUNNING,
    )
    return run, task


def test_recovery_node_matrix_separates_pre_effect_from_effect_unknown() -> None:
    assert RESUMABLE_NODES == {
        "extract_intent",
        "call_model",
        "validate_action",
        "execute_tool",
    }
    assert NON_RESUMABLE_NODES == {"tool_in_flight"}
    assert RESUMABLE_NODES.isdisjoint(NON_RESUMABLE_NODES)


def test_checkpoint_restore_accepts_only_explicit_retired_state_tombstones():
    run, _task = _checkpoint("call_model")
    run.checkpoint["state"]["skill_workflow_stage"] = ""

    validate_run_checkpoint(run.checkpoint)
    restored = restore_agent_state(run.checkpoint["state"])

    assert restored.run_id == str(run.id)


def test_v4_checkpoint_is_read_and_rewritten_as_v5_loop_state():
    run, _task = _checkpoint("call_model")
    legacy = dict(run.checkpoint)
    legacy["state"] = dict(legacy["state"])
    legacy["version"] = 4
    for field_name in (
        "completion_contract",
        "loop_progress",
        "stop_decision",
        "run_control",
    ):
        legacy["state"].pop(field_name)

    validate_run_checkpoint(legacy)
    restored = restore_agent_state(legacy["state"])
    controller = LoopController(ToolGateway(ToolRegistry(), PermissionManager()))
    controller.ensure_initialized(restored)
    RunSupervisor(RunBudget()).ensure_run_control(restored)
    rewritten = build_run_checkpoint(
        job=RunJobMessage.from_dict(legacy["job"]),
        state=restored,
        next_step_seq=legacy["next_step_seq"],
        resume_node=legacy["resume_node"],
    )

    assert rewritten["version"] == 5
    assert rewritten["state"]["completion_contract"]["version"] == (
        "completion-contract-v2"
    )
    validate_run_checkpoint(rewritten)


def test_checkpoint_restore_rejects_unknown_state_fields():
    run, _task = _checkpoint("call_model")
    run.checkpoint["state"]["untrusted_future_field"] = "value"

    with pytest.raises(ValueError, match="未知字段"):
        restore_agent_state(run.checkpoint["state"])


def test_checkpoint_round_trips_and_validates_source_chain_slot_attempts():
    run, _task = _checkpoint("call_model")
    run.checkpoint["state"]["source_chain_slot_attempts"] = {
        "endpoint:frontend": 1,
        "transport:producer": 2,
    }

    validate_run_checkpoint(run.checkpoint)
    restored = restore_agent_state(run.checkpoint["state"])

    assert restored.source_chain_slot_attempts == {
        "endpoint:frontend": 1,
        "transport:producer": 2,
    }

    run.checkpoint["state"]["source_chain_slot_attempts"] = {
        "endpoint:frontend": -1,
    }
    with pytest.raises(ValueError, match="source_chain_slot_attempts"):
        validate_run_checkpoint(run.checkpoint)


def test_checkpoint_round_trips_and_bounds_answer_rewrite_state():
    run, _task = _checkpoint("call_model")
    run.checkpoint["state"]["answer_guard_rejections"] = 1
    run.checkpoint["state"]["answer_guard_feedback"] = "只重写最终回答，不得请求工具。"

    validate_run_checkpoint(run.checkpoint)
    restored = restore_agent_state(run.checkpoint["state"])

    assert restored.answer_guard_rejections == 1
    assert restored.answer_guard_feedback == "只重写最终回答，不得请求工具。"

    run.checkpoint["state"]["answer_guard_feedback"] = "x" * 4_001
    with pytest.raises(ValueError, match="answer_guard state"):
        validate_run_checkpoint(run.checkpoint)


def test_checkpoint_round_trips_and_bounds_source_evidence_retry_state():
    run, _task = _checkpoint("call_model")
    run.checkpoint["state"]["source_chain_evidence_rejections"] = 2

    validate_run_checkpoint(run.checkpoint)
    restored = restore_agent_state(run.checkpoint["state"])

    assert restored.source_chain_evidence_rejections == 2

    run.checkpoint["state"]["source_chain_evidence_rejections"] = 21
    with pytest.raises(ValueError, match="source_chain_evidence_rejections"):
        validate_run_checkpoint(run.checkpoint)


def test_checkpoint_rejects_tampered_intent_policy_or_document_scope():
    run, _task = _checkpoint("call_model")
    run.checkpoint["state"]["intent"]["policy_version"] = "untrusted-version"
    with pytest.raises(ValueError, match="Intent|intent"):
        validate_run_checkpoint(run.checkpoint)

    run, _task = _checkpoint("call_model")
    run.checkpoint["state"]["intent"]["retrieval"].update(
        {
            "mode": "required",
            "document_scope": "selected",
            "resolved_document_ids": [str(uuid4())],
        }
    )
    with pytest.raises(ValueError, match="文档范围"):
        validate_run_checkpoint(run.checkpoint)


@pytest.mark.asyncio
async def test_reconciliation_reschedules_only_safe_checkpoint(monkeypatch):
    run, task = _checkpoint("call_model")
    uow = _FakeUow(run, task)
    monkeypatch.setattr(run_service_module, "PostgresUnitOfWork", lambda _session: uow)
    service = RunApplicationService(lambda: lambda: _SessionContext())

    result = await service.reconcile_expired_runs()

    assert result == {"runs_rescheduled": 1, "runs_failed_closed": 0}
    assert run.status == RunStatus.PAUSED
    assert uow.outbox.values[0].event_type == "run.resume.requested"
    assert uow.outbox.values[0].payload["resume_from_checkpoint"] is True
    assert uow.outbox.values[0].payload["type"] == "run.job"
    assert uow.audits.values[0].event_type == "run.recovery.scheduled"


@pytest.mark.asyncio
async def test_reconciliation_requeues_stale_queued_run_after_delivered_job_loss(monkeypatch):
    run, task = _checkpoint("call_model")
    run.status = RunStatus.QUEUED
    run.worker_id = None
    run.lease_until = None
    run.updated_at = utcnow() - timedelta(minutes=2)
    uow = _FakeUow(run, task)
    previous_event_id = uuid4()
    uow.outbox.latest_run_job = OutboxEvent(
        id=uuid4(), event_id=previous_event_id,
        aggregate_type="AgentRun", aggregate_id=run.id,
        event_type="task.created", schema_version="2B-1a.1",
        payload={
            "job_id": str(previous_event_id), "trace_id": str(run.trace_id),
            "task_id": str(task.id), "run_id": str(run.id),
            "user_goal": task.user_goal, "workspace_path": "",
            "conversation_id": str(task.conversation_id),
            "created_at": (utcnow() - timedelta(minutes=2)).isoformat(),
            "schema_version": "2B-1a.1",
        },
        trace_id=run.trace_id, status=OutboxStatus.DELIVERED,
        created_at=utcnow() - timedelta(minutes=2),
    )
    monkeypatch.setattr(run_service_module, "PostgresUnitOfWork", lambda _session: uow)
    service = RunApplicationService(lambda: lambda: _SessionContext())

    result = await service.reconcile_stale_queued_runs(
        queue_event_exists=_queue_event_missing, stale_seconds=60
    )

    assert result == {"queued_runs_requeued": 1, "queued_runs_failed_closed": 0}
    assert run.status == RunStatus.QUEUED
    assert uow.outbox.values[0].event_type == "run.queue.reconciled"
    assert uow.outbox.values[0].payload["run_id"] == str(run.id)
    assert uow.outbox.values[0].payload["job_id"] != str(previous_event_id)
    assert uow.audits.values[0].event_type == "run.queue.reconciled"


@pytest.mark.asyncio
async def test_reconciliation_does_not_duplicate_pending_queued_job(monkeypatch):
    run, task = _checkpoint("call_model")
    run.status = RunStatus.QUEUED
    run.updated_at = utcnow() - timedelta(minutes=2)
    uow = _FakeUow(run, task)
    uow.outbox.latest_run_job = OutboxEvent(
        id=uuid4(), event_id=uuid4(), aggregate_type="AgentRun",
        aggregate_id=run.id, event_type="task.created", schema_version="2B-1a.1",
        payload={"task_id": str(task.id), "run_id": str(run.id)},
        trace_id=run.trace_id, status=OutboxStatus.PENDING,
        created_at=utcnow() - timedelta(minutes=2),
    )
    monkeypatch.setattr(run_service_module, "PostgresUnitOfWork", lambda _session: uow)
    service = RunApplicationService(lambda: lambda: _SessionContext())

    result = await service.reconcile_stale_queued_runs(
        queue_event_exists=_queue_event_missing, stale_seconds=60
    )

    assert result == {"queued_runs_requeued": 0, "queued_runs_failed_closed": 0}
    assert uow.outbox.values == []


@pytest.mark.asyncio
async def test_reconciliation_fails_closed_after_queued_requeue_budget(monkeypatch):
    run, task = _checkpoint("call_model")
    run.status = RunStatus.QUEUED
    run.updated_at = utcnow() - timedelta(minutes=20)
    uow = _FakeUow(run, task)
    previous_event_id = uuid4()
    uow.outbox.latest_run_job = OutboxEvent(
        id=uuid4(), event_id=previous_event_id, aggregate_type="AgentRun",
        aggregate_id=run.id, event_type="run.queue.reconciled",
        schema_version="2B-1a.1",
        payload={
            "task_id": str(task.id), "run_id": str(run.id),
            "queue_reconciliation_attempt": 3,
        },
        trace_id=run.trace_id, status=OutboxStatus.DELIVERED,
        created_at=utcnow() - timedelta(minutes=10),
    )
    monkeypatch.setattr(run_service_module, "PostgresUnitOfWork", lambda _session: uow)
    service = RunApplicationService(lambda: lambda: _SessionContext())

    result = await service.reconcile_stale_queued_runs(
        queue_event_exists=_queue_event_missing, stale_seconds=60
    )

    assert result == {"queued_runs_requeued": 0, "queued_runs_failed_closed": 1}
    assert run.status == RunStatus.FAILED
    assert run.error["code"] == "RUN_QUEUE_RECONCILIATION_EXHAUSTED"
    assert task.status == TaskStatus.FAILED
    assert uow.events.values[0].type == "agent.run.failed"
    assert uow.outbox.values[0].event_type == "agent.run.failed"
    assert uow.audits.values[0].event_type == "run.queue.reconciliation_exhausted"


@pytest.mark.asyncio
async def test_reconciliation_fails_closed_for_tool_in_flight(monkeypatch):
    run, task = _checkpoint("tool_in_flight")
    uow = _FakeUow(run, task)
    step = ExecutionStep(
        id=uuid4(), run_id=run.id, task_id=task.id,
        type=StepType.TOOL_CALL, status=StepStatus.WAITING_FOR_PERMISSION,
        started_at=utcnow() - timedelta(seconds=2),
    )
    tool_call = ToolCall(
        id=uuid4(), task_id=task.id, run_id=run.id, step_id=step.id,
        provider="native", tool_name="literature.download_arxiv_pdf",
        risk_level="L2", arguments={}, status="running",
        permission_status="approved",
        started_at=utcnow() - timedelta(seconds=2),
    )
    uow.steps.values[step.id] = step
    uow.tool_calls.values[tool_call.id] = tool_call
    monkeypatch.setattr(run_service_module, "PostgresUnitOfWork", lambda _session: uow)
    service = RunApplicationService(lambda: lambda: _SessionContext())

    result = await service.reconcile_expired_runs()

    assert result == {"runs_rescheduled": 0, "runs_failed_closed": 1}
    assert run.status == RunStatus.FAILED
    assert run.checkpoint == {}
    assert task.status == TaskStatus.FAILED
    assert uow.events.values[0].type == "agent.run.failed"
    assert uow.outbox.values[0].event_type == "agent.run.failed"
    assert uow.audits.values[0].event_type == "run.recovery.failed_closed"
    assert run.error["code"] == "RUN_RECOVERY_UNSAFE"
    assert step.status == StepStatus.FAILED
    assert step.error["code"] == "RUN_RECOVERY_UNSAFE"
    assert step.completed_at is not None
    assert tool_call.status == "failed"
    assert tool_call.permission_status == "approved"
    assert tool_call.error["code"] == "RUN_RECOVERY_UNSAFE"
    assert tool_call.completed_at is not None


@pytest.mark.asyncio
async def test_reconciliation_fails_after_recovery_budget_is_exhausted(monkeypatch):
    run, task = _checkpoint("call_model")
    run.checkpoint["state"]["recovery_attempts"] = 3
    uow = _FakeUow(run, task)
    monkeypatch.setattr(run_service_module, "PostgresUnitOfWork", lambda _session: uow)
    service = RunApplicationService(lambda: lambda: _SessionContext())

    result = await service.reconcile_expired_runs()

    assert result == {"runs_rescheduled": 0, "runs_failed_closed": 1}
    assert run.status == RunStatus.FAILED
    assert run.error["code"] == "RUN_RECOVERY_EXHAUSTED"


@pytest.mark.asyncio
async def test_pause_run_persists_requested_state_command_and_audit(monkeypatch):
    run, task = _checkpoint("call_model")
    uow = _FakeUow(run, task)
    monkeypatch.setattr(run_service_module, "PostgresUnitOfWork", lambda _session: uow)
    service = RunApplicationService(lambda: lambda: _SessionContext())

    paused = await service.pause_run(run.id, "user requested")

    assert paused.status == RunStatus.PAUSE_REQUESTED
    assert uow.outbox.values[0].event_type == "run.pause.requested"
    assert uow.outbox.values[0].payload["type"] == "run.pause"
    assert uow.outbox.values[0].payload["run_id"] == str(run.id)
    assert uow.audits.values[0].event_type == "run.pause.requested"


@pytest.mark.asyncio
async def test_resume_run_requeues_exact_persisted_checkpoint(monkeypatch):
    run, task = _checkpoint("call_model")
    run.status = RunStatus.PAUSED
    run.worker_id = None
    run.lease_until = None
    uow = _FakeUow(run, task)
    monkeypatch.setattr(run_service_module, "PostgresUnitOfWork", lambda _session: uow)
    service = RunApplicationService(lambda: lambda: _SessionContext())

    resumed = await service.resume_run(run.id)

    assert resumed.status == RunStatus.RESUME_REQUESTED
    assert uow.outbox.values[0].event_type == "run.resume.requested"
    assert uow.outbox.values[0].payload["type"] == "run.job"
    assert uow.outbox.values[0].payload["resume_from_checkpoint"] is True
    assert uow.outbox.values[0].payload["run_id"] == str(run.id)
    assert uow.audits.values[0].event_type == "run.resume.requested"


@pytest.mark.asyncio
async def test_reconciliation_confirms_requested_pause_at_safe_checkpoint(monkeypatch):
    run, task = _checkpoint("call_model")
    run.status = RunStatus.PAUSE_REQUESTED
    uow = _FakeUow(run, task)
    monkeypatch.setattr(run_service_module, "PostgresUnitOfWork", lambda _session: uow)
    service = RunApplicationService(lambda: lambda: _SessionContext())

    result = await service.reconcile_expired_runs()

    assert result == {"runs_rescheduled": 0, "runs_failed_closed": 0}
    assert run.status == RunStatus.PAUSED
    assert uow.events.values[0].type == "agent.run.paused"
    assert uow.outbox.values[0].event_type == "agent.run.paused"
    assert uow.audits.values[0].event_type == "run.pause.reconciled"


@pytest.mark.asyncio
async def test_retry_failed_model_step_creates_replacement_run(monkeypatch):
    run, task = _checkpoint("call_model")
    step_id = uuid4()
    run.status = RunStatus.FAILED
    run.error = {
        "code": "MODEL_TIMEOUT", "message": "模型调用失败",
        "category": "model", "recoverable": True,
    }
    task.status = TaskStatus.FAILED
    task.active_run_id = run.id
    step = ExecutionStep(
        id=step_id, run_id=run.id, task_id=task.id,
        type=StepType.MODEL_CALL, status=StepStatus.FAILED,
        error={"code": "MODEL_TIMEOUT", "recoverable": True},
    )
    uow = _FakeUow(run, task)
    uow.steps.values[step_id] = step
    monkeypatch.setattr(run_service_module, "PostgresUnitOfWork", lambda _session: uow)
    service = RunApplicationService(lambda: lambda: _SessionContext())

    replacement = await service.retry_failed_step(run.id, step_id)

    assert replacement.id != run.id
    assert replacement.status == RunStatus.QUEUED
    assert replacement.checkpoint["job"]["run_id"] == str(replacement.id)
    assert replacement.checkpoint["job"]["retry_from_checkpoint"] is True
    assert replacement.checkpoint["state"]["run_id"] == str(replacement.id)
    assert task.active_run_id == replacement.id
    assert uow.outbox.values[0].event_type == "run.step_retry.requested"
    assert uow.audits.values[0].event_type == "run.step_retry.requested"

    duplicate = await service.retry_failed_step(run.id, step_id)
    assert duplicate.id == replacement.id
    assert len(uow.outbox.values) == 1


@pytest.mark.asyncio
async def test_retry_failed_intent_model_step_uses_safe_extraction_checkpoint(monkeypatch):
    run, task = _checkpoint("extract_intent")
    step_id = uuid4()
    run.status = RunStatus.FAILED
    run.error = {
        "code": "MODEL_HTTP_ERROR", "message": "Intent 模型调用失败",
        "category": "model", "recoverable": True,
    }
    task.status = TaskStatus.FAILED
    task.active_run_id = run.id
    step = ExecutionStep(
        id=step_id, run_id=run.id, task_id=task.id,
        type=StepType.MODEL_CALL, status=StepStatus.FAILED,
        error={"code": "MODEL_HTTP_ERROR", "recoverable": True},
    )
    uow = _FakeUow(run, task)
    uow.steps.values[step_id] = step
    monkeypatch.setattr(run_service_module, "PostgresUnitOfWork", lambda _session: uow)
    service = RunApplicationService(lambda: lambda: _SessionContext())

    replacement = await service.retry_failed_step(run.id, step_id)

    assert replacement.status == RunStatus.QUEUED
    assert replacement.checkpoint["resume_node"] == "extract_intent"
    assert replacement.checkpoint["job"]["retry_from_checkpoint"] is True


@pytest.mark.asyncio
async def test_retry_failed_step_rejects_tool_effect(monkeypatch):
    run, task = _checkpoint("call_model")
    step_id = uuid4()
    run.status = RunStatus.FAILED
    run.error = {"code": "TOOL_TIMEOUT", "recoverable": True}
    task.status = TaskStatus.FAILED
    task.active_run_id = run.id
    step = ExecutionStep(
        id=step_id, run_id=run.id, task_id=task.id,
        type=StepType.TOOL_CALL, status=StepStatus.FAILED,
        error={"code": "TOOL_TIMEOUT", "recoverable": True},
    )
    uow = _FakeUow(run, task)
    uow.steps.values[step_id] = step
    monkeypatch.setattr(run_service_module, "PostgresUnitOfWork", lambda _session: uow)
    service = RunApplicationService(lambda: lambda: _SessionContext())

    with pytest.raises(run_service_module.errors.AppError) as exc:
        await service.retry_failed_step(run.id, step_id)

    assert exc.value.code == "FAILED_STEP_NOT_RETRYABLE"
    assert not uow.runs.created


@pytest.mark.asyncio
async def test_run_queue_retry_exhaustion_fails_run_and_writes_audit(monkeypatch):
    run, task = _checkpoint("call_model")
    uow = _FakeUow(run, task)
    monkeypatch.setattr(run_service_module, "PostgresUnitOfWork", lambda _session: uow)
    service = RunApplicationService(lambda: lambda: _SessionContext())

    failed = await service.fail_run_queue_delivery(
        run.id,
        "redis-message-1",
        "POSTGRES_TEMPORARY_FAILURE",
        3,
    )
    duplicate = await service.fail_run_queue_delivery(
        run.id,
        "redis-message-1",
        "POSTGRES_TEMPORARY_FAILURE",
        3,
    )

    assert failed is run
    assert duplicate is run
    assert run.status == RunStatus.FAILED
    assert run.checkpoint == {}
    assert task.status == TaskStatus.FAILED
    assert run.error["code"] == "RUN_QUEUE_RETRY_EXHAUSTED"
    assert uow.events.values[0].type == "agent.run.failed"
    assert uow.outbox.values[0].event_type == "agent.run.failed"
    assert uow.audits.values[0].event_type == "run.queue.dead_letter"
    assert len(uow.events.values) == 1
    assert len(uow.outbox.values) == 1
    assert len(uow.audits.values) == 1
