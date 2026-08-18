"""多轮对话 MVP 回归测试。"""
from __future__ import annotations

import asyncio
import base64
import json
from datetime import datetime, timezone
from unittest.mock import MagicMock
from uuid import UUID, uuid4

import pytest

from jarvis_worker.agent.context.context_builder import (
    MAX_HISTORY_CANDIDATE_MESSAGES,
    ConversationContextBuilder,
)
from jarvis_worker.agent.prompts.builder import PromptBuilder
from jarvis_worker.runtime.conversations.service import ConversationApplicationService
from jarvis_worker.shared.domain.models import Conversation, Message, ToolCall


def _make_msg(
    task_id=None,
    role="user",
    content="test",
    conv_id=None,
    created_at=None,
    run_id=None,
):
    return Message(id=uuid4(), conversation_id=conv_id or CONV_ID,
                   task_id=task_id or uuid4(), run_id=run_id,
                   role=role, content=content,
                   created_at=created_at or datetime.now(timezone.utc))


# ================================================================
# Context Builder with fake HistoryReader
# ================================================================
TASK_1 = UUID("a0000000-0000-0000-0000-000000000001")
TASK_2 = UUID("a0000000-0000-0000-0000-000000000002")
CONV_ID = UUID("c0000000-0000-0000-0000-000000000001")


class TestContextBuilderWithFakeReader:
    @staticmethod
    def _make_reader(msgs):
        calls = []
        class FakeReader:
            async def get_recent_history(self, conversation_id, *, exclude_task_id=None, limit=40):
                calls.append((conversation_id, exclude_task_id, limit))
                return msgs
        return FakeReader(), calls

    def test_first_round_excludes_current_task(self):
        reader, calls = self._make_reader([])
        builder = ConversationContextBuilder(reader)
        result = asyncio.run(builder.build_history(CONV_ID, exclude_task_id=TASK_1))
        assert result == []
        assert calls[0][1] == TASK_1

    def test_second_round_contains_first_round(self):
        msgs = [_make_msg(TASK_1, "user", "Q"), _make_msg(TASK_1, "assistant", "A")]
        reader, calls = self._make_reader(msgs)
        builder = ConversationContextBuilder(reader)
        result = asyncio.run(builder.build_history(CONV_ID, exclude_task_id=TASK_2))
        assert len(result) == 2
        assert calls[0][2] == MAX_HISTORY_CANDIDATE_MESSAGES

    def test_run_context_recovers_nearest_source_bearing_turn_tool_provenance(self):
        older_run, latest_run = uuid4(), uuid4()
        artifact_id, document_id, chunk_id = uuid4(), uuid4(), uuid4()
        search_call_id = uuid4()
        older_task = uuid4()
        msgs = [
            _make_msg(older_task, "user", "旧问题", run_id=older_run),
            _make_msg(older_task, "assistant", "旧回答", run_id=older_run),
            _make_msg(TASK_1, "user", "最新问题", run_id=latest_run),
            _make_msg(TASK_1, "assistant", "最新回答", run_id=latest_run),
        ]
        calls = []

        class FakeReader:
            async def get_recent_history(self, *_args, **_kwargs):
                return msgs

            async def get_tool_calls_for_run(self, run_id):
                calls.append(run_id)
                if run_id == latest_run:
                    return []
                return [ToolCall(
                    id=search_call_id,
                    task_id=TASK_1,
                    run_id=older_run,
                    step_id=uuid4(),
                    provider="native",
                    tool_name="rag.search",
                    risk_level="L0",
                    status="completed",
                    arguments={"query": "compare"},
                    result={
                        "kind": "json",
                        "summary": "找到证据",
                        "data": {
                            "results": [{
                                "document_id": str(document_id),
                                "source_artifact_id": str(artifact_id),
                                "chunks": [{
                                    "chunk_id": str(chunk_id),
                                    "role": "primary",
                                }],
                            }],
                        },
                    },
                )]

        context = asyncio.run(
            ConversationContextBuilder(FakeReader()).build_run_context(
                CONV_ID, exclude_task_id=TASK_2
            )
        )

        assert calls == [latest_run, older_run]
        assert context.provenance_run_id == str(older_run)
        assert context.trusted_provenance_links == [{
            "artifact_id": str(artifact_id),
            "rag_document_id": str(document_id),
            "rag_search_tool_call_id": str(search_call_id),
            "rag_chunk_id": str(chunk_id),
        }]

    def test_failed_task_orphan_user_is_not_injected(self):
        """权限拒绝/工具失败只留下 user message，不得污染下一任务。"""
        failed_task = UUID("a0000000-0000-0000-0000-000000000003")
        msgs = [
            _make_msg(TASK_1, "user", "上一轮问题"),
            _make_msg(TASK_1, "assistant", "上一轮回答"),
            _make_msg(failed_task, "user", "创建 stale.txt"),
        ]
        reader, _ = self._make_reader(msgs)

        result = asyncio.run(
            ConversationContextBuilder(reader).build_history(
                CONV_ID, exclude_task_id=TASK_2,
            )
        )

        assert result == [
            {"role": "user", "content": "上一轮问题"},
            {"role": "assistant", "content": "上一轮回答"},
        ]
        assert all("stale.txt" not in message["content"] for message in result)

    def test_only_orphan_messages_produce_empty_history(self):
        failed_task = UUID("a0000000-0000-0000-0000-000000000003")
        corrupt_task = UUID("a0000000-0000-0000-0000-000000000004")
        msgs = [
            _make_msg(failed_task, "user", "被拒绝的写入"),
            _make_msg(corrupt_task, "assistant", "没有对应用户消息"),
            Message(
                id=uuid4(), conversation_id=CONV_ID, task_id=None,
                role="user", content="无法确认所属任务",
                created_at=datetime.now(timezone.utc),
            ),
        ]
        reader, _ = self._make_reader(msgs)

        result = asyncio.run(
            ConversationContextBuilder(reader).build_history(
                CONV_ID, exclude_task_id=TASK_2,
            )
        )

        assert result == []

    def test_turn_limit_preserves_complete_pairs(self):
        msgs = []
        for index in range(3):
            task_id = UUID(f"a0000000-0000-0000-0000-{index + 10:012d}")
            msgs.extend([
                _make_msg(task_id, "user", f"Q{index}"),
                _make_msg(task_id, "assistant", f"A{index}"),
            ])
        reader, _ = self._make_reader(msgs)

        result = asyncio.run(
            ConversationContextBuilder(reader).build_history(
                CONV_ID, exclude_task_id=TASK_2, max_turns=2,
            )
        )

        assert result == [
            {"role": "user", "content": "Q1"},
            {"role": "assistant", "content": "A1"},
            {"role": "user", "content": "Q2"},
            {"role": "assistant", "content": "A2"},
        ]

    def test_char_limit_drops_whole_turn(self):
        msgs = [
            _make_msg(TASK_1, "user", "1234"),
            _make_msg(TASK_1, "assistant", "5678"),
            _make_msg(TASK_2, "user", "abc"),
            _make_msg(TASK_2, "assistant", "def"),
        ]
        reader, _ = self._make_reader(msgs)

        result = asyncio.run(
            ConversationContextBuilder(reader).build_history(
                CONV_ID, max_chars=7,
            )
        )

        assert result == [
            {"role": "user", "content": "abc"},
            {"role": "assistant", "content": "def"},
        ]

    def test_single_oversized_turn_is_not_partially_injected(self):
        msgs = [
            _make_msg(TASK_1, "user", "1234"),
            _make_msg(TASK_1, "assistant", "5678"),
        ]
        reader, _ = self._make_reader(msgs)

        result = asyncio.run(
            ConversationContextBuilder(reader).build_history(
                CONV_ID, max_chars=7,
            )
        )

        assert result == []

    def test_user_goal_only_once_in_prompt(self):
        builder = PromptBuilder()
        history = [{"role": "user", "content": "Q1"}, {"role": "assistant", "content": "A1"}]
        msgs = builder.build_messages(user_goal="Q2", history_messages=history)
        user_q2 = [m for m in msgs if m.role == "user" and m.content == "Q2"]
        assert len(user_q2) == 1

    def test_assistant_history_rebuilds_model_action_json(self):
        """展示层纯文本必须在模型边界重建为合法 finish AgentAction。"""
        builder = PromptBuilder()
        history = [
            {"role": "user", "content": "记住代号"},
            {"role": "assistant", "content": "已记住"},
        ]

        msgs = builder.build_messages(user_goal="代号是什么", history_messages=history)

        assert [m.role for m in msgs] == ["system", "user", "assistant", "user"]
        assert json.loads(msgs[2].content) == {
            "action_type": "finish",
            "final_message": "已记住",
        }

    def test_init_accepts_reader_not_uow(self):
        import inspect
        sig = inspect.signature(ConversationContextBuilder.__init__)
        params = list(sig.parameters.keys())
        assert "history_reader" in params
        assert "uow_factory" not in params


# ================================================================
# ApplicationService 分页（通过 fake Repository/UoW 真实调用）
# ================================================================


class _SessionCtx:
    async def __aenter__(self):
        return MagicMock()

    async def __aexit__(self, *args):
        return None


class TestPaginationWithFakeRepo:
    """真实调用 ConversationApplicationService.get_conversation_detail，
    通过 monkeypatch PostgresUnitOfWork 注入 fake repo。"""

    @pytest.fixture(autouse=True)
    def _patch_uow(self, monkeypatch):
        """替换 PostgresUnitOfWork 为 fake —— 用闭包保存状态。"""
        from jarvis_worker.runtime.conversations import service as cs
        _state = {"conv": None, "msgs": []}

        class FakeConvRepo:
            async def get(self, cid): return _state["conv"]
            async def list_all(self, limit=50, offset=0): return []

        class FakeMsgRepo:
            async def list_recent_page(self, conversation_id, *, limit=50, before_ts=None, before_id=None):
                candidate = [m for m in _state["msgs"] if m.conversation_id == conversation_id]
                if before_ts is not None and before_id is not None:
                    candidate = [m for m in candidate
                                 if m.created_at < before_ts or (m.created_at == before_ts and m.id < before_id)]
                sorted_desc = sorted(candidate, key=lambda m: (m.created_at, m.id), reverse=True)[:limit]
                return list(reversed(sorted_desc))
            async def list_recent_by_conversation(self, *args, **kw): return []

        class FakeUow:
            def __init__(self, session):
                self.conversations = FakeConvRepo()
                self.messages = FakeMsgRepo()
            async def transaction(self):
                return _FakeTx(self)

        class _FakeTx:
            def __init__(self, uow): self._uow = uow
            async def __aenter__(self): return self._uow
            async def __aexit__(self, *a): pass
            async def commit(self): pass
            async def rollback(self): pass
            async def flush(self): pass

        monkeypatch.setattr(cs, "PostgresUnitOfWork", FakeUow)
        self._state = _state

    def _svc(self):
        # uow_factory 返回一个 callable（无参），该 callable 返回 async context manager
        def _uow_factory():
            return lambda: _SessionCtx()
        return ConversationApplicationService(_uow_factory)

    def _set_messages(self, msgs: list[Message], conv: Conversation | None = None):
        self._state["msgs"] = list(msgs)
        self._state["conv"] = conv or Conversation(
            id=CONV_ID,
            title="t",
            created_at=datetime.now(timezone.utc),
            updated_at=datetime.now(timezone.utc),
        )

    def _set_missing_conversation(self):
        self._state["msgs"] = []
        self._state["conv"] = None

    def test_pagination_latest_not_lost(self):
        """limit+1 条消息：返回最新的 limit 条，丢弃最旧的 1 条。"""
        base = datetime(2026, 1, 1, 0, 0, tzinfo=timezone.utc)
        msgs = [
            Message(id=uuid4(), conversation_id=CONV_ID, task_id=uuid4(), role="user",
                    content=f"msg{i}", created_at=base.replace(minute=i))
            for i in range(51)  # 51 = limit + 1
        ]
        self._set_messages(msgs)
        svc = self._svc()

        result = asyncio.run(svc.get_conversation_detail(CONV_ID, limit=50))
        # 应返回最新 50 条（丢弃最旧 1 条）
        assert len(result["messages"]) == 50
        contents = [m.content for m in result["messages"]]
        assert "msg0" not in contents  # 最旧的被丢弃
        assert "msg50" in contents  # 最新的保留

    def test_next_cursor_based_on_earliest(self):
        """next_cursor 基于当前页最早消息。"""
        base = datetime(2026, 1, 1, 0, 0, tzinfo=timezone.utc)
        earliest = Message(id=uuid4(), conversation_id=CONV_ID, task_id=uuid4(), role="user",
                           content="earliest", created_at=base)
        msgs = [earliest] + [
            Message(id=uuid4(), conversation_id=CONV_ID, task_id=uuid4(), role="user",
                    content=f"msg{i}", created_at=base.replace(minute=i))
            for i in range(1, 52)
        ]
        self._set_messages(msgs)
        svc = self._svc()
        result = asyncio.run(svc.get_conversation_detail(CONV_ID, limit=50))
        assert result["next_cursor"] is not None
        parts = json.loads(base64.urlsafe_b64decode(result["next_cursor"].encode()).decode())
        assert UUID(parts[1]) == result["messages"][0].id  # 最早消息的 ID

    def test_two_pages_no_duplicate_no_gap(self):
        """连续两页：无重复无遗漏。"""
        base = datetime(2026, 1, 1, 0, 0, tzinfo=timezone.utc)
        msgs = [
            Message(id=uuid4(), conversation_id=CONV_ID, task_id=uuid4(), role="user",
                    content=f"msg{i:03d}", created_at=base.replace(hour=i // 60, minute=i % 60))
            for i in range(70)
        ]
        self._set_messages(msgs)
        svc = self._svc()
        page1 = asyncio.run(svc.get_conversation_detail(CONV_ID, limit=50))
        assert len(page1["messages"]) == 50
        assert page1["next_cursor"] is not None

        page2 = asyncio.run(svc.get_conversation_detail(CONV_ID, limit=50, before=page1["next_cursor"]))
        assert len(page2["messages"]) == 20  # 70 - 50 = 20

        all_ids = [m.id for m in page1["messages"]] + [m.id for m in page2["messages"]]
        assert len(all_ids) == 70
        assert len(set(all_ids)) == 70  # 无重复
        # 每页旧→新
        p1_ts = [m.created_at for m in page1["messages"]]
        p2_ts = [m.created_at for m in page2["messages"]]
        assert p1_ts == sorted(p1_ts)
        assert p2_ts == sorted(p2_ts)
        if p2_ts and p1_ts:
            assert p2_ts[-1] <= p1_ts[0]  # 第二页在时间上 ≤ 第一页

    def test_exact_limit_returns_all_no_cursor(self):
        """总数恰好等于 limit：next_cursor=null。"""
        base = datetime(2026, 1, 1, 0, 0, tzinfo=timezone.utc)
        msgs = [_make_msg(created_at=base.replace(minute=i)) for i in range(50)]
        self._set_messages(msgs)
        result = asyncio.run(self._svc().get_conversation_detail(CONV_ID, limit=50))
        assert len(result["messages"]) == 50
        assert result["next_cursor"] is None

    def test_less_than_limit_no_cursor(self):
        """总数少于 limit：next_cursor=null。"""
        msgs = [_make_msg() for _ in range(10)]
        self._set_messages(msgs)
        result = asyncio.run(self._svc().get_conversation_detail(CONV_ID, limit=50))
        assert len(result["messages"]) == 10
        assert result["next_cursor"] is None

    def test_not_found(self):
        """会话不存在 → not_found AppError。"""
        from jarvis_worker.shared.errors.application import AppError
        self._set_missing_conversation()
        with pytest.raises(AppError, match="不存在"):
            asyncio.run(self._svc().get_conversation_detail(uuid4()))


# ================================================================
# cursor 严格校验
# ================================================================
class TestCursorValidation:
    def _decode(self, cursor):
        from jarvis_worker.runtime.conversations.service import _decode_cursor
        return _decode_cursor(cursor)

    def _encode(self, ts, mid):
        return base64.urlsafe_b64encode(json.dumps([ts.isoformat(), str(mid)]).encode()).decode()

    def test_valid(self):
        now = datetime.now(timezone.utc)
        mid = uuid4()
        ts, uid = self._decode(self._encode(now, mid))
        assert ts == now
        assert uid == mid

    @pytest.mark.parametrize("cursor,expected_err", [
        ("!!!bad!!!", "无法解码"),
        (base64.urlsafe_b64encode(b"not json").decode(), "非合法 JSON"),
        (base64.urlsafe_b64encode(json.dumps({"time": "value"}).encode()).decode(), "数组长度应为 2"),
        (base64.urlsafe_b64encode(b'["only one"]').decode(), "长度应为 2"),
        (base64.urlsafe_b64encode(json.dumps([123, str(uuid4())]).encode()).decode(), "时间必须是字符串"),
        (base64.urlsafe_b64encode(json.dumps(["not-a-time", str(uuid4())]).encode()).decode(), "非法时间"),
        (base64.urlsafe_b64encode(json.dumps(["2026-07-15T00:00:00", str(uuid4())]).encode()).decode(), "缺少时区"),
        (base64.urlsafe_b64encode(json.dumps([datetime.now(timezone.utc).isoformat(), 123]).encode()).decode(), "UUID 必须是字符串"),
        (base64.urlsafe_b64encode(json.dumps([datetime.now(timezone.utc).isoformat(), "not-uuid"]).encode()).decode(), "非法 UUID"),
    ])
    def test_invalid(self, cursor, expected_err):
        from jarvis_worker.shared.errors.application import AppError
        with pytest.raises(AppError, match=expected_err):
            self._decode(cursor)


# ================================================================
# Worker 历史隔离 / DTO / 失败 / 边界
# ================================================================
class TestWorkerHistoryIsolation:
    def test_executor_no_set_history(self):
        from jarvis_worker.runtime.run_executor import AgentRunExecutor
        assert not hasattr(AgentRunExecutor.__new__(AgentRunExecutor), "set_history")

    def test_history_as_local_param(self):
        from jarvis_worker.runtime.run_executor import AgentRunExecutor
        from jarvis_worker.runtime_bus.messages import RunJobMessage
        calls = []
        class FR:  # Fake Runner
            def run(self, job, default_workspace_root="", cancel_check=None, history_messages=None, **kwargs):
                calls.append(history_messages)
                return []
        ex = AgentRunExecutor(agent_runner=FR(), worker_id="w1")  # type: ignore[arg-type]
        job = RunJobMessage(job_id="j1", trace_id="t1", task_id="t1", run_id="r1", user_goal="t", created_at="2026-01-01T00:00:00Z")
        ex.run_with_cancel_check(job, history_messages=[{"role": "user", "content": "hi"}])
        assert calls == [[{"role": "user", "content": "hi"}]]
        ex.run_with_cancel_check(job, history_messages=None)
        assert calls[-1] is None


class TestModelFailure:
    def test_invalid_action_failed_not_completed(self):
        from jarvis_worker.agent.core.runner import AgentRunner
        from jarvis_worker.runtime_bus.messages import RunJobMessage
        class IP:
            provider_name = "t"
            model_name = "t"
        class IP2(IP):
            def decide_next_action(self, s): return "not an AgentAction"
        class FG:
            def execute(self, r): raise AssertionError
        runner = AgentRunner(model_provider=IP2(), tool_gateway=FG(), worker_id="t")  # type: ignore[arg-type]
        envelopes = runner.run(RunJobMessage(job_id="j1", trace_id="t1", task_id="t1", run_id="r1", user_goal="t", created_at="2026-01-01T00:00:00Z"))
        types = [e.event_type for e in envelopes]
        assert "agent.run.failed" in types
        assert "agent.run.completed" not in types
        assert "model.call.failed" in types


class TestConvDTO:
    def test_updated_at_present(self):
        now = datetime.now(timezone.utc)
        c = Conversation(id=uuid4(), title="t", created_at=now, updated_at=now)
        resp = {"id": str(c.id), "title": c.title, "created_at": c.created_at.isoformat(), "updated_at": c.updated_at.isoformat()}
        assert set(resp.keys()) == {"id", "title", "created_at", "updated_at"}


class TestRunJobConvId:
    def test_roundtrip(self):
        from jarvis_worker.runtime_bus.messages import RunJobMessage
        j = RunJobMessage(job_id="j1", trace_id="t1", task_id="t1", run_id="r1", user_goal="t", created_at="2026-01-01T00:00:00Z", conversation_id="conv-123")
        j2 = RunJobMessage.from_payload(j.to_payload_json())
        assert j2.conversation_id == "conv-123"
