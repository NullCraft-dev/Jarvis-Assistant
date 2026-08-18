"""ToolResult 到 RuntimeEvent 与 AgentState observation 的唯一投影阶段。"""

from __future__ import annotations

import logging
from dataclasses import asdict
from typing import Any, cast

from jarvis_worker.agent.core.actions import AgentAction
from jarvis_worker.agent.core.graph_state import AgentGraphState, AgentGraphUpdate
from jarvis_worker.agent.core.phases.runtime import PhaseRuntime
from jarvis_worker.agent.loop import LoopController
from jarvis_worker.agent.tool_gateway.contracts import ToolRequest, ToolResult
from jarvis_worker.runtime.events import build_runtime_event, deterministic_event_id
from jarvis_worker.runtime_bus.messages import RuntimeEventEnvelope
from jarvis_worker.shared.security import redact_credentials

log = logging.getLogger("jarvis_worker.agent_runner")


def _redact_tool_result_value(value: Any, *, depth: int = 0) -> Any:
    """在 ToolResult 进入 Observation/Event/Checkpoint 前移除高置信度凭据。"""
    if depth > 20:
        return None
    if isinstance(value, str):
        return redact_credentials(value)
    if isinstance(value, dict):
        return {
            key: _redact_tool_result_value(item, depth=depth + 1)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_redact_tool_result_value(item, depth=depth + 1) for item in value]
    if isinstance(value, tuple):
        return tuple(_redact_tool_result_value(item, depth=depth + 1) for item in value)
    return value


def project_tool_result(result: ToolResult) -> dict[str, Any]:
    """把内部 ToolResult 投影为可持久化、供应商无关的公开结果。"""
    payload: dict[str, Any] = {
        "kind": result.kind,
        "summary": redact_credentials(result.summary),
        "data": _redact_tool_result_value(result.data),
    }
    if result.artifact_ids:
        payload["artifact_ids"] = list(result.artifact_ids)
    if result.deliverables:
        payload["deliverables"] = [
            _redact_tool_result_value(asdict(item)) for item in result.deliverables
        ]
    return payload


class ObservationPhase:
    """拥有 observe_result 节点语义；不具备工具执行能力。"""

    def __init__(
        self,
        runtime: PhaseRuntime,
        *,
        loop_controller: LoopController,
    ) -> None:
        self._runtime = runtime
        self._loop_controller = loop_controller

    def run(self, graph_state: AgentGraphState) -> AgentGraphUpdate:
        job, state, turn = graph_state["job"], graph_state["state"], graph_state["turn"]
        action = cast(AgentAction, turn["action"])
        tool_request = cast(ToolRequest, turn["tool_request"])
        result = cast(ToolResult, turn["tool_result"])
        step_seq = graph_state["step_seq"]
        trace_id, task_id, run_id = job.trace_id, job.task_id, job.run_id
        model_action = {
            "action_type": action.action_type,
            "tool_name": action.tool_name,
            "arguments": {
                key: value for key, value in action.arguments.items() if key != "workspace_root"
            },
            "reason": action.reason,
        }
        observation: dict[str, Any] = {
            "tool_call_id": turn["tool_call_id"],
            "tool_name": tool_request.tool_name,
            "model_action": model_action,
            "ok": result.ok,
            "summary": redact_credentials(result.summary),
        }
        produced: list[RuntimeEventEnvelope] = []
        if result.ok:
            log.info(
                "Tool 调用完成: tool=%s tool_call_id=%s ok=true",
                tool_request.tool_name,
                turn["tool_call_id"],
                extra={"step_id": tool_request.step_id},
            )
            safe_data = _redact_tool_result_value(result.data)
            observation["data"] = safe_data
            if result.artifact_ids:
                observation["artifact_ids"] = list(result.artifact_ids)
            payload: dict[str, Any] = {
                "tool_call": {
                    **turn["tool_call_base"],
                    "status": "completed",
                    "result": project_tool_result(result),
                }
            }
            if isinstance(safe_data, dict):
                entries = safe_data.get("entries", [])
                if entries:
                    entries_summary = [
                        {"name": entry["name"], "type": entry["type"]} for entry in entries[:20]
                    ]
                    payload["entries_summary"] = entries_summary
                    payload["entries_count"] = len(entries_summary)
                content = safe_data.get("content")
                if content:
                    payload["content_summary"] = {
                        "path": safe_data.get("path", ""),
                        "size_bytes": safe_data.get("size_bytes", 0),
                        "chars_read": safe_data.get("chars_read", 0),
                        "truncated": safe_data.get("truncated", False),
                        "preview": content[:500] + ("..." if len(content) > 500 else ""),
                    }
            tool_finished = self._runtime.make_event(
                trace_id,
                build_runtime_event(
                    event_type="tool.call.finished",
                    task_id=task_id,
                    run_id=run_id,
                    step_id=tool_request.step_id,
                    event_id=deterministic_event_id(run_id, "tool.call.finished", step_seq),
                    payload=payload,
                ),
            )
            produced.append(tool_finished)
            step_seq += 1
            state.add_observation(observation)
            self._loop_controller.refresh_progress(state)
            state.source_chain_guard_rejections = 0
            state.source_chain_evidence_rejections = 0
            state.effect_guard_feedback = ""
            self._runtime.attach_checkpoint(
                tool_finished, graph_state, step_seq, "call_model", state=state
            )
            if graph_state["publish_cb"] is not None:
                graph_state["publish_cb"](tool_finished)
            return self._runtime.graph_update(graph_state, produced, step_seq, {})

        error = _redact_tool_result_value(result.error) if result.error else {
            "code": "TOOL_FAILED",
            "message": redact_credentials(result.summary),
            "category": "tool",
            "recoverable": False,
        }
        log.warning(
            "Tool 调用失败: tool=%s tool_call_id=%s code=%s recoverable=%s",
            tool_request.tool_name,
            turn["tool_call_id"],
            error.get("code", "TOOL_FAILED"),
            error.get("recoverable", False),
            extra={"step_id": tool_request.step_id},
        )
        observation["error"] = error
        tool_failed = self._runtime.make_event(
            trace_id,
            build_runtime_event(
                event_type="tool.call.failed",
                task_id=task_id,
                run_id=run_id,
                step_id=tool_request.step_id,
                event_id=deterministic_event_id(run_id, "tool.call.failed", step_seq),
                payload={
                    "tool_call": {**turn["tool_call_base"], "status": "failed", "error": error}
                },
            ),
        )
        produced.append(tool_failed)
        step_seq += 1
        if bool(error.get("recoverable", False)):
            state.add_observation(observation)
            self._loop_controller.refresh_progress(state)
            state.source_chain_guard_rejections = 0
            state.source_chain_evidence_rejections = 0
            state.effect_guard_feedback = ""
            self._runtime.attach_checkpoint(
                tool_failed, graph_state, step_seq, "call_model", state=state
            )
            if graph_state["publish_cb"] is not None:
                graph_state["publish_cb"](tool_failed)
            return self._runtime.graph_update(graph_state, produced, step_seq, {})
        produced.append(
            self._runtime.make_failed_event(
                trace_id,
                task_id,
                run_id,
                step_seq,
                code=str(error.get("code", "TOOL_FAILED")),
                message=str(error.get("message", result.summary)),
                category=str(error.get("category", "tool")),
                recoverable=bool(error.get("recoverable", False)),
            )
        )
        return self._runtime.graph_update(graph_state, produced, step_seq, {})
