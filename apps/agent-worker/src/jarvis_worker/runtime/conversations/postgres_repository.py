"""PostgreSQL MessageRepository + ConversationRepository。"""

from datetime import datetime
from typing import Optional
from uuid import UUID

from sqlalchemy import desc, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from jarvis_worker.database.models import ConversationModel, MessageModel, TaskModel
from jarvis_worker.database.repositories.interfaces import ConversationRepository, MessageRepository
from jarvis_worker.shared.domain.models import Conversation, Message


class PostgresMessageRepository(MessageRepository):
    """PostgreSQL Message 持久化 adapter。"""

    def __init__(self, session: AsyncSession):
        self._session = session

    async def create(self, message: Message) -> Message:
        model = MessageModel(
            id=message.id,
            conversation_id=message.conversation_id,
            task_id=message.task_id,
            run_id=message.run_id,
            role=message.role,
            content=message.content,
            tool_call_id=message.tool_call_id,
            created_at=message.created_at,
            metadata_json=message.metadata,
        )
        self._session.add(model)
        return message

    async def get(self, message_id: UUID) -> Message | None:
        model = await self._session.get(MessageModel, message_id)
        return self._to_domain(model) if model else None

    async def list_by_task(self, task_id: UUID) -> list[Message]:
        result = await self._session.execute(
            select(MessageModel)
            .where(MessageModel.task_id == task_id)
            .order_by(MessageModel.created_at.asc())
        )
        return [self._to_domain(m) for m in result.scalars().all()]

    async def list_by_conversation(self, conversation_id: UUID) -> list[Message]:
        result = await self._session.execute(
            select(MessageModel)
            .where(MessageModel.conversation_id == conversation_id)
            .order_by(MessageModel.created_at.asc())
        )
        return [self._to_domain(m) for m in result.scalars().all()]

    async def list_recent_by_conversation(
        self,
        conversation_id: UUID,
        *,
        exclude_task_id: UUID | None = None,
        roles: tuple[str, ...] = ("user", "assistant"),
        limit: int = 40,
    ) -> list[Message]:
        """有界查询——在 SQL 层完成过滤、排序和截断。

        SQL 层面：
        - conversation_id 过滤
        - 排除 exclude_task_id
        - 只查询 user / assistant
        - created_at DESC + id DESC 稳定排序
        - LIMIT 截断
        - 返回前恢复从旧到新顺序
        """
        stmt = (
            select(MessageModel)
            .where(MessageModel.conversation_id == conversation_id)
            .where(MessageModel.role.in_(roles))
        )
        if exclude_task_id is not None:
            stmt = stmt.where(MessageModel.task_id != exclude_task_id)

        stmt = stmt.order_by(desc(MessageModel.created_at), desc(MessageModel.id)).limit(limit)

        result = await self._session.execute(stmt)
        rows = result.scalars().all()
        # 从旧到新恢复
        rows_reversed = list(reversed(rows))
        return [self._to_domain(m) for m in rows_reversed]

    async def list_recent_page(
        self,
        conversation_id: UUID,
        *,
        limit: int = 50,
        before_ts: datetime | None = None,
        before_id: UUID | None = None,
    ) -> list[Message]:
        """基于 (created_at, id) 的键集分页，避免 OFFSET 漂移。

        before_ts + before_id 不为 None 时，只返回该位置之前的消息。
        结果从旧到新排序。
        """
        stmt = (
            select(MessageModel)
            .where(MessageModel.conversation_id == conversation_id)
        )

        if before_ts is not None and before_id is not None:
            from sqlalchemy import and_, or_
            stmt = stmt.where(
                or_(
                    MessageModel.created_at < before_ts,
                    and_(
                        MessageModel.created_at == before_ts,
                        MessageModel.id < before_id,
                    ),
                )
            )

        # DESC 取最近 N 条，再反转恢复从旧到新
        stmt = stmt.order_by(desc(MessageModel.created_at), desc(MessageModel.id)).limit(limit)

        result = await self._session.execute(stmt)
        rows = result.scalars().all()
        return [self._to_domain(m) for m in reversed(rows)]

    @staticmethod
    def _to_domain(m: MessageModel) -> Message:
        return Message(
            id=m.id,
            conversation_id=m.conversation_id,
            task_id=m.task_id,
            run_id=m.run_id,
            role=m.role,
            content=m.content,
            tool_call_id=m.tool_call_id,
            created_at=m.created_at,
            metadata=m.metadata_json,
        )


class PostgresConversationRepository(ConversationRepository):
    """PostgreSQL Conversation 持久化 adapter。"""

    def __init__(self, session: AsyncSession):
        self._session = session

    async def create(self, conversation: Conversation) -> Conversation:
        model = ConversationModel(
            id=conversation.id,
            title=conversation.title,
            created_at=conversation.created_at,
            updated_at=conversation.updated_at,
        )
        self._session.add(model)
        return conversation

    async def get(self, conversation_id: UUID) -> Optional[Conversation]:
        result = await self._session.execute(
            select(ConversationModel).where(ConversationModel.id == conversation_id)
        )
        model = result.scalar_one_or_none()
        if model is None:
            return None
        return self._to_domain(model)

    async def update(self, conversation: Conversation) -> None:
        await self._session.execute(
            update(ConversationModel)
            .where(ConversationModel.id == conversation.id)
            .values(title=conversation.title, updated_at=conversation.updated_at)
        )

    async def get_by_task(self, task_id: UUID) -> Optional[Conversation]:
        result = await self._session.execute(
            select(ConversationModel)
            .join(TaskModel, TaskModel.conversation_id == ConversationModel.id)
            .where(TaskModel.id == task_id)
        )
        model = result.scalar_one_or_none()
        if model is None:
            return None
        return self._to_domain(model)

    async def list_all(self, limit: int = 50, offset: int = 0) -> list[Conversation]:
        result = await self._session.execute(
            select(ConversationModel)
            .order_by(ConversationModel.updated_at.desc())
            .limit(limit)
            .offset(offset)
        )
        return [self._to_domain(m) for m in result.scalars().all()]

    @staticmethod
    def _to_domain(m: ConversationModel) -> Conversation:
        return Conversation(
            id=m.id,
            title=m.title,
            created_at=m.created_at,
            updated_at=m.updated_at,
        )
