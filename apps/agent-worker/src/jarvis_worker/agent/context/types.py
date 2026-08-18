"""ContextManager 的供应商无关数据契约。"""

from __future__ import annotations

from dataclasses import dataclass

from jarvis_worker.agent.models.messages import ModelMessage


@dataclass(frozen=True)
class ModelContextProfile:
    """一次模型调用使用的上下文容量配置。"""

    provider: str
    model: str
    context_window_tokens: int
    max_output_tokens: int
    safety_margin_tokens: int = 1024

    @property
    def input_budget_tokens(self) -> int:
        return (
            self.context_window_tokens
            - self.max_output_tokens
            - self.safety_margin_tokens
        )


@dataclass(frozen=True)
class ContextStats:
    """可安全进入 RuntimeEvent 的上下文统计，不包含消息正文。"""

    policy_version: str
    estimator: str
    estimated_input_tokens: int
    input_budget_tokens: int
    context_window_tokens: int
    max_output_tokens: int
    safety_margin_tokens: int
    included_history_turns: int
    dropped_history_turns: int
    included_observations: int
    dropped_observations: int
    included_memories: int
    dropped_memories: int
    message_count: int
    truncated: bool


@dataclass(frozen=True)
class ContextPackage:
    """ContextManager 交给 ModelProvider 的唯一模型输入。"""

    messages: tuple[ModelMessage, ...]
    profile: ModelContextProfile
    stats: ContextStats
    fingerprint: str
