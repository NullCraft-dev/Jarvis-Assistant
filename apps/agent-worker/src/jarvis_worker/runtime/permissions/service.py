"""PermissionApplicationService — 权限决策、过期与终态收口。"""

import json
import logging
from copy import deepcopy
from uuid import NAMESPACE_URL, UUID, uuid5

from jarvis_worker.database.unit_of_work import PostgresUnitOfWork
from jarvis_worker.runtime.events import build_envelope, build_runtime_event
from jarvis_worker.runtime.permissions.policy import permission_request_is_expired
from jarvis_worker.shared.domain.models import (
    AuditLog,
    OutboxEvent,
    PermissionGrant,
    PermissionRequest,
    PermissionStatus,
    RunStatus,
    RuntimeEvent,
    StepStatus,
    TaskStatus,
    new_id,
    utcnow,
)
from jarvis_worker.shared.errors import application as errors

logger = logging.getLogger(__name__)

SCHEMA_VERSION = "2B-1a.1"
VALID_DECISIONS = {
    "allow_once",
    "allow_for_task",
    "always_allow_for_tool_and_path",
    "always_allow_for_workspace",
    "deny",
}
GRANT_TYPE_BY_DECISION = {
    "allow_for_task": "task",
    "always_allow_for_tool_and_path": "tool_path",
    "always_allow_for_workspace": "workspace",
}


class PermissionApplicationService:
    """Permission Application Service。

    处理权限请求的创建、决策和授权规则。
    """

    def __init__(self, uow_factory):
        self._uow_factory = uow_factory

    async def create_request(self, req: PermissionRequest) -> PermissionRequest:
        """创建权限请求。"""
        session_factory = self._uow_factory()
        async with session_factory() as session:
            uow = PostgresUnitOfWork(session)
            async with uow.transaction() as tx:
                await tx.permissions.create_request(req)
                await tx.commit()
        return req

    async def decide(
        self,
        request_id: UUID,
        decision: str,
        note: str = "",
    ) -> PermissionRequest:
        """做出权限决定。

        幂等：重复提交相同 decision 返回已有结果。
        冲突：提交不同于已有的 decision 返回 CONFLICT AppError。
        """
        expired_during_decision = False
        session_factory = self._uow_factory()
        async with session_factory() as session:
            uow = PostgresUnitOfWork(session)
            async with uow.transaction() as tx:
                req = await tx.permissions.get_request_for_update(request_id)
                if req is None:
                    raise errors.not_found("PermissionRequest", str(request_id))

                validate_permission_decision(req, decision)

                # 幂等：已有决定
                if req.status in (PermissionStatus.APPROVED, PermissionStatus.DENIED, PermissionStatus.CONSUMED):
                    if req.decision == decision:
                        await self._sync_tool_call_permission(tx, req)
                        await tx.commit()
                        return req
                    # 冲突
                    raise errors.permission_conflict(
                        str(request_id), req.decision or "unknown", decision
                    )

                if req.status != PermissionStatus.PENDING:
                    raise errors.permission_not_pending(
                        str(request_id), req.status.value
                    )

                run = await tx.runs.get(req.run_id)
                if run is None:
                    raise errors.not_found("AgentRun", str(req.run_id))
                now = utcnow()
                if permission_request_is_expired(expires_at=req.expires_at, now=now):
                    await self._expire_request_locked(tx, req, now)
                    await tx.commit()
                    expired_during_decision = True
                elif run.status != RunStatus.WAITING_PERMISSION:
                    raise errors.permission_not_pending(
                        str(request_id), run.status.value
                    )
                else:
                    # 更新为新的决定
                    new_status = (
                        PermissionStatus.APPROVED
                        if decision != "deny"
                        else PermissionStatus.DENIED
                    )
                    req.status = new_status
                    req.decision = decision
                    req.decided_at = now
                    req.note = note
                    await tx.permissions.update_request(req)

                    # PermissionRequest and ToolCall are two projections of the same
                    # decision. Persist both atomically so a worker crash after the
                    # click cannot leave the UI showing a permanently pending call.
                    await self._sync_tool_call_permission(tx, req)

                    # 如果永久允许，创建 PermissionGrant
                    grant_type = GRANT_TYPE_BY_DECISION.get(decision)
                    if grant_type:
                        grant = PermissionGrant(
                            id=new_id(),
                            grant_type=grant_type,
                            tool_name=req.tool_name,
                            risk_level_max=req.risk_level,
                            workspace_path=req.scope.get("workspace_path"),
                            path=req.scope.get("path"),
                            created_from_request_id=req.id,
                            created_at=now,
                        )
                        await tx.permissions.create_grant(grant)

                    # 写入 OutboxEvent
                    trace_id = run.trace_id if run is not None else None
                    trace_id = trace_id or new_id()
                    command_id = new_id()
                    outbox = OutboxEvent(
                        id=new_id(),
                        event_id=new_id(),
                        aggregate_type="PermissionRequest",
                        aggregate_id=req.id,
                        event_type="permission.decision",
                        schema_version=SCHEMA_VERSION,
                        payload={
                            "command_id": str(command_id),
                            "trace_id": str(trace_id),
                            "request_id": str(request_id),
                            "task_id": str(req.task_id),
                            "run_id": str(req.run_id),
                            "decision": decision,
                            "note": note,
                            "decided_at": now.isoformat(),
                            "type": "permission.decision",
                            "schema_version": SCHEMA_VERSION,
                        },
                        trace_id=trace_id,
                        created_at=now,
                    )
                    await tx.outbox.create([outbox])
                    await tx.audits.create(AuditLog(
                        id=new_id(), task_id=req.task_id, run_id=req.run_id,
                        step_id=req.step_id, tool_call_id=req.tool_call_id,
                        event_type="permission.decision", actor="user",
                        risk_level=req.risk_level, permission_decision=decision,
                        action_summary=req.action_summary,
                        details={"request_id": str(req.id), "note": note},
                        result_summary=(
                            "approved"
                            if new_status == PermissionStatus.APPROVED
                            else "denied"
                        ),
                        created_at=now,
                    ))
                    await tx.commit()

        if expired_during_decision:
            raise errors.permission_not_pending(
                str(request_id), PermissionStatus.EXPIRED.value
            )

        return req

    async def expire_pending_requests(self, limit: int = 32) -> int:
        """Expire a bounded durable batch and fail waiting Runs closed."""
        if limit < 1 or limit > 256:
            raise ValueError("permission expiry limit 必须在 1..256")
        now = utcnow()
        session_factory = self._uow_factory()
        async with session_factory() as session:
            uow = PostgresUnitOfWork(session)
            async with uow.transaction() as tx:
                requests = await tx.permissions.list_expired_pending_for_update(
                    now, limit=limit
                )
                for req in requests:
                    await self._expire_request_locked(tx, req, now)
                await tx.commit()
        return len(requests)

    async def _expire_request_locked(self, tx, req: PermissionRequest, now) -> None:
        if req.status != PermissionStatus.PENDING:
            return
        req.status = PermissionStatus.EXPIRED
        req.decided_at = now
        req.note = "deadline_elapsed"
        await tx.permissions.update_request(req)

        error = {
            "code": "PERMISSION_REQUEST_EXPIRED",
            "message": "授权请求已过期，操作未执行",
            "category": "permission",
            "recoverable": False,
        }
        if req.tool_call_id is not None:
            tool_call = await tx.tool_calls.get(req.tool_call_id)
            if tool_call is not None and tool_call.permission_status == "pending":
                tool_call.permission_request_id = req.id
                tool_call.permission_status = "expired"
                if tool_call.status in {"pending", "running"}:
                    tool_call.status = "failed"
                    tool_call.error = deepcopy(error)
                    tool_call.completed_at = now
                    if tool_call.started_at is not None:
                        tool_call.duration_ms = max(
                            0, int((now - tool_call.started_at).total_seconds() * 1000)
                        )
                await tx.tool_calls.update(tool_call)

        if req.step_id is not None:
            step = await tx.steps.get(req.step_id)
            if step is not None and step.status in {
                StepStatus.PENDING,
                StepStatus.RUNNING,
                StepStatus.WAITING_FOR_PERMISSION,
            }:
                step.status = StepStatus.FAILED
                step.error = deepcopy(error)
                step.summary = "授权请求已过期"
                step.completed_at = now
                if step.started_at is not None:
                    step.duration_ms = max(
                        0, int((now - step.started_at).total_seconds() * 1000)
                    )
                await tx.steps.update(step)

        run = await tx.runs.get(req.run_id)
        if run is not None and run.status == RunStatus.WAITING_PERMISSION:
            sequence = await tx.events.get_next_sequence(req.run_id)
            transitioned = await tx.runs.update_with_lock(
                run_id=run.id,
                new_status=RunStatus.FAILED.value,
                expected_version=run.version,
                expected_status=RunStatus.WAITING_PERMISSION.value,
                failed_at=now,
                error_json=error,
                worker_id=None,
                lease_until=None,
                checkpoint_json={},
            )
            if not transitioned:
                raise RuntimeError(f"Permission 过期时 Run 状态并发冲突: {run.id}")
            task = await tx.tasks.get(req.task_id)
            if task is not None:
                task.status = TaskStatus.FAILED
                task.updated_at = now
                await tx.tasks.update(task)
            await self._append_expiry_events(
                tx,
                req=req,
                run=run,
                error=error,
                now=now,
                start_sequence=sequence,
            )

        await tx.audits.create(AuditLog(
            id=new_id(), task_id=req.task_id, run_id=req.run_id,
            step_id=req.step_id, tool_call_id=req.tool_call_id,
            event_type="permission.expired", actor="system",
            risk_level=req.risk_level, action_summary=req.action_summary,
            details={
                "request_id": str(req.id),
                "reason": req.note,
                "expires_at": req.expires_at.isoformat() if req.expires_at else None,
            },
            error=error, result_summary="expired", created_at=now,
        ))

    @staticmethod
    async def _append_expiry_events(
        tx,
        *,
        req: PermissionRequest,
        run,
        error: dict,
        now,
        start_sequence: int,
    ) -> None:
        trace_id = run.trace_id or new_id()
        expiry_event_id = uuid5(
            NAMESPACE_URL, f"jarvis:permission-expired:{req.id}"
        )
        failed_event_id = uuid5(
            NAMESPACE_URL, f"jarvis:permission-expired-run-failed:{req.id}"
        )
        expiry_payload = {
            "request_id": str(req.id),
            "tool_call_id": str(req.tool_call_id) if req.tool_call_id else None,
            "reason": "deadline_elapsed",
            "expires_at": req.expires_at.isoformat() if req.expires_at else None,
            "permission_status": "expired",
        }
        definitions = (
            (
                expiry_event_id,
                "permission.expired",
                expiry_payload,
                start_sequence,
                req.step_id,
            ),
            (
                failed_event_id,
                "agent.run.failed",
                {"error": error},
                start_sequence + 1,
                None,
            ),
        )
        runtime_events: list[RuntimeEvent] = []
        outboxes: list[OutboxEvent] = []
        for event_id, event_type, payload, sequence, step_id in definitions:
            event = build_runtime_event(
                event_type=event_type,
                task_id=str(req.task_id),
                run_id=str(req.run_id),
                step_id=str(step_id) if step_id else "",
                event_id=str(event_id),
                payload=payload,
            )
            event["timestamp"] = now.isoformat()
            envelope = build_envelope(event, str(trace_id), "permission-reconciliation")
            runtime_events.append(RuntimeEvent(
                id=event_id,
                event_id=event_id,
                task_id=req.task_id,
                run_id=req.run_id,
                step_id=step_id,
                type=event_type,
                event_sequence=sequence,
                payload=payload,
                created_at=now,
            ))
            outboxes.append(OutboxEvent(
                id=new_id(),
                event_id=event_id,
                aggregate_type="AgentRun",
                aggregate_id=req.run_id,
                event_type=event_type,
                schema_version=SCHEMA_VERSION,
                payload=json.loads(envelope.to_payload_json()),
                trace_id=trace_id,
                created_at=now,
            ))
        await tx.events.append(runtime_events)
        await tx.outbox.create(outboxes)

    @staticmethod
    async def _sync_tool_call_permission(tx, req: PermissionRequest) -> None:
        if req.tool_call_id is None:
            return
        tool_call = await tx.tool_calls.get(req.tool_call_id)
        if tool_call is None:
            return
        tool_call.permission_request_id = req.id
        if req.status in (PermissionStatus.APPROVED, PermissionStatus.CONSUMED):
            tool_call.permission_status = "approved"
        elif req.status == PermissionStatus.DENIED:
            tool_call.permission_status = "denied"
        elif req.status == PermissionStatus.EXPIRED:
            tool_call.permission_status = "expired"
        else:
            return
        await tx.tool_calls.update(tool_call)

    async def get_pending_by_run(self, run_id: UUID) -> list[PermissionRequest]:
        """查询 Run 的待处理权限请求。"""
        await self.expire_pending_requests()
        session_factory = self._uow_factory()
        async with session_factory() as session:
            uow = PostgresUnitOfWork(session)
            if await uow.runs.get(run_id) is None:
                raise errors.not_found("AgentRun", str(run_id))
            return await uow.permissions.list_pending_by_run(run_id)

    async def get_request(self, request_id: UUID) -> PermissionRequest | None:
        """读取权限请求及其内部恢复检查点（仅供 Worker Runtime）。"""
        session_factory = self._uow_factory()
        async with session_factory() as session:
            uow = PostgresUnitOfWork(session)
            return await uow.permissions.get_request(request_id)


def validate_permission_decision(req: PermissionRequest, decision: str) -> None:
    """集中校验权限决定，供 Control Plane 和单元测试复用。"""
    if decision not in VALID_DECISIONS:
        raise errors.validation_error(f"不支持的权限决定: {decision}")
    if decision not in req.allowed_decisions:
        raise errors.validation_error(f"当前权限请求不允许决定: {decision}")
    if req.risk_level in ("L4", "L5") and decision in GRANT_TYPE_BY_DECISION:
        raise errors.validation_error("L4/L5 高风险操作不允许永久授权")
