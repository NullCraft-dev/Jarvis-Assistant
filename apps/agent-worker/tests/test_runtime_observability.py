from __future__ import annotations

from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest

from jarvis_worker.agent.core.checkpoint import build_run_checkpoint
from jarvis_worker.agent.core.state import AgentState
from jarvis_worker.agent.harness import RunBudget, RunSupervisor
from jarvis_worker.runtime.service import RuntimeApplicationService
from jarvis_worker.runtime_bus.messages import RunJobMessage, RuntimeEventEnvelope
from jarvis_worker.shared.domain.models import (
    AgentRun,
    ExecutionStep,
    PermissionRequest,
    PermissionStatus,
    RunStatus,
    StepStatus,
    StepType,
    ToolCall,
)


class _Steps:
    def __init__(self):
        self.values = {}

    async def get(self, step_id):
        return self.values.get(step_id)

    async def list_by_run(self, run_id):
        return sorted(
            (step for step in self.values.values() if step.run_id == run_id),
            key=lambda step: step.order_index,
        )

    async def create(self, step):
        self.values[step.id] = step
        return step

    async def update(self, step):
        self.values[step.id] = step


class _Runs:
    def __init__(self):
        self.updates = []

    async def update_with_lock(self, **values):
        self.updates.append(values)
        return True


class _ToolCalls:
    def __init__(self):
        self.values = {}

    async def get(self, tool_call_id):
        return self.values.get(tool_call_id)

    async def list_by_run(self, run_id):
        return [call for call in self.values.values() if call.run_id == run_id]

    async def create(self, tool_call):
        self.values[tool_call.id] = tool_call
        return tool_call

    async def update(self, tool_call):
        self.values[tool_call.id] = tool_call


class _Audits:
    def __init__(self):
        self.values = []

    async def create(self, audit):
        self.values.append(audit)
        return audit


class _Artifacts:
    def __init__(self):
        self.values = {}

    async def create(self, artifact):
        self.values[artifact.id] = artifact
        return artifact


class _Permissions:
    def __init__(self):
        self.values = {}

    async def list_pending_by_run(self, run_id):
        return [
            request
            for request in self.values.values()
            if request.run_id == run_id and request.status is PermissionStatus.PENDING
        ]

    async def update_request(self, request):
        self.values[request.id] = request


class _Tx:
    def __init__(self):
        self.runs = _Runs()
        self.steps = _Steps()
        self.tool_calls = _ToolCalls()
        self.audits = _Audits()
        self.artifacts = _Artifacts()
        self.permissions = _Permissions()


@pytest.mark.asyncio
async def test_cancelled_run_closes_open_step_and_tool_call_projections():
    service = RuntimeApplicationService(lambda: None)
    tx = _Tx()
    task_id, run_id, step_id, call_id = uuid4(), uuid4(), uuid4(), uuid4()
    started = datetime(2026, 7, 30, 12, 0, tzinfo=timezone.utc)
    cancelled_at = started + timedelta(milliseconds=125)
    tx.steps.values[step_id] = ExecutionStep(
        id=step_id,
        task_id=task_id,
        run_id=run_id,
        type=StepType.TOOL_CALL,
        status=StepStatus.RUNNING,
        started_at=started,
    )
    tx.tool_calls.values[call_id] = ToolCall(
        id=call_id,
        task_id=task_id,
        run_id=run_id,
        step_id=step_id,
        provider="native",
        tool_name="workspace.read_file",
        risk_level="L0",
        arguments={},
        status="running",
        started_at=started,
    )

    await service._cancel_open_children(tx, run_id, cancelled_at)

    step = tx.steps.values[step_id]
    tool_call = tx.tool_calls.values[call_id]
    assert step.status is StepStatus.CANCELLED
    assert step.summary == "运行已取消"
    assert step.completed_at == cancelled_at
    assert step.duration_ms == 125
    assert tool_call.status == "cancelled"
    assert tool_call.error == {
        "code": "RUN_CANCELLED",
        "message": "运行已取消",
        "category": "runtime",
        "recoverable": False,
    }
    assert tool_call.completed_at == cancelled_at
    assert tool_call.duration_ms == 125


@pytest.mark.asyncio
async def test_failed_run_closes_only_open_step_and_tool_call_projections():
    service = RuntimeApplicationService(lambda: None)
    tx = _Tx()
    task_id, run_id, open_step_id, completed_step_id, call_id = (
        uuid4(), uuid4(), uuid4(), uuid4(), uuid4()
    )
    started = datetime(2026, 8, 3, 12, 0, tzinfo=timezone.utc)
    failed_at = started + timedelta(milliseconds=250)
    error = {
        "code": "PERMISSION_RESUME_EFFECT_UNKNOWN",
        "message": "工具执行结果未知",
        "category": "runtime",
        "recoverable": False,
    }
    tx.steps.values[open_step_id] = ExecutionStep(
        id=open_step_id,
        task_id=task_id,
        run_id=run_id,
        type=StepType.TOOL_CALL,
        status=StepStatus.WAITING_FOR_PERMISSION,
        started_at=started,
    )
    tx.steps.values[completed_step_id] = ExecutionStep(
        id=completed_step_id,
        task_id=task_id,
        run_id=run_id,
        type=StepType.MODEL_CALL,
        status=StepStatus.COMPLETED,
        completed_at=started,
    )
    tx.tool_calls.values[call_id] = ToolCall(
        id=call_id,
        task_id=task_id,
        run_id=run_id,
        step_id=open_step_id,
        provider="native",
        tool_name="workspace.create_file",
        risk_level="L2",
        arguments={},
        status="running",
        permission_status="approved",
        started_at=started,
    )

    await service._fail_open_children(tx, run_id, error, failed_at)

    open_step = tx.steps.values[open_step_id]
    assert open_step.status is StepStatus.FAILED
    assert open_step.error == error
    assert open_step.error is not error
    assert open_step.duration_ms == 250
    assert tx.steps.values[completed_step_id].status is StepStatus.COMPLETED
    tool_call = tx.tool_calls.values[call_id]
    assert tool_call.status == "failed"
    assert tool_call.permission_status == "approved"
    assert tool_call.error == error
    assert tool_call.error is not error
    assert tool_call.duration_ms == 250


@pytest.mark.asyncio
async def test_expired_permission_projects_to_pending_tool_call_only():
    service = RuntimeApplicationService(lambda: None)
    tx = _Tx()
    task_id, run_id, step_id, call_id, request_id = (uuid4() for _ in range(5))
    expired_at = datetime(2026, 8, 3, 13, 0, tzinfo=timezone.utc)
    request = PermissionRequest(
        id=request_id,
        task_id=task_id,
        run_id=run_id,
        step_id=step_id,
        tool_call_id=call_id,
        tool_name="workspace.create_file",
        action_summary="创建文件",
        risk_level="L2",
        scope={"type": "once"},
        arguments_summary={},
        status=PermissionStatus.PENDING,
    )
    tool_call = ToolCall(
        id=call_id,
        task_id=task_id,
        run_id=run_id,
        step_id=step_id,
        provider="native",
        tool_name=request.tool_name,
        risk_level="L2",
        arguments={},
        status="pending",
        permission_status="pending",
    )
    tx.permissions.values[request_id] = request
    tx.tool_calls.values[call_id] = tool_call

    await service._expire_pending_permissions(tx, run_id, expired_at, "run_cancelled")

    assert request.status is PermissionStatus.EXPIRED
    assert request.note == "run_cancelled"
    assert tool_call.permission_status == "expired"
    assert tx.audits.values[0].result_summary == "expired"

    request.status = PermissionStatus.PENDING
    tool_call.permission_status = "approved"
    await service._expire_pending_permissions(tx, run_id, expired_at, "run_failed")
    assert tool_call.permission_status == "approved"


def test_permission_checkpoint_is_removed_from_public_event():
    envelope = RuntimeEventEnvelope(
        event_id=str(uuid4()),
        trace_id=str(uuid4()),
        task_id=str(uuid4()),
        run_id=str(uuid4()),
        event_type="permission.required",
        produced_by="worker-test",
        runtime_event={
            "id": "placeholder",
            "type": "permission.required",
            "task_id": "placeholder",
            "run_id": "placeholder",
            "timestamp": "2026-07-16T00:00:00+00:00",
            "payload": {"request": {"id": "req", "_internal_checkpoint": {"version": 1}}},
        },
    )
    envelope.runtime_event["id"] = envelope.event_id
    envelope.runtime_event["task_id"] = envelope.task_id
    envelope.runtime_event["run_id"] = envelope.run_id

    public, checkpoint = RuntimeApplicationService._extract_permission_checkpoint(envelope)

    assert checkpoint == {"version": 1}
    assert "_internal_checkpoint" not in public.runtime_event["payload"]["request"]
    assert "_internal_checkpoint" in envelope.runtime_event["payload"]["request"]


def test_run_checkpoint_is_removed_from_public_event():
    task_id, run_id, trace_id = uuid4(), uuid4(), uuid4()
    job = RunJobMessage(
        job_id=str(uuid4()),
        trace_id=str(trace_id),
        task_id=str(task_id),
        run_id=str(run_id),
        user_goal="test",
        created_at="2026-07-21T00:00:00+00:00",
    )
    state = AgentState(task_id=str(task_id), run_id=str(run_id), user_goal=job.user_goal)
    RunSupervisor(RunBudget()).ensure_run_control(state)
    checkpoint = build_run_checkpoint(
        job=job,
        state=state,
        next_step_seq=2,
        resume_node="extract_intent",
    )
    envelope = RuntimeEventEnvelope(
        event_id=str(uuid4()),
        trace_id=str(trace_id),
        task_id=str(task_id),
        run_id=str(run_id),
        event_type="model.call.started",
        produced_by="worker-test",
        runtime_event={
            "id": "placeholder",
            "type": "model.call.started",
            "task_id": "placeholder",
            "run_id": "placeholder",
            "timestamp": "2026-07-21T00:00:00+00:00",
            "payload": {"provider": "mock"},
        },
        internal={"run_checkpoint": checkpoint},
    )
    envelope.runtime_event["id"] = envelope.event_id
    envelope.runtime_event["task_id"] = envelope.task_id
    envelope.runtime_event["run_id"] = envelope.run_id

    public, _permission, run_checkpoint = RuntimeApplicationService._extract_internal_checkpoints(
        envelope
    )

    assert run_checkpoint["resume_node"] == "extract_intent"
    assert public.internal == {}
    assert "run_checkpoint" not in public.to_payload_json()


@pytest.mark.asyncio
async def test_tool_call_projection_persists_details_audit_and_duration():
    service = RuntimeApplicationService(lambda: None)
    tx = _Tx()
    task_id, run_id, step_id, call_id = uuid4(), uuid4(), uuid4(), uuid4()
    run = AgentRun(id=run_id, task_id=task_id, status=RunStatus.RUNNING)
    started = datetime(2026, 7, 16, tzinfo=timezone.utc)

    await service._record_tool_event(
        tx,
        "tool.call.started",
        {
            "tool_call": {
                "id": str(call_id),
                "tool_name": "workspace.read_file",
                "provider": "native",
                "risk_level": "L0",
                "arguments_summary": {
                    "workspace_root": "/workspace",
                    "path": "AGENTS.md",
                },
            }
        },
        task_id,
        run,
        step_id,
        started,
    )
    await service._record_tool_event(
        tx,
        "tool.call.finished",
        {
            "tool_call": {
                "id": str(call_id),
                "tool_name": "workspace.read_file",
                "status": "completed",
                "result": {"kind": "text", "summary": "已读取 AGENTS.md"},
            }
        },
        task_id,
        run,
        step_id,
        started + timedelta(milliseconds=125),
    )

    persisted = tx.tool_calls.values[call_id]
    assert persisted.status == "completed"
    assert persisted.arguments_summary["path"] == "AGENTS.md"
    assert persisted.result_summary == "已读取 AGENTS.md"
    assert persisted.duration_ms == 125
    assert [audit.event_type for audit in tx.audits.values] == [
        "tool.call.started",
        "tool.call.finished",
    ]
    assert tx.audits.values[-1].tool_call_id == call_id
    assert tx.audits.values[-1].details == {
        "tool_name": "workspace.read_file",
        "provider": "native",
        "arguments_summary": {
            "workspace_root": "/workspace",
            "path": "AGENTS.md",
        },
        "status": "completed",
        "duration_ms": 125,
    }
    assert run.step_count == 1
    assert run.current_step_id == step_id
    assert tx.steps.values[step_id].order_index == 0


@pytest.mark.asyncio
async def test_failed_tool_projection_persists_structured_error_and_audit():
    service = RuntimeApplicationService(lambda: None)
    tx = _Tx()
    task_id, run_id, step_id, call_id = uuid4(), uuid4(), uuid4(), uuid4()
    run = AgentRun(id=run_id, task_id=task_id, status=RunStatus.RUNNING)
    now = datetime(2026, 7, 16, tzinfo=timezone.utc)
    error = {
        "code": "FILE_NOT_FOUND",
        "message": "文件不存在",
        "category": "tool",
        "recoverable": False,
    }

    await service._record_tool_event(
        tx,
        "tool.call.failed",
        {
            "tool_call": {
                "id": str(call_id),
                "tool_name": "workspace.read_file",
                "status": "failed",
                "error": error,
            }
        },
        task_id,
        run,
        step_id,
        now,
    )

    assert tx.tool_calls.values[call_id].status == "failed"
    assert tx.tool_calls.values[call_id].error == error
    assert tx.audits.values[0].error == error


@pytest.mark.asyncio
async def test_create_file_deliverable_is_validated_and_projected_atomically():
    task_id, run_id, step_id, trace_id, call_id = (uuid4(), uuid4(), uuid4(), uuid4(), uuid4())
    content_hash = "a" * 64
    envelope = RuntimeEventEnvelope(
        event_id=str(uuid4()),
        trace_id=str(trace_id),
        task_id=str(task_id),
        run_id=str(run_id),
        event_type="tool.call.finished",
        produced_by="worker-test",
        runtime_event={
            "id": "placeholder",
            "type": "tool.call.finished",
            "task_id": str(task_id),
            "run_id": str(run_id),
            "step_id": str(step_id),
            "timestamp": "2026-07-24T00:00:00+00:00",
            "payload": {
                "tool_call": {
                    "id": str(call_id),
                    "tool_name": "workspace.create_file",
                    "status": "completed",
                    "result": {
                        "kind": "file",
                        "summary": "created",
                        "data": {
                            "created": True,
                            "path": "reports/result.md",
                            "size_bytes": 12,
                            "sha256": content_hash,
                        },
                        "deliverables": [
                            {
                                "kind": "file",
                                "title": "reports/result.md",
                                "path": "reports/result.md",
                                "size_bytes": 12,
                                "mime_type": "text/markdown; charset=utf-8",
                                "content_hash": content_hash,
                            }
                        ],
                    },
                }
            },
        },
    )
    envelope.runtime_event["id"] = envelope.event_id

    public, prepared = RuntimeApplicationService._prepare_tool_deliverables(envelope)

    result = public.runtime_event["payload"]["tool_call"]["result"]
    assert len(result["artifact_ids"]) == 1
    assert result["artifact_ids"][0] == str(prepared[0]["id"])

    tx = _Tx()
    events, outboxes = await RuntimeApplicationService._build_tool_deliverable_records(
        tx,
        public,
        prepared,
        task_id=task_id,
        run_id=run_id,
        step_id=step_id,
        trace_id=trace_id,
        start_sequence=9,
        created_at=datetime(2026, 7, 24, tzinfo=timezone.utc),
    )

    artifact = next(iter(tx.artifacts.values.values()))
    assert artifact.purpose == "deliverable"
    assert artifact.producer_type == "tool"
    assert artifact.source_tool_call_id == call_id
    assert artifact.metadata["workspace_relative_path"] == "reports/result.md"
    assert events[0].event_sequence == 9
    assert events[0].payload["artifact"]["producer"] == {
        "type": "tool",
        "tool_call_id": str(call_id),
    }
    assert outboxes[0].event_type == "artifact.created"


def test_create_file_deliverable_rejects_mismatched_result():
    task_id, run_id, step_id, trace_id, call_id = (uuid4(), uuid4(), uuid4(), uuid4(), uuid4())
    envelope = RuntimeEventEnvelope(
        event_id=str(uuid4()),
        trace_id=str(trace_id),
        task_id=str(task_id),
        run_id=str(run_id),
        event_type="tool.call.finished",
        produced_by="worker-test",
        runtime_event={
            "id": "placeholder",
            "type": "tool.call.finished",
            "task_id": str(task_id),
            "run_id": str(run_id),
            "step_id": str(step_id),
            "timestamp": "2026-07-24T00:00:00+00:00",
            "payload": {
                "tool_call": {
                    "id": str(call_id),
                    "tool_name": "workspace.create_file",
                    "result": {
                        "kind": "file",
                        "data": {
                            "created": True,
                            "path": "safe.md",
                            "size_bytes": 1,
                            "sha256": "a" * 64,
                        },
                        "deliverables": [
                            {
                                "kind": "file",
                                "title": "safe.md",
                                "path": "../escape.md",
                                "size_bytes": 1,
                                "mime_type": "text/markdown; charset=utf-8",
                                "content_hash": "a" * 64,
                            }
                        ],
                    },
                }
            },
        },
    )
    envelope.runtime_event["id"] = envelope.event_id

    with pytest.raises(ValueError, match="相对路径"):
        RuntimeApplicationService._prepare_tool_deliverables(envelope)


@pytest.mark.asyncio
async def test_model_projection_persists_retryable_failed_step():
    service = RuntimeApplicationService(lambda: None)
    tx = _Tx()
    task_id, run_id, step_id = uuid4(), uuid4(), uuid4()
    run = AgentRun(id=run_id, task_id=task_id, status=RunStatus.RUNNING)
    started = datetime(2026, 7, 23, tzinfo=timezone.utc)

    await service._record_model_event(
        tx,
        "model.call.started",
        {
            "provider": "test",
            "model_name": "model",
            "call_id": str(uuid4()),
            "purpose": "intent_extraction",
        },
        task_id,
        run,
        step_id,
        started,
    )
    await service._record_model_event(
        tx,
        "model.call.failed",
        {"error_code": "MODEL_TIMEOUT", "recoverable": True, "duration_ms": 250},
        task_id,
        run,
        step_id,
        started + timedelta(milliseconds=250),
    )

    step = tx.steps.values[step_id]
    assert step.type.value == "model_call"
    assert step.status.value == "failed"
    assert step.duration_ms == 250
    assert step.metadata["purpose"] == "intent_extraction"
    assert step.error == {
        "code": "MODEL_TIMEOUT",
        "message": "模型调用失败",
        "category": "model",
        "recoverable": True,
    }
    assert run.step_count == 1
    assert step.order_index == 0


@pytest.mark.asyncio
async def test_model_projection_persists_safe_answer_validation_details():
    service = RuntimeApplicationService(lambda: None)
    tx = _Tx()
    task_id, run_id, step_id = uuid4(), uuid4(), uuid4()
    run = AgentRun(id=run_id, task_id=task_id, status=RunStatus.RUNNING)
    started = datetime(2026, 8, 9, tzinfo=timezone.utc)
    call_id = str(uuid4())

    await service._record_model_event(
        tx,
        "model.call.started",
        {
            "provider": "test",
            "model_name": "model",
            "call_id": call_id,
        },
        task_id,
        run,
        step_id,
        started,
    )
    validation = {
        "validator_id": "workspace-source-chain-coverage-v4",
        "reason_code": "SOURCE_CHAIN_GLOBAL_CONTRADICTION",
        "rejection_count": 1,
        "max_rewrites": 1,
        "rewrite_available": True,
        "recovery_mode": "answer_rewrite",
        "coverage": {
            "required_evidence_slot_count": 4,
            "covered_evidence_slot_count": 4,
            "complete": True,
        },
        "raw_answer": "不得持久化的模型回答",
        "source_path": "apps/private.py",
    }
    await service._record_model_event(
        tx,
        "model.call.failed",
        {
            "call_id": call_id,
            "error_code": "FINAL_ANSWER_VALIDATION_FAILED",
            "recoverable": True,
            "duration_ms": 300,
            "validation": validation,
        },
        task_id,
        run,
        step_id,
        started + timedelta(milliseconds=300),
    )

    step = tx.steps.values[step_id]
    assert step.error == {
        "code": "FINAL_ANSWER_VALIDATION_FAILED",
        "message": "模型调用失败",
        "category": "model",
        "recoverable": True,
        "details": {
            "answer_validation": {
                key: value
                for key, value in validation.items()
                if key not in {"raw_answer", "source_path"}
            }
        },
    }


@pytest.mark.asyncio
async def test_model_projection_persists_safe_source_navigation_details():
    service = RuntimeApplicationService(lambda: None)
    tx = _Tx()
    task_id, run_id, step_id = uuid4(), uuid4(), uuid4()
    run = AgentRun(id=run_id, task_id=task_id, status=RunStatus.RUNNING)
    started = datetime(2026, 8, 9, tzinfo=timezone.utc)
    call_id = str(uuid4())

    await service._record_model_event(
        tx,
        "model.call.started",
        {
            "provider": "test",
            "model_name": "model",
            "call_id": call_id,
        },
        task_id,
        run,
        step_id,
        started,
    )
    navigation = {
        "policy_version": "source-navigation-v5",
        "reason_code": "DISCOVERY_NO_PROGRESS",
        "tool_class": "discovery",
        "missing_slot_count": 4,
        "proposed_slot_count": 1,
        "proposed_missing_slot_count": 1,
        "discovery_count_since_read": 3,
        "productive_discovery_count": 1,
        "nonprogress_discovery_streak": 2,
        "unique_candidate_count": 30,
        "has_actionable_candidates": True,
        "remaining_call_count": 4,
        "coverage_budget_threshold": 6,
        "coverage_budget_at_risk": True,
        "path": "apps/private.py",
        "query": "secret",
    }
    await service._record_model_event(
        tx,
        "model.call.failed",
        {
            "call_id": call_id,
            "error_code": "SOURCE_CHAIN_PLANNING_STALLED",
            "recoverable": True,
            "duration_ms": 300,
            "navigation_guard": navigation,
        },
        task_id,
        run,
        step_id,
        started + timedelta(milliseconds=300),
    )

    details = tx.steps.values[step_id].error["details"]["source_navigation"]
    assert details["reason_code"] == "DISCOVERY_NO_PROGRESS"
    assert details["unique_candidate_count"] == 30
    assert "path" not in details
    assert "query" not in details


@pytest.mark.asyncio
async def test_step_projection_allocates_contiguous_order_and_rejects_type_collision():
    service = RuntimeApplicationService(lambda: None)
    tx = _Tx()
    task_id, run_id, model_step_id, tool_step_id, call_id = (
        uuid4(),
        uuid4(),
        uuid4(),
        uuid4(),
        uuid4(),
    )
    run = AgentRun(id=run_id, task_id=task_id, status=RunStatus.RUNNING)
    started = datetime(2026, 7, 30, tzinfo=timezone.utc)

    await service._record_model_event(
        tx,
        "model.call.started",
        {"provider": "test", "model_name": "model", "call_id": str(uuid4())},
        task_id,
        run,
        model_step_id,
        started,
    )
    await service._record_tool_event(
        tx,
        "tool.call.started",
        {"tool_call": {"id": str(call_id), "tool_name": "rag.search"}},
        task_id,
        run,
        tool_step_id,
        started + timedelta(milliseconds=1),
    )

    assert run.step_count == 2
    assert run.current_step_id == tool_step_id
    assert [tx.steps.values[item].order_index for item in (model_step_id, tool_step_id)] == [0, 1]
    assert len(tx.runs.updates) == 2

    with pytest.raises(ValueError, match="类型"):
        await service._record_tool_event(
            tx,
            "tool.call.started",
            {"tool_call": {"id": str(uuid4()), "tool_name": "rag.search"}},
            task_id,
            run,
            model_step_id,
            started + timedelta(milliseconds=2),
        )
    assert run.step_count == 2


@pytest.mark.asyncio
async def test_step_projection_rejects_legacy_inconsistent_run_before_allocating():
    service = RuntimeApplicationService(lambda: None)
    tx = _Tx()
    task_id, run_id, legacy_step_id = uuid4(), uuid4(), uuid4()
    run = AgentRun(id=run_id, task_id=task_id, status=RunStatus.RUNNING)
    started = datetime(2026, 7, 30, tzinfo=timezone.utc)

    await service._record_model_event(
        tx,
        "model.call.started",
        {"provider": "test", "model_name": "model", "call_id": str(uuid4())},
        task_id,
        run,
        legacy_step_id,
        started,
    )
    run.step_count = 0

    with pytest.raises(RuntimeError, match="既有 Step 投影不一致"):
        await service._record_model_event(
            tx,
            "model.call.started",
            {"provider": "test", "model_name": "model", "call_id": str(uuid4())},
            task_id,
            run,
            uuid4(),
            started + timedelta(milliseconds=1),
        )

    assert len(tx.steps.values) == 1
    assert len(tx.runs.updates) == 1
