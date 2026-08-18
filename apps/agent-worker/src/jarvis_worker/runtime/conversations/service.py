"""ConversationApplicationService — 会话与消息查询的唯一 Owner。

负责：
- 会话列表
- 会话详情（含有界分页消息，next_cursor 基于最早消息）
- 会话不存在时的结构化错误
- 会话历史（供 Context Builder 使用）

不负责：
- 创建/更新 Conversation（由 TaskApplicationService 在 create_task 时处理）
- 写入消息（由 RuntimeApplicationService 在 agent.run.completed 时处理）
"""

from __future__ import annotations

import base64
import json
import logging
from datetime import datetime
from uuid import UUID

from jarvis_worker.database.unit_of_work import PostgresUnitOfWork
from jarvis_worker.shared.domain.models import Conversation, Message
from jarvis_worker.shared.errors import application as errors

log = logging.getLogger(__name__)

DEFAULT_MESSAGE_LIMIT = 50
MAX_MESSAGE_LIMIT = 100


class ConversationApplicationService:
    """会话查询 Application Service。"""

    def __init__(self, uow_factory):
        self._uow_factory = uow_factory

    # ── 会话列表 ──

    async def list_conversations(
        self, limit: int = 50, offset: int = 0
    ) -> list[Conversation]:
        session_factory = self._uow_factory()
        async with session_factory() as session:
            uow = PostgresUnitOfWork(session)
            return await uow.conversations.list_all(limit=limit, offset=offset)

    # ── 会话详情（有界分页） ──

    async def get_conversation_detail(
        self,
        conversation_id: UUID,
        *,
        limit: int = DEFAULT_MESSAGE_LIMIT,
        before: str | None = None,
    ) -> dict:
        """获取会话及其有界分页消息。

        Args:
            conversation_id: 会话 UUID
            limit: 每页消息数（默认 50，最大 100）
            before: 分页 cursor（base64(json([created_at_iso, message_id]))），
                    None 时取最近一页

        Returns:
            {"conversation": Conversation, "messages": list[Message],
             "next_cursor": str|None}

        Raises:
            AppError: cursor 非法或会话不存在。
        """
        limit = min(max(limit, 1), MAX_MESSAGE_LIMIT)

        session_factory = self._uow_factory()
        async with session_factory() as session:
            uow = PostgresUnitOfWork(session)

            conv = await uow.conversations.get(conversation_id)
            if conv is None:
                raise errors.not_found("Conversation", str(conversation_id))

            # 严格解析 cursor
            before_ts: datetime | None = None
            before_id: UUID | None = None
            if before:
                before_ts, before_id = _decode_cursor(before)

            # 多查 1 条判断 has_more
            messages = await uow.messages.list_recent_page(
                conversation_id=conversation_id,
                limit=limit + 1,
                before_ts=before_ts,
                before_id=before_id,
            )

            # messages 已为旧→新顺序
            has_more = len(messages) > limit
            if has_more:
                # 丢弃最早的那条（多查的）
                messages = messages[1:]  # ← 修复：丢最早（多查的），保留最新 limit 条

            # next_cursor 基于当前页最早消息
            next_cursor = None
            if has_more and messages:
                earliest = messages[0]
                raw = json.dumps([earliest.created_at.isoformat(), str(earliest.id)])
                next_cursor = base64.urlsafe_b64encode(raw.encode()).decode()

            return {
                "conversation": conv,
                "messages": messages,
                "next_cursor": next_cursor,
            }

    # ── 会话历史（Context Builder 用） ──

    async def get_recent_history(
        self,
        conversation_id: UUID,
        *,
        exclude_task_id: UUID | None = None,
        limit: int = 40,
    ) -> list[Message]:
        """获取会话最近消息（有界查询，供 Context Builder 使用）。

        在 Repository 层完成 SQL LIMIT，不读取整条会话。
        """
        session_factory = self._uow_factory()
        async with session_factory() as session:
            uow = PostgresUnitOfWork(session)
            return await uow.messages.list_recent_by_conversation(
                conversation_id,
                exclude_task_id=exclude_task_id,
                roles=("user", "assistant"),
                limit=limit,
            )

    async def get_tool_calls_for_run(self, run_id: UUID):
        """读取一个历史 Run 的持久化 ToolCalls，供可信上下文侧链使用。"""
        session_factory = self._uow_factory()
        async with session_factory() as session:
            uow = PostgresUnitOfWork(session)
            return await uow.tool_calls.list_by_run(run_id)


def _decode_cursor(cursor: str) -> tuple[datetime, UUID]:
    """严格解析分页 cursor。

    cursor 格式: base64(json([created_at_iso, message_id]))
    - 必须是长度为 2 的 JSON 数组
    - [0] 必须是合法带时区 ISO 时间
    - [1] 必须是合法 UUID

    Returns:
        (timezone-aware datetime, UUID)

    Raises:
        AppError: 解析格式、键序或类型非法。
    """
    try:
        raw = base64.urlsafe_b64decode(cursor.encode()).decode()
    except Exception:
        raise errors.validation_error("无效的分页 cursor: 无法解码")

    try:
        parts = json.loads(raw)
    except json.JSONDecodeError:
        raise errors.validation_error("无效的分页 cursor: 非合法 JSON")

    if not isinstance(parts, list) or len(parts) != 2:
        raise errors.validation_error(
            f"无效的分页 cursor: 数组长度应为 2，实际 {len(parts) if isinstance(parts, list) else type(parts).__name__}"
        )

    if not isinstance(parts[0], str):
        raise errors.validation_error("无效的分页 cursor: 时间必须是字符串")
    if not isinstance(parts[1], str):
        raise errors.validation_error("无效的分页 cursor: UUID 必须是字符串")

    # 解析时间
    try:
        ts_str = parts[0]
        # 支持 Z 后缀和 +00:00 格式
        ts = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
    except (ValueError, TypeError):
        raise errors.validation_error("无效的分页 cursor: 非法时间格式")

    if ts.tzinfo is None:
        raise errors.validation_error("无效的分页 cursor: 时间缺少时区")

    # 解析 UUID
    try:
        msg_id = UUID(parts[1])
    except (ValueError, TypeError):
        raise errors.validation_error("无效的分页 cursor: 非法 UUID")

    return ts, msg_id
