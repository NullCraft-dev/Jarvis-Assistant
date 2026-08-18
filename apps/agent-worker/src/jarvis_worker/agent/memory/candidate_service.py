"""Memory v2 候选、确认与异步提取作业的业务边界。"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from sqlalchemy.exc import IntegrityError

from jarvis_worker.database.unit_of_work import PostgresUnitOfWork
from jarvis_worker.shared.domain.models import (
    AuditLog,
    Memory,
    MemoryCandidate,
    MemoryCandidateStatus,
    MemoryCategory,
    MemoryScopeType,
    MemorySensitivity,
    MemorySourceType,
    RunStatus,
    TaskStatus,
    WorkspaceStatus,
    new_id,
    utcnow,
)
from jarvis_worker.shared.errors.application import AppError

_KEY_RE = re.compile(r"^[a-z][a-z0-9_.-]{0,127}$")
_HEX_64_RE = re.compile(r"^[0-9a-f]{64}$")
_SENSITIVE_PATTERNS = (
    re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----", re.I),
    re.compile(r"\b(?:api[_ -]?key|access[_ -]?token|password|passwd|验证码|密码)\s*[:=]\s*\S+", re.I),
    re.compile(r"\bsk-[A-Za-z0-9_-]{16,}\b"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b", re.I),
    re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)"),
    re.compile(r"(?<!\d)\d{17}[\dXx](?!\d)"),
    re.compile(r"(?<!\d)(?:\d[ -]?){13,19}(?!\d)"),
)
log = logging.getLogger("jarvis_worker.memory_candidate_maintenance")


@dataclass(frozen=True)
class ExtractedMemoryCandidateInput:
    scope_type: str
    category: str
    suggested_key: str
    content: str
    source_task_id: UUID
    source_run_id: UUID
    extraction_input_fingerprint: str
    confidence: float
    importance: int
    extraction_policy_version: str
    workspace_id: UUID | None = None
    source_message_ids: tuple[UUID, ...] = ()
    sensitivity: str = "normal"
    extractor_provider: str = ""
    extractor_model: str = ""
    expires_at: datetime | None = None


@dataclass(frozen=True)
class UpdateMemoryCandidateInput:
    expected_version: int
    scope_type: str | None = None
    workspace_id: UUID | None = None
    category: str | None = None
    suggested_key: str | None = None
    content: str | None = None
    importance: int | None = None


@dataclass(frozen=True)
class ResolveMemoryCandidateInput:
    expected_version: int
    note: str = ""


class MemoryCandidateApplicationService:
    def __init__(self, uow_factory):
        self._uow_factory = uow_factory

    async def create_candidate(
        self, data: ExtractedMemoryCandidateInput
    ) -> MemoryCandidate | None:
        """保存一个经结构校验的非敏感候选；重复候选幂等抑制。"""
        scope = self._scope(data.scope_type)
        category = self._category(data.category)
        sensitivity = self._sensitivity(data.sensitivity)
        key = self._key(data.suggested_key)
        content = self._content(data.content)
        self._importance(data.importance)
        self._confidence(data.confidence)
        policy_version = self._bounded(data.extraction_policy_version, 80, "extraction_policy_version")
        fingerprint = data.extraction_input_fingerprint.strip().lower()
        if not _HEX_64_RE.fullmatch(fingerprint):
            raise AppError("VALIDATION_ERROR", "提取输入指纹无效", "validation")
        self._validate_scope_owner(scope, data.workspace_id)
        if sensitivity is MemorySensitivity.SENSITIVE or _contains_sensitive(content):
            raise AppError(
                "MEMORY_CANDIDATE_SENSITIVE",
                "疑似敏感内容不会在未启用加密存储时保存为候选",
                "permission",
            )
        deduplication_key = self._deduplication_key(
            scope, data.workspace_id, category, key, content
        )

        async with self._uow_factory()() as session:
            async with PostgresUnitOfWork(session).transaction() as tx:
                task = await tx.tasks.get(data.source_task_id)
                run = await tx.runs.get(data.source_run_id)
                if (
                    task is None
                    or run is None
                    or run.task_id != task.id
                    or task.status is not TaskStatus.COMPLETED
                    or run.status is not RunStatus.COMPLETED
                ):
                    raise AppError(
                        "MEMORY_CANDIDATE_SOURCE_INVALID",
                        "只有成功完成且互相关联的 Task/Run 可以产生记忆候选",
                        "validation",
                    )
                if scope is MemoryScopeType.WORKSPACE:
                    if task.workspace_id != data.workspace_id:
                        raise AppError(
                            "WORKSPACE_ACCESS_DENIED",
                            "候选记忆的工作区与来源任务不一致",
                            "permission",
                        )
                    await self._require_active_workspace(tx, data.workspace_id)

                existing = await tx.memories.get_by_identity(
                    scope_type=scope.value,
                    workspace_id=data.workspace_id,
                    category=category.value,
                    key=key,
                )
                if existing is not None and existing.content.strip() == content:
                    return None
                pending_duplicate = (
                    await tx.memory_candidates.get_pending_by_deduplication_key(
                        deduplication_key
                    )
                )
                if pending_duplicate is not None:
                    return None

                candidate = MemoryCandidate(
                    id=new_id(),
                    scope_type=scope,
                    workspace_id=data.workspace_id,
                    category=category,
                    suggested_key=key,
                    content=content,
                    source_task_id=data.source_task_id,
                    source_run_id=data.source_run_id,
                    source_message_ids=list(data.source_message_ids),
                    extraction_input_fingerprint=fingerprint,
                    confidence=data.confidence,
                    importance=data.importance,
                    sensitivity=sensitivity,
                    deduplication_key=deduplication_key,
                    extraction_policy_version=policy_version,
                    extractor_provider=self._bounded(data.extractor_provider, 80, "extractor_provider", allow_empty=True),
                    extractor_model=self._bounded(data.extractor_model, 160, "extractor_model", allow_empty=True),
                    conflict_memory_id=existing.id if existing else None,
                    expires_at=data.expires_at,
                )
                try:
                    await tx.memory_candidates.create(candidate)
                    await self._audit(tx, candidate, "memory.candidate.created", "system")
                    await tx.commit()
                except IntegrityError:
                    await tx.rollback()
                    return None
                return candidate

    async def list_candidates(
        self,
        *,
        status: str | None = None,
        workspace_id: UUID | None = None,
        limit: int = 100,
    ) -> list[MemoryCandidate]:
        if status:
            self._candidate_status(status)
        async with self._uow_factory()() as session:
            tx = PostgresUnitOfWork(session)
            return await tx.memory_candidates.list_filtered(
                status=status, workspace_id=workspace_id, limit=min(max(limit, 1), 100)
            )

    async def update_candidate(
        self, candidate_id: UUID, data: UpdateMemoryCandidateInput
    ) -> MemoryCandidate:
        async with self._uow_factory()() as session:
            async with PostgresUnitOfWork(session).transaction() as tx:
                candidate = await self._pending_for_update(tx, candidate_id, data.expected_version)
                scope = self._scope(data.scope_type) if data.scope_type else candidate.scope_type
                workspace_id = (
                    data.workspace_id if data.scope_type is not None else candidate.workspace_id
                )
                category = self._category(data.category) if data.category else candidate.category
                key = self._key(data.suggested_key) if data.suggested_key is not None else candidate.suggested_key
                content = self._content(data.content) if data.content is not None else candidate.content
                importance = data.importance if data.importance is not None else candidate.importance
                self._importance(importance)
                self._validate_scope_owner(scope, workspace_id)
                if scope is MemoryScopeType.WORKSPACE:
                    task = await tx.tasks.get(candidate.source_task_id)
                    if task is None or task.workspace_id != workspace_id:
                        raise AppError(
                            "WORKSPACE_ACCESS_DENIED",
                            "候选只能绑定来源任务的工作区",
                            "permission",
                        )
                    await self._require_active_workspace(tx, workspace_id)
                existing = await tx.memories.get_by_identity(
                    scope_type=scope.value,
                    workspace_id=workspace_id,
                    category=category.value,
                    key=key,
                )
                candidate.scope_type = scope
                candidate.workspace_id = workspace_id
                candidate.category = category
                candidate.suggested_key = key
                candidate.content = content
                candidate.importance = importance
                candidate.conflict_memory_id = (
                    existing.id
                    if existing is not None
                    and existing.content.strip() != content
                    else None
                )
                candidate.version += 1
                candidate.updated_at = utcnow()
                await tx.memory_candidates.update(candidate)
                await self._audit(tx, candidate, "memory.candidate.updated", "user")
                await tx.commit()
                return candidate

    async def expire_due_candidates(
        self, *, now: datetime | None = None, limit: int = 100
    ) -> int:
        """有界领取并终结已到期候选；多 Worker 通过 skip-locked 并发安全。"""
        resolved_at = now or utcnow()
        async with self._uow_factory()() as session:
            async with PostgresUnitOfWork(session).transaction() as tx:
                candidates = await tx.memory_candidates.list_due_for_update(
                    now=resolved_at, limit=min(max(limit, 1), 100)
                )
                for candidate in candidates:
                    candidate.status = MemoryCandidateStatus.EXPIRED
                    candidate.resolved_at = resolved_at
                    candidate.version += 1
                    candidate.updated_at = resolved_at
                    await tx.memory_candidates.update(candidate)
                    await self._audit(
                        tx, candidate, "memory.candidate.expired", "system"
                    )
                await tx.commit()
                return len(candidates)

    async def approve_candidate(
        self, candidate_id: UUID, data: ResolveMemoryCandidateInput
    ) -> tuple[MemoryCandidate, Memory]:
        expired = False
        result: tuple[MemoryCandidate, Memory] | None = None
        async with self._uow_factory()() as session:
            async with PostgresUnitOfWork(session).transaction() as tx:
                candidate = await self._pending_for_update(tx, candidate_id, data.expected_version)
                if candidate.expires_at and candidate.expires_at <= utcnow():
                    candidate.status = MemoryCandidateStatus.EXPIRED
                    candidate.resolved_at = utcnow()
                    candidate.version += 1
                    candidate.updated_at = candidate.resolved_at
                    await tx.memory_candidates.update(candidate)
                    await self._audit(tx, candidate, "memory.candidate.expired", "system")
                    await tx.commit()
                    expired = True
                else:
                    if candidate.scope_type is MemoryScopeType.WORKSPACE:
                        await self._require_active_workspace(tx, candidate.workspace_id)
                    existing = await tx.memories.get_by_identity(
                        scope_type=candidate.scope_type.value,
                        workspace_id=candidate.workspace_id,
                        category=candidate.category.value,
                        key=candidate.suggested_key,
                    )
                    if existing is not None and existing.content.strip() != candidate.content.strip():
                        raise AppError(
                            "MEMORY_CANDIDATE_CONFLICT",
                            "相同作用域、分类和 key 已存在不同内容，请先编辑候选解决冲突",
                            "validation",
                        )
                    memory = existing or Memory(
                        id=new_id(),
                        scope_type=candidate.scope_type,
                        workspace_id=candidate.workspace_id,
                        category=candidate.category,
                        key=candidate.suggested_key,
                        content=candidate.content,
                        source_type=MemorySourceType.CANDIDATE_APPROVED,
                        source_task_id=candidate.source_task_id,
                        importance=candidate.importance,
                    )
                    if existing is None:
                        await tx.memories.create(memory)
                    candidate.status = MemoryCandidateStatus.APPROVED
                    candidate.approved_memory_id = memory.id
                    candidate.resolved_at = utcnow()
                    candidate.resolution_note = self._note(data.note)
                    candidate.version += 1
                    candidate.updated_at = candidate.resolved_at
                    await tx.memory_candidates.update(candidate)
                    await self._audit(tx, candidate, "memory.candidate.approved", "user")
                    await tx.commit()
                    result = (candidate, memory)
        if expired:
            raise AppError("MEMORY_CANDIDATE_EXPIRED", "记忆候选已过期", "validation")
        assert result is not None
        return result

    async def reject_candidate(
        self, candidate_id: UUID, data: ResolveMemoryCandidateInput
    ) -> MemoryCandidate:
        async with self._uow_factory()() as session:
            async with PostgresUnitOfWork(session).transaction() as tx:
                candidate = await self._pending_for_update(tx, candidate_id, data.expected_version)
                candidate.status = MemoryCandidateStatus.REJECTED
                candidate.resolved_at = utcnow()
                candidate.resolution_note = self._note(data.note)
                candidate.version += 1
                candidate.updated_at = candidate.resolved_at
                await tx.memory_candidates.update(candidate)
                await self._audit(tx, candidate, "memory.candidate.rejected", "user")
                await tx.commit()
                return candidate

    @staticmethod
    async def _pending_for_update(tx, candidate_id: UUID, expected_version: int) -> MemoryCandidate:
        candidate = await tx.memory_candidates.get_for_update(candidate_id)
        if candidate is None:
            raise AppError("MEMORY_CANDIDATE_NOT_FOUND", "记忆候选不存在", "not_found")
        if candidate.version != expected_version:
            raise AppError(
                "MEMORY_CANDIDATE_VERSION_CONFLICT",
                "记忆候选已被修改，请刷新后重试",
                "runtime",
                True,
            )
        if candidate.status is not MemoryCandidateStatus.PENDING:
            raise AppError(
                "MEMORY_CANDIDATE_ALREADY_RESOLVED",
                "记忆候选已经处理，不能再次修改或决定",
                "validation",
            )
        return candidate

    @staticmethod
    async def _require_active_workspace(tx, workspace_id: UUID | None) -> None:
        if workspace_id is None:
            raise AppError("VALIDATION_ERROR", "缺少 workspace_id", "validation")
        workspace = await tx.workspaces.get(workspace_id)
        if workspace is None or workspace.status is not WorkspaceStatus.ACTIVE:
            raise AppError("WORKSPACE_NOT_FOUND", "工作区不存在或已撤销", "not_found")

    @staticmethod
    async def _audit(tx, candidate: MemoryCandidate, event_type: str, actor: str) -> None:
        await tx.audits.create(AuditLog(
            id=new_id(),
            event_type=event_type,
            actor=actor,
            action_summary=f"{event_type}: {candidate.suggested_key}",
            task_id=candidate.source_task_id,
            run_id=candidate.source_run_id,
            details={
                "candidate_id": str(candidate.id),
                "status": candidate.status.value,
                "scope_type": candidate.scope_type.value,
                "workspace_id": str(candidate.workspace_id) if candidate.workspace_id else None,
                "category": candidate.category.value,
                "suggested_key": candidate.suggested_key,
                "confidence": candidate.confidence,
                "importance": candidate.importance,
                "conflict_memory_id": str(candidate.conflict_memory_id) if candidate.conflict_memory_id else None,
                "approved_memory_id": str(candidate.approved_memory_id) if candidate.approved_memory_id else None,
                "version": candidate.version,
            },
        ))

    @staticmethod
    def _deduplication_key(
        scope: MemoryScopeType,
        workspace_id: UUID | None,
        category: MemoryCategory,
        key: str,
        content: str,
    ) -> str:
        payload = {
            "scope_type": scope.value,
            "workspace_id": str(workspace_id) if workspace_id else None,
            "category": category.value,
            "key": key,
            "content": " ".join(content.split()).casefold(),
        }
        raw = json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()

    @staticmethod
    def _scope(value: str) -> MemoryScopeType:
        try:
            return MemoryScopeType(value)
        except ValueError as exc:
            raise AppError("VALIDATION_ERROR", "scope_type 仅支持 global 或 workspace", "validation") from exc

    @staticmethod
    def _category(value: str) -> MemoryCategory:
        try:
            return MemoryCategory(value)
        except ValueError as exc:
            raise AppError("VALIDATION_ERROR", "无效的记忆分类", "validation") from exc

    @staticmethod
    def _candidate_status(value: str) -> MemoryCandidateStatus:
        try:
            return MemoryCandidateStatus(value)
        except ValueError as exc:
            raise AppError("VALIDATION_ERROR", "无效的候选状态", "validation") from exc

    @staticmethod
    def _sensitivity(value: str) -> MemorySensitivity:
        try:
            return MemorySensitivity(value)
        except ValueError as exc:
            raise AppError("VALIDATION_ERROR", "无效的敏感等级", "validation") from exc

    @staticmethod
    def _key(value: str) -> str:
        key = value.strip().lower()
        if not _KEY_RE.fullmatch(key):
            raise AppError("VALIDATION_ERROR", "候选 key 格式无效", "validation")
        return key

    @staticmethod
    def _content(value: str) -> str:
        content = value.strip()
        if not content or len(content) > 4000:
            raise AppError("VALIDATION_ERROR", "候选内容长度必须为 1–4000 字符", "validation")
        return content

    @staticmethod
    def _importance(value: int) -> None:
        if not 0 <= value <= 100:
            raise AppError("VALIDATION_ERROR", "importance 必须在 0–100", "validation")

    @staticmethod
    def _confidence(value: float) -> None:
        if not 0.0 <= value <= 1.0:
            raise AppError("VALIDATION_ERROR", "confidence 必须在 0–1", "validation")

    @staticmethod
    def _validate_scope_owner(scope: MemoryScopeType, workspace_id: UUID | None) -> None:
        if scope is MemoryScopeType.GLOBAL and workspace_id is not None:
            raise AppError("VALIDATION_ERROR", "global 候选不能指定 workspace_id", "validation")
        if scope is MemoryScopeType.WORKSPACE and workspace_id is None:
            raise AppError("VALIDATION_ERROR", "workspace 候选必须指定 workspace_id", "validation")

    @staticmethod
    def _bounded(value: str, maximum: int, field_name: str, *, allow_empty: bool = False) -> str:
        normalized = value.strip()
        if (not normalized and not allow_empty) or len(normalized) > maximum:
            raise AppError("VALIDATION_ERROR", f"{field_name} 无效", "validation")
        return normalized

    @staticmethod
    def _note(value: str) -> str:
        note = value.strip()
        if len(note) > 500:
            raise AppError("VALIDATION_ERROR", "处理说明不能超过 500 字符", "validation")
        return note


def _contains_sensitive(content: str) -> bool:
    """模型标签之外的确定性兜底；命中时 fail closed 且不落库。"""
    return any(pattern.search(content) is not None for pattern in _SENSITIVE_PATTERNS)


class MemoryCandidateMaintenanceWorker:
    """独立维护候选终态，不依赖模型 Provider 或是否启用自动提取。"""

    def __init__(
        self,
        service: MemoryCandidateApplicationService,
        *,
        poll_interval: float = 60.0,
    ) -> None:
        self._service = service
        self._poll_interval = max(poll_interval, 1.0)
        self._stop: asyncio.Event | None = None
        self._task: asyncio.Task[None] | None = None

    async def start(self) -> None:
        if self._task is not None and not self._task.done():
            return
        self._stop = asyncio.Event()
        self._task = asyncio.create_task(self._loop())
        log.info("MemoryCandidateMaintenanceWorker 已启动")

    async def stop(self) -> None:
        if self._task is None:
            return
        assert self._stop is not None
        self._stop.set()
        self._task.cancel()
        await asyncio.gather(self._task, return_exceptions=True)
        self._task = None
        log.info("MemoryCandidateMaintenanceWorker 已停止")

    async def _loop(self) -> None:
        assert self._stop is not None
        while not self._stop.is_set():
            try:
                expired = await self._service.expire_due_candidates()
                if expired:
                    log.info("到期 MemoryCandidate 收口完成: expired=%d", expired)
            except Exception:
                log.exception("MemoryCandidate 到期维护失败")
            try:
                await asyncio.wait_for(
                    self._stop.wait(), timeout=self._poll_interval
                )
            except TimeoutError:
                pass
