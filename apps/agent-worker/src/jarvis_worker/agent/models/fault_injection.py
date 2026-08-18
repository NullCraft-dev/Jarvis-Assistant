"""隔离验收专用 ModelProvider 故障注入。

生产默认永不装配。只有全局测试故障开关已开启时，Worker 才会观察屏障目录下
固定名称的 trigger 文件；首次模型入口会原子消费该文件并抛出可恢复超时。
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Callable

from jarvis_worker.agent.context.types import ContextPackage, ModelContextProfile
from jarvis_worker.agent.core.actions import AgentAction
from jarvis_worker.agent.core.state import AgentState
from jarvis_worker.agent.models.errors import model_timeout_error
from jarvis_worker.agent.models.messages import ModelMessage
from jarvis_worker.agent.models.provider import ModelProvider

MODEL_FAILURE_TRIGGER = "model-recoverable-failure.trigger"
MODEL_FAILURE_CONSUMED = "model-recoverable-failure.consumed"


class OneShotRecoverableModelFailureProvider(ModelProvider):
    """在显式文件触发时注入一次可恢复模型超时。"""

    def __init__(self, delegate: ModelProvider, barrier_root: Path) -> None:
        self._delegate = delegate
        self._trigger = barrier_root / MODEL_FAILURE_TRIGGER
        self._consumed = barrier_root / MODEL_FAILURE_CONSUMED

    def _maybe_fail(self) -> None:
        try:
            os.replace(self._trigger, self._consumed)
        except FileNotFoundError:
            return
        raise model_timeout_error("测试故障注入：模型请求超时")

    @property
    def provider_name(self) -> str:
        return self._delegate.provider_name

    @property
    def model_name(self) -> str:
        return self._delegate.model_name

    @property
    def context_profile(self) -> ModelContextProfile:
        return self._delegate.context_profile

    def decide_next_action(self, state: AgentState) -> AgentAction:
        self._maybe_fail()
        return self._delegate.decide_next_action(state)

    def decide_next_action_stream(
        self, state: AgentState, on_text_delta: Callable[[str], None]
    ) -> AgentAction:
        self._maybe_fail()
        return self._delegate.decide_next_action_stream(state, on_text_delta)

    def decide_prepared_context(self, state: AgentState, context: ContextPackage) -> AgentAction:
        self._maybe_fail()
        return self._delegate.decide_prepared_context(state, context)

    def decide_prepared_context_stream(
        self,
        state: AgentState,
        context: ContextPackage,
        on_text_delta: Callable[[str], None],
    ) -> AgentAction:
        self._maybe_fail()
        return self._delegate.decide_prepared_context_stream(state, context, on_text_delta)

    def decide_prepared_context_finish_only(
        self, state: AgentState, context: ContextPackage
    ) -> AgentAction:
        self._maybe_fail()
        return self._delegate.decide_prepared_context_finish_only(state, context)

    def decide_prepared_context_tool_required(
        self, state: AgentState, context: ContextPackage
    ) -> AgentAction:
        self._maybe_fail()
        return self._delegate.decide_prepared_context_tool_required(state, context)

    def complete_structured(
        self,
        messages: list[ModelMessage],
        parser: Callable[[str], Any],
        **kwargs: Any,
    ) -> Any:
        self._maybe_fail()
        return self._delegate.complete_structured(messages, parser, **kwargs)
