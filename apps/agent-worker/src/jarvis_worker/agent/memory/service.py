"""长期记忆 Application Service。

第一版只接收用户显式写入；未来 LLM 提取应先进入独立候选流程。
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from uuid import UUID

from sqlalchemy.exc import IntegrityError

from jarvis_worker.shared.errors.application import AppError
from jarvis_worker.shared.domain.models import (
    AuditLog, Memory, MemoryCategory, MemoryScopeType, MemorySourceType,
    MemoryStatus, WorkspaceStatus, new_id, utcnow,
)
from jarvis_worker.database.unit_of_work import PostgresUnitOfWork

_KEY_RE = re.compile(r"^[a-z][a-z0-9_.-]{0,127}$")
log = logging.getLogger("jarvis_worker.agent.memory")


@dataclass(frozen=True)
class CreateMemoryInput:
    scope_type: str
    category: str
    key: str
    content: str
    workspace_id: UUID | None = None
    importance: int = 50


@dataclass(frozen=True)
class UpdateMemoryInput:
    expected_version: int
    content: str | None = None
    status: str | None = None
    importance: int | None = None


class MemoryApplicationService:
    def __init__(self, uow_factory):
        self._uow_factory = uow_factory

    async def create_memory(self, data: CreateMemoryInput) -> Memory:
        scope = self._scope(data.scope_type)
        category = self._category(data.category)
        key = data.key.strip().lower()
        content = self._content(data.content)
        self._validate_key(key)
        self._validate_importance(data.importance)
        self._validate_scope_owner(scope, data.workspace_id)

        async with self._uow_factory()() as session:
            async with PostgresUnitOfWork(session).transaction() as tx:
                if data.workspace_id:
                    workspace = await tx.workspaces.get(data.workspace_id)
                    if workspace is None or workspace.status is not WorkspaceStatus.ACTIVE:
                        raise AppError("WORKSPACE_NOT_FOUND", "工作区不存在或已撤销", "not_found")
                memory = Memory(
                    id=new_id(), scope_type=scope, workspace_id=data.workspace_id,
                    category=category, key=key, content=content,
                    source_type=MemorySourceType.USER_EXPLICIT,
                    importance=data.importance,
                )
                try:
                    await tx.memories.create(memory)
                    await self._audit(tx, memory, "memory.created")
                    await tx.commit()
                except IntegrityError as exc:
                    await tx.rollback()
                    raise AppError(
                        "MEMORY_KEY_CONFLICT",
                        "相同作用域和分类下已存在同名记忆",
                        "validation",
                    ) from exc
                log.info(
                    "Memory 创建完成: memory_id=%s scope=%s category=%s key=%s version=%d",
                    memory.id,
                    memory.scope_type.value,
                    memory.category.value,
                    memory.key,
                    memory.version,
                )
                return memory

    async def list_memories(
        self, *, scope_type: str | None = None, workspace_id: UUID | None = None,
        status: str | None = None, category: str | None = None,
        query: str | None = None, limit: int = 100,
    ) -> list[Memory]:
        if scope_type:
            self._scope(scope_type)
        if status:
            self._status(status)
        if category:
            self._category(category)
        query = query.strip() if query else None
        if query and len(query) > 200:
            raise AppError("VALIDATION_ERROR", "搜索内容过长", "validation")
        async with self._uow_factory()() as session:
            tx = PostgresUnitOfWork(session)
            return await tx.memories.list_filtered(
                scope_type=scope_type, workspace_id=workspace_id, status=status,
                category=category, query=query, limit=min(max(limit, 1), 100),
            )

    async def update_memory(self, memory_id: UUID, data: UpdateMemoryInput) -> Memory:
        async with self._uow_factory()() as session:
            async with PostgresUnitOfWork(session).transaction() as tx:
                memory = await tx.memories.get_for_update(memory_id)
                if memory is None:
                    raise AppError("MEMORY_NOT_FOUND", "记忆不存在", "not_found")
                if memory.version != data.expected_version:
                    raise AppError(
                        "MEMORY_VERSION_CONFLICT", "记忆已被其他操作修改，请刷新后重试",
                        "runtime", True,
                    )
                if data.content is not None:
                    memory.content = self._content(data.content)
                if data.status is not None:
                    memory.status = self._status(data.status)
                if data.importance is not None:
                    self._validate_importance(data.importance)
                    memory.importance = data.importance
                memory.version += 1
                memory.updated_at = utcnow()
                await tx.memories.update(memory)
                await self._audit(tx, memory, "memory.updated")
                await tx.commit()
                log.info(
                    "Memory 更新完成: memory_id=%s status=%s importance=%d version=%d",
                    memory.id,
                    memory.status.value,
                    memory.importance,
                    memory.version,
                )
                return memory

    async def delete_memory(self, memory_id: UUID) -> Memory:
        async with self._uow_factory()() as session:
            async with PostgresUnitOfWork(session).transaction() as tx:
                memory = await tx.memories.get_for_update(memory_id)
                if memory is None:
                    raise AppError("MEMORY_NOT_FOUND", "记忆不存在", "not_found")
                await self._audit(tx, memory, "memory.deleted")
                await tx.memories.delete(memory_id)
                await tx.commit()
                log.info("Memory 删除完成: memory_id=%s key=%s", memory.id, memory.key)
                return memory

    async def build_context_for_task(self, task_id: UUID, limit: int = 20) -> list[Memory]:
        """按 Task 的 workspace 边界读取 global + workspace active memory。"""
        async with self._uow_factory()() as session:
            tx = PostgresUnitOfWork(session)
            task = await tx.tasks.get(task_id)
            if task is None:
                log.warning("Memory 上下文加载跳过: task 不存在")
                return []
            memories = await tx.memories.list_active_for_context(task.workspace_id, limit=limit)
            log.debug(
                "Memory 上下文查询完成: workspace_id=%s memories=%d limit=%d",
                task.workspace_id,
                len(memories),
                limit,
            )
            return memories

    @staticmethod
    async def _audit(tx: PostgresUnitOfWork, memory: Memory, event_type: str) -> None:
        await tx.audits.create(AuditLog(
            id=new_id(), event_type=event_type, actor="user",
            action_summary=f"{event_type}: {memory.key}",
            details={
                "memory_id": str(memory.id), "scope_type": memory.scope_type.value,
                "workspace_id": str(memory.workspace_id) if memory.workspace_id else None,
                "category": memory.category.value, "key": memory.key,
                "status": memory.status.value, "version": memory.version,
            },
        ))

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
    def _status(value: str) -> MemoryStatus:
        try:
            return MemoryStatus(value)
        except ValueError as exc:
            raise AppError("VALIDATION_ERROR", "无效的记忆状态", "validation") from exc

    @staticmethod
    def _validate_key(value: str) -> None:
        if not _KEY_RE.fullmatch(value):
            raise AppError(
                "VALIDATION_ERROR",
                "key 必须以小写字母开头，且仅包含小写字母、数字、点、横线或下划线",
                "validation",
            )

    @staticmethod
    def _content(value: str) -> str:
        content = value.strip()
        if not content or len(content) > 4000:
            raise AppError("VALIDATION_ERROR", "记忆内容长度必须为 1–4000 字符", "validation")
        return content

    @staticmethod
    def _validate_importance(value: int) -> None:
        if not 0 <= value <= 100:
            raise AppError("VALIDATION_ERROR", "importance 必须在 0–100", "validation")

    @staticmethod
    def _validate_scope_owner(scope: MemoryScopeType, workspace_id: UUID | None) -> None:
        if scope is MemoryScopeType.GLOBAL and workspace_id is not None:
            raise AppError("VALIDATION_ERROR", "global 记忆不能指定 workspace_id", "validation")
        if scope is MemoryScopeType.WORKSPACE and workspace_id is None:
            raise AppError("VALIDATION_ERROR", "workspace 记忆必须指定 workspace_id", "validation")
