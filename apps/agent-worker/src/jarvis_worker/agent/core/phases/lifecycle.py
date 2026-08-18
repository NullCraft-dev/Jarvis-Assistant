"""运行起点与最大迭代终态的生命周期 phase。"""

from __future__ import annotations

from jarvis_worker.agent.core.graph_state import AgentGraphState, AgentGraphUpdate
from jarvis_worker.agent.core.phases.runtime import PhaseRuntime
from jarvis_worker.runtime.events import build_runtime_event, deterministic_event_id
from jarvis_worker.runtime_bus.messages import RuntimeEventEnvelope


class RunLifecyclePhase:
    """拥有生命周期事件语义；不拥有 LangGraph 条件路由。"""

    def __init__(self, *, worker_id: str, max_iterations: int, runtime: PhaseRuntime) -> None:
        self._worker_id = worker_id
        self._max_iterations = max_iterations
        self._runtime = runtime

    def initialize_run(self, graph_state: AgentGraphState) -> AgentGraphUpdate:
        if not graph_state["emit_run_started"]:
            return {}
        job = graph_state["job"]
        step_seq = graph_state["step_seq"]
        started = self._runtime.make_event(
            job.trace_id,
            build_runtime_event(
                event_type="agent.run.started",
                task_id=job.task_id,
                run_id=job.run_id,
                event_id=deterministic_event_id(job.run_id, "agent.run.started", step_seq),
                payload={"agent_id": self._worker_id, "mode": "single_agent"},
            ),
        )
        next_step_seq = step_seq + 1
        resume_node = (
            "call_model" if graph_state["state"].intent is not None else "extract_intent"
        )
        self._runtime.attach_checkpoint(started, graph_state, next_step_seq, resume_node)
        produced = [started]
        if graph_state["publish_cb"] is not None:
            graph_state["publish_cb"](started)
        if graph_state["cancel_check"] and graph_state["cancel_check"]():
            produced.append(
                self._runtime.make_cancelled(job.trace_id, job.task_id, job.run_id)
            )
        elif graph_state["pause_check"] and (pause_id := graph_state["pause_check"]()):
            produced.append(
                self._runtime.make_paused(
                    graph_state, next_step_seq, resume_node, event_id=pause_id
                )
            )
        graph_state["state"].next_step_seq = next_step_seq
        return {
            "step_seq": next_step_seq,
            "emit_run_started": False,
            "envelopes": [*graph_state["envelopes"], *produced],
        }

    def build_max_iterations_failure(
        self, graph_state: AgentGraphState
    ) -> RuntimeEventEnvelope:
        state = graph_state["state"]
        job = graph_state["job"]
        state.final_output = f"达到最大迭代次数 ({self._max_iterations})，任务未完成。"
        return self._runtime.make_event(
            job.trace_id,
            build_runtime_event(
                event_type="agent.run.failed",
                task_id=job.task_id,
                run_id=job.run_id,
                event_id=deterministic_event_id(
                    job.run_id, "agent.run.failed", graph_state["step_seq"]
                ),
                payload={
                    "error": {
                        "code": "MAX_ITERATIONS",
                        "message": state.final_output,
                        "category": "runtime",
                        "recoverable": False,
                    }
                },
            ),
        )
