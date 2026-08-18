"""LangGraph validate_action 节点的动作校验与可信 ToolRequest 构造。"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import time as _time_module
from datetime import datetime, timezone
from typing import Any
from uuid import NAMESPACE_URL, uuid5

from jarvis_worker.agent.context.response_language import (
    ResponseLanguage,
    resolve_response_language_policy,
)
from jarvis_worker.agent.core.actions import AgentAction
from jarvis_worker.agent.core.conversation_constraints import (
    is_prior_answer_transform_goal,
)
from jarvis_worker.agent.core.effect_guard import (
    build_effect_guard_feedback,
    build_workspace_effect_mismatch_feedback,
    find_latest_failed_required_tool,
    find_latest_failed_workspace_evidence,
    find_required_goal_tools,
    find_required_workspace_effect_mismatch,
    intent_requires_clarification,
    rag_document_scope,
    resolved_rag_document_ids,
    workspace_requires_clarification,
)
from jarvis_worker.agent.core.evidence_navigation import (
    evaluate_workspace_source_action_guard,
)
from jarvis_worker.agent.core.final_answer import FinalAnswerValidation, FinalAnswerValidator
from jarvis_worker.agent.core.graph_state import AgentGraphState, AgentGraphUpdate
from jarvis_worker.agent.core.phases.runtime import PhaseRuntime
from jarvis_worker.agent.core.source_chain_validator import SOURCE_CHAIN_VALIDATOR_ID
from jarvis_worker.agent.core.state import AgentState
from jarvis_worker.agent.core.tool_arguments import normalize_tool_arguments
from jarvis_worker.agent.loop import LoopController
from jarvis_worker.agent.models.provider import ModelProvider
from jarvis_worker.agent.research import (
    merge_trusted_knowledge_provenance,
    trusted_knowledge_provenance,
)
from jarvis_worker.agent.tool_gateway.contracts import ToolRequest
from jarvis_worker.agent.tool_gateway.gateway import ToolGateway
from jarvis_worker.runtime.events import (
    build_runtime_event,
    deterministic_event_id,
    deterministic_step_id,
)
from jarvis_worker.runtime_bus.messages import RuntimeEventEnvelope
from jarvis_worker.shared.security import (
    contains_credential,
    redact_credentials,
    requests_credential_persistence,
)

log = logging.getLogger("jarvis_worker.agent_runner")

_SENSITIVE_ARGUMENT_KEYS = {
    "api_key",
    "apikey",
    "authorization",
    "cookie",
    "password",
    "secret",
    "client_secret",
    "private_key",
    "token",
    "access_token",
    "refresh_token",
    "session_token",
}
_CONTENT_REDACTED_KEYS = {"content"}
_SENSITIVE_PERSISTENCE_REFUSAL = (
    "我不能把密码、API 密钥或访问令牌保存到记忆、知识库或文件中，也不会在回复中复述该值。"
    "请将凭据存入系统钥匙串或环境变量，并只提供变量名或凭据引用。"
)
_SENSITIVE_PERSISTENCE_REFUSAL_EN = (
    "I can't save passwords, API keys, or access tokens to memory, the knowledge base, or files, "
    "and I won't repeat the value. Store credentials in the system keychain or an environment "
    "variable, and provide only the variable name or credential reference."
)
_WORKSPACE_CLARIFICATION = (
    "这个请求会修改或删除工作区内容，但当前目标路径、候选范围或保留方式还不够明确。"
    "请提供要处理的具体相对路径，并说明期望动作；在范围明确前我不会执行写入或删除。"
)
_WORKSPACE_CLARIFICATION_EN = (
    "This request would modify or delete workspace content, but the target path, candidate scope, "
    "or retention behavior is not yet clear. Provide the exact relative path and intended action; "
    "I won't write or delete anything until the scope is explicit."
)
_GENERAL_CLARIFICATION = (
    "我还不能确定你希望处理的具体对象和目标。请补充要整理或处理的内容，"
    "例如直接粘贴文本，或给出当前工作区中的具体相对路径，并说明期望的结果；"
    "在目标明确前我不会调用工具或修改任何内容。"
)
_GENERAL_CLARIFICATION_EN = (
    "I can't yet determine the exact object and outcome you want. Provide the text to process or "
    "an exact relative workspace path and the intended result; I won't call tools or modify "
    "anything until the goal is clear."
)
_DOCUMENT_CLARIFICATION = (
    "我无法从当前会话唯一确定你指的是哪份文档。请提供准确标题，或先在 RAG 文档库中明确选择；"
    "在文档身份确认前我不会检索、调用 Workspace 工具或猜测。"
)
_DOCUMENT_CLARIFICATION_EN = (
    "I cannot uniquely identify the document from this conversation. Provide its exact title "
    "or select it in the RAG document library; I will not search, call Workspace tools, or guess "
    "until the document identity is clear."
)
_MAX_CONSECUTIVE_SOURCE_CHAIN_GUARD_REJECTIONS = 2
_MAX_SOURCE_CHAIN_EVIDENCE_RETRIES = 2
_MAX_FINAL_ANSWER_REWRITES = 1
_VALIDATION_REASON_CODE_RE = re.compile(r"^[A-Z][A-Z0-9_]{2,79}$")


def _workspace_existing_target_message(target: str) -> str:
    return f"目标已存在，未执行创建或覆盖：`{target}`。原路径内容保持不变。"


def _workspace_existing_target_message_en(target: str) -> str:
    return (
        f"The target already exists, so no create or overwrite was performed: `{target}`. "
        "Its existing content remains unchanged."
    )


def _host_message_for_response_language(
    state: AgentState,
    *,
    zh: str,
    en: str,
) -> str:
    policy = resolve_response_language_policy(state.memory_items, state.user_goal)
    if policy is not None and policy.effective_language is ResponseLanguage.EN:
        return en
    return zh


def _safe_answer_validation_details(
    validator_id: str,
    validation: FinalAnswerValidation,
    *,
    rejection_count: int,
    rewrite_available: bool,
    recovery_mode: str,
    max_rewrites: int = _MAX_FINAL_ANSWER_REWRITES,
) -> dict[str, Any]:
    """只持久化固定标识、计数和布尔值，不回显答案、反馈、路径或正文。"""
    reason_code = validation.reason_code
    if not isinstance(reason_code, str) or not _VALIDATION_REASON_CODE_RE.fullmatch(reason_code):
        reason_code = "FINAL_ANSWER_REJECTED"
    details: dict[str, Any] = {
        "validator_id": str(validator_id)[:100],
        "reason_code": reason_code,
        "rejection_count": rejection_count,
        "max_rewrites": max_rewrites,
        "rewrite_available": rewrite_available,
        "recovery_mode": recovery_mode,
    }
    diagnostics = validation.diagnostics
    if not isinstance(diagnostics, dict):
        return details
    coverage = diagnostics.get("coverage")
    if isinstance(coverage, dict):
        safe_coverage: dict[str, Any] = {}
        for key in (
            "required_endpoint_count",
            "covered_endpoint_count",
            "required_stage_count",
            "covered_stage_count",
            "required_evidence_slot_count",
            "covered_evidence_slot_count",
            "unique_source_paths",
        ):
            value = coverage.get(key)
            if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
                safe_coverage[key] = min(value, 10_000)
        if isinstance(coverage.get("complete"), bool):
            safe_coverage["complete"] = coverage["complete"]
        schema = coverage.get("schema")
        if (
            isinstance(schema, str)
            and 1 <= len(schema) <= 100
            and re.fullmatch(r"[a-z0-9][a-z0-9._-]*", schema)
        ):
            safe_coverage["schema"] = schema
        if safe_coverage:
            details["coverage"] = safe_coverage
    if isinstance(diagnostics.get("answer_denied_global_coverage"), bool):
        details["answer_denied_global_coverage"] = diagnostics["answer_denied_global_coverage"]
    uncertainty_count = diagnostics.get("uncertainty_clause_count")
    if (
        isinstance(uncertainty_count, int)
        and not isinstance(uncertainty_count, bool)
        and uncertainty_count >= 0
    ):
        details["uncertainty_clause_count"] = min(uncertainty_count, 100)
    return details


def _is_sensitive_persistence_request(state: AgentState) -> bool:
    if not contains_credential(state.user_goal):
        return False
    if requests_credential_persistence(state.user_goal):
        return True
    if not isinstance(state.intent, dict):
        return False
    effects = state.intent.get("effects")
    return isinstance(effects, dict) and effects.get("knowledge_write") == "required"


def _knowledge_effect(state: AgentState, field: str, default: str = "") -> str:
    if not isinstance(state.intent, dict):
        return default
    effects = state.intent.get("effects")
    if not isinstance(effects, dict):
        return default
    value = effects.get(field)
    return value if isinstance(value, str) else default


def _latest_repeated_failed_action(
    state: AgentState, tool_name: str, arguments: dict[str, Any]
) -> dict[str, Any] | None:
    """环境没有成功变化时，阻止相同失败动作再次进入权限与 effect 链。"""
    expected = json.dumps(arguments, sort_keys=True, ensure_ascii=False, default=str)
    for observation in reversed(state.observations):
        if not isinstance(observation, dict):
            continue
        if observation.get("ok") is True:
            return None
        model_action = observation.get("model_action")
        if (
            observation.get("ok") is False
            and observation.get("tool_name") == tool_name
            and isinstance(observation.get("error"), dict)
            and isinstance(model_action, dict)
            and isinstance(model_action.get("arguments"), dict)
            and json.dumps(
                model_action["arguments"], sort_keys=True, ensure_ascii=False, default=str
            )
            == expected
        ):
            return observation
    return None


def _scheduled_arxiv_source_urls(observations: list[dict[str, Any]]) -> list[str]:
    urls: list[str] = []
    for observation in observations:
        if (
            observation.get("tool_name") != "literature.search_arxiv"
            or observation.get("ok") is not True
        ):
            continue
        data = observation.get("data")
        if not isinstance(data, dict) or data.get("source") != "arxiv":
            continue
        results = data.get("results", [])
        if not isinstance(results, list):
            continue
        for item in results[:10]:
            if not isinstance(item, dict):
                continue
            value = item.get("abstract_url")
            if (
                isinstance(value, str)
                and value.startswith("https://arxiv.org/abs/")
                and value not in urls
            ):
                urls.append(value)
    return urls


def _build_arguments_summary(arguments: dict[str, Any]) -> dict[str, Any]:
    def sanitize(value: Any, key: str = "", depth: int = 0) -> Any:
        normalized_key = key.lower().replace("-", "_")
        if normalized_key in _SENSITIVE_ARGUMENT_KEYS:
            return "***"
        if depth >= 3:
            return "[truncated]"
        if isinstance(value, str):
            if normalized_key in _CONTENT_REDACTED_KEYS:
                content_bytes = value.encode("utf-8", errors="replace")
                return {
                    "redacted": True,
                    "size_bytes": len(content_bytes),
                    "sha256": hashlib.sha256(content_bytes).hexdigest(),
                }
            return value[:500] + ("…" if len(value) > 500 else "")
        if isinstance(value, (int, float, bool)) or value is None:
            return value
        if isinstance(value, dict):
            return {
                str(k)[:100]: sanitize(v, str(k), depth + 1) for k, v in list(value.items())[:30]
            }
        if isinstance(value, (list, tuple)):
            return [sanitize(item, depth=depth + 1) for item in list(value)[:30]]
        return f"<{type(value).__name__}>"

    return {
        str(key)[:100]: sanitize(value, str(key)) for key, value in list(arguments.items())[:50]
    }


class ActionValidationPhase:
    """校验未受信任动作并构造下一节点使用的可信请求；不执行 effect。"""

    def __init__(
        self,
        *,
        model: ModelProvider,
        tool_gateway: ToolGateway,
        runtime: PhaseRuntime,
        max_iterations: int,
        final_answer_validators: tuple[FinalAnswerValidator, ...] = (),
        loop_controller: LoopController,
    ) -> None:
        self._model = model
        self._tool_gateway = tool_gateway
        self._runtime = runtime
        self._max_iterations = max_iterations
        self._final_answer_validators = final_answer_validators
        self._loop_controller = loop_controller
        self._source_chain_enabled = any(
            validator.validator_id == SOURCE_CHAIN_VALIDATOR_ID
            for validator in final_answer_validators
        )

    def _make_event(self, trace_id: str, event: dict) -> RuntimeEventEnvelope:
        return self._runtime.make_event(trace_id, event)

    def _make_model_failed(
        self,
        trace_id: str,
        task_id: str,
        run_id: str,
        step_seq: int,
        model_call_id: str,
        model_step_id: str,
        started_at: float,
        error_code: str,
        recoverable: bool = False,
        validation: dict[str, Any] | None = None,
        navigation_guard: dict[str, Any] | None = None,
    ) -> RuntimeEventEnvelope:
        return self._runtime.make_model_failed(
            trace_id,
            task_id,
            run_id,
            step_seq,
            model_call_id,
            model_step_id,
            started_at,
            error_code,
            provider_name=getattr(self._model, "provider_name", "unknown"),
            model_name=getattr(self._model, "model_name", "unknown"),
            recoverable=recoverable,
            validation=validation,
            navigation_guard=navigation_guard,
        )

    def _make_model_completed(
        self,
        trace_id: str,
        task_id: str,
        run_id: str,
        step_seq: int,
        model_call_id: str,
        model_step_id: str,
        started_at: float,
        action_type: str,
    ) -> RuntimeEventEnvelope:
        return self._runtime.make_model_completed(
            trace_id,
            task_id,
            run_id,
            step_seq,
            model_call_id,
            model_step_id,
            started_at,
            action_type,
            provider_name=getattr(self._model, "provider_name", "unknown"),
            model_name=getattr(self._model, "model_name", "unknown"),
        )

    def _make_failed_event(self, *args: Any, **kwargs: Any) -> RuntimeEventEnvelope:
        return self._runtime.make_failed_event(*args, **kwargs)

    def _make_cancelled(self, *args: str) -> RuntimeEventEnvelope:
        return self._runtime.make_cancelled(*args)

    def _make_paused(self, *args: Any, **kwargs: Any) -> RuntimeEventEnvelope:
        return self._runtime.make_paused(*args, **kwargs)

    def _graph_update(
        self,
        graph_state: AgentGraphState,
        produced: list[RuntimeEventEnvelope],
        next_step_seq: int,
        turn: dict[str, Any],
    ) -> AgentGraphUpdate:
        return self._runtime.graph_update(graph_state, produced, next_step_seq, turn)

    def _attach_checkpoint(self, *args: Any, **kwargs: Any) -> None:
        self._runtime.attach_checkpoint(*args, **kwargs)

    def run(self, graph_state: AgentGraphState) -> AgentGraphUpdate:
        """图节点：验证模型输出、finish effect guard，并构造可信 ToolRequest。"""
        job = graph_state["job"]
        state = graph_state["state"]
        turn = graph_state["turn"]
        step_seq = graph_state["step_seq"]
        trace_id, task_id, run_id = job.trace_id, job.task_id, job.run_id
        action = turn.get("action")
        model_call_id = str(turn.get("model_call_id", ""))
        model_step_id = str(turn.get("model_step_id", ""))
        started_at = float(turn.get("model_started_at", _time_module.monotonic()))
        produced: list[RuntimeEventEnvelope] = []

        def invalid(message: str) -> AgentGraphUpdate:
            nonlocal step_seq
            produced.append(
                self._make_model_failed(
                    trace_id,
                    task_id,
                    run_id,
                    step_seq,
                    model_call_id,
                    model_step_id,
                    started_at,
                    "INVALID_AGENT_ACTION",
                )
            )
            step_seq += 1
            produced.append(
                self._make_failed_event(
                    trace_id,
                    task_id,
                    run_id,
                    step_seq,
                    code="INVALID_AGENT_ACTION",
                    message=message,
                )
            )
            return self._graph_update(graph_state, produced, step_seq, {})

        # 模型调用可能持续数秒。暂停或取消若在调用期间到达，必须在消费
        # 模型动作（尤其是执行工具 effect）之前再次建立安全边界。
        if graph_state["cancel_check"] and graph_state["cancel_check"]():
            produced.append(self._make_cancelled(trace_id, task_id, run_id))
            return self._graph_update(graph_state, produced, step_seq, {})
        if graph_state["pause_check"] and (pause_id := graph_state["pause_check"]()):
            produced.append(
                self._make_paused(
                    graph_state, step_seq, "validate_action", turn=turn, event_id=pause_id
                )
            )
            return self._graph_update(graph_state, produced, step_seq, {})
        halt = graph_state["run_supervisor"].before_phase(state)
        if halt is not None:
            produced.append(
                self._make_failed_event(
                    trace_id,
                    task_id,
                    run_id,
                    step_seq,
                    code=halt.code,
                    message=halt.message,
                    category="runtime",
                    recoverable=False,
                )
            )
            return self._graph_update(graph_state, produced, step_seq, {})

        if not isinstance(action, AgentAction):
            log.warning(
                "AgentRunner ModelProvider 返回非法 action 类型: type=%s", type(action).__name__
            )
            return invalid(f"ModelProvider 返回了非法 action 类型: {type(action).__name__}")

        sensitive_persistence = _is_sensitive_persistence_request(state)
        if sensitive_persistence:
            # 模型不得通过换工具名或矛盾的 final_message 绕过凭据持久化策略。
            # 安全策略把动作收敛为确定性拒绝，不执行 ToolGateway effect。
            action = AgentAction.finish(
                _host_message_for_response_language(
                    state,
                    zh=_SENSITIVE_PERSISTENCE_REFUSAL,
                    en=_SENSITIVE_PERSISTENCE_REFUSAL_EN,
                )
            )

        elif rag_document_scope(state.intent) == "unresolved":
            action = AgentAction.finish(
                _host_message_for_response_language(
                    state,
                    zh=_DOCUMENT_CLARIFICATION,
                    en=_DOCUMENT_CLARIFICATION_EN,
                )
            )
        elif intent_requires_clarification(state.intent):
            # Intent schema 穷尽失败后的 unknown 契约不授予任何能力。无论动作模型
            # 提议 finish 还是 call_tool，都在 ToolGateway 之前收敛到确定性澄清。
            action = AgentAction.finish(
                _host_message_for_response_language(
                    state,
                    zh=_GENERAL_CLARIFICATION,
                    en=_GENERAL_CLARIFICATION_EN,
                )
            )
        elif workspace_requires_clarification(state.intent):
            if action.action_type == "finish":
                action = AgentAction.finish(
                    _host_message_for_response_language(
                        state,
                        zh=_WORKSPACE_CLARIFICATION,
                        en=_WORKSPACE_CLARIFICATION_EN,
                    )
                )
            elif action.action_type == "call_tool":
                registry = getattr(self._tool_gateway, "registry", None)
                get_manifest = getattr(registry, "get_manifest", None)
                manifest = get_manifest(action.tool_name) if callable(get_manifest) else None
                if manifest is None or manifest.risk_level_default != "L0":
                    # 模糊副作用请求只允许只读探查；任何写入/删除计划都收敛为确定性澄清。
                    action = AgentAction.finish(
                        _host_message_for_response_language(
                            state,
                            zh=_WORKSPACE_CLARIFICATION,
                            en=_WORKSPACE_CLARIFICATION_EN,
                        )
                    )

        short_circuit_target = self._loop_controller.workspace_effect_short_circuit_target(state)
        if short_circuit_target and (
            action.action_type == "finish"
            or (action.action_type == "call_tool" and action.tool_name == "workspace.create_file")
        ):
            # “目标不存在”是该条件式副作用的显式前置条件。可信只读结果已经证明
            # 目标存在时，Host 在 ToolGateway/Permission 之前收口，既不要求无意义
            # 的写权限，也不接受模型把短路结果描述成创建成功。
            action = AgentAction.finish(
                _host_message_for_response_language(
                    state,
                    zh=_workspace_existing_target_message(short_circuit_target),
                    en=_workspace_existing_target_message_en(short_circuit_target),
                )
            )

        if action.action_type == "finish":
            if not isinstance(action.final_message, str) or not action.final_message.strip():
                return invalid("finish action 的 final_message 为空或不是有效字符串")
            stop_decision = self._loop_controller.evaluate_finish(
                state,
                ignored_requirements=(
                    ("knowledge.create_document",) if sensitive_persistence else ()
                ),
            )
            missing = stop_decision.missing_requirements
            if missing:
                failed_evidence = find_latest_failed_required_tool(missing, state.observations)
                if failed_evidence is None:
                    failed_evidence = find_latest_failed_workspace_evidence(
                        state.intent, state.observations
                    )
                if failed_evidence is not None:
                    error = failed_evidence["error"]
                    produced.append(
                        self._make_model_failed(
                            trace_id,
                            task_id,
                            run_id,
                            step_seq,
                            model_call_id,
                            model_step_id,
                            started_at,
                            "REQUIRED_TOOL_EFFECT_FAILED",
                            recoverable=False,
                        )
                    )
                    step_seq += 1
                    produced.append(
                        self._make_failed_event(
                            trace_id,
                            task_id,
                            run_id,
                            step_seq,
                            code=str(error.get("code", "TOOL_FAILED")),
                            message=str(
                                error.get(
                                    "message",
                                    failed_evidence.get("summary", "工具执行失败"),
                                )
                            ),
                            category=str(error.get("category", "tool")),
                            recoverable=False,
                        )
                    )
                    return self._graph_update(graph_state, produced, step_seq, {})
                next_guard_count = state.effect_guard_rejections + 1
                can_retry = state.iteration + next_guard_count < self._max_iterations
                produced.append(
                    self._make_model_failed(
                        trace_id,
                        task_id,
                        run_id,
                        step_seq,
                        model_call_id,
                        model_step_id,
                        started_at,
                        "REQUIRED_TOOL_EVIDENCE_MISSING",
                        recoverable=can_retry,
                    )
                )
                step_seq += 1
                if can_retry:
                    state.effect_guard_rejections = next_guard_count
                    state.effect_guard_feedback = build_effect_guard_feedback(missing)
                    return self._graph_update(
                        graph_state, produced, step_seq, {"retry_model": True}
                    )
                produced.append(
                    self._make_failed_event(
                        trace_id,
                        task_id,
                        run_id,
                        step_seq,
                        code="REQUIRED_TOOL_NOT_EXECUTED",
                        message="模型未执行当前任务所要求的工具，任务不能标记为完成: "
                        + ", ".join(missing),
                    )
                )
                return self._graph_update(graph_state, produced, step_seq, {})

            # Host-owned deterministic finishes were already localized above. Preserve that
            # single normalized action instead of reconstructing a language-specific branch here.
            final_output = action.final_message
            answer_metadata: dict[str, Any] = {}
            validation_action = AgentAction.finish(
                final_output,
                citations=action.citations,
                insufficient_evidence=action.insufficient_evidence,
            )
            for validator in self._final_answer_validators:
                validation = validator.validate(action=validation_action, state=state)
                if not validation.accepted:
                    if validation.reason_code == "SOURCE_CHAIN_EVIDENCE_INCOMPLETE":
                        next_guard_count = state.source_chain_evidence_rejections + 1
                        can_retry = (
                            state.iteration < self._max_iterations
                            and next_guard_count <= _MAX_SOURCE_CHAIN_EVIDENCE_RETRIES
                        )
                        validation_details = _safe_answer_validation_details(
                            validator.validator_id,
                            validation,
                            rejection_count=next_guard_count,
                            rewrite_available=False,
                            recovery_mode="tool_planning" if can_retry else "none",
                            max_rewrites=0,
                        )
                        failed_event = self._make_model_failed(
                            trace_id,
                            task_id,
                            run_id,
                            step_seq,
                            model_call_id,
                            model_step_id,
                            started_at,
                            "SOURCE_CHAIN_EVIDENCE_INCOMPLETE",
                            recoverable=can_retry,
                            validation=validation_details,
                        )
                        produced.append(failed_event)
                        step_seq += 1
                        if can_retry:
                            state.source_chain_evidence_rejections = next_guard_count
                            state.effect_guard_feedback = validation.feedback[:4_000]
                            self._attach_checkpoint(
                                failed_event,
                                graph_state,
                                step_seq,
                                "call_model",
                                state=state,
                            )
                            return self._graph_update(
                                graph_state, produced, step_seq, {"retry_model": True}
                            )
                        produced.append(
                            self._make_failed_event(
                                trace_id,
                                task_id,
                                run_id,
                                step_seq,
                                code="SOURCE_CHAIN_EVIDENCE_INCOMPLETE",
                                message="源码调用链证据尚未覆盖全部必需端点与阶段",
                                details={"answer_validation": validation_details},
                            )
                        )
                        return self._graph_update(graph_state, produced, step_seq, {})

                    next_guard_count = state.answer_guard_rejections + 1
                    can_retry = next_guard_count <= _MAX_FINAL_ANSWER_REWRITES
                    validation_details = _safe_answer_validation_details(
                        validator.validator_id,
                        validation,
                        rejection_count=next_guard_count,
                        rewrite_available=can_retry,
                        recovery_mode="answer_rewrite" if can_retry else "none",
                    )
                    failed_event = self._make_model_failed(
                        trace_id,
                        task_id,
                        run_id,
                        step_seq,
                        model_call_id,
                        model_step_id,
                        started_at,
                        "FINAL_ANSWER_VALIDATION_FAILED",
                        recoverable=can_retry,
                        validation=validation_details,
                    )
                    produced.append(failed_event)
                    step_seq += 1
                    if can_retry:
                        state.answer_guard_rejections = next_guard_count
                        state.answer_guard_feedback = validation.feedback[:4_000]
                        self._attach_checkpoint(
                            failed_event,
                            graph_state,
                            step_seq,
                            "call_model",
                            state=state,
                        )
                        return self._graph_update(
                            graph_state, produced, step_seq, {"retry_model": True}
                        )
                    produced.append(
                        self._make_failed_event(
                            trace_id,
                            task_id,
                            run_id,
                            step_seq,
                            code="FINAL_ANSWER_VALIDATION_FAILED",
                            message="模型最终回答未通过可信证据校验",
                            details={"answer_validation": validation_details},
                        )
                    )
                    return self._graph_update(graph_state, produced, step_seq, {})
                final_output = validation.output
                validation_action = AgentAction.finish(
                    final_output,
                    citations=action.citations,
                    insufficient_evidence=action.insufficient_evidence,
                )
                if validation.metadata:
                    answer_metadata[validator.validator_id] = validation.metadata

            if contains_credential(state.user_goal):
                final_output = redact_credentials(final_output)

            produced.append(
                self._make_model_completed(
                    trace_id,
                    task_id,
                    run_id,
                    step_seq,
                    model_call_id,
                    model_step_id,
                    started_at,
                    "finish",
                )
            )
            step_seq += 1
            state.final_output = final_output
            artifact_metadata = {"answer_validation": answer_metadata} if answer_metadata else {}
            artifact_id = deterministic_event_id(run_id, "artifact.final", 1)
            artifact_created_at = datetime.now(timezone.utc).isoformat()
            produced.append(
                self._make_event(
                    trace_id,
                    build_runtime_event(
                        event_type="artifact.created",
                        task_id=task_id,
                        run_id=run_id,
                        step_id=model_step_id,
                        event_id=deterministic_event_id(run_id, "artifact.created", step_seq),
                        payload={
                            "artifact": {
                                "id": artifact_id,
                                "task_id": task_id,
                                "run_id": run_id,
                                "kind": "markdown",
                                "title": "最终回复",
                                "purpose": "final_response",
                                "producer": {"type": "runtime"},
                                "content": final_output,
                                "metadata": artifact_metadata,
                                "created_at": artifact_created_at,
                            }
                        },
                    ),
                )
            )
            step_seq += 1
            completed_payload: dict[str, Any] = {
                "output": final_output,
                "total_steps": state.iteration,
            }
            if answer_metadata:
                completed_payload["answer_validation"] = answer_metadata
            produced.append(
                self._make_event(
                    trace_id,
                    build_runtime_event(
                        event_type="agent.run.completed",
                        task_id=task_id,
                        run_id=run_id,
                        event_id=deterministic_event_id(run_id, "agent.run.completed", step_seq),
                        payload=completed_payload,
                    ),
                )
            )
            log.info(
                "AgentRunner finish: task_id=%s run_id=%s iterations=%d",
                task_id,
                run_id,
                state.iteration,
            )
            return self._graph_update(graph_state, produced, step_seq, {})

        if action.action_type != "call_tool":
            return invalid(f"不支持的 action_type: {action.action_type}")
        if not isinstance(action.tool_name, str) or not action.tool_name.strip():
            return invalid("call_tool action 的 tool_name 为空或不是有效字符串")
        if not isinstance(action.arguments, dict):
            return invalid(
                f"call_tool action 的 arguments 不是 dict: {type(action.arguments).__name__}"
            )
        if state.iteration >= self._max_iterations:
            self._loop_controller.record_failure(state, "TOOL_BUDGET_EXHAUSTED")
            produced.append(
                self._make_model_failed(
                    trace_id,
                    task_id,
                    run_id,
                    step_seq,
                    model_call_id,
                    model_step_id,
                    started_at,
                    "PLANNING_TOOL_BUDGET_EXHAUSTED",
                    recoverable=False,
                )
            )
            step_seq += 1
            produced.append(
                self._make_failed_event(
                    trace_id,
                    task_id,
                    run_id,
                    step_seq,
                    code="MAX_ITERATIONS",
                    message=(
                        f"已达到工具调用上限 ({self._max_iterations})；模型未使用最后一次"
                        "收口机会交付已有证据和未完成范围"
                    ),
                    recoverable=False,
                )
            )
            return self._graph_update(graph_state, produced, step_seq, {})

        normalized_arguments = normalize_tool_arguments(action.tool_name, action.arguments)
        if normalized_arguments != action.arguments:
            action = AgentAction.call_tool(
                action.tool_name,
                normalized_arguments,
                action.reason,
            )

        if is_prior_answer_transform_goal(state.user_goal):
            next_guard_count = state.answer_guard_rejections + 1
            can_retry = next_guard_count <= _MAX_FINAL_ANSWER_REWRITES
            validation = FinalAnswerValidation(
                accepted=False,
                output="",
                feedback=(
                    "当前目标只要求转换上一轮已有回答，未授权刷新、扩展或替换证据。"
                    "不得调用工具或新增事实；请只使用会话历史重写 final_message，并保留原有证据边界。"
                ),
                reason_code="HISTORY_TRANSFORM_TOOL_FORBIDDEN",
            )
            validation_details = _safe_answer_validation_details(
                "history-transform-boundary-v1",
                validation,
                rejection_count=next_guard_count,
                rewrite_available=can_retry,
                recovery_mode="answer_rewrite" if can_retry else "none",
            )
            failed_event = self._make_model_failed(
                trace_id,
                task_id,
                run_id,
                step_seq,
                model_call_id,
                model_step_id,
                started_at,
                "HISTORY_TRANSFORM_TOOL_FORBIDDEN",
                recoverable=can_retry,
                validation=validation_details,
            )
            produced.append(failed_event)
            step_seq += 1
            if can_retry:
                state.answer_guard_rejections = next_guard_count
                state.answer_guard_feedback = validation.feedback
                self._attach_checkpoint(
                    failed_event,
                    graph_state,
                    step_seq,
                    "call_model",
                    state=state,
                )
                return self._graph_update(graph_state, produced, step_seq, {"retry_model": True})
            produced.append(
                self._make_failed_event(
                    trace_id,
                    task_id,
                    run_id,
                    step_seq,
                    code="HISTORY_TRANSFORM_TOOL_FORBIDDEN",
                    message="模型连续尝试在历史转换任务中调用工具，已在执行前停止",
                    recoverable=False,
                    details={"answer_validation": validation_details},
                )
            )
            return self._graph_update(graph_state, produced, step_seq, {})

        required_goal_tools = find_required_goal_tools(
            state.user_goal, self._tool_gateway.registry.list_manifests()
        )
        expected_workspace_effect = find_required_workspace_effect_mismatch(
            required_goal_tools,
            action.tool_name,
        )
        if expected_workspace_effect is not None:
            next_guard_count = state.effect_guard_rejections + 1
            can_retry = state.iteration + next_guard_count < self._max_iterations
            produced.append(
                self._make_model_failed(
                    trace_id,
                    task_id,
                    run_id,
                    step_seq,
                    model_call_id,
                    model_step_id,
                    started_at,
                    "REQUIRED_TOOL_ACTION_MISMATCH",
                    recoverable=can_retry,
                )
            )
            step_seq += 1
            if can_retry:
                state.effect_guard_rejections = next_guard_count
                state.effect_guard_feedback = build_workspace_effect_mismatch_feedback(
                    expected_tool_name=expected_workspace_effect,
                    proposed_tool_name=action.tool_name,
                )
                return self._graph_update(graph_state, produced, step_seq, {"retry_model": True})
            produced.append(
                self._make_failed_event(
                    trace_id,
                    task_id,
                    run_id,
                    step_seq,
                    code="REQUIRED_TOOL_ACTION_MISMATCH",
                    message="模型连续选择与用户明确 Workspace 副作用不一致的工具，已在授权前停止",
                    recoverable=False,
                )
            )
            return self._graph_update(graph_state, produced, step_seq, {})

        loop_action_decision = self._loop_controller.evaluate_proposed_action(
            state,
            tool_name=action.tool_name,
            arguments=normalized_arguments,
            guard_repeated_success=not self._source_chain_enabled,
        )
        if loop_action_decision is not None:
            can_retry = loop_action_decision.disposition == "continue"
            produced.append(
                self._make_model_failed(
                    trace_id,
                    task_id,
                    run_id,
                    step_seq,
                    model_call_id,
                    model_step_id,
                    started_at,
                    loop_action_decision.reason_code,
                    recoverable=can_retry,
                )
            )
            step_seq += 1
            if can_retry:
                state.effect_guard_feedback = (
                    (
                        "前一个证据检索域失败后，当前工具属于不同且未获准替代的证据域，"
                        "不能将其结果冒充原来源。请重试原证据域、选择 manifest 明确允许的"
                        "等价来源，或如实报告来源不可用。"
                    )
                    if loop_action_decision.reason_code == "SEMANTIC_SOURCE_SUBSTITUTION"
                    else (
                        "用户明确限定了单一 Workspace 副作用范围，且该范围已经由成功的"
                        "工具结果满足。不得再修改其他文件或路径；请直接根据现有结果收口。"
                    )
                    if loop_action_decision.reason_code == "WORKSPACE_EFFECT_SCOPE_SATISFIED"
                    else (
                        "刚才的工具动作已成功且当前提议与其语义完全相同，不会产生新证据。"
                        "请改变查询、范围或证据来源；若任务已经满足完成契约，请直接收口。"
                    )
                )
                return self._graph_update(graph_state, produced, step_seq, {"retry_model": True})
            produced.append(
                self._make_failed_event(
                    trace_id,
                    task_id,
                    run_id,
                    step_seq,
                    code=(
                        "SEMANTIC_SOURCE_SUBSTITUTION"
                        if loop_action_decision.reason_code == "SEMANTIC_SOURCE_SUBSTITUTION"
                        else "WORKSPACE_EFFECT_SCOPE_EXCEEDED"
                        if loop_action_decision.reason_code
                        == "WORKSPACE_EFFECT_SCOPE_SATISFIED"
                        else "LOOP_NO_PROGRESS"
                    ),
                    message=(
                        "模型连续尝试使用未获准的证据域替代失败来源，已停止无效检索"
                        if loop_action_decision.reason_code == "SEMANTIC_SOURCE_SUBSTITUTION"
                        else "模型连续提出超出用户明确范围的 Workspace 副作用，已在授权前停止"
                        if loop_action_decision.reason_code
                        == "WORKSPACE_EFFECT_SCOPE_SATISFIED"
                        else "模型连续提出相同且已成功的工具动作，已停止无进展循环"
                    ),
                    recoverable=False,
                )
            )
            return self._graph_update(graph_state, produced, step_seq, {})

        repeated_failure = _latest_repeated_failed_action(
            state, action.tool_name, normalized_arguments
        )
        if repeated_failure is not None:
            error = repeated_failure["error"]
            produced.append(
                self._make_model_failed(
                    trace_id,
                    task_id,
                    run_id,
                    step_seq,
                    model_call_id,
                    model_step_id,
                    started_at,
                    "REPEATED_FAILED_TOOL_ACTION",
                    recoverable=False,
                )
            )
            step_seq += 1
            produced.append(
                self._make_failed_event(
                    trace_id,
                    task_id,
                    run_id,
                    step_seq,
                    code=str(error.get("code", "TOOL_FAILED")),
                    message=str(
                        error.get("message", repeated_failure.get("summary", "工具执行失败"))
                    ),
                    category=str(error.get("category", "tool")),
                    recoverable=False,
                )
            )
            return self._graph_update(graph_state, produced, step_seq, {})

        if action.tool_name == "rag.search" and rag_document_scope(state.intent) == "unresolved":
            return invalid(
                "用户指向了特定资料，但 Runtime 无法解析到可信 RAG 文档；"
                "禁止退化为全库检索，应请用户明确资料名称"
            )

        source_guard_decision = (
            evaluate_workspace_source_action_guard(
                state.user_goal,
                state.observations,
                tool_name=action.tool_name,
                arguments=normalized_arguments,
                slot_attempts=state.source_chain_slot_attempts,
                remaining_calls=self._max_iterations - state.iteration,
            )
            if self._source_chain_enabled
            else None
        )
        if source_guard_decision is not None:
            next_guard_count = state.source_chain_guard_rejections + 1
            can_retry = (
                next_guard_count <= _MAX_CONSECUTIVE_SOURCE_CHAIN_GUARD_REJECTIONS
                and state.iteration < self._max_iterations
            )
            produced.append(
                self._make_model_failed(
                    trace_id,
                    task_id,
                    run_id,
                    step_seq,
                    model_call_id,
                    model_step_id,
                    started_at,
                    "SOURCE_CHAIN_PLANNING_STALLED"
                    if can_retry
                    else "SOURCE_CHAIN_NAVIGATION_STALLED",
                    recoverable=can_retry,
                    navigation_guard=source_guard_decision.diagnostics,
                )
            )
            step_seq += 1
            if can_retry:
                state.source_chain_guard_rejections = next_guard_count
                state.effect_guard_feedback = source_guard_decision.feedback
                return self._graph_update(graph_state, produced, step_seq, {"retry_model": True})
            produced.append(
                self._make_failed_event(
                    trace_id,
                    task_id,
                    run_id,
                    step_seq,
                    code="SOURCE_CHAIN_NAVIGATION_STALLED",
                    message=("源码调用链规划连续执行无进展动作，已停止继续消耗模型和工具预算"),
                    recoverable=False,
                    details={"source_navigation": source_guard_decision.diagnostics},
                )
            )
            return self._graph_update(graph_state, produced, step_seq, {})

        knowledge_provenance_links: list[dict[str, str]] | None = None
        if action.tool_name == "knowledge.create_document":
            current_provenance = trusted_knowledge_provenance(state.observations)
            provenance_mode = _knowledge_effect(state, "knowledge_provenance", "skip")
            history_provenance = (
                state.trusted_history_provenance
                if provenance_mode in {"optional", "required"}
                else []
            )
            knowledge_provenance_links = merge_trusted_knowledge_provenance(
                current_provenance,
                history_provenance,
            )
            if provenance_mode == "required" and not knowledge_provenance_links:
                produced.append(
                    self._make_model_failed(
                        trace_id,
                        task_id,
                        run_id,
                        step_seq,
                        model_call_id,
                        model_step_id,
                        started_at,
                        "KNOWLEDGE_PROVENANCE_REQUIRED",
                        recoverable=False,
                    )
                )
                step_seq += 1
                produced.append(
                    self._make_failed_event(
                        trace_id,
                        task_id,
                        run_id,
                        step_seq,
                        code="KNOWLEDGE_PROVENANCE_REQUIRED",
                        message="知识写入要求保留来源，但最近完整对话轮次和当前运行均无可信工具来源",
                        recoverable=False,
                    )
                )
                return self._graph_update(graph_state, produced, step_seq, {})

        model_completed = self._make_model_completed(
            trace_id,
            task_id,
            run_id,
            step_seq,
            model_call_id,
            model_step_id,
            started_at,
            "tool_call",
        )
        produced.append(model_completed)
        step_seq += 1
        ws_root = job.workspace_path or graph_state["default_workspace_root"]
        trusted_arguments = dict(action.arguments)
        if action.tool_name == "rag.search":
            scope = rag_document_scope(state.intent)
            resolved_ids = resolved_rag_document_ids(state.intent)
            if scope == "selected":
                if not resolved_ids:
                    return invalid("Intent 指定文档范围缺少可信 document_ids")
                trusted_arguments["document_ids"] = list(resolved_ids)
            else:
                # document_ids is Runtime-owned. A model cannot independently
                # narrow or redirect retrieval outside the validated Intent.
                trusted_arguments.pop("document_ids", None)
        if action.tool_name.startswith("workspace."):
            trusted_arguments["workspace_root"] = ws_root
        if (
            action.tool_name == "knowledge.create_document"
            and job.scheduled_task_id
            and job.source_policy.get("provider") == "arxiv"
        ):
            # Scheduled source provenance is Runtime-owned. The model writes the
            # report body, but cannot omit or invent the source URLs used for
            # deduplication on the next execution.
            trusted_arguments["source_urls"] = _scheduled_arxiv_source_urls(state.observations)
        if action.tool_name == "knowledge.create_document":
            # Artifact / RAG identities are host facts. Always replace any
            # model-supplied linkage with relationships joined from successful
            # ToolResults in this run.
            trusted_arguments["provenance_links"] = knowledge_provenance_links or []
            requested_title = _knowledge_effect(state, "knowledge_title")
            if requested_title:
                # Explicit titles are part of the validated Intent contract. The
                # action model may draft content, but it cannot expand or rename
                # a title the user supplied verbatim.
                trusted_arguments["title"] = requested_title
        tool_call_id = deterministic_event_id(run_id, "tool.call", step_seq)
        execution_context: dict[str, Any] = {}
        if action.tool_name == "literature.download_arxiv_pdf":
            execution_context["artifact_id"] = str(
                uuid5(
                    NAMESPACE_URL,
                    f"jarvis:artifact:{run_id}:{tool_call_id}:0:literature-pdf",
                )
            )
            execution_context["workspace_path"] = ws_root
        tool_request = ToolRequest(
            task_id=task_id,
            run_id=run_id,
            # Use the checkpointed, Run-global event sequence. AgentState.iteration
            # counts only completed tool observations and can collide with an
            # earlier model step (for example, the second tool used step 2).
            step_id=deterministic_step_id(run_id, step_seq),
            tool_name=action.tool_name,
            arguments=trusted_arguments,
            reason=action.reason,
            requested_by="agent",
            authorization_scope=(
                {
                    "type": "scheduled_task",
                    "scheduled_task_id": job.scheduled_task_id,
                    "authorized_tools": list(job.authorized_tools),
                    "source_policy": dict(job.source_policy),
                }
                if job.scheduled_task_id
                else {}
            ),
            execution_context=execution_context,
        )
        manifest, permission_check, assessment_error = self._tool_gateway.assess(tool_request)
        arguments_summary = _build_arguments_summary(trusted_arguments)
        tool_call_base = {
            "id": tool_call_id,
            "run_id": run_id,
            "step_id": tool_request.step_id,
            "tool_name": action.tool_name,
            "provider": manifest.provider if manifest is not None else "native",
            "mcp_server_id": manifest.mcp_server_id
            if manifest is not None and manifest.provider == "mcp"
            else None,
            "risk_level": permission_check.risk_level
            if permission_check is not None
            else (manifest.risk_level_default if manifest is not None else "L3"),
            "arguments": arguments_summary,
            "arguments_summary": arguments_summary,
            "permission_status": "pending"
            if permission_check is not None and permission_check.needs_user_approval
            else "not_required",
        }
        next_turn = {
            "action": action,
            "tool_request": tool_request,
            "tool_call_base": tool_call_base,
            "tool_call_id": tool_call_id,
            "permission_check": permission_check,
            "assessment_error": assessment_error,
        }
        self._attach_checkpoint(
            model_completed, graph_state, step_seq, "execute_tool", turn=next_turn
        )
        if graph_state["publish_cb"] is not None:
            graph_state["publish_cb"](model_completed)
        return self._graph_update(graph_state, produced, step_seq, next_turn)
