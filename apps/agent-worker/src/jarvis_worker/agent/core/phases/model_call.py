"""LangGraph call_model 节点的模型、上下文与安全流式阶段。"""

from __future__ import annotations

import logging
import time
from dataclasses import asdict
from typing import Any

from jarvis_worker.agent.context.manager import ContextManager
from jarvis_worker.agent.context.types import ModelContextProfile
from jarvis_worker.agent.core.effect_guard import (
    find_missing_workspace_evidence,
    requires_rag_search,
    workspace_semantics,
)
from jarvis_worker.agent.core.evidence_navigation import (
    build_workspace_evidence_navigation_feedback,
    build_workspace_source_chain_feedback,
    workspace_source_chain_requires_more_evidence,
)
from jarvis_worker.agent.core.final_answer import FinalAnswerValidator
from jarvis_worker.agent.core.graph_state import AgentGraphState, AgentGraphUpdate
from jarvis_worker.agent.core.phases.runtime import PhaseRuntime
from jarvis_worker.agent.core.source_chain_validator import SOURCE_CHAIN_VALIDATOR_ID
from jarvis_worker.agent.core.state import AgentState
from jarvis_worker.agent.models.errors import ModelProviderError, model_output_invalid
from jarvis_worker.agent.models.provider import ModelProvider
from jarvis_worker.agent.skills.contracts import SkillLayerError
from jarvis_worker.agent.skills.layer import SkillLayer
from jarvis_worker.runtime.events import (
    build_runtime_event,
    deterministic_event_id,
    deterministic_step_id,
)
from jarvis_worker.runtime_bus.messages import RuntimeEventEnvelope

log = logging.getLogger("jarvis_worker.agent_runner")

_MAX_STREAM_OUTPUT_CHARS = 32_768
_STREAM_DELTA_CHARS = 128
_MAX_RUNTIME_MODEL_OUTPUT_REJECTIONS = 1
_MODEL_OUTPUT_RETRY_FEEDBACK = (
    "上一次模型输出未通过 AgentAction 结构化协议校验。请重新判断下一步，且只返回一个"
    "符合系统 JSON schema 的 call_tool 或 finish 对象；不要输出 Markdown、解释文字或多个对象。"
)
_TOOL_REQUIRED_MODEL_OUTPUT_RETRY_FEEDBACK = (
    "任务所需证据仍不完整，当前处于工具补证模式。唯一合法动作是 call_tool；请从已启用工具中"
    "自主选择能推进任一未覆盖证据面的工具与参数。不得返回 finish、解释文字或多个动作。"
)


def build_tool_budget_feedback(*, used: int, maximum: int) -> str:
    """构造可信、确定性的工具预算反馈，避免模型把最终收口误当成普通规划轮。"""
    remaining = max(0, maximum - used)
    if remaining == 0:
        return (
            f"工具调用预算已耗尽（已使用 {used}/{maximum}）。本轮不得请求任何新动作，必须立即"
            "返回 finish：只基于已有成功 ToolResult 交付已确认事实，明确区分推断与未覆盖范围；"
            "不得编造证据，也不得声称未完成部分已经完成。"
        )
    return (
        f"工具调用预算：已使用 {used}/{maximum}，剩余 {remaining} 次。请优先获取完成当前目标"
        "所必需的直接证据；避免重复调用、无路径约束的宽泛搜索和不能推进目标的逐级目录浏览。"
    )


def has_successful_answer_evidence(state: AgentState) -> bool:
    """Only a successful native RAG search is trusted retrieval evidence."""
    return any(
        isinstance(item, dict)
        and item.get("tool_name") == "rag.search"
        and item.get("ok") is True
        for item in state.observations
    )


def should_enter_finish_only(
    state: AgentState,
    *,
    max_iterations: int,
    source_chain_enabled: bool = True,
) -> bool:
    """Reserve the final two calls of an evidence-only Workspace task for closure.

    A read task that already has the required direct evidence must stop expanding
    the search tree before the hard tool limit.  The dedicated non-streaming
    finish contract can then validate/retry the complete answer without exposing
    partial JSON-wrapped Markdown.  Effectful tasks and unresolved required RAG
    evidence keep the normal decision protocol.
    """
    if state.answer_guard_rejections > 0 or state.answer_guard_feedback:
        return True
    if state.iteration >= max_iterations:
        return True
    if max_iterations - state.iteration > 2:
        return False
    evidence, action, ambiguity = workspace_semantics(state.intent)
    if (
        action != "read"
        or ambiguity != "clear"
        or evidence not in {"metadata", "required"}
        or find_missing_workspace_evidence(
            state.intent,
            state.observations,
            user_goal=state.user_goal,
        )
        or (
            source_chain_enabled
            and workspace_source_chain_requires_more_evidence(
                state.user_goal,
                state.observations,
            )
        )
    ):
        return False
    return not requires_rag_search(state.intent) or has_successful_answer_evidence(state)


def should_require_tool_action(
    state: AgentState,
    *,
    max_iterations: int,
    source_chain_enabled: bool = True,
) -> bool:
    """任一证据 finish 被拒后，在尚有工具预算时硬约束下一步必须取证。

    ``effect_guard_feedback`` 也承载普通结构化输出的纠正提示，不能单独作为
    tool-required 判据。只有 EffectGuard 已实际拒绝过 finish 且反馈仍未被一次
    新 ToolResult 消费时，才进入补证模式；ObservationPhase 会在取得结果后清空
    反馈，使 Agent 回到普通规划。源码链拒绝继续使用自己的持久化计数。
    """
    if state.answer_guard_rejections > 0 or state.answer_guard_feedback:
        return False
    if state.iteration >= max_iterations:
        return False
    effect_guard_requires_tool = (
        state.effect_guard_rejections > 0 and bool(state.effect_guard_feedback)
    )
    source_chain_requires_tool = (
        source_chain_enabled and state.source_chain_evidence_rejections > 0
    )
    return effect_guard_requires_tool or source_chain_requires_tool


def should_buffer_workspace_output(state: AgentState) -> bool:
    """Keep Workspace evidence AgentAction envelopes private until validated."""
    evidence, action, _ = workspace_semantics(state.intent)
    return action == "read" and evidence in {"metadata", "required"}


def _skill_event_payload(skill_context: dict[str, Any] | None) -> dict[str, str]:
    if not skill_context:
        return {}
    return {
        "skill_id": str(skill_context.get("skill_id", "")),
        "skill_version": str(skill_context.get("version", "")),
        "skill_fingerprint": str(skill_context.get("fingerprint", "")),
    }


class ModelCallPhase:
    """拥有 call_model 语义；只返回未受信任 AgentAction，不执行工具。"""

    def __init__(
        self,
        *,
        model: ModelProvider,
        context_manager: ContextManager,
        runtime: PhaseRuntime,
        max_iterations: int,
        skill_layer: SkillLayer | None = None,
        final_answer_validators: tuple[FinalAnswerValidator, ...] = (),
    ) -> None:
        self._model = model
        self._context_manager = context_manager
        self._runtime = runtime
        self._max_iterations = max_iterations
        self._skill_layer = skill_layer
        self._final_answer_validators = final_answer_validators
        self._source_chain_enabled = any(
            validator.validator_id == SOURCE_CHAIN_VALIDATOR_ID
            for validator in final_answer_validators
        )

    def run(self, graph_state: AgentGraphState) -> AgentGraphUpdate:
        job = graph_state["job"]
        state = graph_state["state"]
        step_seq = graph_state["step_seq"]
        trace_id, task_id, run_id = job.trace_id, job.task_id, job.run_id
        produced: list[RuntimeEventEnvelope] = []

        if graph_state["cancel_check"] and graph_state["cancel_check"]():
            produced.append(self._runtime.make_cancelled(trace_id, task_id, run_id))
            return self._runtime.graph_update(graph_state, produced, step_seq, {})
        if graph_state["pause_check"] and (pause_id := graph_state["pause_check"]()):
            produced.append(
                self._runtime.make_paused(
                    graph_state, step_seq, "call_model", event_id=pause_id
                )
            )
            return self._runtime.graph_update(graph_state, produced, step_seq, {})
        halt = graph_state["run_supervisor"].before_model_call(state)
        if halt is not None:
            produced.append(
                self._runtime.make_failed_event(
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
            return self._runtime.graph_update(graph_state, produced, step_seq, {})

        model_call_id = deterministic_event_id(run_id, "model.call", step_seq)
        model_call_sequence = step_seq
        model_step_id = deterministic_step_id(run_id, model_call_sequence)
        started_at = time.monotonic()
        provider_name = getattr(self._model, "provider_name", "unknown")
        model_name = getattr(self._model, "model_name", "unknown")
        log.info(
            "Model 调用开始: provider=%s model=%s call_id=%s",
            provider_name,
            model_name,
            model_call_id,
            extra={"step_id": model_step_id},
        )
        started = self._runtime.make_event(
            trace_id,
            build_runtime_event(
                event_type="model.call.started",
                task_id=task_id,
                run_id=run_id,
                step_id=model_step_id,
                event_id=deterministic_event_id(run_id, "model.call.started", step_seq),
                payload={
                    "provider": provider_name,
                    "model_name": model_name,
                    "call_id": model_call_id,
                },
            ),
        )
        self._runtime.attach_checkpoint(started, graph_state, step_seq + 1, "call_model")
        produced.append(started)
        if graph_state["publish_cb"] is not None:
            graph_state["publish_cb"](started)
        step_seq += 1

        streamed_chars = 0
        stream_delta_index = 0
        pending_delta = ""
        buffer_final_output = any(
            validator.requires_buffered_output(state)
            for validator in self._final_answer_validators
        ) or (
            requires_rag_search(state.intent) and not has_successful_answer_evidence(state)
        ) or bool(
            find_missing_workspace_evidence(
                state.intent,
                state.observations,
                user_goal=state.user_goal,
            )
        )
        # Workspace evidence answers frequently contain long Markdown paths and
        # code identifiers inside the AgentAction JSON envelope. Keep the whole
        # decision buffered until strict parsing succeeds so a malformed envelope
        # can be retried without leaking a partial/duplicated answer to the UI.
        buffer_final_output = buffer_final_output or should_buffer_workspace_output(state)
        tool_required = should_require_tool_action(
            state,
            max_iterations=self._max_iterations,
            source_chain_enabled=self._source_chain_enabled,
        )
        finish_only = False if tool_required else should_enter_finish_only(
            state,
            max_iterations=self._max_iterations,
            source_chain_enabled=self._source_chain_enabled,
        )
        action_mode = (
            "tool_required" if tool_required else "finish_only" if finish_only else "normal"
        )
        buffer_final_output = buffer_final_output or finish_only or tool_required

        def emit_stream_delta() -> None:
            nonlocal pending_delta, stream_delta_index
            if not pending_delta:
                return
            stream_delta_index += 1
            delta_event = self._runtime.make_event(
                trace_id,
                build_runtime_event(
                    event_type="model.delta",
                    task_id=task_id,
                    run_id=run_id,
                    step_id=model_step_id,
                    event_id=deterministic_event_id(
                        run_id, "model.delta", model_call_sequence * 10_000 + stream_delta_index
                    ),
                    payload={"step_id": model_step_id, "delta": pending_delta},
                ),
            )
            produced.append(delta_event)
            if graph_state["publish_cb"] is not None:
                graph_state["publish_cb"](delta_event)
            pending_delta = ""

        def receive_text_delta(delta: str) -> None:
            nonlocal streamed_chars, pending_delta
            if buffer_final_output or not isinstance(delta, str) or not delta:
                return
            remaining = _MAX_STREAM_OUTPUT_CHARS - streamed_chars
            if remaining <= 0:
                return
            pending_delta += delta[:remaining]
            streamed_chars += min(len(delta), remaining)
            while len(pending_delta) >= _STREAM_DELTA_CHARS:
                chunk, pending_delta = (
                    pending_delta[:_STREAM_DELTA_CHARS],
                    pending_delta[_STREAM_DELTA_CHARS:],
                )
                original_pending = pending_delta
                pending_delta = chunk
                emit_stream_delta()
                pending_delta = original_pending

        try:
            if state.skill_context is None:
                resolved_skill = (
                    self._skill_layer.resolve(job) if self._skill_layer is not None else None
                )
                state.skill_context = (
                    resolved_skill.to_state_dict() if resolved_skill is not None else {}
                )
            profile = getattr(
                self._model,
                "context_profile",
                ModelContextProfile(
                    provider=provider_name,
                    model=model_name,
                    context_window_tokens=32_768,
                    max_output_tokens=4_096,
                ),
            )
            runtime_feedback = [
                build_tool_budget_feedback(
                    used=state.iteration,
                    maximum=self._max_iterations,
                )
            ]
            evidence_feedback = build_workspace_evidence_navigation_feedback(
                state.observations,
                remaining_calls=max(0, self._max_iterations - state.iteration),
            )
            if self._source_chain_enabled:
                source_chain_feedback = build_workspace_source_chain_feedback(
                    state.observations,
                    user_goal=state.user_goal,
                    remaining_calls=max(0, self._max_iterations - state.iteration),
                    slot_attempts=state.source_chain_slot_attempts,
                )
                if source_chain_feedback is not None:
                    runtime_feedback.append(source_chain_feedback)
            if evidence_feedback is not None and not finish_only:
                runtime_feedback.append(evidence_feedback)
            context_package = self._context_manager.prepare(
                state,
                profile,
                runtime_feedback=runtime_feedback,
                finish_only=finish_only,
                tool_required=tool_required,
            )
            context_stats = asdict(context_package.stats)
            log.info(
                "Context 构建完成: provider=%s model=%s stats=%s",
                context_package.profile.provider,
                context_package.profile.model,
                context_stats,
                extra={"step_id": model_step_id},
            )
            context_event = self._runtime.make_event(
                trace_id,
                build_runtime_event(
                    event_type="model.context.prepared",
                    task_id=task_id,
                    run_id=run_id,
                    step_id=model_step_id,
                    event_id=deterministic_event_id(run_id, "model.context.prepared", step_seq),
                    payload={
                        "provider": context_package.profile.provider,
                        "model_name": context_package.profile.model,
                        "fingerprint": context_package.fingerprint,
                        "action_mode": action_mode,
                        **_skill_event_payload(state.skill_context),
                        **context_stats,
                    },
                ),
            )
            self._runtime.attach_checkpoint(
                context_event, graph_state, step_seq + 1, "call_model"
            )
            produced.append(context_event)
            if graph_state["publish_cb"] is not None:
                graph_state["publish_cb"](context_event)
            step_seq += 1
            if tool_required:
                action = self._model.decide_prepared_context_tool_required(
                    state, context_package
                )
            elif finish_only:
                action = self._model.decide_prepared_context_finish_only(
                    state, context_package
                )
            elif buffer_final_output:
                decide_prepared = getattr(self._model, "decide_prepared_context", None)
                action = (
                    decide_prepared(state, context_package)
                    if callable(decide_prepared)
                    else self._model.decide_next_action(state)
                )
            else:
                decide_prepared = getattr(
                    self._model, "decide_prepared_context_stream", None
                )
                if callable(decide_prepared):
                    action = decide_prepared(state, context_package, receive_text_delta)
                else:
                    decide_stream = getattr(self._model, "decide_next_action_stream", None)
                    action = (
                        decide_stream(state, receive_text_delta)
                        if callable(decide_stream)
                        else self._model.decide_next_action(state)
                    )
            if tool_required and action.action_type != "call_tool":
                raise model_output_invalid(
                    "工具补证模式只允许 call_tool",
                    failure_kind="unsupported_action",
                )
            emit_stream_delta()
            log.info(
                "Model 调用完成: provider=%s model=%s action=%s duration_ms=%d streamed_chars=%d",
                provider_name,
                model_name,
                getattr(action, "action_type", type(action).__name__),
                int((time.monotonic() - started_at) * 1000),
                streamed_chars,
                extra={"step_id": model_step_id},
            )
        except Exception as exc:
            # Control commands that arrived while the provider call was in flight
            # own the next durable boundary.  A provider failure observed after the
            # command must not overwrite a requested pause/cancel terminal; resume
            # will retry from the pre-call checkpoint without consuming the failed
            # model output or executing an effect.
            if graph_state["cancel_check"] and graph_state["cancel_check"]():
                produced.append(self._runtime.make_cancelled(trace_id, task_id, run_id))
                return self._runtime.graph_update(graph_state, produced, step_seq, {})
            if graph_state["pause_check"] and (
                pause_id := graph_state["pause_check"]()
            ):
                produced.append(
                    self._runtime.make_paused(
                        graph_state,
                        step_seq,
                        "call_model",
                        event_id=pause_id,
                    )
                )
                return self._runtime.graph_update(graph_state, produced, step_seq, {})
            if isinstance(exc, SkillLayerError):
                code, recoverable, message = exc.code, False, "Skill 上下文无效"
                output_failure_kind = None
                attempt_count = 1
            elif isinstance(exc, ModelProviderError):
                code, recoverable, message = exc.code, exc.recoverable, f"模型调用失败: {exc}"
                output_failure_kind = exc.output_failure_kind
                attempt_count = exc.attempt_count
            else:
                code, recoverable, message = (
                    "MODEL_PROVIDER_INTERNAL_ERROR",
                    False,
                    "模型提供者内部错误",
                )
                output_failure_kind = None
                attempt_count = 1
            log.error(
                "Model 调用失败: provider=%s model=%s code=%s recoverable=%s duration_ms=%d error_type=%s",
                provider_name,
                model_name,
                code,
                recoverable,
                int((time.monotonic() - started_at) * 1000),
                type(exc).__name__,
                extra={"step_id": model_step_id},
            )
            retry_structured_output = (
                code == "MODEL_OUTPUT_INVALID"
                and state.model_output_rejections < _MAX_RUNTIME_MODEL_OUTPUT_REJECTIONS
            )
            final_output_retryable = recoverable and code != "MODEL_OUTPUT_INVALID"
            failed_event = self._runtime.make_model_failed(
                trace_id,
                task_id,
                run_id,
                step_seq,
                model_call_id,
                model_step_id,
                started_at,
                code,
                provider_name=provider_name,
                model_name=model_name,
                recoverable=retry_structured_output or final_output_retryable,
                output_failure_kind=output_failure_kind,
                attempt_count=attempt_count,
            )
            produced.append(failed_event)
            step_seq += 1
            if retry_structured_output:
                state.model_output_rejections += 1
                state.effect_guard_feedback = (
                    _TOOL_REQUIRED_MODEL_OUTPUT_RETRY_FEEDBACK
                    if tool_required
                    else _MODEL_OUTPUT_RETRY_FEEDBACK
                )
                self._runtime.attach_checkpoint(
                    failed_event, graph_state, step_seq, "call_model", state=state
                )
                return self._runtime.graph_update(
                    graph_state, produced, step_seq, {"retry_model": True}
                )
            produced.append(
                self._runtime.make_failed_event(
                    trace_id,
                    task_id,
                    run_id,
                    step_seq,
                    code=code,
                    message=message,
                    recoverable=final_output_retryable,
                )
            )
            return self._runtime.graph_update(graph_state, produced, step_seq, {})

        return self._runtime.graph_update(
            graph_state,
            produced,
            step_seq,
            {
                "action": action,
                "model_call_id": model_call_id,
                "model_step_id": model_step_id,
                "model_started_at": started_at,
            },
        )
