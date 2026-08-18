"""ModelMessage — 供应商无关的模型消息角色契约。

Phase 6B-0 v3 审查修复：在 dataclass frozen 之上增加 __post_init__ 运行时校验，
确保 role/content/name/tool_call_id 遵守角色不变量。

职责：
- 定义 system / user / assistant / tool 四种消息角色。
- 运行时校验角色不变量，不依赖类型注解。
- assistant 保存模型生成的结构化 AgentAction。
- tool 保存 ToolGateway 返回的受控 ToolResult。
- assistant 和 tool 使用同一个 tool_call_id 关联。

不负责：
- 转换为任何供应商的消息格式（由未来 Provider Adapter 负责）。
- Token 计数（由 ContextManager 负责）。
- 模型调用（由 ModelProvider 负责）。

这是单 Agent 的消息信任角色，不是 Multi-Agent 的 Planner/Executor/Reviewer 角色。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

# system:   Runtime 不变量、安全规则、JSON Action 契约、工具列表
# user:     当前用户目标
# assistant: 模型生成的 AgentAction（finish / call_tool）
# tool:     ToolGateway 返回的受控 ToolResult
ModelMessageRole = Literal["system", "user", "assistant", "tool"]

_VALID_ROLES = frozenset({"system", "user", "assistant", "tool"})


class ModelMessageValidationError(ValueError):
    """ModelMessage 运行时校验失败。"""


@dataclass(frozen=True)
class ModelMessage:
    """供应商无关的模型消息。

    Attributes:
        role: 消息角色（system / user / assistant / tool）。
        content: 消息正文（字符串）。
        name: tool 消息的 tool_name，或 assistant 工具调用时的 tool_name（可选）。
        tool_call_id: assistant/tool 关联 ID（可选，但 tool 角色必须提供）。
    """

    role: ModelMessageRole
    content: str
    name: str | None = None
    tool_call_id: str | None = None

    def __post_init__(self) -> None:
        """运行时校验角色不变量。"""
        # -- 通用校验 --
        if self.role not in _VALID_ROLES:
            raise ModelMessageValidationError(
                f"非法 role: {self.role!r}，允许: {sorted(_VALID_ROLES)}"
            )

        if not isinstance(self.content, str):
            raise ModelMessageValidationError(
                f"content 必须是 str，实际类型: {type(self.content).__name__}"
            )

        # name 如果提供必须是非空字符串
        if self.name is not None and (
            not isinstance(self.name, str) or self.name.strip() == ""
        ):
            raise ModelMessageValidationError(
                f"name 必须是非空字符串或 None，实际: {self.name!r}"
            )

        # tool_call_id 如果提供必须是非空字符串
        if self.tool_call_id is not None and (
            not isinstance(self.tool_call_id, str) or self.tool_call_id.strip() == ""
        ):
            raise ModelMessageValidationError(
                f"tool_call_id 必须是非空字符串或 None，实际: {self.tool_call_id!r}"
            )

        # -- 角色不变量 --
        if self.role == "system":
            if self.name is not None:
                raise ModelMessageValidationError(
                    "system 消息不允许携带 name"
                )
            if self.tool_call_id is not None:
                raise ModelMessageValidationError(
                    "system 消息不允许携带 tool_call_id"
                )

        elif self.role == "user":
            if self.name is not None:
                raise ModelMessageValidationError(
                    "user 消息不允许携带 name"
                )
            if self.tool_call_id is not None:
                raise ModelMessageValidationError(
                    "user 消息不允许携带 tool_call_id"
                )

        elif self.role == "assistant":
            # assistant 工具调用历史：name 和 tool_call_id 必须同时存在或同时不存在
            has_name = self.name is not None
            has_tcid = self.tool_call_id is not None
            if has_name != has_tcid:
                raise ModelMessageValidationError(
                    "assistant 的 name 和 tool_call_id 必须同时提供或同时省略，"
                    f"当前 name={self.name!r} tool_call_id={self.tool_call_id!r}"
                )

        elif self.role == "tool":
            if not isinstance(self.name, str) or self.name.strip() == "":
                raise ModelMessageValidationError(
                    f"tool 消息必须提供非空 name（tool_name），实际: {self.name!r}"
                )
            if not isinstance(self.tool_call_id, str) or self.tool_call_id.strip() == "":
                raise ModelMessageValidationError(
                    f"tool 消息必须提供非空 tool_call_id，实际: {self.tool_call_id!r}"
                )

    @classmethod
    def system(cls, content: str) -> ModelMessage:
        """构造 system 消息。"""
        return cls(role="system", content=content)

    @classmethod
    def user(cls, content: str) -> ModelMessage:
        """构造 user 消息。"""
        return cls(role="user", content=content)

    @classmethod
    def assistant(
        cls,
        content: str,
        name: str | None = None,
        tool_call_id: str | None = None,
    ) -> ModelMessage:
        """构造 assistant 消息（模型生成的 AgentAction JSON）。

        Args:
            content: JSON 序列化的 AgentAction。
            name: 工具调用时的 tool_name（可选，与 tool_call_id 配对）。
            tool_call_id: 与对应 tool 消息相同的 ID（可选，与 name 配对）。
        """
        return cls(
            role="assistant",
            content=content,
            name=name,
            tool_call_id=tool_call_id,
        )

    @classmethod
    def tool(
        cls,
        content: str,
        name: str,
        tool_call_id: str,
    ) -> ModelMessage:
        """构造 tool 消息（受控 ToolResult JSON）。

        Args:
            content: JSON 序列化的受控 ToolResult。
            name: tool_name（如 "workspace.read_file"）。
            tool_call_id: 与对应 assistant 消息相同的 tool_call_id。
        """
        return cls(
            role="tool",
            content=content,
            name=name,
            tool_call_id=tool_call_id,
        )
