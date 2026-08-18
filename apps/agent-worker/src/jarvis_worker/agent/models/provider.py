"""ModelProvider — 模型决策抽象协议。

AgentRunner 通过此协议调用模型，获取下一个动作。
本轮只定义接口；后续 LangChain/LangGraph 接入时实现真实 provider。

设计意图：
- 与 LangChain BaseChatModel / Runnable 接口兼容。
- 但 AgentRunner 不直接依赖 LangChain 类型，通过此协议解耦。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, Callable

from jarvis_worker.agent.context.types import ContextPackage, ModelContextProfile
from jarvis_worker.agent.core.actions import AgentAction
from jarvis_worker.agent.core.state import AgentState
from jarvis_worker.agent.models.messages import ModelMessage


class ModelProvider(ABC):
    """模型决策抽象协议。

    后续可替换为：
    - 测试替身（仅 pytest 测试包）
    - LangChainModelProvider（后续，封装 ChatOpenAI / ChatAnthropic 等）
    - LangGraphModelProvider（后续，通过 StateGraph node 封装）
    """

    @property
    def provider_name(self) -> str:
        """Provider 标识名，供 observability（model.call.* 事件）使用。"""
        return "unknown"

    @property
    def model_name(self) -> str:
        """模型名，供 observability 使用。"""
        return "unknown"

    @property
    def context_profile(self) -> ModelContextProfile:
        """模型上下文容量；测试替身使用保守默认值。"""
        return ModelContextProfile(
            provider=self.provider_name,
            model=self.model_name,
            context_window_tokens=32_768,
            max_output_tokens=4_096,
        )

    @abstractmethod
    def decide_next_action(self, state: AgentState) -> AgentAction:
        """根据当前 AgentState 决定下一个动作。

        Args:
            state: 当前 AgentState（含 user_goal、observations、iteration 等）

        Returns:
            AgentAction（finish / call_tool）
        """
        ...

    def decide_next_action_stream(
        self,
        state: AgentState,
        on_text_delta: Callable[[str], None],
    ) -> AgentAction:
        """在支持时流式产生安全的最终回复文本。

        默认实现保持既有 Provider 兼容：它们仍只返回完整 AgentAction。实现者只能
        在检测到输出为 ``finish.final_message`` 时调用回调，最终仍须完成 AgentAction
        校验；原始
        模型响应、工具参数和上下文不得通过此回调离开模型层。
        """
        del on_text_delta
        return self.decide_next_action(state)

    def decide_prepared_context(
        self,
        state: AgentState,
        context: ContextPackage,
    ) -> AgentAction:
        """使用已预算上下文决策；默认桥接仅用于既有测试替身。"""
        del context
        return self.decide_next_action(state)

    def decide_prepared_context_stream(
        self,
        state: AgentState,
        context: ContextPackage,
        on_text_delta: Callable[[str], None],
    ) -> AgentAction:
        """流式使用 ContextPackage；生产 Provider 应覆写此入口。"""
        del context
        return self.decide_next_action_stream(state, on_text_delta)

    def decide_prepared_context_finish_only(
        self,
        state: AgentState,
        context: ContextPackage,
    ) -> AgentAction:
        """终态收口决策；生产 Provider 覆写后只接受 finish schema。"""
        return self.decide_prepared_context(state, context)

    def decide_prepared_context_tool_required(
        self,
        state: AgentState,
        context: ContextPackage,
    ) -> AgentAction:
        """证据补全决策；生产 Provider 覆写后只接受 call_tool schema。"""
        return self.decide_prepared_context(state, context)

    def complete_structured(
        self,
        messages: list[ModelMessage],
        parser: Callable[[str], Any],
    ) -> Any:
        """Run a non-streaming structured completion through a caller parser.

        Production providers override this generic Harness entry. Keeping it
        separate from AgentAction avoids disguising intent classification as a
        tool/finish decision.
        """
        del messages, parser
        raise NotImplementedError("ModelProvider does not support structured completion")
