from jarvis_worker.agent.core.required_tool_recovery import (
    recover_required_rag_search_action,
)
from jarvis_worker.agent.core.state import AgentState
from jarvis_worker.agent.loop.contracts import CompletionContract


def _state(*, scope: str = "selected") -> AgentState:
    return AgentState(
        user_goal="根据论文回答问题",
        intent={
            "retrieval": {
                "mode": "required",
                "query": "论文没有回答的重要问题",
                "document_scope": scope,
                "resolved_document_ids": ["11111111-1111-4111-8111-111111111111"],
            }
        },
        completion_contract=CompletionContract(requires_rag_evidence=True).to_state_dict(),
    )


def test_recovers_only_unique_missing_rag_evidence_action() -> None:
    action = recover_required_rag_search_action(
        _state(),
        available_tool_names=frozenset({"rag.search", "workspace.read_file"}),
    )

    assert action is not None
    assert action.tool_name == "rag.search"
    assert action.arguments == {"query": "论文没有回答的重要问题", "top_k": 8}


def test_does_not_recover_unresolved_or_additional_required_effects() -> None:
    assert (
        recover_required_rag_search_action(
            _state(scope="unresolved"),
            available_tool_names=frozenset({"rag.search"}),
        )
        is None
    )
    state = _state()
    state.completion_contract = CompletionContract(
        required_tool_names=("knowledge.create_document",),
        requires_rag_evidence=True,
    ).to_state_dict()
    assert (
        recover_required_rag_search_action(
            state,
            available_tool_names=frozenset({"rag.search", "knowledge.create_document"}),
        )
        is None
    )
