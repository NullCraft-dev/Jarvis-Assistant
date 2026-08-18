"""Repository 接口（ABC）。

所有持久化操作通过 Repository 接口访问。
Repository 不允许自行 commit；事务边界由 Application Service 管理。
Repository 不允许做业务状态判断。
"""

from abc import ABC, abstractmethod
from datetime import datetime
from typing import Optional
from uuid import UUID

from jarvis_worker.shared.domain.models import (
    AgentRun,
    Artifact,
    AuditLog,
    Conversation,
    ExecutionStep,
    Message,
    OutboxEvent,
    PermissionGrant,
    PermissionRequest,
    RuntimeEvent,
    Task,
    ToolCall,
    Workspace,
)


class TaskRepository(ABC):
    """任务持久化。"""

    @abstractmethod
    async def create(self, task: Task) -> Task: ...

    @abstractmethod
    async def get(self, task_id: UUID) -> Optional[Task]: ...

    @abstractmethod
    async def get_by_scheduled_execution(self, execution_id: UUID) -> Optional[Task]: ...

    @abstractmethod
    async def list_all(self, limit: int = 50, offset: int = 0) -> list[Task]: ...

    @abstractmethod
    async def update(self, task: Task) -> None: ...


class RunRepository(ABC):
    """AgentRun 持久化。"""

    @abstractmethod
    async def create(self, run: AgentRun) -> AgentRun: ...

    @abstractmethod
    async def get(self, run_id: UUID) -> Optional[AgentRun]: ...

    @abstractmethod
    async def list_by_task(self, task_id: UUID) -> list[AgentRun]: ...

    @abstractmethod
    async def list_recent(self, limit: int = 50) -> list[AgentRun]:
        """按更新时间倒序返回有界 Run 集合。"""
        ...

    @abstractmethod
    async def list_expired_running(
        self, now: datetime, limit: int = 32
    ) -> list[AgentRun]: ...

    @abstractmethod
    async def list_stale_queued(
        self, updated_before: datetime, limit: int = 32
    ) -> list[AgentRun]: ...

    @abstractmethod
    async def renew_lease(
        self, run_id: UUID, worker_id: str, lease_until: datetime
    ) -> bool: ...

    @abstractmethod
    async def update(self, run: AgentRun) -> None: ...

    @abstractmethod
    async def update_with_lock(
        self,
        run_id: UUID,
        new_status: str,
        expected_version: int,
        expected_status: Optional[str] = None,
        **extra_fields,
    ) -> bool:
        """乐观锁条件更新。返回 True 表示更新成功。"""
        ...


class StepRepository(ABC):
    """ExecutionStep 持久化。"""

    @abstractmethod
    async def create(self, step: ExecutionStep) -> ExecutionStep: ...

    @abstractmethod
    async def update(self, step: ExecutionStep) -> None: ...

    @abstractmethod
    async def get(self, step_id: UUID) -> Optional[ExecutionStep]: ...

    @abstractmethod
    async def list_by_run(self, run_id: UUID) -> list[ExecutionStep]: ...


class EventRepository(ABC):
    """RuntimeEvent 持久化。仅追加，不修改。"""

    @abstractmethod
    async def append(self, events: list[RuntimeEvent]) -> None: ...

    @abstractmethod
    async def list_by_run(self, run_id: UUID) -> list[RuntimeEvent]: ...

    @abstractmethod
    async def get_next_sequence(self, run_id: UUID) -> int: ...


class MessageRepository(ABC):
    """Message 持久化。"""

    @abstractmethod
    async def create(self, message: Message) -> Message: ...

    @abstractmethod
    async def get(self, message_id: UUID) -> Message | None: ...

    @abstractmethod
    async def list_by_task(self, task_id: UUID) -> list[Message]: ...

    @abstractmethod
    async def list_by_conversation(self, conversation_id: UUID) -> list[Message]: ...

    @abstractmethod
    async def list_recent_by_conversation(
        self,
        conversation_id: UUID,
        *,
        exclude_task_id: UUID | None = None,
        roles: tuple[str, ...] = ("user", "assistant"),
        limit: int = 40,
    ) -> list[Message]:
        """有界查询——在 SQL 层完成过滤、排序和截断。

        Args:
            conversation_id: 会话 UUID
            exclude_task_id: 排除指定 Task 的消息
            roles: 只返回这些角色的消息（默认 user/assistant）
            limit: 最大返回条数（必须在 SQL 层 LIMIT）

        Returns:
            从旧到新排序的消息列表（最多 limit 条）。
        """
        ...

    @abstractmethod
    async def list_recent_page(
        self,
        conversation_id: UUID,
        *,
        limit: int = 50,
        before_ts: datetime | None = None,
        before_id: UUID | None = None,
    ) -> list[Message]:
        """有界分页查询——基于 cursor (created_at + id) 的键集分页。

        Repository 只接收已验证的 datetime 和 UUID。
        cursor 解析由 Application Service 负责。

        Returns:
            从旧到新排序的消息列表（最多 limit 条）。
        """
        ...


class ConversationRepository(ABC):
    """Conversation 持久化。"""

    @abstractmethod
    async def create(self, conversation: Conversation) -> Conversation: ...

    @abstractmethod
    async def get(self, conversation_id: UUID) -> Optional[Conversation]: ...

    @abstractmethod
    async def update(self, conversation: Conversation) -> None: ...

    @abstractmethod
    async def get_by_task(self, task_id: UUID) -> Optional[Conversation]: ...

    @abstractmethod
    async def list_all(self, limit: int = 50, offset: int = 0) -> list[Conversation]: ...


class ToolCallRepository(ABC):
    """ToolCall 持久化。"""

    @abstractmethod
    async def create(self, tool_call: ToolCall) -> ToolCall: ...

    @abstractmethod
    async def get(self, tool_call_id: UUID) -> Optional[ToolCall]: ...

    @abstractmethod
    async def update(self, tool_call: ToolCall) -> None: ...

    @abstractmethod
    async def list_by_run(self, run_id: UUID) -> list[ToolCall]: ...


class PermissionRepository(ABC):
    """Permission 持久化。"""

    @abstractmethod
    async def create_request(self, req: PermissionRequest) -> PermissionRequest: ...

    @abstractmethod
    async def update_request(self, req: PermissionRequest) -> None: ...

    @abstractmethod
    async def get_request(self, request_id: UUID) -> Optional[PermissionRequest]: ...

    @abstractmethod
    async def get_request_for_update(self, request_id: UUID) -> Optional[PermissionRequest]:
        """锁定权限请求，供一次性高风险决定原子消费。"""
        ...

    @abstractmethod
    async def list_pending_by_run(self, run_id: UUID) -> list[PermissionRequest]: ...

    @abstractmethod
    async def list_expired_pending_for_update(
        self, now: datetime, limit: int = 32
    ) -> list[PermissionRequest]:
        """Lock a bounded expiry batch without blocking another reconciler."""
        ...

    @abstractmethod
    async def create_grant(self, grant: PermissionGrant) -> PermissionGrant: ...


class AuditRepository(ABC):
    """AuditLog 持久化。

    常规业务只追加；审计保留执行器可在 L4 单次确认后调用有界删除能力。
    """

    @abstractmethod
    async def create(self, log: AuditLog) -> AuditLog: ...

    @abstractmethod
    async def list_by_task(self, task_id: UUID) -> list[AuditLog]: ...

    @abstractmethod
    async def list_by_run(self, run_id: UUID) -> list[AuditLog]: ...

    @abstractmethod
    async def list_page(
        self,
        *,
        limit: int,
        event_type: str | None = None,
        actor: str | None = None,
        task_id: UUID | None = None,
        run_id: UUID | None = None,
        before_created_at: datetime | None = None,
        before_id: UUID | None = None,
    ) -> list[AuditLog]: ...

    @abstractmethod
    async def list_oldest_page(
        self,
        *,
        limit: int,
        created_before: datetime,
        after_created_at: datetime | None = None,
        after_id: UUID | None = None,
    ) -> list[AuditLog]:
        """按最旧优先读取有界页面，供 Application 层执行保留策略。"""
        ...

    @abstractmethod
    async def acquire_retention_execution_lock(self) -> None:
        """在当前事务内串行化审计保留执行器。"""
        ...

    @abstractmethod
    async def delete_by_ids(self, audit_log_ids: list[UUID]) -> int:
        """删除 Application 层已复核的有界 ID 集合并返回实际删除数。"""
        ...


class ArtifactRepository(ABC):
    """Artifact 持久化。"""

    @abstractmethod
    async def get(self, artifact_id: UUID) -> Optional[Artifact]: ...

    @abstractmethod
    async def create(self, artifact: Artifact) -> Artifact: ...

    @abstractmethod
    async def list_by_task(self, task_id: UUID) -> list[Artifact]: ...

    @abstractmethod
    async def list_by_run(self, run_id: UUID) -> list[Artifact]: ...


class OutboxRepository(ABC):
    """Outbox 持久化。"""

    @abstractmethod
    async def create(self, events: list[OutboxEvent]) -> None: ...

    @abstractmethod
    async def claim_pending(
        self, batch_size: int = 32, lease_seconds: int = 30, claimed_by: str = "publisher-01"
    ) -> list[OutboxEvent]: ...

    @abstractmethod
    async def mark_delivered(self, event_ids: list[UUID]) -> None: ...

    @abstractmethod
    async def mark_failed(
        self, event_ids: list[UUID], error_code: str, error_message: str = ""
    ) -> None: ...

    @abstractmethod
    async def reset_stale_dispatching(self, stale_seconds: int = 60) -> int: ...

    @abstractmethod
    async def get_latest_run_job(self, run_id: UUID) -> Optional[OutboxEvent]: ...


class InboxRepository(ABC):
    """Inbox 持久化（幂等消费记录）。"""

    @abstractmethod
    async def try_insert(self, source: str, source_event_id: str) -> bool:
        """尝试插入去重记录。返回 True 表示首次处理（非重复）。"""
        ...

    @abstractmethod
    async def mark_processed(self, source: str, source_event_id: str) -> None: ...


class WorkspaceRepository(ABC):
    """Workspace 持久化。"""

    @abstractmethod
    async def create(self, workspace: "Workspace") -> "Workspace": ...

    @abstractmethod
    async def insert_if_absent(self, workspace: "Workspace") -> bool:
        """按 canonical_path 原子插入；已存在时返回 False。"""
        ...

    @abstractmethod
    async def get(self, workspace_id: UUID) -> Optional["Workspace"]: ...

    @abstractmethod
    async def get_for_update(self, workspace_id: UUID) -> Optional["Workspace"]: ...

    @abstractmethod
    async def get_by_canonical_path(self, canonical_path: str) -> Optional["Workspace"]: ...

    @abstractmethod
    async def get_by_canonical_path_for_update(self, canonical_path: str) -> Optional["Workspace"]: ...

    @abstractmethod
    async def list_active(self) -> list["Workspace"]: ...

    @abstractmethod
    async def list_all(self) -> list["Workspace"]: ...

    @abstractmethod
    async def update(self, workspace: "Workspace") -> None: ...
