"""LangGraph execute_tool 节点的权限状态机与 ToolGateway effect 阶段。"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from jarvis_worker.agent.core.actions import AgentAction
from jarvis_worker.agent.core.checkpoint import build_permission_checkpoint
from jarvis_worker.agent.core.graph_state import AgentGraphState, AgentGraphUpdate
from jarvis_worker.agent.core.phases.runtime import PhaseRuntime
from jarvis_worker.agent.tool_gateway.contracts import (
    PermissionApproval,
    ToolRequest,
    ToolResult,
)
from jarvis_worker.agent.tool_gateway.gateway import ToolGateway
from jarvis_worker.runtime.events import build_runtime_event, deterministic_event_id
from jarvis_worker.runtime_bus.messages import RuntimeEventEnvelope

log = logging.getLogger("jarvis_worker.agent_runner")


def _build_permission_reason(tool_name: str, model_reason: str, fallback_reason: str) -> str:
    fixed_reasons = {
        "workspace.create_file": "需要用户确认后才能创建文件",
        "workspace.create_directory": "需要用户确认后才能创建目录",
        "workspace.move_path": "需要用户确认后才能移动路径",
        "workspace.delete_path": "删除路径属于高风险操作，需要用户确认",
        "literature.download_arxiv_pdf": "需要用户确认后才能把论文 PDF 保存到本地 Artifact Store",
        "rag.ingest_artifact": "需要用户确认后才能将受控 PDF 加入 RAG 摄取与向量化队列",
    }
    return fixed_reasons.get(tool_name, model_reason or fallback_reason)


def _build_permission_action_summary(
    tool_name: str, arguments_summary: dict[str, Any]
) -> str:
    if tool_name == "workspace.create_file":
        path = arguments_summary.get("path", "unknown")
        content_info = arguments_summary.get("content", {})
        if isinstance(content_info, dict):
            size = content_info.get("size_bytes", "?")
            return f"创建新文件: {path} ({size} bytes, 不覆盖已有文件)"
        return f"创建新文件: {path}"
    if tool_name == "literature.download_arxiv_pdf":
        return f"下载 arXiv PDF: {arguments_summary.get('arxiv_id', 'unknown')}"
    if tool_name == "rag.ingest_artifact":
        return f"将 PDF Artifact 加入 RAG: {arguments_summary.get('artifact_id', 'unknown')}"
    if tool_name == "workspace.create_directory":
        return f"创建新目录: {arguments_summary.get('path', 'unknown')} (不覆盖已有路径)"
    if tool_name == "workspace.move_path":
        source = arguments_summary.get("source_path", "unknown")
        destination = arguments_summary.get("destination_path", "unknown")
        return f"移动路径: {source} -> {destination} (不覆盖已有路径)"
    if tool_name == "workspace.delete_path":
        return f"删除路径: {arguments_summary.get('path', 'unknown')} (不递归删除非空目录)"
    path = arguments_summary.get("path")
    if isinstance(path, str) and path:
        return f"{tool_name}: {path[:240]}"
    return f"执行工具: {tool_name}"


class ToolExecutionPhase:
    """唯一允许通过 ToolGateway.execute 发生 effect 的 LangGraph phase。"""

    def __init__(self, *, tool_gateway: ToolGateway, runtime: PhaseRuntime) -> None:
        self._tool_gateway = tool_gateway
        self._runtime = runtime

    def _make_event(self, trace_id: str, event: dict) -> RuntimeEventEnvelope:
        return self._runtime.make_event(trace_id, event)

    def _make_cancelled(self, *args: str) -> RuntimeEventEnvelope:
        return self._runtime.make_cancelled(*args)

    def _make_paused(self, *args: Any, **kwargs: Any) -> RuntimeEventEnvelope:
        return self._runtime.make_paused(*args, **kwargs)

    def _graph_update(
        self, graph_state: AgentGraphState, produced: list[RuntimeEventEnvelope],
        next_step_seq: int, turn: dict[str, Any],
    ) -> AgentGraphUpdate:
        return self._runtime.graph_update(graph_state, produced, next_step_seq, turn)

    def _attach_checkpoint(self, *args: Any, **kwargs: Any) -> None:
        self._runtime.attach_checkpoint(*args, **kwargs)

    def run(self, graph_state: AgentGraphState) -> AgentGraphUpdate:
        """图节点：通过 ToolGateway 与 PermissionManager 发生已校验 effect。"""
        job, state, turn = graph_state["job"], graph_state["state"], graph_state["turn"]
        action: AgentAction = turn["action"]
        tool_request: ToolRequest = turn["tool_request"]
        tool_call_base = dict(turn["tool_call_base"])
        permission_check = turn["permission_check"]
        assessment_error = turn["assessment_error"]
        step_seq = graph_state["step_seq"]
        trace_id, task_id, run_id, step_id = (
            job.trace_id,
            job.task_id,
            job.run_id,
            tool_request.step_id,
        )
        tool_call_id = str(turn["tool_call_id"])
        produced: list[RuntimeEventEnvelope] = []
        if graph_state["cancel_check"] and graph_state["cancel_check"]():
            produced.append(self._make_cancelled(trace_id, task_id, run_id))
            return self._graph_update(graph_state, produced, step_seq, {})
        if graph_state["pause_check"] and (pause_id := graph_state["pause_check"]()):
            produced.append(
                self._make_paused(
                    graph_state,
                    step_seq,
                    "execute_tool",
                    turn=turn,
                    event_id=pause_id,
                )
            )
            return self._graph_update(graph_state, produced, step_seq, turn)
        halt = graph_state["run_supervisor"].before_phase(state)
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
            return self._graph_update(graph_state, produced, step_seq, {})

        tool_started = self._make_event(
            trace_id,
            build_runtime_event(
                event_type="tool.call.started",
                task_id=task_id,
                run_id=run_id,
                step_id=step_id,
                event_id=deterministic_event_id(run_id, "tool.call.started", step_seq),
                payload={
                    "tool_call": {
                        **tool_call_base,
                        "status": "pending"
                        if permission_check is not None and permission_check.needs_user_approval
                        else "running",
                    }
                },
            ),
        )
        self._attach_checkpoint(tool_started, graph_state, step_seq + 1, "tool_in_flight")
        produced.append(tool_started)
        if graph_state["publish_cb"] is not None:
            graph_state["publish_cb"](tool_started)
        step_seq += 1
        if graph_state["cancel_check"] and graph_state["cancel_check"]():
            produced.append(self._make_cancelled(trace_id, task_id, run_id))
            return self._graph_update(graph_state, produced, step_seq, {})

        if permission_check is not None and permission_check.needs_user_approval:
            request_id = deterministic_event_id(run_id, "permission.request", step_seq)
            log.info(
                "Permission 等待用户决定: tool=%s risk=%s request_id=%s tool_call_id=%s",
                action.tool_name,
                permission_check.risk_level,
                request_id,
                tool_call_id,
                extra={"step_id": step_id},
            )
            tool_call_base["permission_request_id"] = request_id
            summary = tool_call_base["arguments_summary"]
            if action.tool_name.startswith("knowledge."):
                permission_scope = {
                    "type": "once",
                    "tool_name": action.tool_name,
                    "vault_id": summary.get("vault_id", "active_jarvis_vault"),
                }
            elif tool_call_base.get("provider") == "mcp":
                permission_scope = {
                    "type": "once",
                    "tool_name": action.tool_name,
                    "mcp_server_id": tool_call_base.get("mcp_server_id"),
                }
            elif action.tool_name == "literature.download_arxiv_pdf":
                permission_scope = {
                    "type": "once",
                    "tool_name": action.tool_name,
                    "source": "arxiv",
                    "arxiv_id": summary.get("arxiv_id", ""),
                    "destination": "artifact_store",
                }
            elif action.tool_name == "rag.ingest_artifact":
                permission_scope = {
                    "type": "once",
                    "tool_name": action.tool_name,
                    "artifact_id": summary.get("artifact_id", ""),
                    "destination": "current_workspace_rag",
                }
            else:
                permission_scope = {
                    "type": "once",
                    "workspace_path": job.workspace_path or graph_state["default_workspace_root"],
                    "path": summary.get("path", summary.get("source_path", "")),
                    "source_path": summary.get("source_path", ""),
                    "destination_path": summary.get("destination_path", ""),
                    "tool_name": action.tool_name,
                }
            request: dict[str, Any] = {
                "id": request_id,
                "task_id": task_id,
                "run_id": run_id,
                "step_id": step_id,
                "tool_call_id": tool_call_id,
                "tool_name": action.tool_name,
                "action_summary": _build_permission_action_summary(action.tool_name, summary),
                "reason": _build_permission_reason(
                    action.tool_name, action.reason, permission_check.reason
                ),
                "risk_level": permission_check.risk_level,
                "scope": permission_scope,
                "arguments_summary": summary,
                "allowed_decisions": permission_check.allowed_decisions,
                "created_at": datetime.now(timezone.utc).isoformat(),
            }
            if graph_state["defer_permission"]:
                request["_internal_checkpoint"] = build_permission_checkpoint(
                    job=job,
                    state=state,
                    next_step_seq=step_seq + 1,
                    permission_request_id=request_id,
                    tool_request=tool_request,
                    tool_call_base=tool_call_base,
                    model_action={
                        "action_type": action.action_type,
                        "tool_name": action.tool_name,
                        "arguments": {
                            key: value
                            for key, value in action.arguments.items()
                            if key != "workspace_root"
                        },
                        "reason": action.reason,
                    },
                )
            produced.append(
                self._make_event(
                    trace_id,
                    build_runtime_event(
                        event_type="permission.required",
                        task_id=task_id,
                        run_id=run_id,
                        step_id=step_id,
                        event_id=deterministic_event_id(run_id, "permission.required", step_seq),
                        payload={"request": request},
                    ),
                )
            )
            step_seq += 1
            if graph_state["defer_permission"]:
                return self._graph_update(graph_state, produced, step_seq, {})
            if graph_state["prepare_wait"] is not None:
                graph_state["prepare_wait"](request_id)
            if graph_state["publish_cb"] is not None:
                # tool.call.started 已在 effect 前同步持久化；这里只发布新产生的
                # permission.required，避免重复触发持久化回调。
                graph_state["publish_cb"](produced[-1])
            decision = (
                graph_state["wait_decision"](request_id) if graph_state["wait_decision"] else None
            )
            if graph_state["cancel_check"] and graph_state["cancel_check"]():
                produced.append(self._make_cancelled(trace_id, task_id, run_id))
                return self._graph_update(graph_state, produced, step_seq, {})
            if decision is None:
                log.warning(
                    "Permission 等待超时: tool=%s request_id=%s",
                    action.tool_name,
                    request_id,
                    extra={"step_id": step_id},
                )
                produced.append(
                    self._make_event(
                        trace_id,
                        build_runtime_event(
                            event_type="permission.expired",
                            task_id=task_id,
                            run_id=run_id,
                            step_id=step_id,
                            event_id=deterministic_event_id(run_id, "permission.expired", step_seq),
                            payload={
                                "request_id": request_id,
                                "tool_call_id": tool_call_id,
                                "reason": "timeout",
                            },
                        ),
                    )
                )
                step_seq += 1
                tool_call_base["permission_status"] = "expired"
                result = ToolResult(
                    ok=False,
                    summary="等待用户授权超时",
                    error={
                        "code": "PERMISSION_TIMEOUT",
                        "message": "等待用户授权超时",
                        "category": "permission",
                        "recoverable": False,
                    },
                )
            else:
                log.info(
                    "Permission 已决定: tool=%s request_id=%s decision=%s",
                    action.tool_name,
                    request_id,
                    decision,
                    extra={"step_id": step_id},
                )
                produced.append(
                    self._make_event(
                        trace_id,
                        build_runtime_event(
                            event_type="permission.resolved",
                            task_id=task_id,
                            run_id=run_id,
                            step_id=step_id,
                            event_id=deterministic_event_id(
                                run_id, "permission.resolved", step_seq
                            ),
                            payload={
                                "request_id": request_id,
                                "decision": decision,
                                "tool_call_id": tool_call_id,
                            },
                        ),
                    )
                )
                step_seq += 1
                if decision == "allow_once":
                    halt = graph_state["run_supervisor"].before_phase(state)
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
                        return self._graph_update(graph_state, produced, step_seq, {})
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
                            "message": "用户拒绝了权限请求",
                            "category": "permission",
                            "recoverable": False,
                        },
                    )
        elif assessment_error is not None:
            result = assessment_error
        else:
            result = self._tool_gateway.execute(tool_request)
        return self._graph_update(
            graph_state,
            produced,
            step_seq,
            {**turn, "tool_call_base": tool_call_base, "tool_result": result},
        )
