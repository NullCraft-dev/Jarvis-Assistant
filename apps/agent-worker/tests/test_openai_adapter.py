"""测试 models/openai_compatible_adapter.py。"""

from __future__ import annotations

import json

import pytest

from jarvis_worker.agent.models.messages import ModelMessage
from jarvis_worker.agent.models.openai_compatible_adapter import (
    AdapterError,
    _make_alias,
    build_chat_messages,
    build_request_body,
)


class TestAlias:
    def test_known_works(self):
        a = _make_alias("workspace.list_files")
        assert "." not in a
        assert len(a) <= 64
        assert a == _make_alias("workspace.list_files")  # 稳定

    def test_cross_process_stable(self):
        assert _make_alias("my.tool.v1") == _make_alias("my.tool.v1")

    def test_dot_vs_underscore_different(self):
        """a.b 和 a_b 必须生成不同 alias。"""
        a1 = _make_alias("a.b")
        a2 = _make_alias("a_b")
        assert a1 != a2

    def test_special_chars(self):
        a = _make_alias("tool with spaces!@#")
        assert " " not in a
        assert "!" not in a

    def test_very_long_name(self):
        a = _make_alias("x" * 100)
        assert len(a) <= 64

    def test_empty_name(self):
        a = _make_alias("")
        assert 1 <= len(a) <= 64


class TestChatMessages:
    def test_system_user(self):
        r = build_chat_messages([ModelMessage.system("s"), ModelMessage.user("u")])
        assert r == [{"role": "system", "content": "s"}, {"role": "user", "content": "u"}]

    def test_tool_no_name_field(self):
        msgs = [
            ModelMessage.system("s"), ModelMessage.user("u"),
            ModelMessage.assistant(
                json.dumps({"action_type": "call_tool", "tool_name": "workspace.read_file", "arguments": {"path": "f"}}),
                name="workspace.read_file", tool_call_id="tc-1",
            ),
            ModelMessage.tool("{}", name="workspace.read_file", tool_call_id="tc-1"),
        ]
        r = build_chat_messages(msgs, native_tool_history=True)
        assert "name" not in r[3]

    def test_function_arguments_only(self):
        msgs = [
            ModelMessage.system("s"), ModelMessage.user("u"),
            ModelMessage.assistant(
                json.dumps({"action_type": "call_tool", "tool_name": "w.rf", "arguments": {"path": "x.md", "nested": {"k": "v"}}, "reason": "r"}),
                name="w.rf", tool_call_id="tc-1",
            ),
            ModelMessage.tool("{}", name="w.rf", tool_call_id="tc-1"),
        ]
        r = build_chat_messages(msgs, native_tool_history=True)
        args = json.loads(r[2]["tool_calls"][0]["function"]["arguments"])
        assert args == {"path": "x.md", "nested": {"k": "v"}}
        assert "action_type" not in args
        assert "reason" not in args

    def test_plain_assistant_no_tool_calls(self):
        r = build_chat_messages([ModelMessage.assistant('{"action_type":"finish"}')])
        assert "tool_calls" not in r[0]

    def test_default_custom_json_history_uses_runtime_data_message(self):
        action = {
            "action_type": "call_tool",
            "tool_name": "rag.ingest_artifact",
            "arguments": {"artifact_id": "artifact-1"},
        }
        tool_result = {
            "tool_name": "rag.ingest_artifact",
            "ok": True,
            "data": {"job_id": "job-1", "status": "queued"},
        }
        messages = [
            ModelMessage.system("s"),
            ModelMessage.user("u"),
            ModelMessage.assistant(
                json.dumps(action),
                name="rag.ingest_artifact",
                tool_call_id="tc-1",
            ),
            ModelMessage.tool(
                json.dumps(tool_result),
                name="rag.ingest_artifact",
                tool_call_id="tc-1",
            ),
        ]

        rendered = build_chat_messages(messages)

        assert [item["role"] for item in rendered] == [
            "system", "user", "assistant", "user",
        ]
        assert all("tool_calls" not in item for item in rendered)
        assert all(item["role"] != "tool" for item in rendered)
        prefix, payload_text = rendered[-1]["content"].split("\n", 1)
        assert prefix.startswith("[Runtime ToolResult")
        payload = json.loads(payload_text)
        assert payload == {
            "runtime_message_type": "tool_result",
            "tool_name": "rag.ingest_artifact",
            "tool_call_id": "tc-1",
            "result": tool_result,
        }

    def test_native_history_remains_explicit_opt_in(self):
        messages = [
            ModelMessage.assistant(
                json.dumps({
                    "action_type": "call_tool",
                    "tool_name": "w.rf",
                    "arguments": {"path": "x.md"},
                }),
                name="w.rf",
                tool_call_id="tc-1",
            ),
            ModelMessage.tool("{}", name="w.rf", tool_call_id="tc-1"),
        ]

        rendered = build_chat_messages(messages, native_tool_history=True)

        assert "tool_calls" in rendered[0]
        assert rendered[1]["role"] == "tool"


class TestAtomicPairValidation:
    def test_orphan_tool(self):
        with pytest.raises(AdapterError, match="孤立"):
            build_chat_messages([ModelMessage.tool("{}", name="w.rf", tool_call_id="x")])

    def test_assistant_no_tool(self):
        with pytest.raises(AdapterError, match="缺少 tool"):
            build_chat_messages([ModelMessage.assistant("{}", name="w.rf", tool_call_id="x")])

    def test_id_mismatch(self):
        msgs = [
            ModelMessage.assistant("{}", name="w.rf", tool_call_id="tc-1"),
            ModelMessage.tool("{}", name="w.rf", tool_call_id="tc-2"),
        ]
        with pytest.raises(AdapterError, match="不匹配"):
            build_chat_messages(msgs)

    def test_name_mismatch(self):
        msgs = [
            ModelMessage.assistant("{}", name="w.rf", tool_call_id="tc-1"),
            ModelMessage.tool("{}", name="w.lf", tool_call_id="tc-1"),
        ]
        with pytest.raises(AdapterError, match="不匹配"):
            build_chat_messages(msgs)

    def test_valid_pair(self):
        msgs = [
            ModelMessage.assistant(
                json.dumps({"action_type": "call_tool", "tool_name": "w.rf", "arguments": {"p": "."}}),
                name="w.rf", tool_call_id="tc-1",
            ),
            ModelMessage.tool("{}", name="w.rf", tool_call_id="tc-1"),
        ]
        build_chat_messages(msgs)  # OK


class TestAssistantContentValidation:
    def test_non_json_rejected(self):
        msgs = [
            ModelMessage.system("s"), ModelMessage.user("u"),
            ModelMessage.assistant("not json", name="w.rf", tool_call_id="tc-1"),
            ModelMessage.tool("{}", name="w.rf", tool_call_id="tc-1"),
        ]
        with pytest.raises(AdapterError, match="合法 JSON"):
            build_chat_messages(msgs)

    def test_array_rejected(self):
        msgs = [
            ModelMessage.system("s"), ModelMessage.user("u"),
            ModelMessage.assistant("[]", name="w.rf", tool_call_id="tc-1"),
            ModelMessage.tool("{}", name="w.rf", tool_call_id="tc-1"),
        ]
        with pytest.raises(AdapterError, match="object"):
            build_chat_messages(msgs)

    def test_no_arguments_rejected(self):
        msgs = [
            ModelMessage.system("s"), ModelMessage.user("u"),
            ModelMessage.assistant('{"action_type":"call_tool","tool_name":"w.rf"}', name="w.rf", tool_call_id="tc-1"),
            ModelMessage.tool("{}", name="w.rf", tool_call_id="tc-1"),
        ]
        with pytest.raises(AdapterError, match="arguments"):
            build_chat_messages(msgs)

    def test_arguments_not_dict_rejected(self):
        msgs = [
            ModelMessage.system("s"), ModelMessage.user("u"),
            ModelMessage.assistant('{"action_type":"call_tool","tool_name":"w.rf","arguments":"not_dict"}', name="w.rf", tool_call_id="tc-1"),
            ModelMessage.tool("{}", name="w.rf", tool_call_id="tc-1"),
        ]
        with pytest.raises(AdapterError, match="arguments"):
            build_chat_messages(msgs)

    def test_action_type_not_call_tool_rejected(self):
        msgs = [
            ModelMessage.system("s"), ModelMessage.user("u"),
            ModelMessage.assistant('{"action_type":"finish","final_message":"x"}', name="w.rf", tool_call_id="tc-1"),
            ModelMessage.tool("{}", name="w.rf", tool_call_id="tc-1"),
        ]
        with pytest.raises(AdapterError, match="call_tool"):
            build_chat_messages(msgs)

    @pytest.mark.parametrize(
        "tool_content",
        ["not json", "[]", "NaN", '{"ok":true,"ok":false}'],
    )
    def test_runtime_tool_content_fails_closed(self, tool_content):
        msgs = [
            ModelMessage.assistant(
                '{"action_type":"call_tool","tool_name":"w.rf","arguments":{}}',
                name="w.rf",
                tool_call_id="tc-1",
            ),
            ModelMessage.tool(
                tool_content,
                name="w.rf",
                tool_call_id="tc-1",
            ),
        ]

        with pytest.raises(AdapterError, match="tool content"):
            build_chat_messages(msgs)


class TestAliasConflictInRequest:
    def test_two_diff_names_diff_alias(self):
        """a.b 和 a_b 生成不同 alias。"""
        a1 = _make_alias("a.b")
        a2 = _make_alias("a_b")
        assert a1 != a2

    def test_two_diff_names_no_conflict(self):
        """两个不同 tool_name 不会冲突。"""
        msgs = [
            ModelMessage.system("s"), ModelMessage.user("u"),
            ModelMessage.assistant(
                json.dumps({"action_type": "call_tool", "tool_name": "tool.a", "arguments": {}}),
                name="tool.a", tool_call_id="t1",
            ),
            ModelMessage.tool("{}", name="tool.a", tool_call_id="t1"),
            ModelMessage.assistant(
                json.dumps({"action_type": "call_tool", "tool_name": "tool_b", "arguments": {}}),
                name="tool_b", tool_call_id="t2",
            ),
            ModelMessage.tool("{}", name="tool_b", tool_call_id="t2"),
        ]
        build_chat_messages(msgs, native_tool_history=True)  # OK


class TestAliasCrossProcess:
    def test_cross_process_stable(self):
        """两个独立进程生成同一 tool_name 的 alias 必须一致。"""
        import subprocess
        import sys
        code = (
            "from jarvis_worker.agent.models.openai_compatible_adapter import _make_alias; "
            "print(_make_alias('my.company.tool.v2'))"
        )
        r1 = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
        r2 = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
        assert r1.stdout.strip() == r2.stdout.strip()
        assert len(r1.stdout.strip()) <= 64


class TestRequestBody:
    def test_vendor_extensions_and_json_mode_are_not_added_by_generic_adapter(self):
        b = build_request_body([ModelMessage.system("s")], model="m")
        assert "thinking" not in b
        assert "response_format" not in b
