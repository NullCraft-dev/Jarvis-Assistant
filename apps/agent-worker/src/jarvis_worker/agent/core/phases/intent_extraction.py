"""LangGraph extract_intent 节点的可信上下文与结构化意图阶段。"""

from __future__ import annotations

import time as _time_module
from typing import Any

from jarvis_worker.agent.core.graph_state import AgentGraphState, AgentGraphUpdate
from jarvis_worker.agent.core.phases.runtime import PhaseRuntime
from jarvis_worker.agent.intents import IntentContextProvider, IntentExtractor, IntentRuntimeContext
from jarvis_worker.agent.intents.rules import (
    build_safe_intent_fallback,
    is_explicit_workspace_content_search_goal,
)
from jarvis_worker.agent.loop import LoopController
from jarvis_worker.agent.models.provider import ModelProvider
from jarvis_worker.agent.tool_gateway.gateway import ToolGateway
from jarvis_worker.runtime.events import (
    build_runtime_event,
    deterministic_event_id,
    deterministic_step_id,
)
from jarvis_worker.runtime_bus.messages import RuntimeEventEnvelope

_MAX_RUNTIME_INTENT_OUTPUT_REJECTIONS = 1
_INTENT_OUTPUT_RETRY_FEEDBACK = (
    "上一次 Intent 候选未通过结构化校验。严格使用 Intent JSON 契约；"
    "特定文档无法可靠映射时使用 document_scope=unresolved，不能退化为 all。"
)


def _enabled_tool_names(tool_gateway: ToolGateway) -> frozenset[str]:
    registry = getattr(tool_gateway, "registry", None)
    list_manifests = getattr(registry, "list_manifests", None)
    if not callable(list_manifests):
        return frozenset()
    return frozenset(
        manifest.name for manifest in list_manifests() if getattr(manifest, "enabled", False)
    )


def _missing_intent_capabilities(
    intent: dict[str, Any],
    available_tool_names: frozenset[str],
    *,
    user_goal: str = "",
) -> tuple[str, ...]:
    required: list[str] = []
    retrieval = intent.get("retrieval")
    if isinstance(retrieval, dict) and retrieval.get("mode") in {"retrieve", "required"}:
        required.append("rag.search")
    effects = intent.get("effects")
    if isinstance(effects, dict):
        if effects.get("knowledge_write") == "required":
            required.append("knowledge.create_document")
        if effects.get("rag_ingestion") == "required":
            required.append("rag.ingest_artifact")
    workspace = intent.get("workspace")
    if isinstance(workspace, dict):
        action = workspace.get("action")
        ambiguity = workspace.get("ambiguity")
        if workspace.get("evidence") == "metadata" and not available_tool_names.intersection(
            {"workspace.list_files", "workspace.get_file_info"}
        ):
            required.append("workspace.metadata")
        if workspace.get("evidence") == "required":
            content_capabilities = {"workspace.read_file", "workspace.read_files"}
            if is_explicit_workspace_content_search_goal(user_goal):
                content_capabilities.add("workspace.search_text")
            if not available_tool_names.intersection(content_capabilities):
                required.append("workspace.content_read")
        if (
            ambiguity == "clear"
            and action == "write"
            and not available_tool_names.intersection(
                {"workspace.create_file", "workspace.create_directory", "workspace.move_path"}
            )
        ):
            required.append("workspace.write")
        if ambiguity == "clear" and action == "destructive":
            required.append("workspace.delete_path")
    return tuple(name for name in required if name not in available_tool_names)


class IntentExtractionPhase:
    """拥有 Intent 提取与验证语义；不执行工具或决定下一条图边。"""

    def __init__(
        self,
        *,
        model: ModelProvider,
        tool_gateway: ToolGateway,
        intent_extractor: IntentExtractor,
        intent_context_provider: IntentContextProvider | None,
        runtime: PhaseRuntime,
        loop_controller: LoopController,
    ) -> None:
        self._model = model
        self._tool_gateway = tool_gateway
        self._intent_extractor = intent_extractor
        self._intent_context_provider = intent_context_provider
        self._runtime = runtime
        self._loop_controller = loop_controller

    def _make_event(self, trace_id: str, event: dict) -> RuntimeEventEnvelope:
        return self._runtime.make_event(trace_id, event)

    def _make_model_failed(self, *args: Any, **kwargs: Any) -> RuntimeEventEnvelope:
        return self._runtime.make_model_failed(
            *args,
            provider_name=getattr(self._model, "provider_name", "unknown"),
            model_name=getattr(self._model, "model_name", "unknown"),
            **kwargs,
        )

    def _make_model_completed(self, *args: Any, **kwargs: Any) -> RuntimeEventEnvelope:
        return self._runtime.make_model_completed(
            *args,
            provider_name=getattr(self._model, "provider_name", "unknown"),
            model_name=getattr(self._model, "model_name", "unknown"),
            **kwargs,
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
        """Resolve one validated Intent candidate inside the observable Agent loop."""
        job = graph_state["job"]
        state = graph_state["state"]
        step_seq = graph_state["step_seq"]
        trace_id, task_id, run_id = job.trace_id, job.task_id, job.run_id
        produced: list[RuntimeEventEnvelope] = []

        if state.intent is not None:
            return self._graph_update(graph_state, produced, step_seq, {})
        if graph_state["cancel_check"] and graph_state["cancel_check"]():
            produced.append(self._make_cancelled(trace_id, task_id, run_id))
            return self._graph_update(graph_state, produced, step_seq, {})
        if graph_state["pause_check"] and (pause_id := graph_state["pause_check"]()):
            produced.append(
                self._make_paused(
                    graph_state,
                    step_seq,
                    "extract_intent",
                    event_id=pause_id,
                )
            )
            return self._graph_update(graph_state, produced, step_seq, {})
        halt = (
            graph_state["run_supervisor"].before_model_call(state)
            if self._intent_extractor.uses_model
            else graph_state["run_supervisor"].before_phase(state)
        )
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

        try:
            if state.intent_context is None:
                context = (
                    self._intent_context_provider.load(task_id)
                    if self._intent_context_provider is not None
                    else IntentRuntimeContext()
                )
                state.intent_context = context.to_state_dict()
            runtime_context = IntentRuntimeContext.from_state_dict(state.intent_context)
        except Exception:
            produced.append(
                self._make_failed_event(
                    trace_id,
                    task_id,
                    run_id,
                    step_seq,
                    code="INTENT_CONTEXT_UNAVAILABLE",
                    message="无法读取当前任务的可信文档目录",
                    recoverable=False,
                )
            )
            return self._graph_update(graph_state, produced, step_seq, {})

        if not self._intent_extractor.uses_model:
            state.intent = self._intent_extractor.extract(
                state.user_goal,
                available_tool_names=_enabled_tool_names(self._tool_gateway),
                runtime_context=runtime_context,
                history_messages=tuple(state.history_messages),
                validation_feedback=state.intent_feedback,
            ).to_state_dict()
            self._loop_controller.ensure_initialized(state)
            return self._graph_update(graph_state, produced, step_seq, {})

        model_call_id = deterministic_event_id(run_id, "intent.model.call", step_seq)
        model_step_id = deterministic_step_id(run_id, step_seq)
        started_at = _time_module.monotonic()
        started = self._make_event(
            trace_id,
            build_runtime_event(
                event_type="model.call.started",
                task_id=task_id,
                run_id=run_id,
                step_id=model_step_id,
                event_id=deterministic_event_id(run_id, "intent.model.call.started", step_seq),
                payload={
                    "provider": getattr(self._model, "provider_name", "unknown"),
                    "model_name": getattr(self._model, "model_name", "unknown"),
                    "call_id": model_call_id,
                    "purpose": "intent_extraction",
                },
            ),
        )
        self._attach_checkpoint(started, graph_state, step_seq + 1, "extract_intent")
        produced.append(started)
        if graph_state["publish_cb"] is not None:
            graph_state["publish_cb"](started)
        step_seq += 1

        try:
            extraction = self._intent_extractor.extract(
                state.user_goal,
                available_tool_names=_enabled_tool_names(self._tool_gateway),
                runtime_context=runtime_context,
                history_messages=tuple(state.history_messages),
                validation_feedback=state.intent_feedback,
            )
        except Exception as exc:
            from jarvis_worker.agent.models.errors import ModelProviderError

            if isinstance(exc, ModelProviderError):
                code = exc.code
                provider_recoverable = exc.recoverable
                failure_kind = exc.output_failure_kind
                attempt_count = exc.attempt_count
            else:
                code = "INTENT_EXTRACTOR_INTERNAL_ERROR"
                provider_recoverable = False
                failure_kind = None
                attempt_count = 1
            retry_output = (
                code == "MODEL_OUTPUT_INVALID"
                and state.intent_rejections < _MAX_RUNTIME_INTENT_OUTPUT_REJECTIONS
            )
            safe_fallback = (
                build_safe_intent_fallback(
                    state.user_goal,
                    available_tool_names=_enabled_tool_names(self._tool_gateway),
                    history_messages=tuple(state.history_messages),
                )
                if code == "MODEL_OUTPUT_INVALID" and not retry_output
                else None
            )
            failed = self._make_model_failed(
                trace_id,
                task_id,
                run_id,
                step_seq,
                model_call_id,
                model_step_id,
                started_at,
                "INTENT_OUTPUT_INVALID" if code == "MODEL_OUTPUT_INVALID" else code,
                recoverable=retry_output or provider_recoverable or safe_fallback is not None,
                output_failure_kind=failure_kind,
                attempt_count=attempt_count,
                purpose="intent_extraction",
            )
            produced.append(failed)
            step_seq += 1
            if retry_output:
                state.intent_rejections += 1
                state.intent_feedback = _INTENT_OUTPUT_RETRY_FEEDBACK
                self._attach_checkpoint(
                    failed,
                    graph_state,
                    step_seq,
                    "extract_intent",
                    state=state,
                )
                if graph_state["publish_cb"] is not None:
                    graph_state["publish_cb"](failed)
                return self._graph_update(graph_state, produced, step_seq, {"retry_intent": True})
            if safe_fallback is not None:
                state.intent = safe_fallback.to_state_dict()
                self._loop_controller.ensure_initialized(state)
                state.intent_feedback = ""
                completed = self._make_model_completed(
                    trace_id,
                    task_id,
                    run_id,
                    step_seq,
                    model_call_id,
                    model_step_id,
                    started_at,
                    "intent_extraction",
                    purpose="intent_extraction",
                )
                step_seq += 1
                self._attach_checkpoint(
                    completed,
                    graph_state,
                    step_seq,
                    "call_model",
                    state=state,
                )
                produced.append(completed)
                if graph_state["publish_cb"] is not None:
                    graph_state["publish_cb"](failed)
                    graph_state["publish_cb"](completed)
                return self._graph_update(graph_state, produced, step_seq, {})
            terminal_code = (
                "INTENT_EXTRACTION_FAILED"
                if code in {"MODEL_OUTPUT_INVALID", "INTENT_EXTRACTOR_INTERNAL_ERROR"}
                else code
            )
            produced.append(
                self._make_failed_event(
                    trace_id,
                    task_id,
                    run_id,
                    step_seq,
                    code=terminal_code,
                    message="Intent 提取失败，任务未进入工具决策阶段",
                    category="model",
                    recoverable=provider_recoverable and code != "MODEL_OUTPUT_INVALID",
                )
            )
            return self._graph_update(graph_state, produced, step_seq, {})

        state.intent = extraction.to_state_dict()
        self._loop_controller.ensure_initialized(state)
        missing_capabilities = _missing_intent_capabilities(
            state.intent,
            _enabled_tool_names(self._tool_gateway),
            user_goal=state.user_goal,
        )
        if missing_capabilities:
            failed = self._make_model_failed(
                trace_id,
                task_id,
                run_id,
                step_seq,
                model_call_id,
                model_step_id,
                started_at,
                "INTENT_CAPABILITY_UNAVAILABLE",
                recoverable=False,
                purpose="intent_extraction",
            )
            produced.append(failed)
            step_seq += 1
            produced.append(
                self._make_failed_event(
                    trace_id,
                    task_id,
                    run_id,
                    step_seq,
                    code="INTENT_CAPABILITY_UNAVAILABLE",
                    message="当前运行缺少 Intent 所需能力: " + ", ".join(missing_capabilities),
                    category="runtime",
                    recoverable=False,
                )
            )
            return self._graph_update(graph_state, produced, step_seq, {})
        state.intent_feedback = ""
        completed = self._make_model_completed(
            trace_id,
            task_id,
            run_id,
            step_seq,
            model_call_id,
            model_step_id,
            started_at,
            "intent_extraction",
            purpose="intent_extraction",
        )
        step_seq += 1
        self._attach_checkpoint(
            completed,
            graph_state,
            step_seq,
            "call_model",
            state=state,
        )
        produced.append(completed)
        if graph_state["publish_cb"] is not None:
            graph_state["publish_cb"](completed)
        return self._graph_update(graph_state, produced, step_seq, {})
