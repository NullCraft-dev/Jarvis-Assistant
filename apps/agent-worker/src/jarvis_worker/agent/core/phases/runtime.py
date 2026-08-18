"""Phase service 共用的图游标、checkpoint 与事件边界。"""

from __future__ import annotations

import time
from typing import Any
from uuid import NAMESPACE_URL, uuid5

from jarvis_worker.agent.core.checkpoint import attach_run_checkpoint, build_run_checkpoint
from jarvis_worker.agent.core.graph_state import AgentGraphState, AgentGraphUpdate
from jarvis_worker.agent.core.state import AgentState
from jarvis_worker.runtime.events import (
    build_envelope,
    build_runtime_event,
    deterministic_event_id,
)
from jarvis_worker.runtime_bus.messages import RuntimeEventEnvelope


class PhaseRuntime:
    """只提供 phase 共享的项目 Runtime 原语，不执行模型或工具。"""

    def __init__(self, worker_id: str) -> None:
        self._worker_id = worker_id

    def make_event(self, trace_id: str, event: dict[str, Any]) -> RuntimeEventEnvelope:
        envelope = build_envelope(event, trace_id, self._worker_id)
        envelope.validate()
        return envelope

    def make_failed_event(
        self,
        trace_id: str,
        task_id: str,
        run_id: str,
        step_seq: int,
        code: str,
        message: str,
        category: str = "runtime",
        recoverable: bool = False,
        details: dict[str, Any] | None = None,
    ) -> RuntimeEventEnvelope:
        return self.make_event(
            trace_id,
            build_runtime_event(
                event_type="agent.run.failed",
                task_id=task_id,
                run_id=run_id,
                event_id=deterministic_event_id(run_id, "agent.run.failed", step_seq),
                payload={
                    "error": {
                        "code": code,
                        "message": message,
                        "category": category,
                        "recoverable": recoverable,
                        **({"details": details} if details is not None else {}),
                    }
                },
            ),
        )

    def make_model_failed(
        self,
        trace_id: str,
        task_id: str,
        run_id: str,
        step_seq: int,
        model_call_id: str,
        model_step_id: str,
        started_at: float,
        error_code: str,
        *,
        provider_name: str,
        model_name: str,
        recoverable: bool = False,
        output_failure_kind: str | None = None,
        attempt_count: int | None = None,
        purpose: str | None = None,
        validation: dict[str, Any] | None = None,
        navigation_guard: dict[str, Any] | None = None,
    ) -> RuntimeEventEnvelope:
        return self.make_event(
            trace_id,
            build_runtime_event(
                event_type="model.call.failed",
                task_id=task_id,
                run_id=run_id,
                step_id=model_step_id,
                event_id=deterministic_event_id(run_id, "model.call.failed", step_seq),
                payload={
                    "provider": provider_name,
                    "model_name": model_name,
                    "call_id": model_call_id,
                    **({"purpose": purpose} if purpose is not None else {}),
                    "duration_ms": int((time.monotonic() - started_at) * 1000),
                    "error_code": error_code,
                    "recoverable": recoverable,
                    **(
                        {"output_failure_kind": output_failure_kind}
                        if output_failure_kind is not None
                        else {}
                    ),
                    **({"attempt_count": attempt_count} if attempt_count is not None else {}),
                    **({"validation": validation} if validation is not None else {}),
                    **(
                        {"navigation_guard": navigation_guard}
                        if navigation_guard is not None
                        else {}
                    ),
                },
            ),
        )

    def make_model_completed(
        self,
        trace_id: str,
        task_id: str,
        run_id: str,
        step_seq: int,
        model_call_id: str,
        model_step_id: str,
        started_at: float,
        action_type: str,
        *,
        provider_name: str,
        model_name: str,
        purpose: str | None = None,
    ) -> RuntimeEventEnvelope:
        return self.make_event(
            trace_id,
            build_runtime_event(
                event_type="model.call.completed",
                task_id=task_id,
                run_id=run_id,
                step_id=model_step_id,
                event_id=deterministic_event_id(run_id, "model.call.completed", step_seq),
                payload={
                    "provider": provider_name,
                    "model_name": model_name,
                    "call_id": model_call_id,
                    **({"purpose": purpose} if purpose is not None else {}),
                    "duration_ms": int((time.monotonic() - started_at) * 1000),
                    "finish_reason": None,
                    "action_type": action_type,
                },
            ),
        )

    def make_cancelled(
        self, trace_id: str, task_id: str, run_id: str
    ) -> RuntimeEventEnvelope:
        return self.make_event(
            trace_id,
            build_runtime_event(
                event_type="agent.run.cancelled",
                task_id=task_id,
                run_id=run_id,
                event_id=deterministic_event_id(run_id, "agent.run.cancelled", 99),
                payload={"run_id": run_id, "reason": "cancelled_by_user"},
            ),
        )

    def make_paused(
        self,
        graph_state: AgentGraphState,
        step_seq: int,
        resume_node: str,
        *,
        event_id: str,
        turn: dict[str, Any] | None = None,
    ) -> RuntimeEventEnvelope:
        job = graph_state["job"]
        envelope = self.make_event(
            job.trace_id,
            build_runtime_event(
                event_type="agent.run.paused",
                task_id=job.task_id,
                run_id=job.run_id,
                event_id=str(uuid5(NAMESPACE_URL, f"jarvis:{event_id}:agent.run.paused")),
                payload={
                    "run_id": job.run_id,
                    "reason": "paused_by_user",
                    "resume_node": resume_node,
                },
            ),
        )
        self.attach_checkpoint(
            envelope, graph_state, step_seq, resume_node, turn=turn
        )
        return envelope

    @staticmethod
    def graph_update(
        graph_state: AgentGraphState,
        produced: list[RuntimeEventEnvelope],
        next_step_seq: int,
        turn: dict[str, Any],
    ) -> AgentGraphUpdate:
        graph_state["state"].next_step_seq = next_step_seq
        return {
            "state": graph_state["state"],
            "step_seq": next_step_seq,
            "emit_run_started": False,
            "turn": turn,
            "envelopes": [*graph_state["envelopes"], *produced],
        }

    @staticmethod
    def attach_checkpoint(
        envelope: RuntimeEventEnvelope,
        graph_state: AgentGraphState,
        next_step_seq: int,
        resume_node: str,
        *,
        turn: dict[str, Any] | None = None,
        state: AgentState | None = None,
    ) -> None:
        checkpoint = build_run_checkpoint(
            job=graph_state["job"],
            state=state or graph_state["state"],
            next_step_seq=next_step_seq,
            resume_node=resume_node,
            turn=turn,
        )
        attach_run_checkpoint(envelope, checkpoint)
