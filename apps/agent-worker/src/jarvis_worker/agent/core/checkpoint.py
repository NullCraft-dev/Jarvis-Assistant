"""AgentRun 的 PostgreSQL checkpoint 契约。

该结构只允许存在于 Worker 内存、内部 RuntimeEvent 和 PostgreSQL；持久化边界会在
写 RuntimeEvent/Outbox 前剥离它。LangGraph checkpointer 不是业务真源。
"""

from __future__ import annotations

from dataclasses import asdict, fields
from typing import Any

from jarvis_worker.agent.core.actions import AgentAction
from jarvis_worker.agent.core.state import AgentState
from jarvis_worker.agent.harness import RunControlState
from jarvis_worker.agent.intents import IntentExtraction, IntentRuntimeContext
from jarvis_worker.agent.loop.contracts import (
    CompletionContract,
    LoopProgressSnapshot,
    StopDecision,
)
from jarvis_worker.agent.tool_gateway.contracts import ToolRequest
from jarvis_worker.runtime_bus.messages import RunJobMessage, RuntimeEventEnvelope

# Version 5 persists Runtime-owned completion/progress/stop state. Version 4
# remains read-only compatible and is upgraded in memory before the next write.
RUN_CHECKPOINT_VERSION = 5
LEGACY_RUN_CHECKPOINT_VERSIONS = frozenset({4})
MAX_RUN_RECOVERY_ATTEMPTS = 3
RESUMABLE_NODES = frozenset(
    {"extract_intent", "call_model", "validate_action", "execute_tool"}
)
NON_RESUMABLE_NODES = frozenset({"tool_in_flight"})
_INTERNAL_KEY = "run_checkpoint"
_LEGACY_AGENT_STATE_FIELDS = frozenset({"skill_workflow_stage"})


def restore_agent_state(raw_state: dict[str, Any]) -> AgentState:
    """Restore a persisted AgentState while narrowly tolerating retired fields.

    Checkpoints are durable data and may outlive the code that created them.  We
    only discard explicitly retired tombstones; any other unknown field remains
    invalid so a corrupted or forged checkpoint still fails closed.
    """
    if not isinstance(raw_state, dict):
        raise ValueError("run checkpoint state 非法")
    allowed = {item.name for item in fields(AgentState)}
    unknown = set(raw_state) - allowed
    unsupported = unknown - _LEGACY_AGENT_STATE_FIELDS
    if unsupported:
        raise ValueError("run checkpoint state 包含未知字段: " + ", ".join(sorted(unsupported)))
    normalized = {key: value for key, value in raw_state.items() if key in allowed}
    try:
        return AgentState(**normalized)
    except (TypeError, ValueError) as exc:
        raise ValueError("run checkpoint state 非法") from exc


def build_run_checkpoint(
    *,
    job: RunJobMessage,
    state: AgentState,
    next_step_seq: int,
    resume_node: str,
    turn: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if resume_node not in RESUMABLE_NODES | NON_RESUMABLE_NODES:
        raise ValueError(f"不支持的 run checkpoint node: {resume_node}")
    if state.run_control is None:
        raise ValueError("checkpoint v5 缺少 Run control state")
    RunControlState.from_state_dict(state.run_control)
    if state.intent is not None:
        if (
            state.completion_contract is None
            or state.loop_progress is None
            or state.stop_decision is None
        ):
            raise ValueError("checkpoint v5 缺少 Loop 控制状态")
        CompletionContract.from_state_dict(state.completion_contract)
        LoopProgressSnapshot.from_state_dict(state.loop_progress)
        StopDecision.from_state_dict(state.stop_decision)
    checkpoint: dict[str, Any] = {
        "version": RUN_CHECKPOINT_VERSION,
        "resume_node": resume_node,
        "job": job.to_dict(),
        "state": asdict(state),
        "next_step_seq": next_step_seq,
    }
    if resume_node == "validate_action":
        if turn is None:
            raise ValueError("validate_action checkpoint 缺少 turn")
        action = turn.get("action")
        model_call_id = turn.get("model_call_id")
        model_step_id = turn.get("model_step_id")
        model_started_at = turn.get("model_started_at")
        if (
            not isinstance(action, AgentAction)
            or not isinstance(model_call_id, str)
            or not model_call_id
            or not isinstance(model_step_id, str)
            or not model_step_id
            or not isinstance(model_started_at, (int, float))
        ):
            raise ValueError("validate_action checkpoint 缺少可信 model action")
        checkpoint["turn"] = {
            "action": asdict(action),
            "model_call_id": model_call_id,
            "model_step_id": model_step_id,
            "model_started_at": float(model_started_at),
        }
    elif turn is not None:
        action = turn.get("action")
        request = turn.get("tool_request")
        if not isinstance(action, AgentAction) or not isinstance(request, ToolRequest):
            raise ValueError("execute_tool checkpoint 缺少可信 action/tool_request")
        checkpoint["turn"] = {
            "action": asdict(action),
            "tool_request": asdict(request),
            "tool_call_base": dict(turn["tool_call_base"]),
            "tool_call_id": str(turn["tool_call_id"]),
        }
    return checkpoint


def validate_run_checkpoint(checkpoint: dict[str, Any]) -> None:
    checkpoint_version = checkpoint.get("version")
    if checkpoint_version not in {
        RUN_CHECKPOINT_VERSION,
        *LEGACY_RUN_CHECKPOINT_VERSIONS,
    }:
        raise ValueError("不支持的 run checkpoint version")
    resume_node = checkpoint.get("resume_node")
    if resume_node not in RESUMABLE_NODES | NON_RESUMABLE_NODES:
        raise ValueError("run checkpoint resume_node 非法")
    if not isinstance(checkpoint.get("job"), dict):
        raise ValueError("run checkpoint 缺少 job")
    if not isinstance(checkpoint.get("state"), dict):
        raise ValueError("run checkpoint 缺少 state")
    if not isinstance(checkpoint.get("next_step_seq"), int) or checkpoint["next_step_seq"] < 0:
        raise ValueError("run checkpoint 缺少 next_step_seq")
    recovery_attempts = checkpoint["state"].get("recovery_attempts", 0)
    if not isinstance(recovery_attempts, int) or recovery_attempts < 0:
        raise ValueError("run checkpoint recovery_attempts 非法")
    answer_guard_rejections = checkpoint["state"].get("answer_guard_rejections", 0)
    answer_guard_feedback = checkpoint["state"].get("answer_guard_feedback", "")
    if (
        not isinstance(answer_guard_rejections, int)
        or isinstance(answer_guard_rejections, bool)
        or not 0 <= answer_guard_rejections <= 20
        or not isinstance(answer_guard_feedback, str)
        or len(answer_guard_feedback) > 4_000
    ):
        raise ValueError("run checkpoint answer_guard state 非法")
    source_evidence_rejections = checkpoint["state"].get(
        "source_chain_evidence_rejections", 0
    )
    if (
        not isinstance(source_evidence_rejections, int)
        or isinstance(source_evidence_rejections, bool)
        or not 0 <= source_evidence_rejections <= 20
    ):
        raise ValueError("run checkpoint source_chain_evidence_rejections 非法")
    source_slot_attempts = checkpoint["state"].get("source_chain_slot_attempts", {})
    if (
        not isinstance(source_slot_attempts, dict)
        or len(source_slot_attempts) > 8
        or any(
            not isinstance(key, str)
            or not key
            or not isinstance(value, int)
            or isinstance(value, bool)
            or not 0 <= value <= 20
            for key, value in source_slot_attempts.items()
        )
    ):
        raise ValueError("run checkpoint source_chain_slot_attempts 非法")
    history_provenance = checkpoint["state"].get(
        "trusted_history_provenance", []
    )
    if (
        not isinstance(history_provenance, list)
        or len(history_provenance) > 50
        or any(
            not isinstance(link, dict)
            or not link
            or any(
                not isinstance(key, str)
                or not key
                or not isinstance(value, str)
                or not value
                or len(value) > 2_048
                for key, value in link.items()
            )
            for link in history_provenance
        )
    ):
        raise ValueError("run checkpoint trusted_history_provenance 非法")
    raw_state = checkpoint["state"]
    v5_fields = {
        "completion_contract",
        "loop_progress",
        "stop_decision",
        "run_control",
    }
    if checkpoint_version in LEGACY_RUN_CHECKPOINT_VERSIONS:
        if any(field_name in raw_state for field_name in v5_fields):
            raise ValueError("checkpoint v4 不得包含 v5 控制状态")
    elif not v5_fields.issubset(raw_state):
        raise ValueError("checkpoint v5 缺少控制状态")
    try:
        job = RunJobMessage.from_dict(dict(checkpoint["job"]))
        state = restore_agent_state(dict(raw_state))
    except (TypeError, ValueError) as exc:
        raise ValueError("run checkpoint job/state 非法") from exc
    if state.task_id != job.task_id or state.run_id != job.run_id:
        raise ValueError("run checkpoint job/state 标识不一致")
    if checkpoint_version == RUN_CHECKPOINT_VERSION:
        if state.run_control is None:
            raise ValueError("checkpoint v5 缺少 Run control state")
        RunControlState.from_state_dict(state.run_control)
    if state.intent is None:
        if resume_node != "extract_intent":
            raise ValueError("run checkpoint 缺少已校验 Intent")
        if checkpoint_version == RUN_CHECKPOINT_VERSION and any(
            value is not None
            for value in (
                state.completion_contract,
                state.loop_progress,
                state.stop_decision,
            )
        ):
            raise ValueError("checkpoint v5 Intent 前不得包含 Loop 控制状态")
    else:
        intent = IntentExtraction.from_state_dict(state.intent)
        if state.intent_context is None:
            raise ValueError("run checkpoint 缺少 Intent Runtime context")
        context = IntentRuntimeContext.from_state_dict(state.intent_context)
        trusted_ids = {document.document_id for document in context.documents}
        if any(
            document_id not in trusted_ids
            for document_id in intent.retrieval.resolved_document_ids
        ):
            raise ValueError("run checkpoint Intent 文档范围不可信")
        if checkpoint_version == RUN_CHECKPOINT_VERSION:
            if (
                state.completion_contract is None
                or state.loop_progress is None
                or state.stop_decision is None
            ):
                raise ValueError("checkpoint v5 缺少 Loop 控制状态")
            CompletionContract.from_state_dict(state.completion_contract)
            LoopProgressSnapshot.from_state_dict(state.loop_progress)
            StopDecision.from_state_dict(state.stop_decision)
    if resume_node == "validate_action":
        turn = checkpoint.get("turn")
        if not isinstance(turn, dict):
            raise ValueError("validate_action checkpoint 缺少 turn")
        try:
            AgentAction(**dict(turn["action"]))
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError("validate_action checkpoint action 非法") from exc
        if not isinstance(turn.get("model_call_id"), str) or not turn["model_call_id"]:
            raise ValueError("validate_action checkpoint 缺少 model_call_id")
        if not isinstance(turn.get("model_step_id"), str) or not turn["model_step_id"]:
            raise ValueError("validate_action checkpoint 缺少 model_step_id")
        if not isinstance(turn.get("model_started_at"), (int, float)):
            raise ValueError("validate_action checkpoint 缺少 model_started_at")
    elif resume_node == "execute_tool":
        turn = checkpoint.get("turn")
        if not isinstance(turn, dict):
            raise ValueError("execute_tool checkpoint 缺少 turn")
        try:
            AgentAction(**dict(turn["action"]))
            ToolRequest(**dict(turn["tool_request"]))
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError("execute_tool checkpoint turn 非法") from exc
        if not isinstance(turn.get("tool_call_base"), dict):
            raise ValueError("execute_tool checkpoint 缺少 tool_call_base")
        if not isinstance(turn.get("tool_call_id"), str) or not turn["tool_call_id"]:
            raise ValueError("execute_tool checkpoint 缺少 tool_call_id")


def is_resumable_run_checkpoint(checkpoint: dict[str, Any]) -> bool:
    try:
        validate_run_checkpoint(checkpoint)
    except (TypeError, ValueError):
        return False
    return checkpoint["resume_node"] in RESUMABLE_NODES


def build_permission_checkpoint(
    *,
    job: RunJobMessage,
    state: AgentState,
    next_step_seq: int,
    permission_request_id: str,
    tool_request: ToolRequest,
    tool_call_base: dict[str, Any],
    model_action: dict[str, Any],
) -> dict[str, Any]:
    """构造只供持久化 PermissionRequest 使用的恢复检查点。"""
    checkpoint = {
        "version": RUN_CHECKPOINT_VERSION,
        "job": job.to_dict(),
        "state": asdict(state),
        "next_step_seq": next_step_seq,
        "permission_request_id": permission_request_id,
        "tool_request": asdict(tool_request),
        "tool_call_base": dict(tool_call_base),
        "model_action": dict(model_action),
    }
    validate_permission_checkpoint(checkpoint)
    return checkpoint


def validate_permission_checkpoint(
    checkpoint: dict[str, Any],
    *,
    expected_request_id: str | None = None,
    expected_task_id: str | None = None,
    expected_run_id: str | None = None,
    expected_step_id: str | None = None,
    expected_tool_call_id: str | None = None,
    expected_tool_name: str | None = None,
) -> None:
    """在已批准工具执行前核对 checkpoint 内部及 PostgreSQL 身份。"""
    if not isinstance(checkpoint, dict):
        raise ValueError("permission checkpoint 非法")
    checkpoint_version = checkpoint.get("version")
    if checkpoint_version not in {
        RUN_CHECKPOINT_VERSION,
        *LEGACY_RUN_CHECKPOINT_VERSIONS,
    }:
        raise ValueError("不支持的 permission checkpoint version")
    if not isinstance(checkpoint.get("next_step_seq"), int) or checkpoint["next_step_seq"] < 0:
        raise ValueError("permission checkpoint 缺少 next_step_seq")
    required_dicts = ("job", "state", "tool_request", "tool_call_base", "model_action")
    if any(not isinstance(checkpoint.get(key), dict) for key in required_dicts):
        raise ValueError("permission checkpoint 结构非法")
    request_id = checkpoint.get("permission_request_id")
    if not isinstance(request_id, str) or not request_id:
        raise ValueError("permission checkpoint 缺少 request id")
    raw_state = checkpoint["state"]
    v5_fields = {
        "completion_contract",
        "loop_progress",
        "stop_decision",
        "run_control",
    }
    if checkpoint_version in LEGACY_RUN_CHECKPOINT_VERSIONS:
        if any(field_name in raw_state for field_name in v5_fields):
            raise ValueError("permission checkpoint v4 不得包含 v5 控制状态")
    elif not v5_fields.issubset(raw_state):
        raise ValueError("permission checkpoint v5 缺少控制状态")
    try:
        job = RunJobMessage.from_dict(dict(checkpoint["job"]))
        state = restore_agent_state(dict(raw_state))
        tool_request = ToolRequest(**dict(checkpoint["tool_request"]))
    except (TypeError, ValueError) as exc:
        raise ValueError("permission checkpoint job/state/tool request 非法") from exc
    tool_call = checkpoint["tool_call_base"]
    model_action = checkpoint["model_action"]
    if not tool_request.step_id or not tool_request.tool_name:
        raise ValueError("permission checkpoint 工具身份缺失")
    if state.task_id != job.task_id or state.run_id != job.run_id:
        raise ValueError("permission checkpoint job/state 标识不一致")
    if checkpoint_version == RUN_CHECKPOINT_VERSION:
        if state.run_control is None:
            raise ValueError("permission checkpoint v5 缺少 Run control state")
        RunControlState.from_state_dict(state.run_control)
        if (
            state.completion_contract is None
            or state.loop_progress is None
            or state.stop_decision is None
        ):
            raise ValueError("permission checkpoint v5 缺少 Loop 控制状态")
        CompletionContract.from_state_dict(state.completion_contract)
        LoopProgressSnapshot.from_state_dict(state.loop_progress)
        StopDecision.from_state_dict(state.stop_decision)
    if tool_request.task_id != job.task_id or tool_request.run_id != job.run_id:
        raise ValueError("permission checkpoint tool request 标识不一致")
    if (
        str(tool_call.get("run_id", "")) != job.run_id
        or str(tool_call.get("step_id", "")) != tool_request.step_id
        or str(tool_call.get("tool_name", "")) != tool_request.tool_name
        or not str(tool_call.get("id", ""))
        or str(tool_call.get("permission_request_id", "")) != request_id
    ):
        raise ValueError("permission checkpoint tool call 标识不一致")
    if (
        model_action.get("action_type") != "call_tool"
        or model_action.get("tool_name") != tool_request.tool_name
        or not isinstance(model_action.get("arguments"), dict)
    ):
        raise ValueError("permission checkpoint model action 标识不一致")

    expected = {
        "request": (expected_request_id, request_id),
        "task": (expected_task_id, job.task_id),
        "run": (expected_run_id, job.run_id),
        "step": (expected_step_id, tool_request.step_id),
        "tool_call": (expected_tool_call_id, str(tool_call["id"])),
        "tool_name": (expected_tool_name, tool_request.tool_name),
    }
    mismatched = [name for name, (wanted, actual) in expected.items() if wanted is not None and wanted != actual]
    if mismatched:
        raise ValueError("permission checkpoint 与持久化身份不一致: " + ", ".join(mismatched))


def attach_run_checkpoint(
    envelope: RuntimeEventEnvelope,
    checkpoint: dict[str, Any],
) -> RuntimeEventEnvelope:
    """把内部 checkpoint 附到 envelope；调用方随后必须同步持久化该事件。"""
    validate_run_checkpoint(checkpoint)
    envelope.internal[_INTERNAL_KEY] = checkpoint
    return envelope
