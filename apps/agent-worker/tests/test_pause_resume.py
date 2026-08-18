"""Run pause/resume 的安全 checkpoint 行为。"""

import tempfile
from pathlib import Path
from uuid import uuid4

from jarvis_worker.agent.core.runner import AgentRunner
from jarvis_worker.agent.models.errors import ModelProviderError
from jarvis_worker.agent.models.provider import ModelProvider
from tests.test_agent_runner import _make_agent_runner, _make_job, _make_tool_gateway


def test_model_lifecycle_events_share_stable_step_id() -> None:
    events = _make_agent_runner().run(_make_job("hello"))
    started = next(event for event in events if event.event_type == "model.call.started")
    completed = next(event for event in events if event.event_type == "model.call.completed")

    assert started.runtime_event["step_id"]
    assert completed.runtime_event["step_id"] == started.runtime_event["step_id"]


def test_pause_before_model_persists_resumable_checkpoint() -> None:
    runner = _make_agent_runner()
    pause_command_id = str(uuid4())

    events = runner.run(
        _make_job("hello"), pause_check=lambda: pause_command_id
    )

    assert [event.event_type for event in events] == [
        "agent.run.started",
        "agent.run.paused",
    ]
    checkpoint = events[-1].internal["run_checkpoint"]
    assert checkpoint["resume_node"] == "extract_intent"
    assert "run_checkpoint" not in events[-1].to_payload_json()

    resumed = runner.resume_from_checkpoint(checkpoint)
    resumed_types = [event.event_type for event in resumed]
    assert "agent.run.started" not in resumed_types
    assert resumed_types[-1] == "agent.run.completed"


def test_pause_before_tool_effect_resumes_without_duplicate_effect() -> None:
    with tempfile.TemporaryDirectory() as workspace:
        Path(workspace, "readme.md").write_text("hello")
        runner = _make_agent_runner()
        pause_command_id = str(uuid4())
        checks = 0

        def pause_at_tool_boundary() -> str | None:
            nonlocal checks
            checks += 1
            return pause_command_id if checks >= 5 else None

        events = runner.run(
            _make_job("列出文件", workspace_path=workspace),
            pause_check=pause_at_tool_boundary,
        )

        types = [event.event_type for event in events]
        assert types[-1] == "agent.run.paused"
        assert "tool.call.started" not in types
        checkpoint = events[-1].internal["run_checkpoint"]
        assert checkpoint["resume_node"] == "execute_tool"

        resumed = runner.resume_from_checkpoint(checkpoint)
        resumed_types = [event.event_type for event in resumed]
        assert resumed_types.count("tool.call.started") == 1
        assert resumed_types.count("tool.call.finished") == 1
        assert resumed_types[-1] == "agent.run.completed"


def test_pause_arriving_during_model_call_wins_before_finish() -> None:
    runner = _make_agent_runner()
    pause_command_id = str(uuid4())
    checks = 0

    def pause_after_model_returns() -> str | None:
        nonlocal checks
        checks += 1
        return pause_command_id if checks >= 4 else None

    events = runner.run(
        _make_job("hello"), pause_check=pause_after_model_returns
    )

    types = [event.event_type for event in events]
    assert types[-1] == "agent.run.paused"
    assert "agent.run.completed" not in types
    checkpoint = events[-1].internal["run_checkpoint"]
    assert checkpoint["resume_node"] == "validate_action"

    resumed = runner.resume_from_checkpoint(checkpoint)
    resumed_types = [event.event_type for event in resumed]
    assert resumed_types[-1] == "agent.run.completed"
    assert "model.call.started" not in resumed_types


def test_pause_arriving_during_model_call_wins_over_provider_failure() -> None:
    marker = {"pause_arrived": False}

    class FailingAfterPauseArrives(ModelProvider):
        def decide_next_action(self, _state):
            marker["pause_arrived"] = True
            raise ModelProviderError(
                "MODEL_PROVIDER_ERROR",
                "injected provider failure",
                recoverable=False,
            )

    pause_command_id = str(uuid4())
    runner = AgentRunner(
        model_provider=FailingAfterPauseArrives(),
        tool_gateway=_make_tool_gateway(),
    )

    events = runner.run(
        _make_job("请全面分析工作区所有 Markdown 文件；不要修改。"),
        pause_check=lambda: pause_command_id if marker["pause_arrived"] else None,
    )

    types = [event.event_type for event in events]
    assert types[-1] == "agent.run.paused"
    assert "agent.run.failed" not in types
    assert "model.call.failed" not in types
    checkpoint = events[-1].internal["run_checkpoint"]
    assert checkpoint["resume_node"] == "call_model"
