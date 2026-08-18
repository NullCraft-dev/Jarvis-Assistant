"""Agent Worker 内置 capability 的显式清单。"""

from __future__ import annotations

from jarvis_worker.agent.tool_gateway.catalog import collect_tool_manifests
from jarvis_worker.agent.tool_gateway.contracts import ToolManifest
from jarvis_worker.agent.tool_gateway.modules import CapabilityModule
from jarvis_worker.agent.tools.workspace.module import create_workspace_capability


def create_builtin_capability_modules(
    *,
    knowledge_executor=None,
    literature_executor=None,
    literature_search_executor=None,
    rag_search_executor=None,
    rag_ingestion_executor=None,
    rag_await_ingestion_executor=None,
) -> tuple[CapabilityModule, ...]:
    """创建本进程启用的内置 capabilities。

    新增领域能力时在这里显式加入一个 module factory；当前不做动态发现。
    """
    modules = [create_workspace_capability()]
    if knowledge_executor is not None:
        from jarvis_worker.agent.tools.knowledge.module import create_knowledge_capability

        modules.append(create_knowledge_capability(knowledge_executor))
    if literature_executor is not None:
        from jarvis_worker.agent.tools.literature.module import create_literature_capability

        modules.append(
            create_literature_capability(literature_executor, literature_search_executor)
        )
    if rag_search_executor is not None:
        from jarvis_worker.agent.tools.rag.module import create_rag_capability

        modules.append(
            create_rag_capability(
                rag_search_executor,
                rag_ingestion_executor,
                rag_await_ingestion_executor,
            )
        )
    return tuple(modules)


def builtin_tool_manifests() -> tuple[ToolManifest, ...]:
    """返回内置工具 manifests，供 Prompt/Parser fallback 复用同一真源。"""
    return collect_tool_manifests(create_builtin_capability_modules())


def builtin_tool_names() -> frozenset[str]:
    """返回内置工具名集合。"""
    return frozenset(manifest.name for manifest in builtin_tool_manifests())
