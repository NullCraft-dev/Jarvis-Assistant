"""AgentRunner — 由 LangGraph 编排的单 Agent 执行循环。

核心循环：model.decide → AgentAction → ToolGateway → observe → 循环 → finish。

当前生产链路（Phase 6C）：
- 使用 ModelProvider 产出结构化 AgentAction；生产装配为 OpenAI-compatible provider。
- Tool 执行必须经过 ToolGateway.execute(ToolRequest)。
- 事件构造复用现有 runtime.events 工具。
- 支持 cancel_check 中断循环。
- 支持 max_iterations 防止无限循环。
- LangGraph StateGraph 负责单轮迭代、继续、暂停和终态路由。
- AgentState 仍是项目 Runtime 的状态 owner；未引入 LangGraph checkpoint 真源。
"""

from __future__ import annotations

import logging
from typing import Any, Callable

from jarvis_worker.agent.context.manager import ContextManager
from jarvis_worker.agent.context.response_language import (
    ResponseLanguagePreferenceValidator,
)
from jarvis_worker.agent.core.actions import AgentAction
from jarvis_worker.agent.core.answer_constraints import ExplicitAnswerConstraintValidator
from jarvis_worker.agent.core.checkpoint import (
    attach_run_checkpoint,
    build_run_checkpoint,
    restore_agent_state,
    validate_permission_checkpoint,
    validate_run_checkpoint,
)
from jarvis_worker.agent.core.final_answer import FinalAnswerValidator
from jarvis_worker.agent.core.final_answer_integrity import (
    CitationVerdictConsistencyValidator,
    FinalMessageIntegrityValidator,
)
from jarvis_worker.agent.core.graph import compile_single_agent_graph
from jarvis_worker.agent.core.graph_nodes import AgentGraphNodes
from jarvis_worker.agent.core.graph_state import AgentGraphState, AgentGraphUpdate
from jarvis_worker.agent.core.phases.action_validation import ActionValidationPhase
from jarvis_worker.agent.core.phases.intent_extraction import IntentExtractionPhase
from jarvis_worker.agent.core.phases.lifecycle import RunLifecyclePhase
from jarvis_worker.agent.core.phases.model_call import ModelCallPhase
from jarvis_worker.agent.core.phases.observation import ObservationPhase, project_tool_result
from jarvis_worker.agent.core.phases.runtime import PhaseRuntime
from jarvis_worker.agent.core.phases.tool_execution import ToolExecutionPhase
from jarvis_worker.agent.core.state import AgentState
from jarvis_worker.agent.core.workspace_listing_projection import (
    WorkspaceListingProjectionValidator,
)
from jarvis_worker.agent.harness import RunBudget, RunSupervisor
from jarvis_worker.agent.intents import (
    IntentContextProvider,
    IntentExtractor,
    RuleBasedIntentExtractor,
)
from jarvis_worker.agent.loop import LoopController
from jarvis_worker.agent.models.provider import ModelProvider
from jarvis_worker.agent.prompts.builder import PromptBuilder
from jarvis_worker.agent.skills.layer import SkillLayer
from jarvis_worker.agent.tool_gateway.contracts import PermissionApproval, ToolRequest, ToolResult
from jarvis_worker.agent.tool_gateway.gateway import ToolGateway
from jarvis_worker.runtime.events import (
    build_runtime_event,
    deterministic_event_id,
)
from jarvis_worker.runtime_bus.messages import RunJobMessage, RuntimeEventEnvelope

log = logging.getLogger("jarvis_worker.agent_runner")

_GRAPH_FIXED_STEP_ALLOWANCE = 12
_GRAPH_STEPS_PER_TOOL_ITERATION = 4
_GRAPH_GUARD_RETRY_CLASSES = 3
_GRAPH_STEPS_PER_GUARD_RETRY = 2


class AgentRunner:
    """Agent 最小执行循环。

    职责：
    - 初始化 AgentState（从 RunJobMessage + default_workspace_root）
    - 执行 observe → decide → act → observe 循环
    - 通过 ToolGateway 执行工具（不直接 os.listdir 等）
    - 构造 RuntimeEventEnvelope 序列
    - 在 cancel_check 时中断循环

    不负责：
    - 真实 LLM 调用（由 ModelProvider 实现）
    - LangGraph 图装配和节点路由（由 agent.graph / agent.graph_nodes 负责）
    - 工具执行细节（由 ToolGateway 负责）
    - 事件发布到 Redis（由 AgentWorker 负责）

    用法：
        runner = AgentRunner(model_provider, tool_gateway)
        envelopes = runner.run(job, default_workspace_root="/path/to/project")
    """

    def __init__(
        self,
        model_provider: ModelProvider,
        tool_gateway: ToolGateway,
        worker_id: str = "agent-default",
        max_iterations: int = 3,
        max_model_calls: int | None = None,
        max_run_seconds: int = 900,
        context_manager: ContextManager | None = None,
        skill_layer: SkillLayer | None = None,
        final_answer_validators: tuple[FinalAnswerValidator, ...] = (),
        intent_extractor: IntentExtractor | None = None,
        intent_context_provider: IntentContextProvider | None = None,
    ):
        try:
            run_budget = RunBudget(
                max_tool_iterations=max_iterations,
                max_model_calls=(
                    max_model_calls
                    if max_model_calls is not None
                    else min(100, max_iterations * 4 + 8)
                ),
                max_run_seconds=max_run_seconds,
            )
        except ValueError as exc:
            raise ValueError(str(exc).replace("max_tool_iterations", "max_iterations")) from exc
        self._model = model_provider
        self._tool_gateway = tool_gateway
        self._worker_id = worker_id
        self._run_supervisor = RunSupervisor(run_budget)
        self._max_iterations = run_budget.max_tool_iterations
        self._graph_recursion_limit = (
            _GRAPH_FIXED_STEP_ALLOWANCE
            + _GRAPH_STEPS_PER_TOOL_ITERATION * self._max_iterations
            + _GRAPH_GUARD_RETRY_CLASSES * _GRAPH_STEPS_PER_GUARD_RETRY * self._max_iterations
        )
        self._phase_runtime = PhaseRuntime(worker_id)
        self._loop_controller = LoopController(self._tool_gateway)
        self._observation_phase = ObservationPhase(
            self._phase_runtime,
            loop_controller=self._loop_controller,
        )
        self._skill_layer = skill_layer
        self._final_answer_validators = (
            WorkspaceListingProjectionValidator(),
            ResponseLanguagePreferenceValidator(),
            ExplicitAnswerConstraintValidator(),
            FinalMessageIntegrityValidator(),
            CitationVerdictConsistencyValidator(),
            *final_answer_validators,
        )
        self._intent_extractor = intent_extractor or RuleBasedIntentExtractor()
        self._intent_context_provider = intent_context_provider
        if context_manager is not None:
            self._context_manager = context_manager
        else:
            registry = getattr(tool_gateway, "registry", None)
            prompt_builder = (
                PromptBuilder.from_registry(registry) if registry is not None else PromptBuilder()
            )
            self._context_manager = ContextManager(prompt_builder)
        self._model_call_phase = ModelCallPhase(
            model=self._model,
            context_manager=self._context_manager,
            runtime=self._phase_runtime,
            max_iterations=self._max_iterations,
            skill_layer=self._skill_layer,
            final_answer_validators=self._final_answer_validators,
        )
        self._action_validation_phase = ActionValidationPhase(
            model=self._model,
            tool_gateway=self._tool_gateway,
            runtime=self._phase_runtime,
            max_iterations=self._max_iterations,
            final_answer_validators=self._final_answer_validators,
            loop_controller=self._loop_controller,
        )
        self._tool_execution_phase = ToolExecutionPhase(
            tool_gateway=self._tool_gateway,
            runtime=self._phase_runtime,
        )
        self._intent_extraction_phase = IntentExtractionPhase(
            model=self._model,
            tool_gateway=self._tool_gateway,
            intent_extractor=self._intent_extractor,
            intent_context_provider=self._intent_context_provider,
            runtime=self._phase_runtime,
            loop_controller=self._loop_controller,
        )
        self._lifecycle_phase = RunLifecyclePhase(
            worker_id=self._worker_id,
            max_iterations=self._max_iterations,
            runtime=self._phase_runtime,
        )
        self._graph_nodes = AgentGraphNodes(
            initialize_run=self._lifecycle_phase.initialize_run,
            extract_intent=self._intent_extraction_phase.run,
            call_model=self._model_call_phase.run,
            validate_action=self._action_validation_phase.run,
            execute_tool=self._tool_execution_phase.run,
            observe_result=self._observation_phase.run,
            build_max_iterations_failure=self._lifecycle_phase.build_max_iterations_failure,
            max_iterations=max_iterations,
        )
        self._graph = compile_single_agent_graph(self._graph_nodes)

    def run(
        self,
        job: RunJobMessage,
        default_workspace_root: str = "",
        cancel_check: Callable[[], bool] | None = None,
        pause_check: Callable[[], str | None] | None = None,
        history_messages: list[dict[str, str]] | None = None,
        trusted_history_provenance: list[dict[str, str]] | None = None,
        memory_items: list[dict[str, Any]] | None = None,
        prepare_wait: Callable[[str], None] | None = None,
        wait_decision: Callable[[str], str | None] | None = None,
        publish_cb: Callable[[RuntimeEventEnvelope], None] | None = None,
        defer_permission: bool = False,
        _initial_state: AgentState | None = None,
        _step_seq: int = 1,
        _emit_run_started: bool = True,
    ) -> list[RuntimeEventEnvelope]:
        """通过 LangGraph 执行一次完整的 AgentRun。

        Args:
            job: RunJobMessage（含 task_id / run_id / user_goal / workspace_path）
            default_workspace_root: 默认 workspace 根目录（job.workspace_path 优先）
            cancel_check: 每个步骤前检查是否应取消，返回 True 表示已收到 cancel
            history_messages: 同一会话的历史消息 [{role, content}, ...]（多轮对话用）
            trusted_history_provenance: 最近完整历史 Run 的 Runtime 可信来源侧链

        Returns:
            RuntimeEventEnvelope 列表
        """
        ws_root = job.workspace_path or default_workspace_root
        state = _initial_state or AgentState(
            task_id=job.task_id,
            run_id=job.run_id,
            user_goal=job.user_goal,
            workspace_root=ws_root,
            history_messages=list(history_messages) if history_messages else [],
            trusted_history_provenance=(
                list(trusted_history_provenance) if trusted_history_provenance else []
            ),
            memory_items=list(memory_items) if memory_items else [],
        )
        state.next_step_seq = _step_seq
        self._run_supervisor.ensure_run_control(state)
        self._loop_controller.ensure_initialized(state)
        cancellation = self._run_supervisor.bind_cancellation(cancel_check)
        result = self._graph.invoke(
            {
                "job": job,
                "default_workspace_root": default_workspace_root,
                "cancel_check": cancellation.is_cancelled,
                "pause_check": pause_check,
                "history_messages": history_messages,
                "trusted_history_provenance": trusted_history_provenance,
                "memory_items": memory_items,
                "prepare_wait": prepare_wait,
                "wait_decision": wait_decision,
                "publish_cb": publish_cb,
                "defer_permission": defer_permission,
                "run_supervisor": self._run_supervisor,
                "state": state,
                "step_seq": _step_seq,
                "emit_run_started": _emit_run_started,
                "resume_node": "initialize_run",
                "turn": {},
                "envelopes": [],
            },
            {"recursion_limit": self._graph_recursion_limit},
        )
        envelopes = result["envelopes"]
        self._run_supervisor.validate_result(envelopes)
        return envelopes

    def resume_from_checkpoint(
        self,
        checkpoint: dict[str, Any],
        cancel_check: Callable[[], bool] | None = None,
        pause_check: Callable[[], str | None] | None = None,
        publish_cb: Callable[[RuntimeEventEnvelope], None] | None = None,
    ) -> list[RuntimeEventEnvelope]:
        """从 PostgreSQL Run checkpoint 恢复安全节点。"""
        validate_run_checkpoint(checkpoint)
        resume_node = str(checkpoint["resume_node"])
        if resume_node not in (
            "extract_intent",
            "call_model",
            "validate_action",
            "execute_tool",
        ):
            raise ValueError(f"checkpoint 节点不可自动恢复: {resume_node}")
        job = RunJobMessage.from_dict(dict(checkpoint["job"]))
        state = restore_agent_state(dict(checkpoint["state"]))
        self._run_supervisor.ensure_run_control(state)
        self._loop_controller.ensure_initialized(state)
        step_seq = int(checkpoint["next_step_seq"])
        turn: dict[str, Any] = {}
        if resume_node == "validate_action":
            saved_turn = dict(checkpoint["turn"])
            turn = {
                "action": AgentAction(**dict(saved_turn["action"])),
                "model_call_id": str(saved_turn["model_call_id"]),
                "model_step_id": str(saved_turn["model_step_id"]),
                "model_started_at": float(saved_turn["model_started_at"]),
            }
        elif resume_node == "execute_tool":
            saved_turn = dict(checkpoint["turn"])
            action = AgentAction(**dict(saved_turn["action"]))
            tool_request = ToolRequest(**dict(saved_turn["tool_request"]))
            _manifest, permission_check, assessment_error = self._tool_gateway.assess(tool_request)
            turn = {
                "action": action,
                "tool_request": tool_request,
                "tool_call_base": dict(saved_turn["tool_call_base"]),
                "tool_call_id": str(saved_turn["tool_call_id"]),
                "permission_check": permission_check,
                "assessment_error": assessment_error,
            }
        state.next_step_seq = step_seq
        cancellation = self._run_supervisor.bind_cancellation(cancel_check)
        result = self._graph.invoke(
            {
                "job": job,
                "default_workspace_root": state.workspace_root,
                "cancel_check": cancellation.is_cancelled,
                "pause_check": pause_check,
                "history_messages": state.history_messages,
                "trusted_history_provenance": state.trusted_history_provenance,
                "memory_items": state.memory_items,
                "prepare_wait": None,
                "wait_decision": None,
                "publish_cb": publish_cb,
                "defer_permission": True,
                "run_supervisor": self._run_supervisor,
                "state": state,
                "step_seq": step_seq,
                "emit_run_started": False,
                "resume_node": resume_node,
                "turn": turn,
                "envelopes": [],
            },
            {"recursion_limit": self._graph_recursion_limit},
        )
        envelopes = result["envelopes"]
        self._run_supervisor.validate_result(envelopes)
        return envelopes

    def _initialize_run(self, graph_state: AgentGraphState) -> AgentGraphUpdate:
        return self._lifecycle_phase.initialize_run(graph_state)

    def _extract_intent(self, graph_state: AgentGraphState) -> AgentGraphUpdate:
        """兼容入口；Intent 语义由独立 IntentExtractionPhase 拥有。"""
        return self._intent_extraction_phase.run(graph_state)

    def _call_model(self, graph_state: AgentGraphState) -> AgentGraphUpdate:
        """兼容入口；节点语义由独立 ModelCallPhase 拥有。"""
        return self._model_call_phase.run(graph_state)

    def _validate_action(self, graph_state: AgentGraphState) -> AgentGraphUpdate:
        """兼容入口；节点语义由独立 ActionValidationPhase 拥有。"""
        return self._action_validation_phase.run(graph_state)

    def _execute_tool(self, graph_state: AgentGraphState) -> AgentGraphUpdate:
        """兼容入口；权限与 effect 语义由独立 ToolExecutionPhase 拥有。"""
        return self._tool_execution_phase.run(graph_state)

    def _observe_result(self, graph_state: AgentGraphState) -> AgentGraphUpdate:
        """兼容入口；节点语义由独立 ObservationPhase 拥有。"""
        return self._observation_phase.run(graph_state)

    def _graph_update(
        self,
        graph_state: AgentGraphState,
        produced: list[RuntimeEventEnvelope],
        next_step_seq: int,
        turn: dict[str, Any],
    ) -> AgentGraphUpdate:
        return self._phase_runtime.graph_update(graph_state, produced, next_step_seq, turn)

    def _attach_checkpoint(
        self,
        envelope: RuntimeEventEnvelope,
        graph_state: AgentGraphState,
        next_step_seq: int,
        resume_node: str,
        *,
        turn: dict[str, Any] | None = None,
        state: AgentState | None = None,
    ) -> None:
        self._phase_runtime.attach_checkpoint(
            envelope,
            graph_state,
            next_step_seq,
            resume_node,
            turn=turn,
            state=state,
        )

    def _build_max_iterations_failure(self, graph_state: AgentGraphState) -> RuntimeEventEnvelope:
        return self._lifecycle_phase.build_max_iterations_failure(graph_state)

    def resume_permission(
        self,
        checkpoint: dict[str, Any],
        decision: str,
        cancel_check: Callable[[], bool] | None = None,
        publish_cb: Callable[[RuntimeEventEnvelope], None] | None = None,
    ) -> list[RuntimeEventEnvelope]:
        """从持久化权限检查点恢复 Agent loop。

        检查点只保存可信 ToolRequest 和 AgentState；审批命令必须先由
        PermissionApplicationService 校验为该 request 的终态，Worker 才会调用此方法。
        """
        validate_permission_checkpoint(checkpoint)

        job = RunJobMessage.from_dict(dict(checkpoint["job"]))
        state = restore_agent_state(dict(checkpoint["state"]))
        self._run_supervisor.ensure_run_control(state)
        self._loop_controller.ensure_initialized(state)
        tool_request = ToolRequest(**dict(checkpoint["tool_request"]))
        tool_call_base = dict(checkpoint["tool_call_base"])
        model_action = dict(checkpoint["model_action"])
        request_id = str(checkpoint["permission_request_id"])
        step_seq = int(checkpoint["next_step_seq"])
        trace_id, task_id, run_id = job.trace_id, job.task_id, job.run_id
        step_id = tool_request.step_id
        tool_call_id = str(tool_call_base["id"])
        envelopes: list[RuntimeEventEnvelope] = []

        cancellation = self._run_supervisor.bind_cancellation(cancel_check)
        if cancellation.is_cancelled():
            return [self._make_cancelled(trace_id, task_id, run_id)]

        permission_resolved = self._make_event(
            trace_id,
            build_runtime_event(
                event_type="permission.resolved",
                task_id=task_id,
                run_id=run_id,
                step_id=step_id,
                event_id=deterministic_event_id(run_id, "permission.resolved", step_seq),
                payload={
                    "request_id": request_id,
                    "decision": decision,
                    "tool_call_id": tool_call_id,
                },
            ),
        )
        envelopes.append(permission_resolved)
        step_seq += 1

        if decision == "allow_once":
            halt = self._run_supervisor.before_phase(state)
            if halt is not None:
                envelopes.append(
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
                return envelopes
            # 获批副作用必须先把不可恢复边界同步写入 PostgreSQL。若 Worker 在
            # ToolGateway 返回前消失，reconciliation 只能按 effect unknown 失败收口，
            # 不能从旧的 permission checkpoint 再次执行工具。
            attach_run_checkpoint(
                permission_resolved,
                build_run_checkpoint(
                    job=job,
                    state=state,
                    next_step_seq=step_seq,
                    resume_node="tool_in_flight",
                ),
            )
            if publish_cb is not None:
                publish_cb(permission_resolved)
            tool_call_base["permission_status"] = "approved"
            result = self._tool_gateway.execute(
                tool_request,
                approval=PermissionApproval(request_id=request_id, decision="allow_once"),
            )
        else:
            tool_call_base["permission_status"] = "denied"
            result = ToolResult(
                ok=False,
                summary="用户拒绝了权限请求",
                error={
                    "code": "PERMISSION_DENIED",
                    "message": "用户拒绝了此操作的权限请求",
                    "category": "permission",
                    "recoverable": False,
                },
            )

        observation: dict[str, Any] = {
            "tool_call_id": tool_call_id,
            "tool_name": tool_request.tool_name,
            "model_action": model_action,
            "ok": result.ok,
            "summary": result.summary,
        }
        if result.ok:
            observation["data"] = result.data
            if result.artifact_ids:
                observation["artifact_ids"] = list(result.artifact_ids)
            tool_finished = self._make_event(
                trace_id,
                build_runtime_event(
                    event_type="tool.call.finished",
                    task_id=task_id,
                    run_id=run_id,
                    step_id=step_id,
                    event_id=deterministic_event_id(run_id, "tool.call.finished", step_seq),
                    payload={
                        "tool_call": {
                            **tool_call_base,
                            "status": "completed",
                            "result": project_tool_result(result),
                        }
                    },
                ),
            )
            envelopes.append(tool_finished)
            step_seq += 1
            state.add_observation(observation)
            self._loop_controller.refresh_progress(state)
            state.source_chain_guard_rejections = 0
            state.source_chain_evidence_rejections = 0
            state.effect_guard_feedback = ""
            attach_run_checkpoint(
                tool_finished,
                build_run_checkpoint(
                    job=job,
                    state=state,
                    next_step_seq=step_seq,
                    resume_node="call_model",
                ),
            )
            envelopes.extend(
                self.run(
                    job,
                    cancel_check=cancellation.is_cancelled,
                    defer_permission=True,
                    _initial_state=state,
                    _step_seq=step_seq,
                    _emit_run_started=False,
                )
            )
            return envelopes

        error_info = result.error or {
            "code": "TOOL_FAILED",
            "message": result.summary,
            "category": "tool",
            "recoverable": False,
        }
        observation["error"] = error_info
        tool_failed = self._make_event(
            trace_id,
            build_runtime_event(
                event_type="tool.call.failed",
                task_id=task_id,
                run_id=run_id,
                step_id=step_id,
                event_id=deterministic_event_id(run_id, "tool.call.failed", step_seq),
                payload={
                    "tool_call": {
                        **tool_call_base,
                        "status": "failed",
                        "error": error_info,
                    }
                },
            ),
        )
        envelopes.append(tool_failed)
        step_seq += 1
        if bool(error_info.get("recoverable", False)):
            state.add_observation(observation)
            self._loop_controller.refresh_progress(state)
            state.source_chain_guard_rejections = 0
            state.source_chain_evidence_rejections = 0
            state.effect_guard_feedback = ""
            attach_run_checkpoint(
                tool_failed,
                build_run_checkpoint(
                    job=job,
                    state=state,
                    next_step_seq=step_seq,
                    resume_node="call_model",
                ),
            )
            envelopes.extend(
                self.run(
                    job,
                    cancel_check=cancellation.is_cancelled,
                    defer_permission=True,
                    _initial_state=state,
                    _step_seq=step_seq,
                    _emit_run_started=False,
                )
            )
            return envelopes
        envelopes.append(
            self._make_failed_event(
                trace_id,
                task_id,
                run_id,
                step_seq,
                code=str(error_info.get("code", "TOOL_FAILED")),
                message=str(error_info.get("message", result.summary)),
                category=str(error_info.get("category", "tool")),
                recoverable=False,
            )
        )
        return envelopes

    # -- 内部工具方法 --

    def _make_event(
        self,
        trace_id: str,
        event: dict,
    ) -> RuntimeEventEnvelope:
        """兼容入口；事件 envelope 由共享 PhaseRuntime 构造。"""
        return self._phase_runtime.make_event(trace_id, event)

    def _make_model_failed(
        self,
        trace_id: str,
        task_id: str,
        run_id: str,
        step_seq: int,
        model_call_id: str,
        model_step_id: str,
        t_start: float,
        error_code: str,
        recoverable: bool = False,
        *,
        output_failure_kind: str | None = None,
        attempt_count: int | None = None,
        purpose: str | None = None,
    ) -> RuntimeEventEnvelope:
        return self._phase_runtime.make_model_failed(
            trace_id,
            task_id,
            run_id,
            step_seq,
            model_call_id,
            model_step_id,
            t_start,
            error_code,
            provider_name=getattr(self._model, "provider_name", "unknown"),
            model_name=getattr(self._model, "model_name", "unknown"),
            recoverable=recoverable,
            output_failure_kind=output_failure_kind,
            attempt_count=attempt_count,
            purpose=purpose,
        )

    def _make_model_completed(
        self,
        trace_id: str,
        task_id: str,
        run_id: str,
        step_seq: int,
        model_call_id: str,
        model_step_id: str,
        t_start: float,
        action_type: str,
        *,
        purpose: str | None = None,
    ) -> RuntimeEventEnvelope:
        return self._phase_runtime.make_model_completed(
            trace_id,
            task_id,
            run_id,
            step_seq,
            model_call_id,
            model_step_id,
            t_start,
            action_type,
            provider_name=getattr(self._model, "provider_name", "unknown"),
            model_name=getattr(self._model, "model_name", "unknown"),
            purpose=purpose,
        )

    def _make_cancelled(self, trace_id: str, task_id: str, run_id: str) -> RuntimeEventEnvelope:
        return self._phase_runtime.make_cancelled(trace_id, task_id, run_id)

    def _make_paused(
        self,
        graph_state: AgentGraphState,
        step_seq: int,
        resume_node: str,
        *,
        turn: dict[str, Any] | None = None,
        event_id: str,
    ) -> RuntimeEventEnvelope:
        return self._phase_runtime.make_paused(
            graph_state,
            step_seq,
            resume_node,
            turn=turn,
            event_id=event_id,
        )

    def _make_failed_event(
        self,
        trace_id: str,
        task_id: str,
        run_id: str,
        step_seq: int,
        code: str,
        message: str,
        category: str = "runtime",
        recoverable: bool = False,
    ) -> RuntimeEventEnvelope:
        return self._phase_runtime.make_failed_event(
            trace_id,
            task_id,
            run_id,
            step_seq,
            code,
            message,
            category,
            recoverable,
        )
