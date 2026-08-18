"""测试 agent/action_parser.py — Agent Action Parser。

覆盖：
- parse finish JSON -> AgentAction.finish
- parse call_tool workspace.list_files -> AgentAction.call_tool
- parse call_tool workspace.read_file -> AgentAction.call_tool
- 非 JSON 文本
- JSON array
- 缺 action_type
- 未知 action_type
- finish 缺 final_message
- finish final_message 空字符串
- call_tool 缺 tool_name
- call_tool tool_name 不在白名单
- call_tool 缺 arguments
- call_tool arguments 不是 object
"""

from __future__ import annotations

import json

import pytest

from jarvis_worker.agent.core.action_parser import (
    _DEFAULT_ALLOWED_TOOLS,
    MAX_FINAL_MESSAGE_CHARS,
    AgentActionFailureKind,
    ParseAgentActionError,
    parse_agent_action,
)
from jarvis_worker.agent.core.actions import AgentAction

# ============================================================
# Parser 成功测试
# ============================================================

class TestParseSuccess:
    """Parser 成功场景。"""

    def test_parse_finish_json(self) -> None:
        """解析 finish JSON -> AgentAction.finish。"""
        raw = '{"action_type": "finish", "final_message": "任务已完成"}'
        action = parse_agent_action(raw)
        assert isinstance(action, AgentAction)
        assert action.action_type == "finish"
        assert action.final_message == "任务已完成"
        assert action.tool_name == ""
        assert action.arguments == {}

    def test_parse_finish_accepts_exact_output_boundary(self) -> None:
        final_message = "x" * MAX_FINAL_MESSAGE_CHARS

        action = parse_agent_action(
            json.dumps({"action_type": "finish", "final_message": final_message})
        )

        assert action.final_message == final_message

    def test_parse_finish_with_reason(self) -> None:
        """finish 带多余字段（如 reason）仍正常解析。"""
        raw = '{"action_type": "finish", "final_message": "完成", "reason": "无需工具"}'
        action = parse_agent_action(raw)
        assert action.action_type == "finish"
        assert action.final_message == "完成"

    def test_parse_finish_with_rag_citations(self) -> None:
        chunk_id = "11111111-1111-1111-1111-111111111111"
        action = parse_agent_action(
            '{"action_type":"finish","final_message":"有证据的回答",'
            f'"citations":[{{"chunk_id":"{chunk_id}"}}],'
            '"insufficient_evidence":false}'
        )

        assert action.citations == ({"chunk_id": chunk_id},)
        assert action.insufficient_evidence is False

    def test_parse_finish_repairs_only_invalid_json_backslash_escapes(self) -> None:
        raw = (
            '{"action_type":"finish","final_message":'
            '"公式为 \\gamma 与合法换行\\n保持不变"}'
        )

        action = parse_agent_action(raw)

        assert action.final_message == "公式为 \\gamma 与合法换行\n保持不变"

    def test_parse_call_tool_list_files(self) -> None:
        """解析 call_tool workspace.list_files -> AgentAction.call_tool。

        LLM 只提供模型可控参数（path），workspace_root 由 AgentRunner 注入。
        """
        raw = (
            '{"action_type": "call_tool",'
            ' "tool_name": "workspace.list_files",'
            ' "arguments": {"path": "."},'
            ' "reason": "用户要求列出文件"}'
        )
        action = parse_agent_action(raw)
        assert isinstance(action, AgentAction)
        assert action.action_type == "call_tool"
        assert action.tool_name == "workspace.list_files"
        assert action.arguments == {"path": "."}
        assert action.reason == "用户要求列出文件"

    def test_parse_call_tool_read_file(self) -> None:
        """解析 call_tool workspace.read_file -> AgentAction.call_tool。

        LLM 只提供模型可控参数（path），workspace_root 由 AgentRunner 注入。
        """
        raw = (
            '{"action_type": "call_tool",'
            ' "tool_name": "workspace.read_file",'
            ' "arguments": {"path": "README.md"},'
            ' "reason": "读取 README"}'
        )
        action = parse_agent_action(raw)
        assert isinstance(action, AgentAction)
        assert action.action_type == "call_tool"
        assert action.tool_name == "workspace.read_file"
        assert action.arguments == {"path": "README.md"}

    def test_tool_required_mode_accepts_call_tool_and_rejects_finish(self) -> None:
        action = parse_agent_action(
            '{"action_type":"call_tool","tool_name":"workspace.read_file",'
            '"arguments":{"path":"README.md"}}',
            allowed_action_types=frozenset({"call_tool"}),
        )

        assert action.action_type == "call_tool"
        with pytest.raises(ParseAgentActionError) as exc_info:
            parse_agent_action(
                '{"action_type":"finish","final_message":"提前结束"}',
                allowed_action_types=frozenset({"call_tool"}),
            )
        assert (
            exc_info.value.failure_kind
            == AgentActionFailureKind.UNSUPPORTED_ACTION.value
        )

    def test_finish_only_mode_rejects_call_tool(self) -> None:
        with pytest.raises(ParseAgentActionError) as exc_info:
            parse_agent_action(
                '{"action_type":"call_tool","tool_name":"workspace.read_file",'
                '"arguments":{"path":"README.md"}}',
                allowed_action_types=frozenset({"finish"}),
            )

        assert (
            exc_info.value.failure_kind
            == AgentActionFailureKind.UNSUPPORTED_ACTION.value
        )

    @pytest.mark.parametrize(
        "allowed_action_types",
        [frozenset(), frozenset({"delete"}), frozenset({"finish", "delete"})],
    )
    def test_allowed_action_types_must_be_non_empty_known_subset(
        self,
        allowed_action_types: frozenset[str],
    ) -> None:
        with pytest.raises(ValueError):
            parse_agent_action(
                '{"action_type":"finish","final_message":"完成"}',
                allowed_action_types=allowed_action_types,
            )

    def test_parse_call_tool_get_file_info(self) -> None:
        raw = (
            '{"action_type": "call_tool",'
            ' "tool_name": "workspace.get_file_info",'
            ' "arguments": {"path": "README.md"},'
            ' "reason": "查看元信息"}'
        )
        action = parse_agent_action(raw)
        assert action.tool_name == "workspace.get_file_info"
        assert action.arguments == {"path": "README.md"}

    def test_parse_call_tool_no_reason(self) -> None:
        """call_tool 不提供 reason 字段时，reason 应为空字符串。"""
        raw = (
            '{"action_type": "call_tool",'
            ' "tool_name": "workspace.list_files",'
            ' "arguments": {"path": "."}}'
        )
        action = parse_agent_action(raw)
        assert action.action_type == "call_tool"
        assert action.reason == ""

    def test_parse_finish_strips_whitespace(self) -> None:
        """finish final_message 两端空白应被 strip。"""
        raw = '{"action_type": "finish", "final_message": "  完成  "}'
        action = parse_agent_action(raw)
        assert action.final_message == "完成"

    def test_parse_finish_unwraps_nested_agent_action_json(self) -> None:
        nested = json.dumps(
            {
                "action_type": "finish",
                "final_message": "## 最终回答\n\n- 第一项",
            },
            ensure_ascii=False,
        )
        raw = json.dumps(
            {"action_type": "finish", "final_message": nested},
            ensure_ascii=False,
        )

        action = parse_agent_action(raw)

        assert action.final_message == "## 最终回答\n\n- 第一项"

    def test_parse_finish_unwraps_whole_markdown_fence(self) -> None:
        raw = json.dumps(
            {
                "action_type": "finish",
                "final_message": "```markdown\n**完成**\n```",
            },
            ensure_ascii=False,
        )

        action = parse_agent_action(raw)

        assert action.final_message == "**完成**"

    def test_parse_finish_preserves_user_json_content(self) -> None:
        final_message = '{"status":"ok","items":[1,2]}'
        raw = json.dumps(
            {"action_type": "finish", "final_message": final_message},
            ensure_ascii=False,
        )

        action = parse_agent_action(raw)

        assert action.final_message == final_message

    def test_parse_call_tool_strips_tool_name(self) -> None:
        """call_tool tool_name 两端空白应被 strip。"""
        raw = (
            '{"action_type": "call_tool",'
            ' "tool_name": "  workspace.list_files  ",'
            ' "arguments": {"path": "."}}'
        )
        action = parse_agent_action(raw)
        assert action.tool_name == "workspace.list_files"


# ============================================================
# Parser 失败测试
# ============================================================

class TestParseFailure:
    """Parser 失败场景。"""

    # -- JSON 格式错误 --

    def test_non_json_text(self) -> None:
        """非 JSON 文本应抛出 ParseAgentActionError。"""
        with pytest.raises(ParseAgentActionError) as exc_info:
            parse_agent_action("这不可能是 JSON")
        assert "不是合法 JSON" in str(exc_info.value)
        assert exc_info.value.failure_kind == AgentActionFailureKind.INVALID_JSON.value
        assert not hasattr(exc_info.value, "raw_text")

    def test_whole_json_fence_is_unwrapped_before_strict_validation(self) -> None:
        action = parse_agent_action(
            "```JSON\r\n"
            '{"action_type":"finish","final_message":"完成"}'
            "\r\n```"
        )

        assert action.action_type == "finish"
        assert action.final_message == "完成"

    def test_json_fence_with_extra_text_is_not_repaired(self) -> None:
        with pytest.raises(ParseAgentActionError) as exc_info:
            parse_agent_action(
                "说明：\n```json\n"
                '{"action_type":"finish","final_message":"完成"}'
                "\n```"
            )
        assert exc_info.value.failure_kind == AgentActionFailureKind.INVALID_JSON.value

    def test_literal_control_chars_inside_string_are_normalized_by_decoder(self) -> None:
        action = parse_agent_action(
            '{"action_type":"call_tool",'
            '"tool_name":"knowledge.create_document",'
            '"arguments":{"title":"测试笔记","content":"第一行\n\t第二行"}}',
            allowed_tools=frozenset({"knowledge.create_document"}),
        )

        assert action.arguments == {
            "title": "测试笔记",
            "content": "第一行\n\t第二行",
        }

    def test_structural_json_damage_is_still_rejected(self) -> None:
        with pytest.raises(ParseAgentActionError) as exc_info:
            parse_agent_action(
                '{"action_type":"finish","final_message":"缺少右括号"'
            )

        assert exc_info.value.failure_kind == AgentActionFailureKind.INVALID_JSON.value

    def test_adjacent_action_objects_are_sequentialized_to_first(self) -> None:
        action = parse_agent_action(
            '{"action_type":"call_tool","tool_name":"rag.ingest_artifact",'
            '"arguments":{"artifact_id":"artifact-1"}}\n'
            '{"action_type":"call_tool","tool_name":"knowledge.create_document",'
            '"arguments":{"title":"笔记","kind":"note","content":"正文"}}',
            allowed_tools=frozenset(
                {"rag.ingest_artifact", "knowledge.create_document"}
            ),
        )

        assert action.tool_name == "rag.ingest_artifact"
        assert action.arguments == {"artifact_id": "artifact-1"}

    def test_json_object_followed_by_prose_is_not_sequentialized(self) -> None:
        with pytest.raises(ParseAgentActionError) as exc_info:
            parse_agent_action(
                '{"action_type":"finish","final_message":"完成"}\n继续说明'
            )

        assert exc_info.value.failure_kind == AgentActionFailureKind.INVALID_JSON.value

    def test_duplicate_field_is_rejected(self) -> None:
        with pytest.raises(ParseAgentActionError) as exc_info:
            parse_agent_action(
                '{"action_type":"finish","action_type":"call_tool",'
                '"final_message":"done"}'
            )
        assert exc_info.value.failure_kind == AgentActionFailureKind.DUPLICATE_FIELD.value

    @pytest.mark.parametrize("constant", ["NaN", "Infinity", "-Infinity"])
    def test_non_standard_json_constant_is_rejected(self, constant: str) -> None:
        with pytest.raises(ParseAgentActionError) as exc_info:
            parse_agent_action(
                '{"action_type":"call_tool",'
                '"tool_name":"workspace.list_files",'
                f'"arguments":{{"depth":{constant}}}}}'
            )
        assert (
            exc_info.value.failure_kind
            == AgentActionFailureKind.INVALID_JSON_CONSTANT.value
        )

    def test_json_array(self) -> None:
        """JSON array 应被拒绝。"""
        with pytest.raises(ParseAgentActionError) as exc_info:
            parse_agent_action('[{"action_type": "finish"}]')
        assert "JSON object" in str(exc_info.value)
        assert "list" in str(exc_info.value).lower()

    def test_json_number(self) -> None:
        """JSON 数字不是 object。"""
        with pytest.raises(ParseAgentActionError) as exc_info:
            parse_agent_action("42")
        assert "JSON object" in str(exc_info.value)

    def test_json_string(self) -> None:
        """JSON 字符串不是 object。"""
        with pytest.raises(ParseAgentActionError) as exc_info:
            parse_agent_action('"hello"')
        assert "JSON object" in str(exc_info.value)

    def test_json_null(self) -> None:
        """JSON null 不是 object。"""
        with pytest.raises(ParseAgentActionError) as exc_info:
            parse_agent_action("null")
        assert "JSON object" in str(exc_info.value)

    # -- action_type 校验 --

    def test_missing_action_type(self) -> None:
        """缺 action_type 应报错。"""
        with pytest.raises(ParseAgentActionError) as exc_info:
            parse_agent_action('{"final_message": "hello"}')
        assert "action_type" in str(exc_info.value).lower()
        assert "缺失" in str(exc_info.value)
        assert exc_info.value.failure_kind == AgentActionFailureKind.MISSING_FIELD.value

    def test_action_type_not_string(self) -> None:
        """action_type 非字符串应报错。"""
        with pytest.raises(ParseAgentActionError) as exc_info:
            parse_agent_action('{"action_type": 123}')
        assert "必须是字符串" in str(exc_info.value)

    def test_action_type_empty_string(self) -> None:
        """action_type 为空字符串应报错。"""
        with pytest.raises(ParseAgentActionError) as exc_info:
            parse_agent_action('{"action_type": ""}')
        assert "空字符串" in str(exc_info.value)

    def test_action_type_whitespace_only(self) -> None:
        """action_type 仅空白字符应报错。"""
        with pytest.raises(ParseAgentActionError) as exc_info:
            parse_agent_action('{"action_type": "   "}')
        assert "空字符串" in str(exc_info.value)

    def test_unknown_action_type(self) -> None:
        """未知 action_type 应报错。"""
        with pytest.raises(ParseAgentActionError) as exc_info:
            parse_agent_action('{"action_type": "sing_a_song"}')
        assert "未知" in str(exc_info.value)
        assert "sing_a_song" not in str(exc_info.value)

    # -- finish 校验 --

    def test_finish_missing_final_message(self) -> None:
        """finish 缺 final_message 应报错。"""
        with pytest.raises(ParseAgentActionError) as exc_info:
            parse_agent_action('{"action_type": "finish"}')
        assert "final_message" in str(exc_info.value).lower()
        assert "缺少" in str(exc_info.value)

    def test_finish_final_message_empty(self) -> None:
        """finish final_message 空字符串应报错。"""
        with pytest.raises(ParseAgentActionError) as exc_info:
            parse_agent_action('{"action_type": "finish", "final_message": ""}')
        assert "空字符串" in str(exc_info.value)

    def test_finish_final_message_whitespace_only(self) -> None:
        """finish final_message 仅空白字符应报错。"""
        with pytest.raises(ParseAgentActionError) as exc_info:
            parse_agent_action('{"action_type": "finish", "final_message": "   "}')
        assert "空字符串" in str(exc_info.value)

    def test_finish_final_message_over_capacity_fails_closed(self) -> None:
        raw = json.dumps(
            {
                "action_type": "finish",
                "final_message": "x" * (MAX_FINAL_MESSAGE_CHARS + 1),
            }
        )

        with pytest.raises(ParseAgentActionError) as exc_info:
            parse_agent_action(raw)

        assert exc_info.value.failure_kind == "response_too_large"

    def test_finish_final_message_not_string(self) -> None:
        """finish final_message 不是字符串应报错。"""
        with pytest.raises(ParseAgentActionError) as exc_info:
            parse_agent_action('{"action_type": "finish", "final_message": 123}')
        assert "必须是字符串" in str(exc_info.value)

    def test_finish_final_message_is_null(self) -> None:
        """finish final_message 为 null 应报错（JSON null → Python None → 命中缺失检查）。"""
        with pytest.raises(ParseAgentActionError) as exc_info:
            parse_agent_action('{"action_type": "finish", "final_message": null}')
        # JSON null 解析为 Python None，触发 final_message 缺失检查
        assert "final_message" in str(exc_info.value).lower()

    def test_finish_rejects_unknown_top_level_field(self) -> None:
        with pytest.raises(ParseAgentActionError) as exc_info:
            parse_agent_action(
                '{"action_type":"finish","final_message":"done","debug":true}'
            )
        assert (
            exc_info.value.failure_kind
            == AgentActionFailureKind.UNEXPECTED_FIELD.value
        )

    # -- call_tool 校验 --

    def test_call_tool_missing_tool_name(self) -> None:
        """call_tool 缺 tool_name 应报错。"""
        with pytest.raises(ParseAgentActionError) as exc_info:
            parse_agent_action(
                '{"action_type": "call_tool",'
                ' "arguments": {"path": "."}}'
            )
        assert "tool_name" in str(exc_info.value).lower()
        assert "缺少" in str(exc_info.value)

    def test_call_tool_tool_name_empty(self) -> None:
        """call_tool tool_name 为空字符串应报错。"""
        with pytest.raises(ParseAgentActionError) as exc_info:
            parse_agent_action(
                '{"action_type": "call_tool",'
                ' "tool_name": "",'
                ' "arguments": {"path": "."}}'
            )
        assert "空字符串" in str(exc_info.value)

    def test_call_tool_tool_name_not_string(self) -> None:
        """call_tool tool_name 不是字符串应报错。"""
        with pytest.raises(ParseAgentActionError) as exc_info:
            parse_agent_action(
                '{"action_type": "call_tool",'
                ' "tool_name": 42,'
                ' "arguments": {"path": "."}}'
            )
        assert "必须是字符串" in str(exc_info.value)

    def test_call_tool_tool_name_not_in_allowlist(self) -> None:
        """call_tool tool_name 不在白名单应报错。"""
        with pytest.raises(ParseAgentActionError) as exc_info:
            parse_agent_action(
                '{"action_type": "call_tool",'
                ' "tool_name": "rm -rf /",'
                ' "arguments": {"path": "/"}}'
            )
        assert "不在允许列表中" in str(exc_info.value)
        assert "rm -rf /" not in str(exc_info.value)
        assert exc_info.value.failure_kind == AgentActionFailureKind.TOOL_NOT_ALLOWED.value

    def test_call_tool_tool_name_not_in_custom_allowlist(self) -> None:
        """使用自定义白名单时，不在白名单的工具应报错。"""
        custom = frozenset({"workspace.read_file"})
        with pytest.raises(ParseAgentActionError) as exc_info:
            parse_agent_action(
                '{"action_type": "call_tool",'
                ' "tool_name": "workspace.list_files",'
                ' "arguments": {"path": "."}}',
                allowed_tools=custom,
            )
        assert "不在允许列表中" in str(exc_info.value)

    def test_call_tool_missing_arguments(self) -> None:
        """call_tool 缺 arguments 应报错。"""
        with pytest.raises(ParseAgentActionError) as exc_info:
            parse_agent_action(
                '{"action_type": "call_tool",'
                ' "tool_name": "workspace.list_files"}'
            )
        assert "arguments" in str(exc_info.value).lower()
        assert "缺少" in str(exc_info.value)

    def test_call_tool_arguments_not_object(self) -> None:
        """call_tool arguments 不是 object 应报错。"""
        with pytest.raises(ParseAgentActionError) as exc_info:
            parse_agent_action(
                '{"action_type": "call_tool",'
                ' "tool_name": "workspace.list_files",'
                ' "arguments": "path=."}'
            )
        assert "json object" in str(exc_info.value).lower()

    def test_call_tool_arguments_is_array(self) -> None:
        """call_tool arguments 是 array 应报错。"""
        with pytest.raises(ParseAgentActionError) as exc_info:
            parse_agent_action(
                '{"action_type": "call_tool",'
                ' "tool_name": "workspace.list_files",'
                ' "arguments": [1, 2, 3]}'
            )
        assert "json object" in str(exc_info.value).lower()

    def test_call_tool_arguments_is_null(self) -> None:
        """call_tool arguments 是 null 应报错。"""
        with pytest.raises(ParseAgentActionError) as exc_info:
            parse_agent_action(
                '{"action_type": "call_tool",'
                ' "tool_name": "workspace.list_files",'
                ' "arguments": null}'
            )
        assert "arguments" in str(exc_info.value).lower()

    def test_call_tool_reason_must_be_string(self) -> None:
        with pytest.raises(ParseAgentActionError) as exc_info:
            parse_agent_action(
                '{"action_type":"call_tool",'
                '"tool_name":"workspace.list_files",'
                '"arguments":{"path":"."},"reason":42}'
            )
        assert (
            exc_info.value.failure_kind
            == AgentActionFailureKind.INVALID_FIELD_TYPE.value
        )

    # -- 可信运行时参数拒绝 --

    def test_call_tool_rejects_workspace_root_in_arguments(self) -> None:
        """LLM 在 arguments 中提供 workspace_root="/" 应被拒绝。"""
        with pytest.raises(ParseAgentActionError) as exc_info:
            parse_agent_action(
                '{"action_type": "call_tool",'
                ' "tool_name": "workspace.list_files",'
                ' "arguments": {"workspace_root": "/", "path": "."}}'
            )
        assert "workspace_root" in str(exc_info.value)
        assert "可信" in str(exc_info.value) or "AgentRunner" in str(exc_info.value)

    def test_call_tool_rejects_workspace_root_in_read_file(self) -> None:
        """read_file 的 arguments 含 workspace_root 也应被拒绝。"""
        with pytest.raises(ParseAgentActionError) as exc_info:
            parse_agent_action(
                '{"action_type": "call_tool",'
                ' "tool_name": "workspace.read_file",'
                ' "arguments": {"workspace_root": "/", "path": "etc/passwd"}}'
            )
        assert "workspace_root" in str(exc_info.value)


# ============================================================
# 默认白名单
# ============================================================

class TestDefaultAllowlist:
    """默认白名单验证。"""

    def test_default_allowlist_contains_expected_tools(self) -> None:
        """默认白名单包含 list_files、read_file、create_file。"""
        assert "workspace.list_files" in _DEFAULT_ALLOWED_TOOLS
        assert "workspace.read_file" in _DEFAULT_ALLOWED_TOOLS
        assert "workspace.create_file" in _DEFAULT_ALLOWED_TOOLS
        assert "workspace.create_directory" in _DEFAULT_ALLOWED_TOOLS
        assert "workspace.move_path" in _DEFAULT_ALLOWED_TOOLS
        assert "workspace.delete_path" in _DEFAULT_ALLOWED_TOOLS
        assert "workspace.search_files" in _DEFAULT_ALLOWED_TOOLS
        assert "workspace.search_text" in _DEFAULT_ALLOWED_TOOLS
        assert "workspace.get_file_info" in _DEFAULT_ALLOWED_TOOLS
        assert len(_DEFAULT_ALLOWED_TOOLS) == 10


# ============================================================
# 自定义白名单
# ============================================================

class TestCustomAllowlist:
    """自定义白名单验证。"""

    def test_custom_allowlist_replaces_default(self) -> None:
        """自定义白名单替换默认白名单。"""
        custom = frozenset({"workspace.read_file"})
        # workspace.list_files 在默认白名单但不在自定义白名单 → 应失败
        with pytest.raises(ParseAgentActionError):
            parse_agent_action(
                '{"action_type": "call_tool",'
                ' "tool_name": "workspace.list_files",'
                ' "arguments": {"path": "."}}',
                allowed_tools=custom,
            )
        # workspace.read_file 在自定义白名单 → 应成功
        action = parse_agent_action(
            '{"action_type": "call_tool",'
            ' "tool_name": "workspace.read_file",'
            ' "arguments": {"path": "README.md"}}',
            allowed_tools=custom,
        )
        assert action.tool_name == "workspace.read_file"
