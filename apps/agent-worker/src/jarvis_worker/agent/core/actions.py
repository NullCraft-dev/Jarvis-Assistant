"""AgentAction — 模型输出的结构化动作。

本轮仅支持两种动作类型：
- finish: 模型认为任务已完成，携带 final_message。
- call_tool: 模型决定调用某个工具，携带 tool_name / arguments / reason。

后续 LangGraph 接入后，可扩展为 plan / ask_user / delegate 等动作类型。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

# 动作类型：finish（完成）/ call_tool（调用工具）
ActionType = Literal["finish", "call_tool"]


@dataclass
class AgentAction:
    """模型输出的单个动作。

    Attributes:
        action_type: 动作类型
        final_message: finish 时的最终消息（仅 finish 类型有效）
        tool_name: 要调用的工具名（仅 call_tool 类型有效）
        arguments: 工具参数 dict（仅 call_tool 类型有效）
        reason: 选择此工具的原因/推理说明
    """

    action_type: ActionType
    final_message: str = ""
    tool_name: str = ""
    arguments: dict[str, Any] = field(default_factory=dict)
    reason: str = ""
    citations: tuple[dict[str, str], ...] = ()
    insufficient_evidence: bool = False

    @classmethod
    def finish(
        cls,
        final_message: str,
        *,
        citations: tuple[dict[str, str], ...] = (),
        insufficient_evidence: bool = False,
    ) -> "AgentAction":
        """快捷构造 finish 动作。"""
        return cls(
            action_type="finish",
            final_message=final_message,
            citations=citations,
            insufficient_evidence=insufficient_evidence,
        )

    @classmethod
    def call_tool(
        cls,
        tool_name: str,
        arguments: dict[str, Any],
        reason: str = "",
    ) -> "AgentAction":
        """快捷构造 call_tool 动作。"""
        return cls(
            action_type="call_tool",
            tool_name=tool_name,
            arguments=arguments,
            reason=reason,
        )
