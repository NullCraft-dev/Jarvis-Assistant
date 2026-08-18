"""LangGraph 的 Worker 内部状态契约。

此 TypedDict 仅在进程内的 StateGraph 传递控制信息；它不进入 Redis、
PostgreSQL、RuntimeEvent 或 Web DTO。项目可恢复状态的 owner 仍是 AgentState
及其持久化投影。
"""

from __future__ import annotations

from typing import Any, Callable, TypedDict

from jarvis_worker.agent.core.state import AgentState
from jarvis_worker.agent.harness import RunSupervisor
from jarvis_worker.runtime_bus.messages import RunJobMessage, RuntimeEventEnvelope


class AgentGraphState(TypedDict):
    """单 Agent LangGraph 的内部运行态。"""

    job: RunJobMessage
    default_workspace_root: str
    cancel_check: Callable[[], bool] | None
    pause_check: Callable[[], str | None] | None
    history_messages: list[dict[str, str]] | None
    trusted_history_provenance: list[dict[str, str]] | None
    memory_items: list[dict[str, Any]] | None
    prepare_wait: Callable[[str], None] | None
    wait_decision: Callable[[str], str | None] | None
    publish_cb: Callable[[RuntimeEventEnvelope], None] | None
    defer_permission: bool
    run_supervisor: RunSupervisor
    state: AgentState
    step_seq: int
    emit_run_started: bool
    resume_node: str
    turn: dict[str, Any]
    envelopes: list[RuntimeEventEnvelope]


class AgentGraphUpdate(TypedDict, total=False):
    """节点返回给 LangGraph 的有界状态变更。"""

    state: AgentState
    step_seq: int
    emit_run_started: bool
    turn: dict[str, Any]
    envelopes: list[RuntimeEventEnvelope]
