"""RunApplicationService — AgentRun 状态机 + 乐观锁 + 条件更新。"""

import json
import logging
from collections.abc import Awaitable, Callable
from copy import deepcopy
from dataclasses import dataclass
from datetime import timedelta
from typing import Optional
from uuid import NAMESPACE_URL, UUID, uuid5

from jarvis_worker.agent.core.checkpoint import (
    MAX_RUN_RECOVERY_ATTEMPTS,
    is_resumable_run_checkpoint,
)
from jarvis_worker.database.unit_of_work import PostgresUnitOfWork
from jarvis_worker.runtime.events import (
    build_envelope,
    build_runtime_event,
    deterministic_event_id,
)
from jarvis_worker.runtime.permissions.policy import (
    permission_request_deadline,
    permission_request_is_expired,
)
from jarvis_worker.shared.domain.models import (
    AgentRun,
    AuditLog,
    OutboxEvent,
    PermissionRequest,
    PermissionStatus,
    RunStatus,
    RuntimeEvent,
    StepStatus,
    StepType,
    TaskStatus,
    WorkspaceStatus,
    new_id,
    utcnow,
)
from jarvis_worker.shared.errors import application as errors

logger = logging.getLogger(__name__)

# 状态迁移表
VALID_TRANSITIONS: dict[RunStatus, set[RunStatus]] = {
    RunStatus.QUEUED: {RunStatus.RUNNING, RunStatus.CANCEL_REQUESTED},
    RunStatus.RUNNING: {
        RunStatus.WAITING_PERMISSION, RunStatus.COMPLETED,
        RunStatus.FAILED, RunStatus.CANCEL_REQUESTED,
        RunStatus.PAUSE_REQUESTED, RunStatus.PAUSED,
    },
    RunStatus.WAITING_PERMISSION: {
        RunStatus.RUNNING, RunStatus.FAILED, RunStatus.CANCEL_REQUESTED,
    },
    RunStatus.PAUSED: {
        RunStatus.RUNNING, RunStatus.RESUME_REQUESTED,
        RunStatus.FAILED, RunStatus.CANCEL_REQUESTED,
    },
    RunStatus.PAUSE_REQUESTED: {
        RunStatus.PAUSED, RunStatus.COMPLETED, RunStatus.FAILED,
        RunStatus.CANCEL_REQUESTED,
    },
    RunStatus.RESUME_REQUESTED: {
        RunStatus.RUNNING, RunStatus.FAILED, RunStatus.CANCEL_REQUESTED,
    },
    RunStatus.CANCEL_REQUESTED: {RunStatus.CANCELLING},
    RunStatus.CANCELLING: {RunStatus.CANCELLED},
}

TERMINAL_STATUSES = {RunStatus.COMPLETED, RunStatus.FAILED, RunStatus.CANCELLED}
SCHEMA_VERSION = "2B-1a.1"
DLQ_RETRY_TOOL_NAME = "runtime.retry_failed_run"
DLQ_RETRY_ERROR_CODE = "RUN_QUEUE_RETRY_EXHAUSTED"
TERMINAL_EVENT_REPAIR_TOOL_NAME = "runtime.repair_missing_terminal_event"
MAX_QUEUED_RUN_RECONCILIATION_ATTEMPTS = 3


@dataclass(frozen=True)
class DlqRetryEvidence:
    source: str
    record_id: str
    original_message_id: str
    error_code: str
    task_id: UUID
    run_id: UUID
    payload_sha256: str = ""


@dataclass(frozen=True)
class DlqRetryInspection:
    eligible: bool
    reason_code: str
    reason: str
    task_id: UUID
    run_id: UUID
    risk_level: str = "L3"
    requires_confirmation: bool = True
    allowed_decisions: tuple[str, str] = ("allow_once", "deny")


@dataclass(frozen=True)
class DlqRetryResolution:
    request: PermissionRequest
    previous_run_id: UUID
    new_run: AgentRun | None = None


@dataclass(frozen=True)
class TerminalEventRepairInspection:
    eligible: bool
    reason_code: str
    reason: str
    task_id: UUID | None
    run_id: UUID
    expected_event_type: str | None = None
    risk_level: str = "L3"
    requires_confirmation: bool = True
    allowed_decisions: tuple[str, str] = ("allow_once", "deny")


@dataclass(frozen=True)
class TerminalEventRepairResolution:
    request: PermissionRequest
    repaired_event_id: UUID | None = None
    repaired_event_type: str | None = None


class RunApplicationService:
    """AgentRun Application Service。

    负责状态迁移、乐观锁和并发保护。
    """

    def __init__(self, uow_factory, workspace_policy=None):
        self._uow_factory = uow_factory
        self._workspace_policy = workspace_policy

    async def inspect_terminal_event_repair(
        self, run_id: UUID
    ) -> TerminalEventRepairInspection:
        """只读判断 failed Run 是否可安全补写唯一终态事件。"""
        session_factory = self._uow_factory()
        async with session_factory() as session:
            uow = PostgresUnitOfWork(session)
            return await self._assess_terminal_event_repair(uow, run_id)

    async def create_terminal_event_repair_request(
        self, run_id: UUID
    ) -> PermissionRequest:
        """创建持久化 L3 单次确认；此步骤不修改 RuntimeEvent。"""
        request_id = uuid5(
            NAMESPACE_URL, f"jarvis:terminal-event-repair:{run_id}"
        )
        session_factory = self._uow_factory()
        async with session_factory() as session:
            uow = PostgresUnitOfWork(session)
            async with uow.transaction() as tx:
                existing = await tx.permissions.get_request(request_id)
                if existing is not None:
                    await tx.commit()
                    return existing
                inspection = await self._assess_terminal_event_repair(tx, run_id)
                if not inspection.eligible or inspection.task_id is None:
                    raise errors.AppError(
                        code=inspection.reason_code,
                        message=inspection.reason,
                        category="validation",
                        recoverable=False,
                    )
                now = utcnow()
                request = PermissionRequest(
                    id=request_id,
                    task_id=inspection.task_id,
                    run_id=run_id,
                    tool_name=TERMINAL_EVENT_REPAIR_TOOL_NAME,
                    action_summary="为 failed Run 补写缺失的唯一终态 RuntimeEvent",
                    reason="PostgreSQL Run 已是 failed，但缺少 agent.run.failed 事件",
                    risk_level="L3",
                    scope={"type": "once", "run_id": str(run_id)},
                    arguments_summary={
                        "run_id": str(run_id),
                        "event_type": inspection.expected_event_type,
                    },
                    allowed_decisions=["allow_once", "deny"],
                    checkpoint={
                        "version": 1,
                        "action": "repair_missing_terminal_event",
                        "run_id": str(run_id),
                        "event_type": inspection.expected_event_type,
                    },
                    created_at=now,
                    expires_at=permission_request_deadline(now),
                )
                await tx.permissions.create_request(request)
                await tx.audits.create(AuditLog(
                    id=new_id(), task_id=inspection.task_id, run_id=run_id,
                    event_type="runtime.repair.permission_requested", actor="user",
                    risk_level="L3", action_summary=request.action_summary,
                    details={"request_id": str(request.id), "repair_code": "TERMINAL_EVENT_MISSING"},
                    result_summary="pending", created_at=now,
                ))
                await tx.commit()
                return request

    async def resolve_terminal_event_repair_request(
        self, request_id: UUID, decision: str, note: str = ""
    ) -> TerminalEventRepairResolution:
        """拒绝或单次消费修复权限；批准时原子追加 Event、Outbox 与 AuditLog。"""
        if decision not in ("allow_once", "deny"):
            raise errors.validation_error("终态事件修复只允许 allow_once 或 deny")
        session_factory = self._uow_factory()
        async with session_factory() as session:
            uow = PostgresUnitOfWork(session)
            async with uow.transaction() as tx:
                request = await tx.permissions.get_request_for_update(request_id)
                if request is None:
                    raise errors.not_found("PermissionRequest", str(request_id))
                if request.tool_name != TERMINAL_EVENT_REPAIR_TOOL_NAME:
                    raise errors.validation_error("权限请求不属于终态事件修复")
                if request.status in (PermissionStatus.CONSUMED, PermissionStatus.DENIED):
                    if request.decision != decision:
                        raise errors.permission_conflict(
                            str(request_id), request.decision or "unknown", decision
                        )
                    repaired_id = (request.checkpoint or {}).get("repaired_event_id")
                    try:
                        parsed_repaired_id = UUID(str(repaired_id)) if repaired_id else None
                    except ValueError:
                        parsed_repaired_id = None
                    await tx.commit()
                    return TerminalEventRepairResolution(
                        request=request,
                        repaired_event_id=parsed_repaired_id,
                        repaired_event_type=(request.checkpoint or {}).get("event_type"),
                    )
                if request.status != PermissionStatus.PENDING:
                    raise errors.permission_not_pending(str(request_id), request.status.value)

                now = utcnow()
                if permission_request_is_expired(
                    expires_at=request.expires_at, now=now
                ):
                    raise errors.permission_not_pending(
                        str(request_id), PermissionStatus.EXPIRED.value
                    )
                if decision == "deny":
                    request.status = PermissionStatus.DENIED
                    request.decision = decision
                    request.decided_at = now
                    request.note = note[:500]
                    await tx.permissions.update_request(request)
                    await tx.audits.create(AuditLog(
                        id=new_id(), task_id=request.task_id, run_id=request.run_id,
                        event_type="runtime.repair.permission_decision", actor="user",
                        risk_level="L3", permission_decision="deny",
                        action_summary=request.action_summary,
                        details={"request_id": str(request.id), "note": note[:500]},
                        result_summary="denied", created_at=now,
                    ))
                    await tx.commit()
                    return TerminalEventRepairResolution(request=request)

                inspection = await self._assess_terminal_event_repair(tx, request.run_id)
                if not inspection.eligible:
                    raise errors.AppError(
                        code=inspection.reason_code,
                        message=inspection.reason,
                        category="validation",
                        recoverable=False,
                    )
                run = await tx.runs.get(request.run_id)
                assert run is not None and inspection.expected_event_type is not None
                sequence = await tx.events.get_next_sequence(run.id)
                event_id = UUID(deterministic_event_id(
                    str(run.id), inspection.expected_event_type, sequence
                ))
                safe_error = self._safe_terminal_error(run.error)
                payload = {
                    "error": safe_error,
                    "repair": {
                        "code": "TERMINAL_EVENT_MISSING",
                        "permission_request_id": str(request.id),
                    },
                }
                trace_id = run.trace_id or new_id()
                runtime_event = build_runtime_event(
                    event_type=inspection.expected_event_type,
                    task_id=str(run.task_id),
                    run_id=str(run.id),
                    event_id=str(event_id),
                    payload=payload,
                )
                envelope = build_envelope(runtime_event, str(trace_id), "storage-repair")
                await tx.events.append([RuntimeEvent(
                    id=event_id, event_id=event_id, task_id=run.task_id, run_id=run.id,
                    type=inspection.expected_event_type, event_sequence=sequence,
                    payload=payload, created_at=now,
                )])
                await tx.outbox.create([OutboxEvent(
                    id=new_id(), event_id=event_id, aggregate_type="AgentRun",
                    aggregate_id=run.id, event_type=inspection.expected_event_type,
                    schema_version=SCHEMA_VERSION,
                    payload=json.loads(envelope.to_payload_json()),
                    trace_id=trace_id, causation_id=request.id, created_at=now,
                )])
                request.status = PermissionStatus.CONSUMED
                request.decision = "allow_once"
                request.decided_at = now
                request.note = note[:500]
                request.checkpoint = {
                    **request.checkpoint,
                    "repaired_event_id": str(event_id),
                }
                await tx.permissions.update_request(request)
                await tx.audits.create(AuditLog(
                    id=new_id(), task_id=run.task_id, run_id=run.id,
                    event_type="runtime.repair.applied", actor="user",
                    risk_level="L3", permission_decision="allow_once",
                    action_summary=request.action_summary,
                    details={
                        "request_id": str(request.id),
                        "repair_code": "TERMINAL_EVENT_MISSING",
                        "event_id": str(event_id),
                        "event_type": inspection.expected_event_type,
                    },
                    result_summary="repaired", created_at=now,
                ))
                await tx.commit()
                return TerminalEventRepairResolution(
                    request=request,
                    repaired_event_id=event_id,
                    repaired_event_type=inspection.expected_event_type,
                )

    async def _assess_terminal_event_repair(
        self, tx, run_id: UUID
    ) -> TerminalEventRepairInspection:
        run = await tx.runs.get(run_id)
        task_id = run.task_id if run is not None else None

        def result(eligible: bool, code: str, reason: str):
            return TerminalEventRepairInspection(
                eligible=eligible, reason_code=code, reason=reason,
                task_id=task_id, run_id=run_id,
                expected_event_type="agent.run.failed" if eligible else None,
            )

        if run is None:
            return result(False, "REPAIR_RUN_NOT_FOUND", "PostgreSQL 中找不到该 Run")
        if run.status != RunStatus.FAILED or run.failed_at is None:
            return result(False, "REPAIR_RUN_NOT_FAILED", "仅允许修复具有 failed_at 的 failed Run")
        if not isinstance(run.error, dict) or not str(run.error.get("code", "")).strip():
            return result(False, "REPAIR_ERROR_EVIDENCE_MISSING", "Run 缺少可验证的安全错误证据")
        if await tx.tasks.get(run.task_id) is None:
            return result(False, "REPAIR_TASK_NOT_FOUND", "Run 关联的 Task 不存在")
        events = await tx.events.list_by_run(run.id)
        sequences = [event.event_sequence for event in events]
        if sequences != list(range(1, len(events) + 1)):
            return result(False, "REPAIR_EVENT_SEQUENCE_INVALID", "现有事件序号不连续，不能安全追加")
        terminal_types = {
            event.type for event in events
            if event.type in {"agent.run.completed", "agent.run.failed", "agent.run.cancelled"}
        }
        if "agent.run.failed" in terminal_types:
            return result(False, "REPAIR_ALREADY_APPLIED", "agent.run.failed 已存在")
        if terminal_types:
            return result(False, "REPAIR_TERMINAL_CONFLICT", "存在冲突的终态事件，禁止自动补写")
        return result(True, "TERMINAL_EVENT_REPAIR_ELIGIBLE", "可补写唯一 agent.run.failed 事件")

    @staticmethod
    def _safe_terminal_error(error: dict | None) -> dict:
        source = error or {}
        return {
            "code": str(source.get("code") or "RUN_FAILED"),
            "message": str(source.get("message") or "运行失败")[:500],
            "category": str(source.get("category") or "runtime")[:80],
            "recoverable": bool(source.get("recoverable")),
        }

    async def retry_failed_step(self, run_id: UUID, step_id: UUID) -> AgentRun:
        """从可恢复模型失败前的 checkpoint 创建 replacement Run。"""
        replacement_id = uuid5(
            NAMESPACE_URL, f"jarvis:{run_id}:{step_id}:failed-step-retry"
        )
        session_factory = self._uow_factory()
        async with session_factory() as session:
            uow = PostgresUnitOfWork(session)
            async with uow.transaction() as tx:
                existing = await tx.runs.get(replacement_id)
                if existing is not None:
                    await tx.commit()
                    return existing
                source = await tx.runs.get(run_id)
                if source is None:
                    raise errors.not_found("AgentRun", str(run_id))
                task = await tx.tasks.get(source.task_id)
                step = await tx.steps.get(step_id)
                if task is None or step is None or step.run_id != source.id:
                    raise errors.not_found("ExecutionStep", str(step_id))
                if (
                    source.status != RunStatus.FAILED
                    or task.status != TaskStatus.FAILED
                    or task.active_run_id != source.id
                ):
                    raise errors.invalid_state_transition(
                        source.status.value, "retry_failed_step"
                    )
                step_error = step.error or {}
                source_error = source.error or {}
                if (
                    step.type != StepType.MODEL_CALL
                    or step.status != StepStatus.FAILED
                    or not bool(step_error.get("recoverable"))
                    or step_error.get("code") != source_error.get("code")
                    or not bool(source_error.get("recoverable"))
                    or not is_resumable_run_checkpoint(source.checkpoint)
                    or source.checkpoint.get("resume_node")
                    not in {"extract_intent", "call_model"}
                ):
                    raise errors.AppError(
                        code="FAILED_STEP_NOT_RETRYABLE",
                        message="该失败步骤没有可安全恢复的模型检查点",
                        category="validation", recoverable=False,
                    )

                now = utcnow()
                trace_id, job_id = new_id(), new_id()
                checkpoint = deepcopy(source.checkpoint)
                checkpoint["job"].update({
                    "job_id": str(job_id), "trace_id": str(trace_id),
                    "run_id": str(replacement_id), "created_at": now.isoformat(),
                    "retry_from_checkpoint": True,
                })
                checkpoint["state"]["run_id"] = str(replacement_id)
                checkpoint["state"]["recovery_attempts"] = 0
                checkpoint["state"]["next_step_seq"] = 1
                checkpoint["next_step_seq"] = 1

                source_metadata = dict(source.metadata)
                source_metadata["retry_replacement_id"] = str(replacement_id)
                source_metadata["retry_step_id"] = str(step_id)
                claimed = await tx.runs.update_with_lock(
                    run_id=source.id, new_status=RunStatus.FAILED.value,
                    expected_version=source.version,
                    expected_status=RunStatus.FAILED.value,
                    metadata_json=source_metadata,
                )
                if not claimed:
                    raise errors.run_version_conflict(str(source.id))

                replacement = AgentRun(
                    id=replacement_id, task_id=task.id, status=RunStatus.QUEUED,
                    version=1, checkpoint=checkpoint, created_at=now, updated_at=now,
                    metadata={
                        "trace_id": str(trace_id),
                        "retry_of_run_id": str(source.id),
                        "retry_of_step_id": str(step.id),
                    },
                )
                await tx.runs.create(replacement)
                await tx.flush()
                task.status = TaskStatus.RUNNING
                task.active_run_id = replacement.id
                task.updated_at = now
                task.completed_at = None
                await tx.tasks.update(task)

                request_event_id = new_id()
                await tx.events.append([RuntimeEvent(
                    id=request_event_id, event_id=request_event_id,
                    task_id=task.id, run_id=replacement.id,
                    type="agent.run.retry_requested", event_sequence=1,
                    payload={
                        "previous_run_id": str(source.id),
                        "failed_step_id": str(step.id),
                        "new_run_id": str(replacement.id),
                    }, created_at=now,
                )])
                job_payload = dict(checkpoint["job"])
                await tx.outbox.create([OutboxEvent(
                    id=new_id(), event_id=job_id, aggregate_type="AgentRun",
                    aggregate_id=replacement.id,
                    event_type="run.step_retry.requested",
                    schema_version=SCHEMA_VERSION, payload=job_payload,
                    trace_id=trace_id, correlation_id=source.id,
                    causation_id=step.id, created_at=now,
                )])
                await tx.audits.create(AuditLog(
                    id=new_id(), task_id=task.id, run_id=source.id, step_id=step.id,
                    event_type="run.step_retry.requested", actor="user",
                    action_summary="从可恢复模型步骤创建 replacement Run",
                    details={"new_run_id": str(replacement.id)},
                    result_summary="queued", created_at=now,
                ))
                await tx.commit()
                return replacement

    async def inspect_dlq_retry(self, evidence: DlqRetryEvidence) -> DlqRetryInspection:
        """只读核对 Redis 诊断证据与 PostgreSQL 权威状态。"""
        session_factory = self._uow_factory()
        async with session_factory() as session:
            uow = PostgresUnitOfWork(session)
            run = await uow.runs.get(evidence.run_id)
            task = await uow.tasks.get(evidence.task_id)
            return await self._assess_dlq_retry(uow, evidence, run, task)

    async def create_dlq_retry_request(self, evidence: DlqRetryEvidence) -> PermissionRequest:
        """为可安全重试的 Run 创建持久化 L3 单次权限请求。"""
        request_id = self._dlq_retry_request_id(evidence)
        session_factory = self._uow_factory()
        async with session_factory() as session:
            uow = PostgresUnitOfWork(session)
            async with uow.transaction() as tx:
                existing = await tx.permissions.get_request(request_id)
                if existing is not None:
                    await tx.commit()
                    return existing
                run = await tx.runs.get(evidence.run_id)
                task = await tx.tasks.get(evidence.task_id)
                inspection = await self._assess_dlq_retry(tx, evidence, run, task)
                if not inspection.eligible:
                    raise errors.AppError(
                        code=inspection.reason_code,
                        message=inspection.reason,
                        category="validation",
                        recoverable=False,
                    )
                now = utcnow()
                request = PermissionRequest(
                    id=request_id,
                    task_id=evidence.task_id,
                    run_id=evidence.run_id,
                    tool_name=DLQ_RETRY_TOOL_NAME,
                    action_summary="基于权威任务数据创建一个新的 Agent Run",
                    reason="原 Run 在进入执行前因运行队列重试耗尽而失败",
                    risk_level="L3",
                    scope={"type": "once", "task_id": str(evidence.task_id)},
                    arguments_summary={
                        "source": evidence.source,
                        "dlq_record_id": evidence.record_id,
                        "previous_run_id": str(evidence.run_id),
                        "error_code": evidence.error_code,
                    },
                    allowed_decisions=["allow_once", "deny"],
                    checkpoint={
                        "version": 1,
                        "action": "dlq_retry_as_new_run",
                        "source": evidence.source,
                        "record_id": evidence.record_id,
                        "original_message_id": evidence.original_message_id,
                        "error_code": evidence.error_code,
                        "task_id": str(evidence.task_id),
                        "run_id": str(evidence.run_id),
                        "payload_sha256": evidence.payload_sha256,
                    },
                    created_at=now,
                    expires_at=permission_request_deadline(now),
                )
                await tx.permissions.create_request(request)
                await tx.audits.create(AuditLog(
                    id=new_id(), task_id=evidence.task_id, run_id=evidence.run_id,
                    event_type="run.retry.permission_requested", actor="user",
                    risk_level="L3", action_summary=request.action_summary,
                    details={"request_id": str(request.id), "dlq_record_id": evidence.record_id},
                    result_summary="pending", created_at=now,
                ))
                await tx.commit()
                return request

    async def resolve_dlq_retry_request(
        self, request_id: UUID, decision: str, note: str = ""
    ) -> DlqRetryResolution:
        """原子拒绝或消费 DLQ retry 权限；批准时创建全新 Run，不重放原消息。"""
        if decision not in ("allow_once", "deny"):
            raise errors.validation_error("DLQ 受控重试只允许 allow_once 或 deny")
        session_factory = self._uow_factory()
        async with session_factory() as session:
            uow = PostgresUnitOfWork(session)
            async with uow.transaction() as tx:
                request = await tx.permissions.get_request_for_update(request_id)
                if request is None:
                    raise errors.not_found("PermissionRequest", str(request_id))
                if request.tool_name != DLQ_RETRY_TOOL_NAME:
                    raise errors.validation_error("权限请求不属于 DLQ 受控重试")
                if request.status in (PermissionStatus.CONSUMED, PermissionStatus.DENIED):
                    if request.decision != decision:
                        raise errors.permission_conflict(
                            str(request_id), request.decision or "unknown", decision
                        )
                    new_run = None
                    if request.status == PermissionStatus.CONSUMED:
                        raw_new_run_id = (request.checkpoint or {}).get("new_run_id")
                        try:
                            new_run = await tx.runs.get(UUID(str(raw_new_run_id))) if raw_new_run_id else None
                        except ValueError:
                            new_run = None
                    await tx.commit()
                    return DlqRetryResolution(
                        request=request,
                        previous_run_id=request.run_id,
                        new_run=new_run,
                    )
                if request.status != PermissionStatus.PENDING:
                    raise errors.permission_not_pending(str(request_id), request.status.value)

                now = utcnow()
                if permission_request_is_expired(
                    expires_at=request.expires_at, now=now
                ):
                    raise errors.permission_not_pending(
                        str(request_id), PermissionStatus.EXPIRED.value
                    )
                if decision == "deny":
                    request.status = PermissionStatus.DENIED
                    request.decision = decision
                    request.decided_at = now
                    request.note = note[:500]
                    await tx.permissions.update_request(request)
                    await tx.audits.create(AuditLog(
                        id=new_id(), task_id=request.task_id, run_id=request.run_id,
                        event_type="run.retry.permission_decision", actor="user",
                        risk_level="L3", permission_decision="deny",
                        action_summary=request.action_summary,
                        details={"request_id": str(request.id), "note": note[:500]},
                        result_summary="denied", created_at=now,
                    ))
                    await tx.commit()
                    return DlqRetryResolution(request=request, previous_run_id=request.run_id)

                evidence = self._evidence_from_request(request)
                previous_run = await tx.runs.get(request.run_id)
                task = await tx.tasks.get(request.task_id)
                inspection = await self._assess_dlq_retry(tx, evidence, previous_run, task)
                if not inspection.eligible:
                    raise errors.AppError(
                        code=inspection.reason_code,
                        message=inspection.reason,
                        category="validation",
                        recoverable=False,
                    )
                assert previous_run is not None and task is not None
                trace_id = new_id()
                new_run = AgentRun(
                    id=new_id(), task_id=task.id, status=RunStatus.QUEUED,
                    version=1, created_at=now, updated_at=now,
                    metadata={
                        "trace_id": str(trace_id),
                        "retry_of_run_id": str(previous_run.id),
                        "dlq_record_id": evidence.record_id,
                    },
                )
                await tx.runs.create(new_run)
                await tx.flush()
                task.status = TaskStatus.RUNNING
                task.active_run_id = new_run.id
                task.updated_at = now
                task.completed_at = None
                task.cancelled_at = None
                await tx.tasks.update(task)

                event_id = new_id()
                await tx.events.append([RuntimeEvent(
                    id=new_id(), event_id=event_id, task_id=task.id, run_id=new_run.id,
                    type="agent.run.retry_requested", event_sequence=1,
                    payload={
                        "previous_run_id": str(previous_run.id),
                        "new_run_id": str(new_run.id),
                        "permission_request_id": str(request.id),
                    },
                    created_at=now,
                )])
                job_id = new_id()
                await tx.outbox.create([OutboxEvent(
                    id=new_id(), event_id=job_id, aggregate_type="AgentRun",
                    aggregate_id=new_run.id, event_type="run.retry.requested",
                    schema_version=SCHEMA_VERSION,
                    payload={
                        "job_id": str(job_id), "trace_id": str(trace_id),
                        "task_id": str(task.id), "run_id": str(new_run.id),
                        "user_goal": task.user_goal,
                        "workspace_path": task.workspace_path or "",
                        "conversation_id": str(task.conversation_id),
                        "created_at": now.isoformat(), "schema_version": SCHEMA_VERSION,
                    },
                    trace_id=trace_id, correlation_id=previous_run.id,
                    causation_id=request.id, created_at=now,
                )])
                request.status = PermissionStatus.CONSUMED
                request.decision = "allow_once"
                request.decided_at = now
                request.note = note[:500]
                request.checkpoint = {**request.checkpoint, "new_run_id": str(new_run.id)}
                await tx.permissions.update_request(request)
                await tx.audits.create(AuditLog(
                    id=new_id(), task_id=task.id, run_id=previous_run.id,
                    event_type="run.retry.requested", actor="user",
                    risk_level="L3", permission_decision="allow_once",
                    action_summary=request.action_summary,
                    details={
                        "request_id": str(request.id),
                        "dlq_record_id": evidence.record_id,
                        "previous_run_id": str(previous_run.id),
                        "new_run_id": str(new_run.id),
                    },
                    result_summary="queued", created_at=now,
                ))
                await tx.commit()
                return DlqRetryResolution(
                    request=request, previous_run_id=previous_run.id, new_run=new_run
                )

    async def _assess_dlq_retry(self, tx, evidence, run, task) -> DlqRetryInspection:
        def result(eligible: bool, code: str, reason: str) -> DlqRetryInspection:
            return DlqRetryInspection(
                eligible=eligible, reason_code=code, reason=reason,
                task_id=evidence.task_id, run_id=evidence.run_id,
            )
        if evidence.source != "run_queue":
            return result(False, "DLQ_SOURCE_NOT_RETRYABLE", "只有 Run Queue 失败可创建新 Run")
        if evidence.error_code != DLQ_RETRY_ERROR_CODE:
            return result(False, "DLQ_ERROR_NOT_RETRYABLE", "该错误不允许人工重试")
        if run is None or task is None:
            return result(False, "DLQ_AUTHORITY_NOT_FOUND", "PostgreSQL 中找不到关联 Task 或 Run")
        if run.task_id != task.id:
            return result(False, "DLQ_AUTHORITY_MISMATCH", "DLQ 关联关系与权威数据不一致")
        if run.status != RunStatus.FAILED:
            return result(False, "DLQ_RUN_STATE_CHANGED", "原 Run 已不再处于 failed 状态")
        if (run.error or {}).get("code") != DLQ_RETRY_ERROR_CODE:
            return result(False, "DLQ_RUN_ERROR_MISMATCH", "原 Run 的权威错误不支持重试")
        if task.status != TaskStatus.FAILED or task.active_run_id != run.id:
            return result(False, "DLQ_TASK_STATE_CHANGED", "Task 已有更新状态或新的活动 Run")
        if task.workspace_id is not None:
            workspace = await tx.workspaces.get(task.workspace_id)
            if (
                workspace is None
                or workspace.status != WorkspaceStatus.ACTIVE
                or workspace.canonical_path != task.workspace_path
            ):
                return result(False, "DLQ_WORKSPACE_UNAVAILABLE", "原工作区已撤销或发生变化")
        elif task.workspace_path and self._workspace_policy is not None:
            try:
                self._workspace_policy.resolve(task.workspace_path)
            except errors.AppError:
                return result(False, "DLQ_WORKSPACE_UNAVAILABLE", "原工作区已不在允许范围")
        return result(True, "DLQ_RETRY_ELIGIBLE", "可基于 PostgreSQL 权威任务数据创建新 Run")

    @staticmethod
    def _dlq_retry_request_id(evidence: DlqRetryEvidence) -> UUID:
        return uuid5(
            NAMESPACE_URL,
            f"jarvis:dlq-retry:{evidence.source}:{evidence.record_id}:{evidence.run_id}",
        )

    @staticmethod
    def _evidence_from_request(request: PermissionRequest) -> DlqRetryEvidence:
        checkpoint = request.checkpoint or {}
        if checkpoint.get("action") != "dlq_retry_as_new_run":
            raise errors.validation_error("DLQ 重试权限检查点无效")
        try:
            return DlqRetryEvidence(
                source=str(checkpoint["source"]),
                record_id=str(checkpoint["record_id"]),
                original_message_id=str(checkpoint["original_message_id"]),
                error_code=str(checkpoint["error_code"]),
                task_id=UUID(str(checkpoint["task_id"])),
                run_id=UUID(str(checkpoint["run_id"])),
                payload_sha256=str(checkpoint.get("payload_sha256", "")),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise errors.validation_error("DLQ 重试权限检查点无效") from exc

    def _check_transition(self, current: RunStatus, target: RunStatus) -> None:
        """校验状态迁移合法性。"""
        allowed = VALID_TRANSITIONS.get(current, set())
        if target not in allowed:
            raise errors.invalid_state_transition(current.value, target.value)

    async def claim_run(
        self, run_id: UUID, worker_id: str, expected_version: int
    ) -> AgentRun:
        """Worker claim 一个 queued Run（queued → running）。

        Raises:
            AppError: 状态迁移非法、version 冲突或 run 不存在。
        """
        session_factory = self._uow_factory()
        async with session_factory() as session:
            uow = PostgresUnitOfWork(session)
            async with uow.transaction() as tx:
                run = await tx.runs.get(run_id)
                if run is None:
                    raise errors.not_found("AgentRun", str(run_id))

                self._check_transition(run.status, RunStatus.RUNNING)

                success = await tx.runs.update_with_lock(
                    run_id=run_id,
                    new_status=RunStatus.RUNNING.value,
                    expected_version=expected_version,
                    expected_status=RunStatus.QUEUED.value,
                    worker_id=worker_id,
                    started_at=utcnow(),
                    lease_until=utcnow() + timedelta(seconds=60),
                )
                if not success:
                    raise errors.run_version_conflict(str(run_id))

                await tx.commit()

        # 重新读取
        return await self.get_run(run_id)

    async def claim_job(self, run_id: UUID, worker_id: str, source_event_id: str) -> tuple[AgentRun, str]:
        """幂等 claim RunJob，返回 execute/cancel/duplicate。"""
        session_factory = self._uow_factory()
        async with session_factory() as session:
            uow = PostgresUnitOfWork(session)
            async with uow.transaction() as tx:
                run = await tx.runs.get(run_id)
                if run is None:
                    raise errors.not_found("AgentRun", str(run_id))

                first_seen = await tx.inbox.try_insert("run-queue", source_event_id)
                if not first_seen:
                    await tx.commit()
                    return run, "duplicate"

                if run.status == RunStatus.CANCEL_REQUESTED:
                    await tx.inbox.mark_processed("run-queue", source_event_id)
                    await tx.commit()
                    return run, "cancel"

                self._check_transition(run.status, RunStatus.RUNNING)
                success = await tx.runs.update_with_lock(
                    run_id=run_id,
                    new_status=RunStatus.RUNNING.value,
                    expected_version=run.version,
                    expected_status=RunStatus.QUEUED.value,
                    worker_id=worker_id,
                    started_at=utcnow(),
                    lease_until=utcnow() + timedelta(seconds=60),
                )
                if not success:
                    raise errors.run_version_conflict(str(run_id))
                await tx.inbox.mark_processed("run-queue", source_event_id)
                await tx.commit()

        claimed = await self.get_run(run_id)
        if claimed is None:
            raise errors.not_found("AgentRun", str(run_id))
        return claimed, "execute"

    async def claim_permission_resume(
        self, run_id: UUID, worker_id: str
    ) -> tuple[AgentRun, str]:
        """在执行获批工具前持久化占用恢复权。

        waiting_permission -> running 的条件更新发生在工具执行之前，防止 Redis
        command 重投时并发或静默重复执行高风险动作。过期 lease 返回 stale，
        由 Worker 以可见失败收口，不盲目重放可能已产生副作用的工具。
        """
        session_factory = self._uow_factory()
        async with session_factory() as session:
            uow = PostgresUnitOfWork(session)
            async with uow.transaction() as tx:
                run = await tx.runs.get(run_id)
                if run is None:
                    raise errors.not_found("AgentRun", str(run_id))
                if run.status in TERMINAL_STATUSES or run.status in (
                    RunStatus.CANCEL_REQUESTED, RunStatus.CANCELLING,
                ):
                    await tx.commit()
                    return run, "skip"
                if run.status == RunStatus.RUNNING:
                    disposition = (
                        "stale"
                        if run.lease_until is not None and run.lease_until <= utcnow()
                        else "busy"
                    )
                    await tx.commit()
                    return run, disposition

                self._check_transition(run.status, RunStatus.RUNNING)
                success = await tx.runs.update_with_lock(
                    run_id=run_id,
                    new_status=RunStatus.RUNNING.value,
                    expected_version=run.version,
                    expected_status=RunStatus.WAITING_PERMISSION.value,
                    worker_id=worker_id,
                    lease_until=utcnow() + timedelta(seconds=60),
                )
                if not success:
                    raise errors.run_version_conflict(str(run_id))
                await tx.commit()

        claimed = await self.get_run(run_id)
        if claimed is None:
            raise errors.not_found("AgentRun", str(run_id))
        return claimed, "execute"

    async def claim_recovery(
        self, run_id: UUID, worker_id: str, source_event_id: str
    ) -> tuple[AgentRun, str]:
        """幂等占用 reconciliation 调度的 paused Run。"""
        session_factory = self._uow_factory()
        async with session_factory() as session:
            uow = PostgresUnitOfWork(session)
            async with uow.transaction() as tx:
                run = await tx.runs.get(run_id)
                if run is None:
                    raise errors.not_found("AgentRun", str(run_id))
                first_seen = await tx.inbox.try_insert("run-recovery", source_event_id)
                if not first_seen:
                    await tx.commit()
                    return run, "duplicate"
                if (
                    run.status not in (RunStatus.PAUSED, RunStatus.RESUME_REQUESTED)
                    or not is_resumable_run_checkpoint(run.checkpoint)
                ):
                    await tx.inbox.mark_processed("run-recovery", source_event_id)
                    await tx.commit()
                    return run, "duplicate"
                checkpoint = deepcopy(run.checkpoint)
                state = checkpoint.setdefault("state", {})
                attempts = int(state.get("recovery_attempts", 0)) + 1
                state["recovery_attempts"] = attempts
                if attempts > MAX_RUN_RECOVERY_ATTEMPTS:
                    await tx.inbox.mark_processed("run-recovery", source_event_id)
                    await tx.commit()
                    return run, "duplicate"
                success = await tx.runs.update_with_lock(
                    run_id=run.id,
                    new_status=RunStatus.RUNNING.value,
                    expected_version=run.version,
                    expected_status=run.status.value,
                    worker_id=worker_id,
                    lease_until=utcnow() + timedelta(seconds=60),
                    checkpoint_json=checkpoint,
                )
                if not success:
                    raise errors.run_version_conflict(str(run_id))
                await tx.inbox.mark_processed("run-recovery", source_event_id)
                await tx.commit()
        claimed = await self.get_run(run_id)
        if claimed is None:
            raise errors.not_found("AgentRun", str(run_id))
        return claimed, "execute"

    async def renew_run_lease(self, run_id: UUID, worker_id: str) -> bool:
        """仅当前 owner Worker 可续租 running Run。"""
        session_factory = self._uow_factory()
        async with session_factory() as session:
            uow = PostgresUnitOfWork(session)
            async with uow.transaction() as tx:
                renewed = await tx.runs.renew_lease(
                    run_id, worker_id, utcnow() + timedelta(seconds=60)
                )
                await tx.commit()
                return renewed

    async def fail_run_queue_delivery(
        self,
        run_id: UUID,
        source_event_id: str,
        error_code: str,
        delivery_count: int,
    ) -> AgentRun | None:
        """RunJob 重试耗尽时，以 PostgreSQL + RuntimeEvent + AuditLog 收口。

        Redis DLQ 写入发生在本事务成功之后。若 Redis 写入失败，后续重复调用通过
        Inbox 幂等返回，原 pending 消息仍可再次尝试 DLQ 原子迁移。
        """
        now = utcnow()
        session_factory = self._uow_factory()
        async with session_factory() as session:
            uow = PostgresUnitOfWork(session)
            async with uow.transaction() as tx:
                first_seen = await tx.inbox.try_insert(
                    "run-queue-dlq", source_event_id
                )
                run = await tx.runs.get(run_id)
                if not first_seen:
                    await tx.commit()
                    return run
                if run is None:
                    await tx.inbox.mark_processed(
                        "run-queue-dlq", source_event_id
                    )
                    await tx.commit()
                    return None
                if run.status in TERMINAL_STATUSES or run.status in (
                    RunStatus.CANCEL_REQUESTED, RunStatus.CANCELLING,
                ):
                    await tx.inbox.mark_processed(
                        "run-queue-dlq", source_event_id
                    )
                    await tx.commit()
                    return run

                error = {
                    "code": "RUN_QUEUE_RETRY_EXHAUSTED",
                    "message": "运行队列处理重试次数已耗尽",
                    "category": "runtime",
                    "recoverable": False,
                    "details": {
                        "delivery_count": delivery_count,
                        "last_error_code": error_code,
                    },
                }
                success = await tx.runs.update_with_lock(
                    run_id=run.id,
                    new_status=RunStatus.FAILED.value,
                    expected_version=run.version,
                    expected_status=run.status.value,
                    failed_at=now,
                    error_json=error,
                    worker_id=None,
                    lease_until=None,
                    checkpoint_json={},
                )
                if not success:
                    raise errors.run_version_conflict(str(run_id))

                task = await tx.tasks.get(run.task_id)
                if task is not None:
                    task.status = TaskStatus.FAILED
                    task.updated_at = now
                    await tx.tasks.update(task)
                await self._fail_open_children(tx, run.id, error, now)

                event_id = deterministic_event_id(
                    str(run.id), "agent.run.failed", 9200 + run.version
                )
                trace_id = str(run.trace_id or new_id())
                runtime_event = build_runtime_event(
                    event_type="agent.run.failed",
                    task_id=str(run.task_id),
                    run_id=str(run.id),
                    event_id=event_id,
                    payload={"error": error},
                )
                envelope = build_envelope(
                    runtime_event, trace_id, "run-queue-recovery"
                )
                sequence = await tx.events.get_next_sequence(run.id)
                await tx.events.append([RuntimeEvent(
                    id=UUID(event_id), event_id=UUID(event_id),
                    task_id=run.task_id, run_id=run.id,
                    type="agent.run.failed", event_sequence=sequence,
                    payload={"error": error}, created_at=now,
                )])
                await tx.outbox.create([OutboxEvent(
                    id=new_id(), event_id=UUID(event_id),
                    aggregate_type="AgentRun", aggregate_id=run.id,
                    event_type="agent.run.failed", schema_version=SCHEMA_VERSION,
                    payload=json.loads(envelope.to_payload_json()),
                    trace_id=UUID(trace_id), created_at=now,
                )])
                await tx.audits.create(AuditLog(
                    id=new_id(), task_id=run.task_id, run_id=run.id,
                    event_type="run.queue.dead_letter", actor="system",
                    action_summary="运行队列处理重试耗尽",
                    details={
                        "source_event_id": source_event_id,
                        "delivery_count": delivery_count,
                        "last_error_code": error_code,
                    },
                    error=error, result_summary="failed", created_at=now,
                ))
                await tx.inbox.mark_processed(
                    "run-queue-dlq", source_event_id
                )
                await tx.commit()
        return await self.get_run(run_id)

    @staticmethod
    async def _fail_open_children(tx, run_id: UUID, error: dict, now) -> None:
        """Close non-terminal Step/ToolCall projections with their owning Run."""
        open_step_statuses = {
            StepStatus.PENDING,
            StepStatus.RUNNING,
            StepStatus.WAITING_FOR_PERMISSION,
        }
        for step in await tx.steps.list_by_run(run_id):
            if step.status not in open_step_statuses:
                continue
            step.status = StepStatus.FAILED
            step.error = deepcopy(error)
            step.completed_at = now
            if step.started_at is not None:
                step.duration_ms = max(
                    0, int((now - step.started_at).total_seconds() * 1000)
                )
            await tx.steps.update(step)

        for tool_call in await tx.tool_calls.list_by_run(run_id):
            if tool_call.status not in {"pending", "running"}:
                continue
            tool_call.status = "failed"
            tool_call.error = deepcopy(error)
            tool_call.completed_at = now
            if tool_call.started_at is not None:
                tool_call.duration_ms = max(
                    0, int((now - tool_call.started_at).total_seconds() * 1000)
                )
            await tx.tool_calls.update(tool_call)

    async def reconcile_expired_runs(self, limit: int = 32) -> dict[str, int]:
        """安全 checkpoint 重排；未知工具结果一律失败收口。"""
        result = {
            "runs_rescheduled": 0,
            "runs_failed_closed": 0,
        }
        now = utcnow()
        session_factory = self._uow_factory()
        async with session_factory() as session:
            uow = PostgresUnitOfWork(session)
            async with uow.transaction() as tx:
                runs = await tx.runs.list_expired_running(now, limit=limit)
                for run in runs:
                    checkpoint = run.checkpoint or {}
                    resumable = is_resumable_run_checkpoint(checkpoint)
                    raw_attempts = (
                        (checkpoint.get("state") or {}).get("recovery_attempts", 0)
                        if isinstance(checkpoint.get("state"), dict)
                        else 0
                    )
                    attempts = raw_attempts if isinstance(raw_attempts, int) else 0
                    if run.status == RunStatus.PAUSE_REQUESTED and resumable:
                        success = await tx.runs.update_with_lock(
                            run_id=run.id,
                            new_status=RunStatus.PAUSED.value,
                            expected_version=run.version,
                            expected_status=RunStatus.PAUSE_REQUESTED.value,
                            worker_id=None,
                            lease_until=None,
                        )
                        if not success:
                            continue
                        event_id = deterministic_event_id(
                            str(run.id), "agent.run.paused", 9100 + run.version
                        )
                        trace_id = str(run.trace_id or new_id())
                        runtime_event = build_runtime_event(
                            event_type="agent.run.paused",
                            task_id=str(run.task_id), run_id=str(run.id),
                            event_id=event_id,
                            payload={
                                "run_id": str(run.id),
                                "reason": "worker_lease_expired_after_pause_request",
                                "resume_node": checkpoint["resume_node"],
                            },
                        )
                        envelope = build_envelope(
                            runtime_event, trace_id, "reconciliation"
                        )
                        sequence = await tx.events.get_next_sequence(run.id)
                        await tx.events.append([RuntimeEvent(
                            id=UUID(event_id), event_id=UUID(event_id),
                            task_id=run.task_id, run_id=run.id,
                            type="agent.run.paused", event_sequence=sequence,
                            payload=runtime_event["payload"], created_at=now,
                        )])
                        await tx.outbox.create([OutboxEvent(
                            id=new_id(), event_id=UUID(event_id),
                            aggregate_type="AgentRun", aggregate_id=run.id,
                            event_type="agent.run.paused",
                            schema_version=SCHEMA_VERSION,
                            payload=json.loads(envelope.to_payload_json()),
                            trace_id=UUID(trace_id), created_at=now,
                        )])
                        await tx.audits.create(AuditLog(
                            id=new_id(), task_id=run.task_id, run_id=run.id,
                            event_type="run.pause.reconciled", actor="system",
                            action_summary="Worker 中断后在安全检查点确认暂停",
                            details={"resume_node": checkpoint["resume_node"]},
                            result_summary="paused", created_at=now,
                        ))
                        continue
                    if (
                        run.status == RunStatus.RUNNING
                        and resumable
                        and attempts < MAX_RUN_RECOVERY_ATTEMPTS
                    ):
                        success = await tx.runs.update_with_lock(
                            run_id=run.id,
                            new_status=RunStatus.PAUSED.value,
                            expected_version=run.version,
                            expected_status=RunStatus.RUNNING.value,
                            worker_id=None,
                            lease_until=None,
                        )
                        if not success:
                            continue
                        job_data = dict(checkpoint["job"])
                        event_id = new_id()
                        job_data.update({
                            "job_id": str(event_id),
                            "created_at": now.isoformat(),
                            "resume_from_checkpoint": True,
                            "type": "run.job",
                        })
                        await tx.outbox.create([OutboxEvent(
                            id=new_id(),
                            event_id=event_id,
                            aggregate_type="AgentRun",
                            aggregate_id=run.id,
                            event_type="run.resume.requested",
                            schema_version=SCHEMA_VERSION,
                            payload=job_data,
                            trace_id=run.trace_id or new_id(),
                            created_at=now,
                        )])
                        await tx.audits.create(AuditLog(
                            id=new_id(), task_id=run.task_id, run_id=run.id,
                            event_type="run.recovery.scheduled", actor="system",
                            action_summary="重新调度可安全恢复的运行",
                            details={"resume_node": checkpoint["resume_node"]},
                            created_at=now,
                        ))
                        result["runs_rescheduled"] += 1
                        continue

                    exhausted = resumable and attempts >= MAX_RUN_RECOVERY_ATTEMPTS
                    error = {
                        "code": (
                            "RUN_RECOVERY_EXHAUSTED"
                            if exhausted else "RUN_RECOVERY_UNSAFE"
                        ),
                        "message": (
                            "运行已达到最大恢复次数"
                            if exhausted
                            else "Worker 中断时工具结果未知，已阻止自动重放"
                        ),
                        "category": "runtime",
                        "recoverable": False,
                    }
                    success = await tx.runs.update_with_lock(
                        run_id=run.id,
                        new_status=RunStatus.FAILED.value,
                        expected_version=run.version,
                        expected_status=run.status.value,
                        failed_at=now,
                        error_json=error,
                        worker_id=None,
                        lease_until=None,
                        checkpoint_json={},
                    )
                    if not success:
                        continue
                    task = await tx.tasks.get(run.task_id)
                    if task is not None:
                        task.status = TaskStatus.FAILED
                        task.updated_at = now
                        await tx.tasks.update(task)
                    await self._fail_open_children(tx, run.id, error, now)
                    event_id = deterministic_event_id(
                        str(run.id), "agent.run.failed", 9000 + run.version
                    )
                    trace_id = str(run.trace_id or new_id())
                    runtime_event = build_runtime_event(
                        event_type="agent.run.failed",
                        task_id=str(run.task_id),
                        run_id=str(run.id),
                        event_id=event_id,
                        payload={"error": error},
                    )
                    envelope = build_envelope(runtime_event, trace_id, "reconciliation")
                    sequence = await tx.events.get_next_sequence(run.id)
                    await tx.events.append([RuntimeEvent(
                        id=UUID(event_id), event_id=UUID(event_id),
                        task_id=run.task_id, run_id=run.id,
                        type="agent.run.failed", event_sequence=sequence,
                        payload={"error": error}, created_at=now,
                    )])
                    await tx.outbox.create([OutboxEvent(
                        id=new_id(), event_id=UUID(event_id),
                        aggregate_type="AgentRun", aggregate_id=run.id,
                        event_type="agent.run.failed", schema_version=SCHEMA_VERSION,
                        payload=json.loads(envelope.to_payload_json()),
                        trace_id=UUID(trace_id), created_at=now,
                    )])
                    await tx.audits.create(AuditLog(
                        id=new_id(), task_id=run.task_id, run_id=run.id,
                        event_type="run.recovery.failed_closed", actor="system",
                        action_summary="运行恢复失败并安全收口",
                        details={
                            "resume_node": checkpoint.get("resume_node", "missing"),
                            "recovery_attempts": attempts,
                        },
                        error=error, result_summary="failed", created_at=now,
                    ))
                    result["runs_failed_closed"] += 1
                await tx.commit()
        return result

    async def reconcile_stale_queued_runs(
        self,
        *,
        queue_event_exists: Callable[[UUID], Awaitable[bool]] | None = None,
        stale_seconds: int = 60,
        limit: int = 32,
    ) -> dict[str, int]:
        """从 PostgreSQL 权威状态重投长期未被领取的 queued Run。

        仅当最近一次 durable queue job 已标记 delivered 且也已超过宽限期时
        创建新的 OutboxEvent。pending/dispatching 仍由 OutboxPublisher 拥有，dead
        仍须显式诊断，避免绕过既有重试预算。
        """
        result = {
            "queued_runs_requeued": 0,
            "queued_runs_failed_closed": 0,
        }
        now = utcnow()
        cutoff = now - timedelta(seconds=max(1, stale_seconds))
        session_factory = self._uow_factory()
        async with session_factory() as session:
            uow = PostgresUnitOfWork(session)
            async with uow.transaction() as tx:
                runs = await tx.runs.list_stale_queued(cutoff, limit=limit)
                for run in runs:
                    latest = await tx.outbox.get_latest_run_job(run.id)
                    if (
                        latest is None
                        or latest.status.value != "delivered"
                        or str(latest.payload.get("run_id", "")) != str(run.id)
                        or str(latest.payload.get("task_id", "")) != str(run.task_id)
                    ):
                        continue
                    # 仅凭 queued 超时无法区分 Redis 丢失和正常积压。没有明确的
                    # Redis 缺失证据时保守跳过，避免把背压误判为消息丢失。
                    if queue_event_exists is None or await queue_event_exists(
                        latest.event_id
                    ):
                        continue
                    raw_attempt = latest.payload.get("queue_reconciliation_attempt", 0)
                    attempt = raw_attempt if isinstance(raw_attempt, int) else 0
                    retry_after = latest.created_at + timedelta(
                        seconds=max(1, stale_seconds) * (2 ** min(attempt, 8))
                    )
                    if retry_after > now:
                        continue
                    if attempt >= MAX_QUEUED_RUN_RECONCILIATION_ATTEMPTS:
                        error = {
                            "code": "RUN_QUEUE_RECONCILIATION_EXHAUSTED",
                            "message": "运行多次投递后仍未被 Worker 领取",
                            "category": "runtime",
                            "recoverable": False,
                        }
                        claimed = await tx.runs.update_with_lock(
                            run_id=run.id,
                            new_status=RunStatus.FAILED.value,
                            expected_version=run.version,
                            expected_status=RunStatus.QUEUED.value,
                            failed_at=now,
                            error_json=error,
                        )
                        if not claimed:
                            continue
                        task = await tx.tasks.get(run.task_id)
                        if task is not None:
                            task.status = TaskStatus.FAILED
                            task.updated_at = now
                            await tx.tasks.update(task)
                        failed_event_id = deterministic_event_id(
                            str(run.id), "agent.run.failed", 9200 + run.version
                        )
                        trace_id = str(run.trace_id or latest.trace_id)
                        runtime_event = build_runtime_event(
                            event_type="agent.run.failed",
                            task_id=str(run.task_id),
                            run_id=str(run.id),
                            event_id=failed_event_id,
                            payload={"error": error},
                        )
                        envelope = build_envelope(
                            runtime_event, trace_id, "reconciliation"
                        )
                        sequence = await tx.events.get_next_sequence(run.id)
                        await tx.events.append([RuntimeEvent(
                            id=UUID(failed_event_id),
                            event_id=UUID(failed_event_id),
                            task_id=run.task_id,
                            run_id=run.id,
                            type="agent.run.failed",
                            event_sequence=sequence,
                            payload={"error": error},
                            created_at=now,
                        )])
                        await tx.outbox.create([OutboxEvent(
                            id=new_id(),
                            event_id=UUID(failed_event_id),
                            aggregate_type="AgentRun",
                            aggregate_id=run.id,
                            event_type="agent.run.failed",
                            schema_version=SCHEMA_VERSION,
                            payload=json.loads(envelope.to_payload_json()),
                            trace_id=UUID(trace_id),
                            created_at=now,
                        )])
                        await tx.audits.create(AuditLog(
                            id=new_id(),
                            task_id=run.task_id,
                            run_id=run.id,
                            event_type="run.queue.reconciliation_exhausted",
                            actor="system",
                            action_summary="Run Queue 对账重投达到上限并安全收口",
                            details={"attempts": attempt},
                            error=error,
                            result_summary="failed",
                            created_at=now,
                        ))
                        result["queued_runs_failed_closed"] += 1
                        continue
                    event_id = new_id()
                    payload = deepcopy(latest.payload)
                    payload.update({
                        "job_id": str(event_id),
                        "created_at": now.isoformat(),
                        "reconciled_from_event_id": str(latest.event_id),
                        "queue_reconciliation_attempt": attempt + 1,
                    })
                    claimed = await tx.runs.update_with_lock(
                        run_id=run.id,
                        new_status=RunStatus.QUEUED.value,
                        expected_version=run.version,
                        expected_status=RunStatus.QUEUED.value,
                    )
                    if not claimed:
                        continue
                    await tx.outbox.create([OutboxEvent(
                        id=new_id(),
                        event_id=event_id,
                        aggregate_type="AgentRun",
                        aggregate_id=run.id,
                        event_type="run.queue.reconciled",
                        schema_version=SCHEMA_VERSION,
                        payload=payload,
                        trace_id=run.trace_id or latest.trace_id,
                        correlation_id=latest.event_id,
                        created_at=now,
                    )])
                    await tx.audits.create(AuditLog(
                        id=new_id(),
                        task_id=run.task_id,
                        run_id=run.id,
                        event_type="run.queue.reconciled",
                        actor="system",
                        action_summary="重新投递长期未被 Worker 领取的 queued Run",
                        details={"previous_event_id": str(latest.event_id)},
                        result_summary="queued",
                        created_at=now,
                    ))
                    result["queued_runs_requeued"] += 1
                await tx.commit()
        return result

    async def cancel_run(self, run_id: UUID, reason: str = "") -> AgentRun:
        """用户请求取消（queued/running → cancel_requested）。

        幂等：如果已是 cancel_requested/cancelling/cancelled，返回当前状态。
        """
        session_factory = self._uow_factory()
        async with session_factory() as session:
            uow = PostgresUnitOfWork(session)
            async with uow.transaction() as tx:
                run = await tx.runs.get(run_id)
                if run is None:
                    raise errors.not_found("AgentRun", str(run_id))

                # 幂等：已处于取消相关状态
                if run.status in (RunStatus.CANCEL_REQUESTED, RunStatus.CANCELLING, RunStatus.CANCELLED):
                    await tx.commit()
                    return run

                # 终态不可取消
                if run.status in TERMINAL_STATUSES:
                    raise errors.run_already_terminal(str(run_id), run.status.value)

                self._check_transition(run.status, RunStatus.CANCEL_REQUESTED)

                success = await tx.runs.update_with_lock(
                    run_id=run_id,
                    new_status=RunStatus.CANCEL_REQUESTED.value,
                    expected_version=run.version,
                )
                if not success:
                    raise errors.run_version_conflict(str(run_id))

                command_id = new_id()
                trace_id = run.trace_id or new_id()
                now = utcnow()
                await tx.outbox.create([OutboxEvent(
                    id=new_id(),
                    event_id=command_id,
                    aggregate_type="AgentRun",
                    aggregate_id=run.id,
                    event_type="run.cancel.requested",
                    schema_version=SCHEMA_VERSION,
                    payload={
                        "command_id": str(command_id),
                        "trace_id": str(trace_id),
                        "task_id": str(run.task_id),
                        "run_id": str(run.id),
                        "type": "run.cancel",
                        "requested_at": now.isoformat(),
                        "reason": reason,
                        "schema_version": SCHEMA_VERSION,
                    },
                    trace_id=trace_id,
                    created_at=now,
                )])
                await tx.audits.create(AuditLog(
                    id=new_id(), task_id=run.task_id, run_id=run.id,
                    event_type="run.cancel.requested", actor="user",
                    action_summary="用户请求取消运行",
                    details={"reason": reason} if reason else {},
                    created_at=now,
                ))

                await tx.commit()

        return await self.get_run(run_id)

    async def pause_run(self, run_id: UUID, reason: str = "") -> AgentRun:
        """请求在下一个安全 checkpoint 暂停 active Run。

        这里只写入 pause_requested 和 durable command；只有 Worker 在 effect
        边界发出 agent.run.paused 后，Run 才会进入 paused。
        """
        session_factory = self._uow_factory()
        async with session_factory() as session:
            uow = PostgresUnitOfWork(session)
            async with uow.transaction() as tx:
                run = await tx.runs.get(run_id)
                if run is None:
                    raise errors.not_found("AgentRun", str(run_id))
                if run.status in (RunStatus.PAUSE_REQUESTED, RunStatus.PAUSED):
                    await tx.commit()
                    return run
                if run.status in TERMINAL_STATUSES:
                    raise errors.run_already_terminal(str(run_id), run.status.value)
                if run.status != RunStatus.RUNNING:
                    raise errors.invalid_state_transition(
                        run.status.value, RunStatus.PAUSE_REQUESTED.value
                    )

                self._check_transition(run.status, RunStatus.PAUSE_REQUESTED)
                success = await tx.runs.update_with_lock(
                    run_id=run.id,
                    new_status=RunStatus.PAUSE_REQUESTED.value,
                    expected_version=run.version,
                    expected_status=RunStatus.RUNNING.value,
                )
                if not success:
                    raise errors.run_version_conflict(str(run_id))

                command_id = new_id()
                trace_id = run.trace_id or new_id()
                now = utcnow()
                await tx.outbox.create([OutboxEvent(
                    id=new_id(), event_id=command_id,
                    aggregate_type="AgentRun", aggregate_id=run.id,
                    event_type="run.pause.requested", schema_version=SCHEMA_VERSION,
                    payload={
                        "command_id": str(command_id),
                        "trace_id": str(trace_id),
                        "task_id": str(run.task_id),
                        "run_id": str(run.id),
                        "type": "run.pause",
                        "requested_at": now.isoformat(),
                        "reason": reason,
                        "schema_version": SCHEMA_VERSION,
                    },
                    trace_id=trace_id, created_at=now,
                )])
                await tx.audits.create(AuditLog(
                    id=new_id(), task_id=run.task_id, run_id=run.id,
                    event_type="run.pause.requested", actor="user",
                    action_summary="用户请求暂停运行",
                    details={"reason": reason} if reason else {},
                    created_at=now,
                ))
                await tx.commit()
        return await self.get_run(run_id)

    async def resume_run(self, run_id: UUID) -> AgentRun:
        """把用户确认恢复的 paused Run 通过 Outbox 重新入队。"""
        session_factory = self._uow_factory()
        async with session_factory() as session:
            uow = PostgresUnitOfWork(session)
            async with uow.transaction() as tx:
                run = await tx.runs.get(run_id)
                if run is None:
                    raise errors.not_found("AgentRun", str(run_id))
                if run.status == RunStatus.RESUME_REQUESTED:
                    await tx.commit()
                    return run
                if run.status in TERMINAL_STATUSES:
                    raise errors.run_already_terminal(str(run_id), run.status.value)
                if (
                    run.status != RunStatus.PAUSED
                    or not is_resumable_run_checkpoint(run.checkpoint)
                ):
                    raise errors.invalid_state_transition(
                        run.status.value, RunStatus.RESUME_REQUESTED.value
                    )

                self._check_transition(run.status, RunStatus.RESUME_REQUESTED)
                success = await tx.runs.update_with_lock(
                    run_id=run.id,
                    new_status=RunStatus.RESUME_REQUESTED.value,
                    expected_version=run.version,
                    expected_status=RunStatus.PAUSED.value,
                    worker_id=None,
                    lease_until=None,
                )
                if not success:
                    raise errors.run_version_conflict(str(run_id))

                now = utcnow()
                job_data = dict(run.checkpoint["job"])
                event_id = new_id()
                job_data.update({
                    "job_id": str(event_id),
                    "created_at": now.isoformat(),
                    "resume_from_checkpoint": True,
                    "type": "run.job",
                })
                await tx.outbox.create([OutboxEvent(
                    id=new_id(), event_id=event_id,
                    aggregate_type="AgentRun", aggregate_id=run.id,
                    event_type="run.resume.requested", schema_version=SCHEMA_VERSION,
                    payload=job_data,
                    trace_id=run.trace_id or new_id(), created_at=now,
                )])
                await tx.audits.create(AuditLog(
                    id=new_id(), task_id=run.task_id, run_id=run.id,
                    event_type="run.resume.requested", actor="user",
                    action_summary="用户请求恢复运行",
                    details={"resume_node": run.checkpoint["resume_node"]},
                    created_at=now,
                ))
                await tx.commit()
        return await self.get_run(run_id)

    async def update_run_status(
        self,
        run_id: UUID,
        new_status: RunStatus,
        expected_version: int,
        **extra_fields,
    ) -> AgentRun:
        """通用状态更新（带乐观锁）。

        Raises:
            AppError: 状态迁移非法或 version 冲突。
        """
        session_factory = self._uow_factory()
        async with session_factory() as session:
            uow = PostgresUnitOfWork(session)
            async with uow.transaction() as tx:
                run = await tx.runs.get(run_id)
                if run is None:
                    raise errors.not_found("AgentRun", str(run_id))

                if run.status not in TERMINAL_STATUSES:
                    self._check_transition(run.status, new_status)

                success = await tx.runs.update_with_lock(
                    run_id=run_id,
                    new_status=new_status.value,
                    expected_version=expected_version,
                    **extra_fields,
                )
                if not success:
                    raise errors.run_version_conflict(str(run_id))

                await tx.commit()

        return await self.get_run(run_id)

    async def get_run(self, run_id: UUID) -> Optional[AgentRun]:
        """查询 Run。"""
        session_factory = self._uow_factory()
        async with session_factory() as session:
            uow = PostgresUnitOfWork(session)
            return await uow.runs.get(run_id)

    async def get_runs_by_task(self, task_id: UUID) -> list[AgentRun]:
        """查询 Task 的所有 Run。"""
        session_factory = self._uow_factory()
        async with session_factory() as session:
            uow = PostgresUnitOfWork(session)
            return await uow.runs.list_by_task(task_id)

    async def get_task(self, task_id: UUID) -> Optional:
        """查询 Task。"""
        session_factory = self._uow_factory()
        async with session_factory() as session:
            uow = PostgresUnitOfWork(session)
            return await uow.tasks.get(task_id)

    async def list_tasks(self, limit: int = 50, offset: int = 0) -> list:
        """查询 Task 列表。"""
        session_factory = self._uow_factory()
        async with session_factory() as session:
            uow = PostgresUnitOfWork(session)
            return await uow.tasks.list_all(limit=limit, offset=offset)
