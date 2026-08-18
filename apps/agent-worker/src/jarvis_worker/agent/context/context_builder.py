"""ConversationContextBuilder — 构造 AgentRunner 模型上下文的历史部分。

依赖边界：
  ConversationContextBuilder
    → ConversationHistoryReader (Protocol)
    → Application Service (ConversationApplicationService)
    → Repository interface
    → PostgreSQL adapter

不直接导入 storage.postgres.*、SQLAlchemy session 或 UnitOfWork。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Protocol
from uuid import UUID

from jarvis_worker.agent.research.lineage import (
    trusted_knowledge_provenance_from_tool_calls,
)
from jarvis_worker.shared.domain.models import Message, ToolCall

log = logging.getLogger("jarvis_worker.agent.context_builder")

# -- 有界查询候选上限 --
# 对应 MAX_HISTORY_TURNS × 2（每轮 user+assistant）加余量
MAX_HISTORY_CANDIDATE_MESSAGES = 40

# -- 上下文截断边界 --
MAX_HISTORY_TURNS = 10
MAX_HISTORY_CHARS = 8000


@dataclass(frozen=True)
class ConversationRunContext:
    """Model-visible history plus a separate Runtime-only provenance sidecar."""

    history_messages: list[dict[str, str]] = field(default_factory=list)
    trusted_provenance_links: list[dict[str, str]] = field(default_factory=list)
    provenance_run_id: str | None = None


class ConversationHistoryReader(Protocol):
    """会话历史读取接口——Context Builder 的唯一依赖。

    具体实现在 Application Service 层，由 adapter 注入。
    Context Builder 不依赖 PostgreSQL、SQLAlchemy 或 session factory。
    """

    async def get_recent_history(
        self,
        conversation_id: UUID,
        *,
        exclude_task_id: UUID | None = None,
        limit: int = MAX_HISTORY_CANDIDATE_MESSAGES,
    ) -> list[Message]:
        """有界查询——Repository 层在 SQL 中完成 LIMIT。

        Args:
            conversation_id: 会话 UUID
            exclude_task_id: 排除当前 Task 的消息
            limit: 候选消息上限（在 Repository 层执行）

        Returns:
            从旧到新的有界消息列表。
        """
        ...

    async def get_tool_calls_for_run(self, run_id: UUID) -> list[ToolCall]:
        """Return durable ToolCalls for one completed historical Run."""
        ...


class ConversationContextBuilder:
    """从 Application Service 获取有界会话历史，构造模型上下文。

    依赖 ConversationHistoryReader（Protocol），不依赖具体存储实现。

    用法：
        builder = ConversationContextBuilder(history_reader)
        history = await builder.build_history(conversation_id, exclude_task_id=task_id)
    """

    def __init__(self, history_reader: ConversationHistoryReader):
        self._reader = history_reader

    async def build_history(
        self,
        conversation_id: UUID,
        *,
        exclude_task_id: UUID | None = None,
        max_turns: int = MAX_HISTORY_TURNS,
        max_chars: int = MAX_HISTORY_CHARS,
    ) -> list[dict]:
        """查询并截断会话历史。

        候选消息已在 Repository 层经过有界查询（SQL LIMIT）。
        本方法在有限候选消息上执行轮次和字符截断。

        Args:
            conversation_id: 会话 UUID
            exclude_task_id: 排除指定 Task 的消息（防止当前 user_goal 重复注入）
            max_turns: 最多保留的轮次数
            max_chars: 历史总字符数上限

        Returns:
            list[dict]: [{role, content}, ...]，从旧到新。
        """
        messages = await self._reader.get_recent_history(
            conversation_id,
            exclude_task_id=exclude_task_id,
            limit=MAX_HISTORY_CANDIDATE_MESSAGES,
        )

        selected = self._truncate_messages(
            messages, max_turns=max_turns, max_chars=max_chars
        )
        return self._project_messages(selected)

    async def build_run_context(
        self,
        conversation_id: UUID,
        *,
        exclude_task_id: UUID | None = None,
        max_turns: int = MAX_HISTORY_TURNS,
        max_chars: int = MAX_HISTORY_CHARS,
    ) -> ConversationRunContext:
        """Build bounded history and provenance from its nearest source-bearing turn.

        Completed assistant turns selected by the same history bounds are scanned
        newest-first.  The nearest run with durable completed ToolCalls supplies
        provenance; plain assistant text and model-supplied IDs never do.  This
        preserves a trusted source chain across an intermediate summarisation turn
        without reaching outside the bounded conversation context.
        """
        messages = await self._reader.get_recent_history(
            conversation_id,
            exclude_task_id=exclude_task_id,
            limit=MAX_HISTORY_CANDIDATE_MESSAGES,
        )
        selected = self._truncate_messages(
            messages, max_turns=max_turns, max_chars=max_chars
        )
        history = self._project_messages(selected)
        seen_run_ids: set[UUID] = set()
        for message in reversed(selected):
            if (
                message.role != "assistant"
                or message.run_id is None
                or message.run_id in seen_run_ids
            ):
                continue
            seen_run_ids.add(message.run_id)
            tool_calls = await self._reader.get_tool_calls_for_run(message.run_id)
            provenance = trusted_knowledge_provenance_from_tool_calls(tool_calls)
            if provenance:
                return ConversationRunContext(
                    history_messages=history,
                    trusted_provenance_links=provenance,
                    provenance_run_id=str(message.run_id),
                )
        return ConversationRunContext(history_messages=history)

    @staticmethod
    def _truncate(
        messages: list[Message],
        *,
        max_turns: int,
        max_chars: int,
    ) -> list[dict]:
        """在有限候选消息上执行轮次和字符截断。

        候选消息已由 Repository 层完成：
        - conversation_id 过滤
        - exclude_task_id 排除
        - role ∈ {user, assistant}
        - SQL LIMIT 上限

        本层只做防御性过滤、完整轮次配对和最终截断。

        只有同一 task_id 下按时间顺序出现的 user -> assistant 才能进入
        模型上下文。失败、取消或仍在运行的 Task 只有 user message，必须保留
        在 PostgreSQL 供 UI/审计使用，但不能作为未配对历史影响后续工具选择。
        """
        return ConversationContextBuilder._project_messages(
            ConversationContextBuilder._truncate_messages(
                messages, max_turns=max_turns, max_chars=max_chars
            )
        )

    @staticmethod
    def _truncate_messages(
        messages: list[Message],
        *,
        max_turns: int,
        max_chars: int,
    ) -> list[Message]:
        """Select bounded complete turns while preserving trusted Message IDs."""
        # 防御性角色过滤（Repository 层已过滤，此处为安全网）
        dialog = [m for m in messages if m.role in ("user", "assistant")]
        if not dialog:
            return []

        if max_turns <= 0 or max_chars <= 0:
            return []

        # 按 task_id 配对完整轮次。task_id 为空的历史消息没有可靠归属，
        # fail closed：不猜测相邻消息是否属于同一轮。
        pending_users: dict[UUID, Message] = {}
        complete_turns: list[tuple[Message, Message]] = []
        for msg in dialog:
            if msg.task_id is None:
                continue
            if msg.role == "user":
                # 每个 Task 当前只应有一条 user message；重复数据保留最早一条，
                # 避免后写入内容覆盖原始任务目标。
                pending_users.setdefault(msg.task_id, msg)
                continue

            user_message = pending_users.pop(msg.task_id, None)
            if user_message is not None:
                complete_turns.append((user_message, msg))

        if not complete_turns:
            if dialog:
                log.debug("会话历史无完整轮次: 候选=%d", len(dialog))
            return []

        # 数量与字符都按完整轮次截断，永不产生孤立 user/assistant。
        keep_turns = complete_turns[-max_turns:]
        total_chars = sum(
            len(message.content or "")
            for turn in keep_turns
            for message in turn
        )
        while total_chars > max_chars and keep_turns:
            removed_turn = keep_turns.pop(0)
            total_chars -= sum(len(message.content or "") for message in removed_turn)

        keep = [message for turn in keep_turns for message in turn]

        if keep:
            log.debug(
                "会话历史: 候选=%d, 注入=%d, chars=%d",
                len(dialog), len(keep), total_chars,
            )

        return keep

    @staticmethod
    def _project_messages(messages: list[Message]) -> list[dict[str, str]]:
        return [
            {"role": message.role, "content": message.content or ""}
            for message in messages
        ]
