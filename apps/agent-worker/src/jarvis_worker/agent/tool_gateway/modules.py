"""Capability module 的最小声明契约。

Capability module 只负责把一个领域能力的 ToolManifest 与 native/MCP executor
绑定起来。它不是新的工具注册中心，也不能绕过 ToolGateway 执行工具。
"""

from __future__ import annotations

from dataclasses import dataclass

from jarvis_worker.agent.tool_gateway.registry import ToolExecutorFn
from jarvis_worker.agent.tool_gateway.contracts import ToolManifest


@dataclass(frozen=True, slots=True)
class ToolBinding:
    """一个工具声明及其执行器绑定。"""

    manifest: ToolManifest
    executor: ToolExecutorFn


@dataclass(frozen=True, slots=True)
class CapabilityModule:
    """可显式装配的一组相关工具。

    CapabilityModule 仅描述能力，不拥有权限判断、工具执行、审计或事件发布。
    """

    capability_id: str
    version: str
    tool_bindings: tuple[ToolBinding, ...]

    def __post_init__(self) -> None:
        if not self.capability_id.strip():
            raise ValueError("capability_id 不能为空")
        if not self.version.strip():
            raise ValueError("capability version 不能为空")
        if not self.tool_bindings:
            raise ValueError(f"capability {self.capability_id} 必须至少声明一个工具")
