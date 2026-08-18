"""RunSupervisor 的预算、取消与停止边界测试。"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest

from jarvis_worker.agent.core.state import AgentState
from jarvis_worker.agent.harness import (
    CancellationController,
    RunBudget,
    RunSupervisor,
    RuntimeInvariantViolation,
)
from jarvis_worker.runtime.events import build_runtime_event
from jarvis_worker.runtime_bus.messages import RuntimeEventEnvelope


def _envelope(event_type: str) -> RuntimeEventEnvelope:
    event_id = str(uuid4())
    runtime_event = build_runtime_event(
        event_type=event_type,
        task_id="task-1",
        run_id="run-1",
        event_id=event_id,
        payload={},
    )
    return RuntimeEventEnvelope(
        event_id=event_id,
        trace_id="trace-1",
        task_id="task-1",
        run_id="run-1",
        event_type=event_type,
        produced_by="test-worker",
        runtime_event=runtime_event,
    )


@pytest.mark.parametrize("value", (0, -1, 21, True, 1.5))
def test_run_budget_rejects_invalid_or_unbounded_tool_iterations(value) -> None:
    with pytest.raises(ValueError, match="max_tool_iterations"):
        RunBudget(max_tool_iterations=value)


def test_cancellation_controller_latches_first_positive_signal() -> None:
    responses = iter((False, True, False))
    controller = CancellationController(lambda: next(responses))

    assert controller.is_cancelled() is False
    assert controller.is_cancelled() is True
    assert controller.is_cancelled() is True


def test_run_supervisor_persists_and_enforces_model_call_budget() -> None:
    now = datetime(2026, 8, 12, tzinfo=timezone.utc)
    supervisor = RunSupervisor(
        RunBudget(max_tool_iterations=3, max_model_calls=1, max_run_seconds=60),
        now=lambda: now,
    )
    state = AgentState()

    assert supervisor.before_model_call(state) is None
    exhausted = supervisor.before_model_call(state)

    assert exhausted is not None
    assert exhausted.code == "MODEL_CALL_BUDGET_EXHAUSTED"
    assert state.run_control["model_calls_used"] == 1


def test_run_supervisor_enforces_persisted_deadline_after_resume() -> None:
    current = [datetime(2026, 8, 12, tzinfo=timezone.utc)]
    supervisor = RunSupervisor(
        RunBudget(max_tool_iterations=3, max_model_calls=8, max_run_seconds=30),
        now=lambda: current[0],
    )
    state = AgentState()
    supervisor.ensure_run_control(state)
    current[0] += timedelta(seconds=31)

    halted = supervisor.before_phase(state)

    assert halted is not None
    assert halted.code == "RUN_DEADLINE_EXCEEDED"


def test_run_supervisor_accepts_one_terminal_event_at_end() -> None:
    supervisor = RunSupervisor(RunBudget(max_tool_iterations=3))

    supervisor.validate_result([_envelope("agent.run.started"), _envelope("agent.run.completed")])


def test_run_supervisor_accepts_permission_as_single_suspension_boundary() -> None:
    supervisor = RunSupervisor(RunBudget(max_tool_iterations=3))

    supervisor.validate_result([_envelope("tool.call.started"), _envelope("permission.required")])


@pytest.mark.parametrize(
    "events, message",
    (
        ([], "未产生任何"),
        (["agent.run.started"], "缺少终态或未决挂起"),
        (
            ["agent.run.completed", "model.call.started"],
            "终态事件之后仍产生",
        ),
        (
            ["agent.run.completed", "agent.run.failed"],
            "多个终态事件",
        ),
    ),
)
def test_run_supervisor_fails_closed_on_invalid_stop_boundary(events, message) -> None:
    supervisor = RunSupervisor(RunBudget(max_tool_iterations=3))
    envelopes = [_envelope(event_type) for event_type in events]

    with pytest.raises(RuntimeInvariantViolation, match=message):
        supervisor.validate_result(envelopes)
