"""Capability module 的显式装配工具。

当前只支持代码内显式装配，不做目录扫描、entry point discovery 或插件市场。
ToolRegistry 仍是运行时唯一工具注册中心。
"""

from __future__ import annotations

from collections.abc import Iterable

from jarvis_worker.agent.tool_gateway.modules import CapabilityModule
from jarvis_worker.agent.tool_gateway.registry import ToolRegistry
from jarvis_worker.agent.tool_gateway.contracts import ToolManifest


def collect_tool_manifests(
    modules: Iterable[CapabilityModule],
) -> tuple[ToolManifest, ...]:
    """按 module/binding 声明顺序返回 manifests。"""
    return tuple(
        binding.manifest
        for module in modules
        for binding in module.tool_bindings
    )


def install_capability_modules(
    registry: ToolRegistry,
    modules: Iterable[CapabilityModule],
) -> tuple[str, ...]:
    """预检后把 capability tools 安装到现有 ToolRegistry。

    重复 capability_id、重复工具名、空工具名或不可调用 executor 会在修改
    registry 前失败，避免可预见的部分注册状态。
    """
    module_list = tuple(modules)
    existing_tool_names = {manifest.name for manifest in registry.list_manifests()}
    seen_capability_ids: set[str] = set()
    seen_tool_names: set[str] = set()

    for module in module_list:
        if module.capability_id in seen_capability_ids:
            raise ValueError(f"capability {module.capability_id} 重复声明")
        seen_capability_ids.add(module.capability_id)

        for binding in module.tool_bindings:
            tool_name = binding.manifest.name.strip()
            if not tool_name:
                raise ValueError(f"capability {module.capability_id} 包含空工具名")
            if not callable(binding.executor):
                raise ValueError(f"工具 {tool_name} 的 executor 不可调用")
            if tool_name in existing_tool_names or tool_name in seen_tool_names:
                raise ValueError(f"工具 {tool_name} 重复声明")
            seen_tool_names.add(tool_name)

    for module in module_list:
        for binding in module.tool_bindings:
            registry.register(binding.manifest, binding.executor)

    return tuple(module.capability_id for module in module_list)
