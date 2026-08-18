"""ToolRegistry 启动组装。

具体 ToolManifest 与 executor binding 由各 capability module 拥有；bootstrap 只把
显式启用的内置 modules 安装到运行时唯一的 ToolRegistry。
"""

from __future__ import annotations

from jarvis_worker.agent.tool_gateway import install_capability_modules
from jarvis_worker.agent.tool_gateway.registry import ToolRegistry
from jarvis_worker.agent.tools.builtin import create_builtin_capability_modules


def create_tool_registry(
    *,
    knowledge_executor=None,
    literature_executor=None,
    literature_search_executor=None,
    rag_search_executor=None,
    rag_ingestion_executor=None,
    rag_await_ingestion_executor=None,
    additional_modules=(),
) -> ToolRegistry:
    """创建并装配所有内置 capability tools。"""
    registry = ToolRegistry()
    install_capability_modules(
        registry,
        create_builtin_capability_modules(
            knowledge_executor=knowledge_executor,
            literature_executor=literature_executor,
            literature_search_executor=literature_search_executor,
            rag_search_executor=rag_search_executor,
            rag_ingestion_executor=rag_ingestion_executor,
            rag_await_ingestion_executor=rag_await_ingestion_executor,
        ),
    )
    install_capability_modules(registry, additional_modules)
    return registry
