"""TaskApplicationService — 创建任务（完整 UnitOfWork 事务，支持 Conversation 复用）。"""

import logging
from dataclasses import dataclass
from typing import Optional
from uuid import UUID

from jarvis_worker.database.unit_of_work import PostgresUnitOfWork
from jarvis_worker.runtime.workspaces.workspace_policy import WorkspacePolicy
from jarvis_worker.shared.domain.models import (
    AgentRun,
    Conversation,
    Message,
    OutboxEvent,
    RunStatus,
    RuntimeEvent,
    Task,
    TaskStatus,
    new_id,
    utcnow,
)
from jarvis_worker.shared.errors import application as errors
from jarvis_worker.shared.security import redact_credentials

logger = logging.getLogger(__name__)

SCHEMA_VERSION = "2B-1a.1"


@dataclass
class CreateTaskInput:
    user_goal: str
    workspace_path: Optional[str] = None
    workspace_id: Optional[UUID] = None  # 优先于 workspace_path
    conversation_id: Optional[UUID] = None
    title: Optional[str] = None
    trace_id: Optional[UUID] = None
    metadata: dict | None = None
    scheduled_execution_id: Optional[UUID] = None


@dataclass
class CreateTaskResult:
    task: Task
    run: AgentRun
    conversation: Conversation
    message: Message
    initial_event: RuntimeEvent
    trace_id: UUID
    outbox_event_id: UUID


class TaskApplicationService:
    """任务 Application Service — 创建任务的完整事务 Owner。"""

    def __init__(
        self,
        uow_factory,
        workspace_policy: WorkspacePolicy | None = None,
        workspace_service=None,  # WorkspaceApplicationService，用于 workspace_id 校验
    ):
        self._uow_factory = uow_factory
        self._workspace_policy = workspace_policy
        self._workspace_service = workspace_service

    async def create_task(self, input_data: CreateTaskInput) -> CreateTaskResult:
        """创建任务（单一 PostgreSQL 事务）。

        1. conversation_id 有值 → 校验 Conversation 存在，复用
        2. conversation_id 为空 → 创建新 Conversation
        3. 创建 Task（FK → Conversation）
        4. 创建 AgentRun (status=queued, version=1)
        5. 写入初始 RuntimeEvent (task.created)
        6. 写入 OutboxEvent (task.created → run-queue)

        事务提交成功后才返回权威 DTO。
        不在此方法内发布 Redis——由 OutboxPublisher 异步负责。

        Raises:
            AppError: 事务失败、Conversation 不存在或数据库不可用。
        """
        # 解析 Workspace：workspace_id 优先于 workspace_path
        # 两者都为空时使用 WorkspacePolicy.resolve(None) 恢复默认行为
        resolved_workspace_id: UUID | None = None
        workspace_path: str | None = None

        if input_data.workspace_id is not None:
            # workspace_id 优先：事务内校验在下方 uow.transaction() 中完成
            if self._workspace_service is None:
                raise errors.AppError(
                    code="WORKSPACE_PICKER_UNAVAILABLE",
                    message="工作区服务不可用",
                    category="internal",
                )
            resolved_workspace_id = input_data.workspace_id
            # workspace_path 快照将在事务内通过 validate_for_task_within_tx 获取
        elif input_data.workspace_path:
            # 兼容旧路径：经 WorkspacePolicy 校验
            workspace_path = (
                self._workspace_policy.resolve(input_data.workspace_path)
                if self._workspace_policy is not None
                else input_data.workspace_path
            )
            # 传统路径：不做 workspace_id 绑定
        else:
            # 两者都为空：恢复 JARVIS_WORKSPACE_ROOT 默认行为
            workspace_path = (
                self._workspace_policy.resolve(None)
                if self._workspace_policy is not None
                else None
            )
            # 如果也没有默认工作区，workspace_path 为 None（ToolGateway 执行时防呆）

        task_id = new_id()
        run_id = new_id()
        msg_id = new_id()
        event_id = new_id()
        outbox_event_id = new_id()
        trace_id = input_data.trace_id or new_id()
        now = utcnow()

        title_source = input_data.title or input_data.user_goal
        safe_title_source = redact_credentials(title_source).strip() or "新任务"
        title = safe_title_source[:100] + ("..." if len(safe_title_source) > 100 else "")

        session_factory = self._uow_factory()
        async with session_factory() as session:
            uow = PostgresUnitOfWork(session)
            async with uow.transaction() as tx:
                try:
                    # 0. 在同一事务内完成 workspace 校验（避免"校验后撤销"竞态）
                    if resolved_workspace_id is not None:
                        if self._workspace_service is None:
                            raise errors.AppError(
                                code="WORKSPACE_PICKER_UNAVAILABLE",
                                message="工作区服务不可用",
                                category="internal",
                            )
                        workspace_path = await self._workspace_service.validate_for_task_within_tx(
                            tx, resolved_workspace_id,
                        )

                    # 1. 创建或复用 Conversation
                    conv_is_new = True
                    if input_data.conversation_id:
                        conversation = await tx.conversations.get(input_data.conversation_id)
                        if conversation is None:
                            raise errors.not_found("Conversation", str(input_data.conversation_id))
                        conv_is_new = False
                        conversation.updated_at = now
                        await tx.conversations.update(conversation)
                    else:
                        conversation = Conversation(
                            id=new_id(),
                            title=title,
                            created_at=now,
                            updated_at=now,
                        )
                        await tx.conversations.create(conversation)

                    # Repository 使用独立 ORM model 且不建立跨 aggregate relationship，
                    # 因此必须先 flush Conversation，避免后续子记录抢先 autoflush。
                    await tx.flush()

                    # 2. 创建 Task
                    task = Task(
                        id=task_id,
                        conversation_id=conversation.id,
                        title=title,
                        user_goal=input_data.user_goal,
                        status=TaskStatus.RUNNING,
                        workspace_path=workspace_path,
                        workspace_id=resolved_workspace_id,
                        active_run_id=run_id,
                        created_at=now,
                        updated_at=now,
                        metadata=dict(input_data.metadata or {}),
                        scheduled_execution_id=input_data.scheduled_execution_id,
                    )
                    await tx.tasks.create(task)
                    await tx.flush()

                    # 3. 写入用户 Message
                    message = Message(
                        id=msg_id,
                        conversation_id=conversation.id,
                        task_id=task_id,
                        role="user",
                        content=input_data.user_goal,
                        created_at=now,
                    )
                    await tx.messages.create(message)

                    # 4. 创建 AgentRun
                    run = AgentRun(
                        id=run_id,
                        task_id=task_id,
                        status=RunStatus.QUEUED,
                        version=1,
                        created_at=now,
                        updated_at=now,
                        metadata={"trace_id": str(trace_id)},
                    )
                    await tx.runs.create(run)
                    await tx.flush()

                    # 5. 写入初始 RuntimeEvent
                    initial_event = RuntimeEvent(
                        id=new_id(),
                        event_id=event_id,
                        type="task.created",
                        payload={
                            "task_id": str(task_id),
                            "run_id": str(run_id),
                            "user_goal": input_data.user_goal,
                        },
                        task_id=task_id,
                        run_id=run_id,
                        event_sequence=await self._next_seq(tx, run_id),
                        created_at=now,
                    )
                    await tx.events.append([initial_event])

                    # 6. 写入 OutboxEvent
                    outbox_event = OutboxEvent(
                        id=new_id(),
                        event_id=outbox_event_id,
                        aggregate_type="AgentRun",
                        aggregate_id=run_id,
                        event_type="task.created",
                        schema_version=SCHEMA_VERSION,
                        payload={
                            "job_id": str(outbox_event_id),
                            "trace_id": str(trace_id),
                            "task_id": str(task_id),
                            "run_id": str(run_id),
                            "user_goal": input_data.user_goal,
                            "workspace_path": workspace_path or "",
                            "conversation_id": str(conversation.id),
                            "created_at": now.isoformat(),
                            "schema_version": SCHEMA_VERSION,
                            "scheduled_task_id": str((input_data.metadata or {}).get("scheduled_task_id", "")),
                            "authorized_tools": list((input_data.metadata or {}).get("authorized_tools", [])),
                            "source_policy": dict((input_data.metadata or {}).get("source_policy", {})),
                        },
                        trace_id=trace_id,
                        created_at=now,
                    )
                    await tx.outbox.create([outbox_event])

                    await tx.commit()

                    logger.info(
                        "任务已创建: task_id=%s run_id=%s conv_id=%s conv_is_new=%s",
                        task_id, run_id, conversation.id, conv_is_new,
                        extra={
                            "trace_id": str(trace_id),
                            "task_id": str(task_id),
                            "run_id": str(run_id),
                        },
                    )
                    return CreateTaskResult(
                        task=task, run=run, conversation=conversation,
                        message=message, initial_event=initial_event,
                        trace_id=trace_id, outbox_event_id=outbox_event_id,
                    )

                except errors.AppError:
                    await tx.rollback()
                    raise
                except Exception as e:
                    logger.error("创建任务事务失败: task_id=%s error=%s", task_id, e)
                    await tx.rollback()
                    raise errors.task_create_failed(
                        "数据库事务失败"
                    ) from e

    async def _next_seq(self, uow: PostgresUnitOfWork, run_id: UUID) -> int:
        """获取下一个 event_sequence。"""
        return await uow.events.get_next_sequence(run_id)
