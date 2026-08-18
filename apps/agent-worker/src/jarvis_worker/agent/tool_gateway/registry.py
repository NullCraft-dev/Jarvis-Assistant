"""ToolRegistry — 运行时唯一的工具注册与查找中心。

内置工具由 capability modules 显式装配；未来 MCP discovery 也必须归一化为
ToolManifest + executor 后注册到这里，不能建立第二套 registry。
"""

from __future__ import annotations

import logging
from typing import Callable

from jarvis_worker.agent.tool_gateway.contracts import ToolManifest, ToolRequest, ToolResult

log = logging.getLogger("jarvis_worker.tool_registry")

# 工具执行器签名：接收 ToolRequest，返回 ToolResult
ToolExecutorFn = Callable[[ToolRequest], ToolResult]


class ToolRegistry:
    """工具注册中心。

    职责：
      - 注册工具 manifest 和执行器
      - 按名称查找工具
      - 列出所有已注册工具

    不负责：
      - 权限检查
      - 执行工具（由 ToolGateway 负责）
      - 审计/事件发布
    """

    def __init__(self):
        self._manifests: dict[str, ToolManifest] = {}
        self._executors: dict[str, ToolExecutorFn] = {}

    def register(self, manifest: ToolManifest, executor: ToolExecutorFn) -> None:
        """注册一个工具及其执行器。

        Args:
            manifest: 工具 manifest
            executor: 工具执行器函数

        Raises:
            ValueError: 工具名已存在
        """
        if manifest.name in self._manifests:
            raise ValueError(f"工具 {manifest.name} 已注册")
        self._manifests[manifest.name] = manifest
        self._executors[manifest.name] = executor
        log.info(
            "注册工具: %s provider=%s risk=%s",
            manifest.name,
            manifest.provider,
            manifest.risk_level_default,
        )

    def get_manifest(self, tool_name: str) -> ToolManifest | None:
        """按名称查找工具 manifest。"""
        return self._manifests.get(tool_name)

    def get_executor(self, tool_name: str) -> ToolExecutorFn | None:
        """按名称查找工具执行器。"""
        return self._executors.get(tool_name)

    def list_manifests(self) -> list[ToolManifest]:
        """返回所有已注册工具的 manifest 列表。"""
        return list(self._manifests.values())

    def has(self, tool_name: str) -> bool:
        """判断工具是否已注册。"""
        return tool_name in self._manifests

    @property
    def tool_count(self) -> int:
        return len(self._manifests)
