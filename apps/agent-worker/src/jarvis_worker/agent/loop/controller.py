"""Completion、Progress 与 Stop 的 Runtime owner。"""

from __future__ import annotations

import hashlib
import json
from typing import Any

from jarvis_worker.agent.core.effect_guard import (
    WORKSPACE_EFFECT_TOOLS,
    conditional_no_overwrite_target,
    find_missing_successful_tool_evidence,
    find_missing_workspace_evidence,
    find_required_goal_tools,
    has_confirmed_workspace_target,
    has_exclusive_single_workspace_effect_scope,
    intent_requires_clarification,
    required_effect_tools,
    requires_rag_search,
    workspace_requires_clarification,
    workspace_semantics,
)
from jarvis_worker.agent.core.state import AgentState
from jarvis_worker.agent.loop.contracts import (
    CompletionContract,
    LoopProgressSnapshot,
    StopDecision,
)
from jarvis_worker.agent.tool_gateway.gateway import ToolGateway


class LoopController:
    """从可信 Intent/ToolResult 构造 Loop 控制状态。"""

    def __init__(self, tool_gateway: ToolGateway) -> None:
        self._tool_gateway = tool_gateway

    def ensure_initialized(self, state: AgentState) -> None:
        if state.intent is None:
            return
        if state.completion_contract is None:
            state.completion_contract = self._build_completion_contract(state).to_state_dict()
        else:
            contract = CompletionContract.from_state_dict(state.completion_contract)
            if state.completion_contract.get("version") != contract.version:
                target = (
                    conditional_no_overwrite_target(state.user_goal)
                    if "workspace.create_file" in contract.required_tool_names
                    else ""
                )
                state.completion_contract = CompletionContract(
                    required_tool_names=contract.required_tool_names,
                    requires_rag_evidence=contract.requires_rag_evidence,
                    workspace_evidence=contract.workspace_evidence,
                    workspace_action=contract.workspace_action,
                    workspace_effect_precondition=(
                        "target_absent" if target else "none"
                    ),
                    workspace_effect_target=target,
                    clarification_required=contract.clarification_required,
                ).to_state_dict()
        if state.loop_progress is None:
            state.loop_progress = self._build_progress(state.observations).to_state_dict()
        else:
            LoopProgressSnapshot.from_state_dict(state.loop_progress)
        if state.stop_decision is None:
            state.stop_decision = StopDecision(
                disposition="continue",
                reason_code="LOOP_INITIALIZED",
            ).to_state_dict()
        else:
            StopDecision.from_state_dict(state.stop_decision)

    def refresh_progress(self, state: AgentState) -> LoopProgressSnapshot:
        self.ensure_initialized(state)
        progress = self._build_progress(state.observations)
        state.loop_progress = progress.to_state_dict()
        state.stop_decision = StopDecision(
            disposition="continue",
            reason_code=(
                "OBSERVATION_ADVANCED"
                if progress.last_observation_advanced
                else "OBSERVATION_NO_PROGRESS"
            ),
        ).to_state_dict()
        return progress

    def evaluate_finish(
        self,
        state: AgentState,
        *,
        ignored_requirements: tuple[str, ...] = (),
    ) -> StopDecision:
        self.ensure_initialized(state)
        contract = CompletionContract.from_state_dict(state.completion_contract)
        if contract.clarification_required:
            decision = StopDecision(
                disposition="clarify",
                reason_code="CLARIFICATION_REQUIRED",
            )
            state.stop_decision = decision.to_state_dict()
            return decision

        ignored = frozenset(ignored_requirements)
        effect_short_circuited = self._effect_precondition_short_circuited(
            contract,
            state.observations,
        )
        if effect_short_circuited:
            ignored = ignored | {"workspace.create_file"}
        required_tool_names = tuple(
            name for name in contract.required_tool_names if name not in ignored
        )
        missing = list(
            find_missing_successful_tool_evidence(
                required_tool_names,
                state.observations,
            )
        )
        if (
            contract.requires_rag_evidence
            and not self._has_successful_tool(state, "rag.search")
            and not self._has_successful_goal_effect(state)
        ):
            missing.append("rag.search")
        missing.extend(
            find_missing_workspace_evidence(
                state.intent,
                state.observations,
                user_goal=state.user_goal,
                workspace_effect_satisfied=effect_short_circuited,
            )
        )
        missing = list(dict.fromkeys(missing))
        if missing:
            decision = StopDecision(
                disposition="continue",
                reason_code="COMPLETION_REQUIREMENTS_MISSING",
                missing_requirements=tuple(missing),
            )
        else:
            decision = StopDecision(
                disposition="complete",
                reason_code=(
                    "WORKSPACE_EFFECT_PRECONDITION_SHORT_CIRCUITED"
                    if effect_short_circuited
                    else "COMPLETION_CONTRACT_SATISFIED"
                ),
            )
        state.stop_decision = decision.to_state_dict()
        return decision

    def workspace_effect_short_circuit_target(self, state: AgentState) -> str:
        """Return the exact target when trusted evidence makes the effect unnecessary."""
        self.ensure_initialized(state)
        contract = CompletionContract.from_state_dict(state.completion_contract)
        return (
            contract.workspace_effect_target
            if self._effect_precondition_short_circuited(contract, state.observations)
            else ""
        )

    def record_failure(self, state: AgentState, reason_code: str) -> StopDecision:
        decision = StopDecision(disposition="fail", reason_code=reason_code)
        state.stop_decision = decision.to_state_dict()
        return decision

    def evaluate_proposed_action(
        self,
        state: AgentState,
        *,
        tool_name: str,
        arguments: dict[str, Any],
        guard_repeated_success: bool = True,
    ) -> StopDecision | None:
        """Reject an immediately repeated successful action before another effect.

        Re-reading the same target later can be valid after another observation or
        mutation. Only a consecutive, semantically identical action is treated as
        no progress. The first rejection asks for a strategy change; repeating the
        rejected proposal fails closed without consuming another tool call.
        """
        self.ensure_initialized(state)
        if not state.observations:
            return None
        latest = state.observations[-1]
        if not isinstance(latest, dict):
            return None
        if latest.get("ok") is False and self._is_disallowed_evidence_substitution(
            failed_tool_name=str(latest.get("tool_name", "")),
            proposed_tool_name=tool_name,
        ):
            prior = StopDecision.from_state_dict(state.stop_decision)
            decision = StopDecision(
                disposition=(
                    "fail"
                    if prior.reason_code == "SEMANTIC_SOURCE_SUBSTITUTION"
                    else "continue"
                ),
                reason_code="SEMANTIC_SOURCE_SUBSTITUTION",
            )
            state.stop_decision = decision.to_state_dict()
            return decision
        if self._would_exceed_exclusive_workspace_effect_scope(
            state,
            proposed_tool_name=tool_name,
        ):
            prior = StopDecision.from_state_dict(state.stop_decision)
            decision = StopDecision(
                disposition=(
                    "fail"
                    if prior.reason_code == "WORKSPACE_EFFECT_SCOPE_SATISFIED"
                    else "continue"
                ),
                reason_code="WORKSPACE_EFFECT_SCOPE_SATISFIED",
            )
            state.stop_decision = decision.to_state_dict()
            return decision
        if latest.get("ok") is not True:
            return None
        if not guard_repeated_success:
            return None
        latest_action = latest.get("model_action")
        latest_arguments = latest_action.get("arguments") if isinstance(latest_action, dict) else {}
        if not isinstance(latest_arguments, dict):
            latest_arguments = {}
        previous = self._action_fingerprint(str(latest.get("tool_name", "")), latest_arguments)
        proposed = self._action_fingerprint(tool_name, arguments)
        if previous != proposed:
            return None
        prior = StopDecision.from_state_dict(state.stop_decision)
        decision = StopDecision(
            disposition=("fail" if prior.reason_code == "STRATEGY_CHANGE_REQUIRED" else "continue"),
            reason_code=(
                "LOOP_NO_PROGRESS"
                if prior.reason_code == "STRATEGY_CHANGE_REQUIRED"
                else "STRATEGY_CHANGE_REQUIRED"
            ),
        )
        state.stop_decision = decision.to_state_dict()
        return decision

    @staticmethod
    def _would_exceed_exclusive_workspace_effect_scope(
        state: AgentState,
        *,
        proposed_tool_name: str,
    ) -> bool:
        """Block a second workspace mutation when the user's scope is exhausted."""
        if (
            proposed_tool_name not in WORKSPACE_EFFECT_TOOLS
            or not has_exclusive_single_workspace_effect_scope(state.user_goal)
        ):
            return False
        successful_effects = [
            observation
            for observation in state.observations
            if isinstance(observation, dict)
            and observation.get("ok") is True
            and observation.get("tool_name") in WORKSPACE_EFFECT_TOOLS
        ]
        return len(successful_effects) == 1

    def _is_disallowed_evidence_substitution(
        self,
        *,
        failed_tool_name: str,
        proposed_tool_name: str,
    ) -> bool:
        """Compare manifest-owned evidence domains without knowing tool names."""
        failed = self._evidence_semantics(failed_tool_name)
        proposed = self._evidence_semantics(proposed_tool_name)
        if failed is None or proposed is None:
            return False
        failed_operation, failed_domain, allowed_substitutes = failed
        proposed_operation, proposed_domain, _ = proposed
        return bool(
            failed_operation == "retrieve_evidence"
            and proposed_operation == failed_operation
            and proposed_domain != failed_domain
            and proposed_domain not in allowed_substitutes
        )

    def _evidence_semantics(
        self, tool_name: str
    ) -> tuple[str, str, frozenset[str]] | None:
        registry = getattr(self._tool_gateway, "registry", None)
        get_manifest = getattr(registry, "get_manifest", None)
        manifest = get_manifest(tool_name) if callable(get_manifest) else None
        metadata = getattr(manifest, "metadata", None)
        loop = metadata.get("loop") if isinstance(metadata, dict) else None
        if not isinstance(loop, dict):
            return None
        operation = loop.get("operation")
        evidence_domain = loop.get("evidence_domain")
        substitutes = loop.get("substitutable_evidence_domains", [])
        if (
            not isinstance(operation, str)
            or not operation
            or not isinstance(evidence_domain, str)
            or not evidence_domain
            or not isinstance(substitutes, list)
            or any(not isinstance(item, str) or not item for item in substitutes)
        ):
            return None
        return operation, evidence_domain, frozenset(substitutes)

    def _build_completion_contract(self, state: AgentState) -> CompletionContract:
        registry = getattr(self._tool_gateway, "registry", None)
        list_manifests = getattr(registry, "list_manifests", None)
        manifests = tuple(list_manifests()) if callable(list_manifests) else ()
        enabled_names = frozenset(
            manifest.name for manifest in manifests if getattr(manifest, "enabled", False)
        )
        required = tuple(
            dict.fromkeys(
                (
                    *find_required_goal_tools(state.user_goal, manifests),
                    *required_effect_tools(state.intent, enabled_names),
                )
            )
        )
        evidence, action, _ambiguity = workspace_semantics(state.intent)
        conditional_target = (
            conditional_no_overwrite_target(state.user_goal)
            if "workspace.create_file" in required
            else ""
        )
        return CompletionContract(
            required_tool_names=required,
            requires_rag_evidence=requires_rag_search(state.intent),
            workspace_evidence=evidence,
            workspace_action=action,
            workspace_effect_precondition=(
                "target_absent" if conditional_target else "none"
            ),
            workspace_effect_target=conditional_target,
            clarification_required=(
                workspace_requires_clarification(state.intent)
                or intent_requires_clarification(state.intent)
            ),
        )

    @staticmethod
    def _effect_precondition_short_circuited(
        contract: CompletionContract,
        observations: list[dict[str, Any]],
    ) -> bool:
        return bool(
            contract.workspace_effect_precondition == "target_absent"
            and contract.workspace_effect_target
            and has_confirmed_workspace_target(
                observations,
                contract.workspace_effect_target,
            )
        )

    @classmethod
    def _build_progress(cls, observations: list[dict[str, Any]]) -> LoopProgressSnapshot:
        successful_fingerprints: list[str] = []
        failed_fingerprints: list[str] = []
        successful_tools: list[str] = []
        failed_tools: list[str] = []
        no_progress_streak = 0
        last_advanced = False
        for observation in observations[-40:]:
            fingerprint = cls._observation_fingerprint(observation)
            tool_name = observation.get("tool_name")
            ok = observation.get("ok") is True
            advanced = bool(ok and fingerprint not in successful_fingerprints)
            if ok:
                if fingerprint not in successful_fingerprints:
                    successful_fingerprints.append(fingerprint)
                if isinstance(tool_name, str) and tool_name not in successful_tools:
                    successful_tools.append(tool_name)
            else:
                if fingerprint not in failed_fingerprints:
                    failed_fingerprints.append(fingerprint)
                if isinstance(tool_name, str) and tool_name not in failed_tools:
                    failed_tools.append(tool_name)
            no_progress_streak = 0 if advanced else no_progress_streak + 1
            last_advanced = advanced
        return LoopProgressSnapshot(
            tool_calls_used=len(observations[-40:]),
            successful_tool_names=tuple(successful_tools),
            failed_tool_names=tuple(failed_tools),
            successful_action_fingerprints=tuple(successful_fingerprints),
            failed_action_fingerprints=tuple(failed_fingerprints),
            no_progress_streak=no_progress_streak,
            last_observation_advanced=last_advanced,
        )

    @staticmethod
    def _observation_fingerprint(observation: dict[str, Any]) -> str:
        model_action = observation.get("model_action")
        arguments = model_action.get("arguments") if isinstance(model_action, dict) else {}
        payload = {
            "action": LoopController._action_fingerprint(
                str(observation.get("tool_name", "")),
                arguments if isinstance(arguments, dict) else {},
            ),
            "ok": observation.get("ok") is True,
        }
        encoded = json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()

    @staticmethod
    def _action_fingerprint(tool_name: str, arguments: dict[str, Any]) -> str:
        encoded = json.dumps(
            {"tool_name": tool_name, "arguments": arguments},
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()

    @staticmethod
    def _has_successful_tool(state: AgentState, tool_name: str) -> bool:
        return any(
            isinstance(item, dict) and item.get("tool_name") == tool_name and item.get("ok") is True
            for item in state.observations
        )

    @staticmethod
    def _has_successful_goal_effect(state: AgentState) -> bool:
        return any(
            isinstance(item, dict)
            and item.get("ok") is True
            and item.get("tool_name")
            in {
                "knowledge.create_document",
                "literature.download_arxiv_pdf",
                "rag.ingest_artifact",
            }
            for item in state.observations
        )
