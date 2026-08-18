from pathlib import Path

import pytest

from jarvis_worker.agent.core.actions import AgentAction
from jarvis_worker.agent.core.state import AgentState
from jarvis_worker.agent.models.errors import ModelProviderError
from jarvis_worker.agent.models.fault_injection import (
    MODEL_FAILURE_CONSUMED,
    MODEL_FAILURE_TRIGGER,
    OneShotRecoverableModelFailureProvider,
)
from jarvis_worker.agent.models.provider import ModelProvider


class _Delegate(ModelProvider):
    def __init__(self):
        self.calls = 0

    def decide_next_action(self, state: AgentState) -> AgentAction:
        self.calls += 1
        return AgentAction.finish("ok")


def test_one_shot_model_failure_requires_trigger_and_is_consumed(tmp_path: Path):
    delegate = _Delegate()
    provider = OneShotRecoverableModelFailureProvider(delegate, tmp_path)
    state = AgentState(task_id="t", run_id="r", user_goal="test")

    assert provider.decide_next_action(state).final_message == "ok"
    (tmp_path / MODEL_FAILURE_TRIGGER).write_text("armed", encoding="utf-8")

    with pytest.raises(ModelProviderError) as captured:
        provider.decide_next_action(state)

    assert captured.value.code == "MODEL_TIMEOUT"
    assert captured.value.recoverable is True
    assert not (tmp_path / MODEL_FAILURE_TRIGGER).exists()
    assert (tmp_path / MODEL_FAILURE_CONSUMED).exists()
    assert provider.decide_next_action(state).final_message == "ok"
    assert delegate.calls == 2
