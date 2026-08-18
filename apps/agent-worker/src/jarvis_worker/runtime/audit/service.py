"""AuditLog 查询与保留 Application Service。

审计日志仍由各业务服务写入；本模块提供有界安全投影、导出和 L4 保留执行。
原始 details/error 不得穿透到 Web DTO。
"""

from __future__ import annotations

import asyncio
import base64
import csv
import hashlib
import io
import json
import logging
import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, AsyncIterator
from uuid import NAMESPACE_URL, UUID, uuid5

from jarvis_worker.database.unit_of_work import PostgresUnitOfWork
from jarvis_worker.runtime.permissions.policy import (
    permission_request_deadline,
    permission_request_is_expired,
)
from jarvis_worker.shared.domain.models import (
    AgentRun,
    AuditLog,
    Conversation,
    PermissionRequest,
    PermissionStatus,
    RunStatus,
    Task,
    TaskStatus,
    new_id,
    utcnow,
)
from jarvis_worker.shared.errors import application as errors
from jarvis_worker.shared.observability.logging import sanitize_message

DEFAULT_AUDIT_LIMIT = 50
MAX_AUDIT_LIMIT = 100
AUDIT_EXPORT_PAGE_SIZE = 100
DEFAULT_AUDIT_EXPORT_MAX_ROWS = 5_000
MAX_AUDIT_EXPORT_ROWS = 10_000
MIN_AUDIT_EXPORT_BYTES = 1_024
DEFAULT_AUDIT_EXPORT_MAX_BYTES = 5 * 1024 * 1024
MAX_AUDIT_EXPORT_BYTES = 10 * 1024 * 1024
AUDIT_RETENTION_PAGE_SIZE = 100
DEFAULT_AUDIT_RETENTION_STANDARD_DAYS = 90
DEFAULT_AUDIT_RETENTION_EXTENDED_DAYS = 365
MIN_AUDIT_RETENTION_DAYS = 30
MAX_AUDIT_RETENTION_DAYS = 3_650
DEFAULT_AUDIT_RETENTION_MAX_SCAN = 1_000
MAX_AUDIT_RETENTION_SCAN = 10_000
DEFAULT_AUDIT_RETENTION_MAX_CANDIDATES = 100
MAX_AUDIT_RETENTION_CANDIDATES = 1_000
_MAX_TEXT = 240
_MAX_DETAILS_ITEMS = 20
_EXPORT_FIELDS = (
    "id",
    "event_type",
    "actor",
    "action_summary",
    "task_id",
    "run_id",
    "step_id",
    "tool_call_id",
    "risk_level",
    "permission_decision",
    "result_summary",
    "error_code",
    "details_summary",
    "created_at",
)
_SENSITIVE_KEY_PARTS = (
    "api_key",
    "apikey",
    "authorization",
    "password",
    "secret",
    "token",
    "cookie",
    "credential",
    "passwd",
    "pwd",
    "access_key",
    "private_key",
    "content",
    "prompt",
    "stack",
    "traceback",
)
_URL_CREDENTIALS = re.compile(
    r"\b(?P<scheme>https?://)[^/\s:@]+:[^@/\s]+@",
    re.IGNORECASE,
)
_PERMANENT_RETENTION_EVENT_PARTS = (
    "cleanup",
    "delete",
    "purge",
    "revoke",
    "recover",
    "repair",
    "restore",
)
AUDIT_RETENTION_TOOL_NAME = "audit.apply_retention_policy"
_AUDIT_RETENTION_CONVERSATION_ID = uuid5(
    NAMESPACE_URL, "jarvis:audit-retention-maintenance"
)
logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class AuditLogQueryItem:
    id: str
    event_type: str
    actor: str
    action_summary: str
    task_id: str | None
    run_id: str | None
    step_id: str | None
    tool_call_id: str | None
    risk_level: str | None
    permission_decision: str | None
    result_summary: str | None
    error_code: str | None
    details_summary: dict[str, Any]
    created_at: str


@dataclass(frozen=True)
class AuditRetentionPreview:
    dry_run: bool
    standard_days: int
    extended_days: int
    standard_before: str
    extended_before: str
    max_scan: int
    max_candidates: int
    scanned_records: int
    candidate_records: int
    protected_records: int
    extended_retained_records: int
    has_more: bool


@dataclass(frozen=True)
class AuditRetentionResolution:
    request: PermissionRequest
    deleted_records: int
    has_more: bool


@dataclass(frozen=True)
class _AuditRetentionScan:
    preview: AuditRetentionPreview
    candidate_ids: tuple[UUID, ...]
    candidate_sha256: str


class AuditQueryApplicationService:
    """审计查询/导出/保留 owner；通过 Repository interface 访问 Storage。"""

    def __init__(self, uow_factory):
        self._uow_factory = uow_factory

    async def list_audit_logs(
        self,
        *,
        limit: int = DEFAULT_AUDIT_LIMIT,
        event_type: str | None = None,
        actor: str | None = None,
        task_id: UUID | None = None,
        run_id: UUID | None = None,
        before: str | None = None,
    ) -> dict[str, Any]:
        limit = min(max(limit, 1), MAX_AUDIT_LIMIT)
        before_created_at, before_id = _decode_cursor(before) if before else (None, None)

        logs = await self._list_page(
            limit=limit + 1,
            event_type=event_type,
            actor=actor,
            task_id=task_id,
            run_id=run_id,
            before_created_at=before_created_at,
            before_id=before_id,
        )

        has_more = len(logs) > limit
        page = logs[:limit]
        next_cursor = _encode_cursor(page[-1]) if has_more and page else None
        return {"audit_logs": [_to_safe_item(log) for log in page], "next_cursor": next_cursor}

    def export_audit_logs(
        self,
        *,
        export_format: str = "jsonl",
        max_rows: int = DEFAULT_AUDIT_EXPORT_MAX_ROWS,
        max_bytes: int = DEFAULT_AUDIT_EXPORT_MAX_BYTES,
        event_type: str | None = None,
        actor: str | None = None,
        task_id: UUID | None = None,
        run_id: UUID | None = None,
        before: str | None = None,
    ) -> AsyncIterator[bytes]:
        """创建有界导出流；参数和 cursor 在响应开始前完成校验。"""
        if export_format not in {"jsonl", "csv"}:
            raise errors.validation_error("审计导出格式只支持 jsonl 或 csv")
        if isinstance(max_rows, bool) or not 1 <= max_rows <= MAX_AUDIT_EXPORT_ROWS:
            raise errors.validation_error(
                f"审计导出 max_rows 必须在 1-{MAX_AUDIT_EXPORT_ROWS}"
            )
        if (
            isinstance(max_bytes, bool)
            or not MIN_AUDIT_EXPORT_BYTES <= max_bytes <= MAX_AUDIT_EXPORT_BYTES
        ):
            raise errors.validation_error(
                "审计导出 max_bytes 必须在 "
                f"{MIN_AUDIT_EXPORT_BYTES}-{MAX_AUDIT_EXPORT_BYTES}"
            )
        before_created_at, before_id = _decode_cursor(before) if before else (None, None)
        filters = {
            "event_type": _clip(event_type, 50) if event_type else None,
            "actor": _clip(actor, 100) if actor else None,
            "task_id": str(task_id) if task_id else None,
            "run_id": str(run_id) if run_id else None,
            "before_set": before is not None,
        }
        return self._stream_audit_logs(
            export_format=export_format,
            max_rows=max_rows,
            max_bytes=max_bytes,
            event_type=event_type,
            actor=actor,
            task_id=task_id,
            run_id=run_id,
            before_created_at=before_created_at,
            before_id=before_id,
            filters=filters,
        )

    async def preview_retention(
        self,
        *,
        standard_days: int = DEFAULT_AUDIT_RETENTION_STANDARD_DAYS,
        extended_days: int = DEFAULT_AUDIT_RETENTION_EXTENDED_DAYS,
        max_scan: int = DEFAULT_AUDIT_RETENTION_MAX_SCAN,
        max_candidates: int = DEFAULT_AUDIT_RETENTION_MAX_CANDIDATES,
        now: datetime | None = None,
    ) -> AuditRetentionPreview:
        """只读扫描候选；不归档、不删除，也不返回候选正文或 ID。"""
        _validate_retention_bounds(
            standard_days=standard_days,
            extended_days=extended_days,
            max_scan=max_scan,
            max_candidates=max_candidates,
        )
        evaluated_at = now or utcnow()
        if evaluated_at.tzinfo is None:
            raise errors.validation_error("审计保留预演 now 必须包含时区")
        scan = await self._scan_retention(
            repository=None,
            standard_days=standard_days,
            extended_days=extended_days,
            max_scan=max_scan,
            max_candidates=max_candidates,
            evaluated_at=evaluated_at,
        )
        preview = scan.preview
        await self._record_retention_preview_audit(preview)
        return preview

    async def create_retention_request(
        self,
        *,
        standard_days: int = DEFAULT_AUDIT_RETENTION_STANDARD_DAYS,
        extended_days: int = DEFAULT_AUDIT_RETENTION_EXTENDED_DAYS,
        max_scan: int = DEFAULT_AUDIT_RETENTION_MAX_SCAN,
        max_candidates: int = DEFAULT_AUDIT_RETENTION_MAX_CANDIDATES,
    ) -> PermissionRequest:
        """冻结候选快照并创建 L4 单次确认；本方法不删除任何日志。"""
        _validate_retention_bounds(
            standard_days=standard_days,
            extended_days=extended_days,
            max_scan=max_scan,
            max_candidates=max_candidates,
        )
        evaluated_at = utcnow()
        session_factory = self._uow_factory()
        async with session_factory() as session:
            async with PostgresUnitOfWork(session).transaction() as tx:
                await tx.audits.acquire_retention_execution_lock()
                scan = await self._scan_retention(
                    repository=tx.audits,
                    standard_days=standard_days,
                    extended_days=extended_days,
                    max_scan=max_scan,
                    max_candidates=max_candidates,
                    evaluated_at=evaluated_at,
                )
                if not scan.candidate_ids:
                    raise errors.AppError(
                        code="AUDIT_RETENTION_NO_CANDIDATES",
                        message="当前保留策略没有可清理的审计记录",
                        category="validation",
                        recoverable=True,
                    )
                request_id = uuid5(
                    NAMESPACE_URL,
                    "jarvis:audit-retention:"
                    f"{standard_days}:{extended_days}:{max_scan}:{max_candidates}:"
                    f"{scan.candidate_sha256}",
                )
                for _ in range(20):
                    existing = await tx.permissions.get_request(request_id)
                    if existing is None:
                        break
                    if (
                        existing.status is PermissionStatus.DENIED
                        and existing.decided_at is not None
                    ):
                        request_id = uuid5(
                            request_id,
                            f"retry-after-deny:{existing.decided_at.isoformat()}",
                        )
                        continue
                    await tx.commit()
                    return existing
                else:
                    raise errors.AppError(
                        code="AUDIT_RETENTION_REQUEST_LIMIT",
                        message="同一候选快照的确认请求过多，请稍后重试",
                        category="conflict",
                        recoverable=True,
                    )

                now = utcnow()
                task_id = uuid5(request_id, "task")
                run_id = uuid5(request_id, "run")
                if await tx.conversations.get(_AUDIT_RETENTION_CONVERSATION_ID) is None:
                    await tx.conversations.create(
                        Conversation(
                            id=_AUDIT_RETENTION_CONVERSATION_ID,
                            title="审计保留维护",
                            created_at=now,
                            updated_at=now,
                        )
                    )
                    await tx.flush()
                await tx.tasks.create(
                    Task(
                        id=task_id,
                        title="审计日志保留清理确认",
                        user_goal="按已预演的保留策略清理过期审计记录",
                        conversation_id=_AUDIT_RETENTION_CONVERSATION_ID,
                        status=TaskStatus.COMPLETED,
                        risk_level="L4",
                        created_at=now,
                        updated_at=now,
                        completed_at=now,
                        metadata={"operation_type": "audit_retention"},
                    )
                )
                await tx.flush()
                await tx.runs.create(
                    AgentRun(
                        id=run_id,
                        task_id=task_id,
                        status=RunStatus.COMPLETED,
                        created_at=now,
                        updated_at=now,
                        started_at=now,
                        completed_at=now,
                        metadata={"operation_type": "audit_retention"},
                    )
                )
                await tx.flush()
                task = await tx.tasks.get(task_id)
                if task is None:
                    raise errors.not_found("Task", str(task_id))
                task.active_run_id = run_id
                await tx.tasks.update(task)

                request = PermissionRequest(
                    id=request_id,
                    task_id=task_id,
                    run_id=run_id,
                    tool_name=AUDIT_RETENTION_TOOL_NAME,
                    action_summary=(
                        "永久删除已超过保留期的审计记录 "
                        f"（本批最多 {max_candidates} 条）"
                    ),
                    reason="审计记录删除不可撤销，必须逐批单次确认",
                    risk_level="L4",
                    scope={"type": "once", "resource": "audit_logs"},
                    arguments_summary={
                        "standard_days": standard_days,
                        "extended_days": extended_days,
                        "scanned_records": scan.preview.scanned_records,
                        "candidate_records": scan.preview.candidate_records,
                        "protected_records": scan.preview.protected_records,
                        "has_more": scan.preview.has_more,
                    },
                    allowed_decisions=["allow_once", "deny"],
                    checkpoint={
                        "version": 1,
                        "action": "audit_retention",
                        "evaluated_at": evaluated_at.isoformat(),
                        "standard_days": standard_days,
                        "extended_days": extended_days,
                        "max_scan": max_scan,
                        "max_candidates": max_candidates,
                        "candidate_records": scan.preview.candidate_records,
                        "candidate_sha256": scan.candidate_sha256,
                    },
                    created_at=now,
                    expires_at=permission_request_deadline(now),
                )
                await tx.permissions.create_request(request)
                await tx.audits.create(
                    self._retention_audit(
                        request,
                        event_type="audit.retention.permission_requested",
                        result_summary="pending",
                        now=now,
                        details={
                            "candidate_records": scan.preview.candidate_records,
                            "candidate_sha256": scan.candidate_sha256,
                            "has_more": scan.preview.has_more,
                        },
                    )
                )
                await tx.commit()
                return request

    async def resolve_retention_request(
        self,
        request_id: UUID,
        decision: str,
        note: str = "",
    ) -> AuditRetentionResolution:
        """原子消费确认；批准时重新扫描和分类，快照不一致则不删除。"""
        if decision not in {"allow_once", "deny"}:
            raise errors.validation_error("审计保留执行只允许 allow_once 或 deny")
        session_factory = self._uow_factory()
        async with session_factory() as session:
            async with PostgresUnitOfWork(session).transaction() as tx:
                request = await tx.permissions.get_request_for_update(request_id)
                if request is None:
                    raise errors.not_found("PermissionRequest", str(request_id))
                if request.tool_name != AUDIT_RETENTION_TOOL_NAME:
                    raise errors.validation_error("权限请求不属于审计保留执行")
                if request.status in {
                    PermissionStatus.CONSUMED,
                    PermissionStatus.DENIED,
                }:
                    if request.decision != decision:
                        raise errors.permission_conflict(
                            str(request_id), request.decision or "unknown", decision
                        )
                    deleted = int((request.checkpoint or {}).get("deleted_records", 0))
                    has_more = bool((request.checkpoint or {}).get("has_more", False))
                    await tx.commit()
                    return AuditRetentionResolution(request, deleted, has_more)
                if request.status is not PermissionStatus.PENDING:
                    raise errors.permission_not_pending(
                        str(request_id), request.status.value
                    )

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
                    await tx.audits.create(
                        self._retention_audit(
                            request,
                            event_type="audit.retention.permission_decision",
                            result_summary="denied",
                            now=now,
                            decision=decision,
                            note=note,
                        )
                    )
                    await tx.commit()
                    return AuditRetentionResolution(request, 0, False)

                checkpoint = request.checkpoint or {}
                try:
                    evaluated_at = datetime.fromisoformat(
                        str(checkpoint["evaluated_at"])
                    )
                    standard_days = int(checkpoint["standard_days"])
                    extended_days = int(checkpoint["extended_days"])
                    max_scan = int(checkpoint["max_scan"])
                    max_candidates = int(checkpoint["max_candidates"])
                    expected_count = int(checkpoint["candidate_records"])
                    expected_sha256 = str(checkpoint["candidate_sha256"])
                except (KeyError, TypeError, ValueError):
                    raise errors.validation_error(
                        "审计保留权限检查点无效"
                    ) from None
                if evaluated_at.tzinfo is None:
                    raise errors.validation_error("审计保留权限检查点缺少时区")
                _validate_retention_bounds(
                    standard_days=standard_days,
                    extended_days=extended_days,
                    max_scan=max_scan,
                    max_candidates=max_candidates,
                )
                await tx.audits.acquire_retention_execution_lock()
                scan = await self._scan_retention(
                    repository=tx.audits,
                    standard_days=standard_days,
                    extended_days=extended_days,
                    max_scan=max_scan,
                    max_candidates=max_candidates,
                    evaluated_at=evaluated_at,
                )
                if (
                    len(scan.candidate_ids) != expected_count
                    or scan.candidate_sha256 != expected_sha256
                ):
                    raise errors.AppError(
                        code="AUDIT_RETENTION_SNAPSHOT_CHANGED",
                        message="审计保留候选已变化，请重新预演并确认",
                        category="conflict",
                        recoverable=True,
                    )
                deleted = await tx.audits.delete_by_ids(list(scan.candidate_ids))
                if deleted != expected_count:
                    raise errors.AppError(
                        code="AUDIT_RETENTION_DELETE_CONFLICT",
                        message="审计记录在清理事务中发生变化，本次未完成",
                        category="conflict",
                        recoverable=True,
                    )
                request.status = PermissionStatus.CONSUMED
                request.decision = decision
                request.decided_at = now
                request.note = note[:500]
                request.checkpoint = {
                    **checkpoint,
                    "deleted_records": deleted,
                    "has_more": scan.preview.has_more,
                    "completed_at": now.isoformat(),
                }
                await tx.permissions.update_request(request)
                await tx.audits.create(
                    self._retention_audit(
                        request,
                        event_type="audit.retention.applied",
                        result_summary=f"deleted={deleted}",
                        now=now,
                        decision=decision,
                        note=note,
                        details={
                            "deleted_records": deleted,
                            "candidate_sha256": scan.candidate_sha256,
                            "has_more": scan.preview.has_more,
                        },
                    )
                )
                await tx.commit()
                return AuditRetentionResolution(
                    request=request,
                    deleted_records=deleted,
                    has_more=scan.preview.has_more,
                )

    async def _scan_retention(
        self,
        *,
        repository,
        standard_days: int,
        extended_days: int,
        max_scan: int,
        max_candidates: int,
        evaluated_at: datetime,
    ) -> _AuditRetentionScan:
        standard_before = evaluated_at - timedelta(days=standard_days)
        extended_before = evaluated_at - timedelta(days=extended_days)
        scanned_records = 0
        protected_records = 0
        extended_retained_records = 0
        candidate_ids: list[UUID] = []
        after_created_at: datetime | None = None
        after_id: UUID | None = None
        has_more = False

        while scanned_records < max_scan and len(candidate_ids) < max_candidates:
            page_size = min(AUDIT_RETENTION_PAGE_SIZE, max_scan - scanned_records)
            if repository is None:
                logs = await self._list_oldest_page(
                    limit=page_size + 1,
                    created_before=standard_before,
                    after_created_at=after_created_at,
                    after_id=after_id,
                )
            else:
                logs = await repository.list_oldest_page(
                    limit=page_size + 1,
                    created_before=standard_before,
                    after_created_at=after_created_at,
                    after_id=after_id,
                )
            page = logs[:page_size]
            storage_has_more = len(logs) > page_size
            if not page:
                break
            processed_in_page = 0
            for log in page:
                retention_class = _retention_class(log)
                scanned_records += 1
                processed_in_page += 1
                if retention_class == "permanent":
                    protected_records += 1
                elif retention_class == "extended" and log.created_at >= extended_before:
                    extended_retained_records += 1
                else:
                    candidate_ids.append(log.id)
                    if len(candidate_ids) >= max_candidates:
                        has_more = processed_in_page < len(page) or storage_has_more
                        break
            if len(candidate_ids) >= max_candidates:
                break
            if scanned_records >= max_scan:
                has_more = processed_in_page < len(page) or storage_has_more
                break
            if not storage_has_more:
                break
            after_created_at = page[-1].created_at
            after_id = page[-1].id

        candidate_sha256 = hashlib.sha256(
            "\n".join(str(value) for value in candidate_ids).encode()
        ).hexdigest()
        preview = AuditRetentionPreview(
            dry_run=True,
            standard_days=standard_days,
            extended_days=extended_days,
            standard_before=standard_before.isoformat(),
            extended_before=extended_before.isoformat(),
            max_scan=max_scan,
            max_candidates=max_candidates,
            scanned_records=scanned_records,
            candidate_records=len(candidate_ids),
            protected_records=protected_records,
            extended_retained_records=extended_retained_records,
            has_more=has_more,
        )
        return _AuditRetentionScan(
            preview=preview,
            candidate_ids=tuple(candidate_ids),
            candidate_sha256=candidate_sha256,
        )

    async def _stream_audit_logs(
        self,
        *,
        export_format: str,
        max_rows: int,
        max_bytes: int,
        event_type: str | None,
        actor: str | None,
        task_id: UUID | None,
        run_id: UUID | None,
        before_created_at: datetime | None,
        before_id: UUID | None,
        filters: dict[str, Any],
    ) -> AsyncIterator[bytes]:
        row_count = 0
        byte_count = 0
        truncated = False
        completed = False
        digest = hashlib.sha256()

        try:
            if export_format == "csv":
                header = _csv_line(dict.fromkeys(_EXPORT_FIELDS, ""), header=True)
                digest.update(header)
                byte_count += len(header)
                yield header

            while row_count < max_rows:
                logs = await self._list_page(
                    limit=AUDIT_EXPORT_PAGE_SIZE + 1,
                    event_type=event_type,
                    actor=actor,
                    task_id=task_id,
                    run_id=run_id,
                    before_created_at=before_created_at,
                    before_id=before_id,
                )
                page = logs[:AUDIT_EXPORT_PAGE_SIZE]
                has_more = len(logs) > AUDIT_EXPORT_PAGE_SIZE
                if not page:
                    break

                page_rows_emitted = 0
                for log in page:
                    if row_count >= max_rows:
                        truncated = True
                        break
                    row = _serialize_export_row(_to_safe_item(log), export_format)
                    if byte_count + len(row) > max_bytes:
                        truncated = True
                        break
                    digest.update(row)
                    byte_count += len(row)
                    row_count += 1
                    page_rows_emitted += 1
                    yield row

                if truncated:
                    break
                if row_count >= max_rows:
                    truncated = page_rows_emitted < len(page) or has_more
                    break
                if not has_more:
                    break
                before_created_at = page[-1].created_at
                before_id = page[-1].id

            completed = True
        finally:
            event_type_name = (
                "audit.export.completed" if completed else "audit.export.failed"
            )
            error_code = None if completed else "AUDIT_EXPORT_INTERRUPTED"
            try:
                await asyncio.shield(
                    self._record_export_audit(
                        event_type=event_type_name,
                        export_format=export_format,
                        filters=filters,
                        row_count=row_count,
                        byte_count=byte_count,
                        sha256=digest.hexdigest(),
                        truncated=truncated,
                        max_rows=max_rows,
                        max_bytes=max_bytes,
                        error_code=error_code,
                    )
                )
            except Exception as exc:
                logger.error(
                    "审计导出结果记录失败: event_type=%s error_type=%s",
                    event_type_name,
                    type(exc).__name__,
                )

    async def _list_page(
        self,
        *,
        limit: int,
        event_type: str | None,
        actor: str | None,
        task_id: UUID | None,
        run_id: UUID | None,
        before_created_at: datetime | None,
        before_id: UUID | None,
    ) -> list[AuditLog]:
        session_factory = self._uow_factory()
        async with session_factory() as session:
            uow = PostgresUnitOfWork(session)
            return await uow.audits.list_page(
                limit=limit,
                event_type=event_type,
                actor=actor,
                task_id=task_id,
                run_id=run_id,
                before_created_at=before_created_at,
                before_id=before_id,
            )

    async def _list_oldest_page(
        self,
        *,
        limit: int,
        created_before: datetime,
        after_created_at: datetime | None,
        after_id: UUID | None,
    ) -> list[AuditLog]:
        session_factory = self._uow_factory()
        async with session_factory() as session:
            uow = PostgresUnitOfWork(session)
            return await uow.audits.list_oldest_page(
                limit=limit,
                created_before=created_before,
                after_created_at=after_created_at,
                after_id=after_id,
            )

    async def _record_export_audit(
        self,
        *,
        event_type: str,
        export_format: str,
        filters: dict[str, Any],
        row_count: int,
        byte_count: int,
        sha256: str,
        truncated: bool,
        max_rows: int,
        max_bytes: int,
        error_code: str | None,
    ) -> None:
        audit = AuditLog(
            id=new_id(),
            event_type=event_type,
            actor="user",
            risk_level="L0",
            permission_decision="explicit_user_action",
            action_summary="导出审计日志",
            details={
                "format": export_format,
                "filters": filters,
                "row_count": row_count,
                "byte_count": byte_count,
                "sha256": sha256,
                "truncated": truncated,
                "max_rows": max_rows,
                "max_bytes": max_bytes,
            },
            result_summary=(
                f"completed: rows={row_count}, bytes={byte_count}, truncated={truncated}"
                if error_code is None
                else f"failed: {error_code}"
            ),
            error=(
                None
                if error_code is None
                else {
                    "code": error_code,
                    "category": "runtime",
                    "recoverable": True,
                }
            ),
        )
        session_factory = self._uow_factory()
        async with session_factory() as session:
            uow = PostgresUnitOfWork(session)
            async with uow.transaction() as tx:
                await tx.audits.create(audit)
                await tx.commit()

    async def _record_retention_preview_audit(
        self,
        preview: AuditRetentionPreview,
    ) -> None:
        audit = AuditLog(
            id=new_id(),
            event_type="audit.retention.previewed",
            actor="user",
            risk_level="L0",
            permission_decision="explicit_user_action",
            action_summary="预演审计日志保留策略",
            details={
                "dry_run": True,
                "standard_days": preview.standard_days,
                "extended_days": preview.extended_days,
                "max_scan": preview.max_scan,
                "max_candidates": preview.max_candidates,
                "scanned_records": preview.scanned_records,
                "candidate_records": preview.candidate_records,
                "protected_records": preview.protected_records,
                "extended_retained_records": preview.extended_retained_records,
                "has_more": preview.has_more,
            },
            result_summary=(
                "previewed: "
                f"scanned={preview.scanned_records}, "
                f"candidates={preview.candidate_records}, "
                f"has_more={preview.has_more}"
            ),
        )
        session_factory = self._uow_factory()
        async with session_factory() as session:
            uow = PostgresUnitOfWork(session)
            async with uow.transaction() as tx:
                await tx.audits.create(audit)
                await tx.commit()

    @staticmethod
    def _retention_audit(
        request: PermissionRequest,
        *,
        event_type: str,
        result_summary: str,
        now: datetime,
        decision: str | None = None,
        note: str = "",
        details: dict[str, Any] | None = None,
    ) -> AuditLog:
        return AuditLog(
            id=new_id(),
            task_id=request.task_id,
            run_id=request.run_id,
            event_type=event_type,
            actor="user",
            risk_level="L4",
            permission_decision=decision,
            action_summary=request.action_summary,
            details={
                "request_id": str(request.id),
                "note": note[:500],
                **(details or {}),
            },
            result_summary=result_summary,
            created_at=now,
        )


def _to_safe_item(log: AuditLog) -> AuditLogQueryItem:
    """构造浏览器可见审计投影，阻止原始异常/凭据/大内容泄漏。"""
    error_code = log.error.get("code") if isinstance(log.error, dict) else None
    return AuditLogQueryItem(
        id=str(log.id),
        event_type=_clip(log.event_type),
        actor=_clip(log.actor),
        action_summary=_clip(log.action_summary),
        task_id=str(log.task_id) if log.task_id else None,
        run_id=str(log.run_id) if log.run_id else None,
        step_id=str(log.step_id) if log.step_id else None,
        tool_call_id=str(log.tool_call_id) if log.tool_call_id else None,
        risk_level=_clip(log.risk_level) if log.risk_level else None,
        permission_decision=_clip(log.permission_decision) if log.permission_decision else None,
        result_summary=_clip(log.result_summary) if log.result_summary else None,
        error_code=_clip(error_code) if isinstance(error_code, str) else None,
        details_summary=_safe_value(log.details),
        created_at=log.created_at.isoformat(),
    )


def _safe_value(value: Any, *, depth: int = 0) -> Any:
    if depth >= 3:
        return "[内容已省略]"
    if value is None or isinstance(value, bool | int | float):
        return value
    if isinstance(value, str):
        return _clip(value)
    if isinstance(value, dict):
        output: dict[str, Any] = {}
        for key, item in list(value.items())[:_MAX_DETAILS_ITEMS]:
            key_text = _clip(str(key), 80)
            if any(part in key_text.lower() for part in _SENSITIVE_KEY_PARTS):
                output[key_text] = "[已脱敏]"
            else:
                output[key_text] = _safe_value(item, depth=depth + 1)
        if len(value) > _MAX_DETAILS_ITEMS:
            output["_truncated"] = True
        return output
    if isinstance(value, (list, tuple)):
        output = [_safe_value(item, depth=depth + 1) for item in value[:_MAX_DETAILS_ITEMS]]
        if len(value) > _MAX_DETAILS_ITEMS:
            output.append("[其余内容已省略]")
        return output
    return _clip(str(value))


def _clip(value: str, limit: int = _MAX_TEXT) -> str:
    safe = _URL_CREDENTIALS.sub(r"\g<scheme>***:***@", sanitize_message(value))
    return safe if len(safe) <= limit else f"{safe[:limit]}…"


def _serialize_export_row(item: AuditLogQueryItem, export_format: str) -> bytes:
    values = {field: getattr(item, field) for field in _EXPORT_FIELDS}
    if export_format == "jsonl":
        return (
            json.dumps(
                values,
                ensure_ascii=False,
                separators=(",", ":"),
            )
            + "\n"
        ).encode("utf-8")
    return _csv_line(values)


def _csv_line(values: dict[str, Any], *, header: bool = False) -> bytes:
    buffer = io.StringIO(newline="")
    writer = csv.writer(buffer, lineterminator="\n")
    if header:
        writer.writerow(_EXPORT_FIELDS)
    else:
        row = []
        for field in _EXPORT_FIELDS:
            value = values[field]
            if field == "details_summary":
                value = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
            elif value is None:
                value = ""
            row.append(_safe_csv_cell(str(value)))
        writer.writerow(row)
    return buffer.getvalue().encode("utf-8")


def _safe_csv_cell(value: str) -> str:
    """阻止审计摘要在电子表格中被解释为公式。"""
    return f"'{value}" if value.startswith(("=", "+", "-", "@", "\t", "\r")) else value


def _retention_class(log: AuditLog) -> str:
    """Application 层唯一保留分类：standard / extended / permanent。"""
    event_type = log.event_type.lower()
    if (
        event_type.startswith("audit.retention.")
        or log.risk_level in {"L4", "L5"}
        or any(
        part in event_type for part in _PERMANENT_RETENTION_EVENT_PARTS
        )
    ):
        return "permanent"
    if log.risk_level == "L3" or event_type.startswith("permission."):
        return "extended"
    return "standard"


def _validate_retention_bounds(
    *,
    standard_days: int,
    extended_days: int,
    max_scan: int,
    max_candidates: int,
) -> None:
    values = (standard_days, extended_days, max_scan, max_candidates)
    if any(isinstance(value, bool) for value in values):
        raise errors.validation_error("审计保留预演参数必须是整数")
    if not MIN_AUDIT_RETENTION_DAYS <= standard_days <= MAX_AUDIT_RETENTION_DAYS:
        raise errors.validation_error(
            "standard_days 必须在 "
            f"{MIN_AUDIT_RETENTION_DAYS}-{MAX_AUDIT_RETENTION_DAYS}"
        )
    if not MIN_AUDIT_RETENTION_DAYS <= extended_days <= MAX_AUDIT_RETENTION_DAYS:
        raise errors.validation_error(
            "extended_days 必须在 "
            f"{MIN_AUDIT_RETENTION_DAYS}-{MAX_AUDIT_RETENTION_DAYS}"
        )
    if extended_days <= standard_days:
        raise errors.validation_error("extended_days 必须大于 standard_days")
    if not 1 <= max_scan <= MAX_AUDIT_RETENTION_SCAN:
        raise errors.validation_error(
            f"max_scan 必须在 1-{MAX_AUDIT_RETENTION_SCAN}"
        )
    if not 1 <= max_candidates <= MAX_AUDIT_RETENTION_CANDIDATES:
        raise errors.validation_error(
            "max_candidates 必须在 "
            f"1-{MAX_AUDIT_RETENTION_CANDIDATES}"
        )


def _encode_cursor(log: AuditLog) -> str:
    raw = json.dumps([log.created_at.isoformat(), str(log.id)], separators=(",", ":"))
    return base64.urlsafe_b64encode(raw.encode()).decode()


def _decode_cursor(cursor: str) -> tuple[datetime, UUID]:
    try:
        raw = base64.urlsafe_b64decode(cursor.encode()).decode()
        parts = json.loads(raw)
        if not isinstance(parts, list) or len(parts) != 2 or not all(isinstance(item, str) for item in parts):
            raise ValueError
        timestamp = datetime.fromisoformat(parts[0])
        if timestamp.tzinfo is None:
            raise ValueError
        return timestamp, UUID(parts[1])
    except Exception:
        raise errors.validation_error("无效的审计分页 cursor")
