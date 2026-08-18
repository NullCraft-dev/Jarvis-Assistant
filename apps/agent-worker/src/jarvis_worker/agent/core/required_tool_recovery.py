"""Narrow host recovery for a uniquely derivable missing evidence action."""

from __future__ import annotations

from jarvis_worker.agent.core.actions import AgentAction
from jarvis_worker.agent.core.effect_guard import rag_document_scope
from jarvis_worker.agent.core.state import AgentState
from jarvis_worker.agent.loop.contracts import CompletionContract


def recover_required_rag_search_action(
    state: AgentState,
    *,
    available_tool_names: frozenset[str],
) -> AgentAction | None:
    """Recover only the exact RAG evidence action already required by Intent.

    The host supplies no answer and performs no effect. The resulting action still
    traverses ActionValidation, ToolGateway, PermissionManager, audit, and Loop.
    """

    if "rag.search" not in available_tool_names or not isinstance(state.completion_contract, dict):
        return None
    try:
        contract = CompletionContract.from_state_dict(state.completion_contract)
    except ValueError:
        return None
    if not contract.requires_rag_evidence or contract.required_tool_names:
        return None
    if any(
        isinstance(item, dict) and item.get("tool_name") == "rag.search" and item.get("ok") is True
        for item in state.observations
    ):
        return None
    if rag_document_scope(state.intent) not in {"all", "selected"}:
        return None
    retrieval = state.intent.get("retrieval") if isinstance(state.intent, dict) else None
    query = retrieval.get("query") if isinstance(retrieval, dict) else None
    if not isinstance(query, str) or not query.strip():
        query = state.user_goal
    query = query.strip()[:2_000]
    if not query:
        return None
    return AgentAction.call_tool(
        "rag.search",
        {"query": query, "top_k": 8},
        "Runtime 根据已校验 Intent 恢复唯一缺失的 RAG 证据动作",
    )
