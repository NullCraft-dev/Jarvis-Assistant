"""CompletionContract、ProgressTracker 与 StopController 回归。"""

from __future__ import annotations

from jarvis_worker.agent.core.state import AgentState
from jarvis_worker.agent.intents import (
    IntentExtraction,
    IntentRuntimeContext,
    IntentWorkspace,
    RetrievalIntent,
)
from jarvis_worker.agent.loop import (
    CompletionContract,
    LoopController,
    LoopProgressSnapshot,
    StopDecision,
)
from jarvis_worker.agent.permissions.manager import PermissionManager
from jarvis_worker.agent.tool_gateway.contracts import ToolManifest, ToolResult
from jarvis_worker.agent.tool_gateway.gateway import ToolGateway
from jarvis_worker.agent.tool_gateway.registry import ToolRegistry


def _gateway() -> ToolGateway:
    registry = ToolRegistry()
    for name in (
        "rag.search",
        "workspace.read_file",
        "workspace.search_files",
        "workspace.create_file",
        "workspace.move_path",
    ):
        registry.register(
            ToolManifest(name=name, risk_level_default="L0"),
            lambda _request: ToolResult(ok=True, summary="ok"),
        )
    return ToolGateway(registry, PermissionManager())


def _state(*, clarification_required: bool = False) -> AgentState:
    intent = IntentExtraction(
        primary_intent="document_question",
        retrieval=RetrievalIntent(
            mode="required",
            query="运行控制",
            confidence=1.0,
            reason="用户要求文档证据",
            document_scope="all",
        ),
        workspace=IntentWorkspace(
            evidence="required",
            action="read",
            ambiguity=("clarification_required" if clarification_required else "clear"),
            reason="需要读取工作区证据",
        ),
    )
    return AgentState(
        task_id="task-1",
        run_id="run-1",
        user_goal="请结合工作区和知识库说明运行控制",
        intent=intent.to_state_dict(),
        intent_context=IntentRuntimeContext().to_state_dict(),
    )


def _observation(tool_name: str, arguments: dict, *, ok: bool = True) -> dict:
    return {
        "tool_call_id": f"call-{tool_name}-{len(arguments)}",
        "tool_name": tool_name,
        "model_action": {
            "action_type": "call_tool",
            "tool_name": tool_name,
            "arguments": arguments,
            "reason": "获取证据",
        },
        "ok": ok,
        "summary": "ok" if ok else "failed",
    }


def test_loop_controller_freezes_contract_from_validated_intent() -> None:
    state = _state()
    controller = LoopController(_gateway())

    controller.ensure_initialized(state)

    contract = CompletionContract.from_state_dict(state.completion_contract)
    progress = LoopProgressSnapshot.from_state_dict(state.loop_progress)
    decision = StopDecision.from_state_dict(state.stop_decision)
    assert contract.requires_rag_evidence is True
    assert contract.workspace_evidence == "required"
    assert progress.tool_calls_used == 0
    assert decision.reason_code == "LOOP_INITIALIZED"


def test_successful_workspace_write_satisfies_conflicting_metadata_intent() -> None:
    state = _state()
    state.user_goal = "创建 restart-test.txt，在权限卡出现后重启服务，再批准。"
    state.intent["retrieval"] = {
        "mode": "skip",
        "query": "",
        "reason": "不需要知识检索",
        "confidence": 1.0,
        "document_refs": [],
        "document_scope": "none",
        "resolved_document_ids": [],
    }
    # 复现真实 REC-06：schema 合法，但 action=read 与明确创建目标冲突。
    state.intent["workspace"] = {
        "evidence": "metadata",
        "action": "read",
        "ambiguity": "clear",
        "reason": "先确认目标元数据",
        "listing_entry_types": [],
    }
    controller = LoopController(_gateway())
    controller.ensure_initialized(state)
    state.observations.append(_observation("workspace.create_file", {"path": "restart-test.txt"}))
    controller.refresh_progress(state)

    contract = CompletionContract.from_state_dict(state.completion_contract)
    decision = controller.evaluate_finish(state)

    assert contract.required_tool_names == ("workspace.create_file",)
    assert contract.workspace_evidence == "metadata"
    assert decision.disposition == "complete"
    assert decision.missing_requirements == ()


def test_progress_tracker_detects_repeated_action_without_new_progress() -> None:
    state = _state()
    controller = LoopController(_gateway())
    controller.ensure_initialized(state)
    state.observations.append(_observation("workspace.read_file", {"path": "a.md"}))

    first = controller.refresh_progress(state)
    state.observations.append(_observation("workspace.read_file", {"path": "a.md"}))
    repeated = controller.refresh_progress(state)
    state.observations.append(_observation("workspace.read_file", {"path": "b.md"}))
    advanced = controller.refresh_progress(state)

    assert first.last_observation_advanced is True
    assert repeated.last_observation_advanced is False
    assert repeated.no_progress_streak == 1
    assert advanced.last_observation_advanced is True
    assert advanced.no_progress_streak == 0


def test_proposed_action_guard_requires_strategy_change_then_fails_closed() -> None:
    state = _state()
    controller = LoopController(_gateway())
    controller.ensure_initialized(state)
    state.observations.append(_observation("workspace.read_file", {"path": "a.md"}))
    controller.refresh_progress(state)

    first = controller.evaluate_proposed_action(
        state,
        tool_name="workspace.read_file",
        arguments={"path": "a.md"},
    )
    second = controller.evaluate_proposed_action(
        state,
        tool_name="workspace.read_file",
        arguments={"path": "a.md"},
    )
    changed = controller.evaluate_proposed_action(
        state,
        tool_name="workspace.read_file",
        arguments={"path": "b.md"},
    )

    assert first is not None and first.disposition == "continue"
    assert first.reason_code == "STRATEGY_CHANGE_REQUIRED"
    assert second is not None and second.disposition == "fail"
    assert second.reason_code == "LOOP_NO_PROGRESS"
    assert changed is None


def test_proposed_action_guard_blocks_any_second_effect_outside_exclusive_scope() -> None:
    state = _state()
    state.user_goal = "把 incoming/meeting-notes.md 移动到刚建的目录，其他文件不动。"
    controller = LoopController(_gateway())
    controller.ensure_initialized(state)
    state.observations.append(
        _observation(
            "workspace.move_path",
            {"source": "incoming/meeting-notes.md", "destination": "archive/meeting-notes.md"},
        )
    )

    first = controller.evaluate_proposed_action(
        state,
        tool_name="workspace.move_path",
        arguments={"source": "report-final.md", "destination": "archive/report-final.md"},
    )
    repeated = controller.evaluate_proposed_action(
        state,
        tool_name="workspace.delete_path",
        arguments={"path": "unrelated.md"},
    )

    assert first is not None and first.disposition == "continue"
    assert first.reason_code == "WORKSPACE_EFFECT_SCOPE_SATISFIED"
    assert repeated is not None and repeated.disposition == "fail"
    assert repeated.reason_code == "WORKSPACE_EFFECT_SCOPE_SATISFIED"


def test_exclusive_effect_scope_survives_a_later_failed_observation() -> None:
    state = _state()
    state.user_goal = "把 incoming/meeting-notes.md 移动到刚建的目录，其他文件不动。"
    controller = LoopController(_gateway())
    controller.ensure_initialized(state)
    state.observations.extend(
        [
            _observation(
                "workspace.move_path",
                {
                    "source": "incoming/meeting-notes.md",
                    "destination": "archive/meeting-notes.md",
                },
            ),
            _observation("workspace.read_file", {"path": "missing.md"}, ok=False),
        ]
    )

    decision = controller.evaluate_proposed_action(
        state,
        tool_name="workspace.move_path",
        arguments={"source": "unrelated.md", "destination": "archive/unrelated.md"},
    )

    assert decision is not None
    assert decision.reason_code == "WORKSPACE_EFFECT_SCOPE_SATISFIED"


def test_proposed_action_guard_does_not_invent_exclusive_scope() -> None:
    state = _state()
    state.user_goal = "把 a.md 和 b.md 移动到 archive 目录。"
    controller = LoopController(_gateway())
    controller.ensure_initialized(state)
    state.observations.append(
        _observation(
            "workspace.move_path",
            {"source": "a.md", "destination": "archive/a.md"},
        )
    )

    assert controller.evaluate_proposed_action(
        state,
        tool_name="workspace.move_path",
        arguments={"source": "b.md", "destination": "archive/b.md"},
    ) is None


def test_proposed_action_guard_rejects_unapproved_cross_domain_substitution() -> None:
    registry = ToolRegistry()
    for name, domain, substitutes in (
        ("source.primary_search", "external.primary", []),
        ("source.equivalent_search", "external.equivalent", []),
        ("knowledge.local_search", "workspace.local", []),
    ):
        registry.register(
            ToolManifest(
                name=name,
                risk_level_default="L0",
                metadata={"loop": {
                    "operation": "retrieve_evidence",
                    "evidence_domain": domain,
                    "substitutable_evidence_domains": substitutes,
                }},
            ),
            lambda _request: ToolResult(ok=True),
        )
    controller = LoopController(ToolGateway(registry, PermissionManager()))
    state = _state()
    state.observations.append(
        _observation("source.primary_search", {"query": "runtime"}, ok=False)
    )

    decision = controller.evaluate_proposed_action(
        state,
        tool_name="knowledge.local_search",
        arguments={"query": "runtime"},
        guard_repeated_success=False,
    )
    repeated = controller.evaluate_proposed_action(
        state,
        tool_name="knowledge.local_search",
        arguments={"query": "different query"},
        guard_repeated_success=False,
    )

    assert decision is not None
    assert decision.disposition == "continue"
    assert decision.reason_code == "SEMANTIC_SOURCE_SUBSTITUTION"
    assert repeated is not None
    assert repeated.disposition == "fail"
    assert repeated.reason_code == "SEMANTIC_SOURCE_SUBSTITUTION"


def test_proposed_action_guard_accepts_manifest_approved_equivalent_domain() -> None:
    registry = ToolRegistry()
    registry.register(
        ToolManifest(
            name="source.primary_search",
            metadata={"loop": {
                "operation": "retrieve_evidence",
                "evidence_domain": "external.primary",
                "substitutable_evidence_domains": ["external.equivalent"],
            }},
        ),
        lambda _request: ToolResult(ok=True),
    )
    registry.register(
        ToolManifest(
            name="source.equivalent_search",
            metadata={"loop": {
                "operation": "retrieve_evidence",
                "evidence_domain": "external.equivalent",
                "substitutable_evidence_domains": [],
            }},
        ),
        lambda _request: ToolResult(ok=True),
    )
    controller = LoopController(ToolGateway(registry, PermissionManager()))
    state = _state()
    state.observations.append(
        _observation("source.primary_search", {"query": "runtime"}, ok=False)
    )

    assert controller.evaluate_proposed_action(
        state,
        tool_name="source.equivalent_search",
        arguments={"query": "runtime"},
    ) is None


def test_stop_controller_requires_all_frozen_evidence_before_completion() -> None:
    state = _state()
    controller = LoopController(_gateway())
    controller.ensure_initialized(state)

    missing = controller.evaluate_finish(state)
    state.observations.extend(
        [
            _observation("rag.search", {"query": "运行控制"}),
            _observation("workspace.read_file", {"path": "runtime.md"}),
        ]
    )
    controller.refresh_progress(state)
    complete = controller.evaluate_finish(state)

    assert missing.disposition == "continue"
    assert "rag.search" in missing.missing_requirements
    assert any(
        requirement.startswith("workspace 文件正文读取")
        for requirement in missing.missing_requirements
    )
    assert complete.disposition == "complete"
    assert complete.reason_code == "COMPLETION_CONTRACT_SATISFIED"


def test_stop_controller_uses_explicit_clarification_terminal() -> None:
    state = _state(clarification_required=True)
    controller = LoopController(_gateway())

    decision = controller.evaluate_finish(state)

    assert decision.disposition == "clarify"
    assert decision.reason_code == "CLARIFICATION_REQUIRED"


def test_stop_controller_treats_unknown_intent_as_general_clarification() -> None:
    state = _state()
    assert state.intent is not None
    state.intent["primary_intent"] = "unknown"
    controller = LoopController(_gateway())

    decision = controller.evaluate_finish(state)

    assert decision.disposition == "clarify"
    assert decision.reason_code == "CLARIFICATION_REQUIRED"


def test_completion_contract_accepts_v1_and_rewrites_conditional_effect_to_v2() -> None:
    state = AgentState(
        task_id="task-conditional",
        run_id="run-conditional",
        user_goal=(
            "创建 notes/existing.md，写入‘新版本’，如果已存在就先告诉我，不要覆盖。"
        ),
        intent=IntentExtraction(
            primary_intent="task",
            retrieval=RetrievalIntent(mode="skip", query="", confidence=1.0, reason=""),
            workspace=IntentWorkspace(
                evidence="skip",
                action="write",
                ambiguity="clear",
                reason="条件式创建",
            ),
        ).to_state_dict(),
        intent_context=IntentRuntimeContext().to_state_dict(),
        completion_contract={
            "required_tool_names": ["workspace.create_file"],
            "requires_rag_evidence": False,
            "workspace_evidence": "skip",
            "workspace_action": "write",
            "clarification_required": False,
            "version": "completion-contract-v1",
        },
    )
    controller = LoopController(_gateway())

    controller.ensure_initialized(state)

    contract = CompletionContract.from_state_dict(state.completion_contract)
    assert contract.version == "completion-contract-v2"
    assert contract.workspace_effect_precondition == "target_absent"
    assert contract.workspace_effect_target == "notes/existing.md"


def test_conditional_create_can_complete_from_exact_existing_target_evidence() -> None:
    state = AgentState(
        task_id="task-conditional",
        run_id="run-conditional",
        user_goal=(
            "创建 notes/existing.md，写入‘新版本’，如果已存在就先告诉我，不要覆盖。"
        ),
        intent=IntentExtraction(
            primary_intent="task",
            retrieval=RetrievalIntent(mode="skip", query="", confidence=1.0, reason=""),
            workspace=IntentWorkspace(
                evidence="skip",
                action="write",
                ambiguity="clear",
                reason="条件式创建",
            ),
        ).to_state_dict(),
        intent_context=IntentRuntimeContext().to_state_dict(),
    )
    controller = LoopController(_gateway())
    controller.ensure_initialized(state)
    before = controller.evaluate_finish(state)
    state.observations.append(
        {
            "tool_call_id": "call-search",
            "tool_name": "workspace.search_files",
            "model_action": {
                "action_type": "call_tool",
                "tool_name": "workspace.search_files",
                "arguments": {"path": "notes", "query": "existing.md"},
                "reason": "检查前置条件",
            },
            "ok": True,
            "summary": "found",
            "data": {
                "matches": [
                    {"path": "notes/existing.md", "name": "existing.md", "type": "file"}
                ]
            },
        }
    )
    controller.refresh_progress(state)

    after = controller.evaluate_finish(state)

    assert "workspace.create_file" in before.missing_requirements
    assert after.disposition == "complete"
    assert after.reason_code == "WORKSPACE_EFFECT_PRECONDITION_SHORT_CIRCUITED"
    assert controller.workspace_effect_short_circuit_target(state) == "notes/existing.md"
