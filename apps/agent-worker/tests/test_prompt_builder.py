"""测试 prompts/builder.py — PromptBuilder（Phase 6B-0 v3 审查修复）。

覆盖：
- 原子 assistant/tool 消息对
- observation 字段缺失 → PromptBuildError
- 递归 JSON-safe sanitizer
- 合法消息顺序
"""

from __future__ import annotations

import json

import pytest

from jarvis_worker.agent.prompts.builder import (
    MAX_ARGUMENT_DEPTH,
    MAX_ARGUMENT_ITEMS,
    MAX_ARGUMENT_KEY_LENGTH,
    MAX_ARGUMENT_STRING_LENGTH,
    MAX_ENTRY_NAME_LENGTH,
    MAX_ERROR_MESSAGE_LENGTH,
    MAX_LIST_FILES_ENTRIES,
    MAX_OBSERVATIONS,
    MAX_PATH_LENGTH,
    MAX_READ_FILE_CHARS,
    MAX_REASON_LENGTH,
    MAX_SUMMARY_LENGTH,
    PromptBuilder,
    PromptBuildError,
)
from jarvis_worker.bootstrap.tool_registry import create_tool_registry


@pytest.fixture
def builder() -> PromptBuilder:
    return PromptBuilder()


def _read_obs(tool_call_id="tc-1", content="hello", path="README.md") -> dict:
    return {
        "tool_call_id": tool_call_id,
        "tool_name": "workspace.read_file",
        "model_action": {
            "action_type": "call_tool",
            "tool_name": "workspace.read_file",
            "arguments": {"path": path},
            "reason": "读取",
        },
        "ok": True,
        "summary": f"已读 {path}",
        "data": {"path": path, "content": content, "truncated": False},
    }


def _read_files_obs(tool_call_id="tc-batch") -> dict:
    return {
        "tool_call_id": tool_call_id,
        "tool_name": "workspace.read_files",
        "model_action": {
            "action_type": "call_tool",
            "tool_name": "workspace.read_files",
            "arguments": {
                "files": [
                    {"path": "src/a.py", "start_line": 10, "max_lines": 20},
                    {"path": "src/b.py", "start_line": 30, "max_lines": 20},
                ]
            },
            "reason": "批量读取证据",
        },
        "ok": True,
        "summary": "Batch read: 1/2 files succeeded",
        "data": {
            "requested_files": 2,
            "succeeded_files": 1,
            "failed_files": 1,
            "files": [
                {
                    "path": "src/a.py",
                    "ok": True,
                    "content": "evidence" * 1000,
                    "start_line": 10,
                    "end_line": 29,
                    "total_lines": 100,
                    "truncated": False,
                },
                {
                    "path": "src/b.py",
                    "ok": False,
                    "suggested_paths": ["src/runtime/b.py", "src/domain/b.py"],
                    "error": {"code": "FILE_NOT_FOUND", "message": "missing"},
                },
            ],
            "truncated": False,
        },
    }


def _list_obs(tool_call_id="tc-1", entries=None) -> dict:
    if entries is None:
        entries = [{"name": "A.md", "type": "file"}, {"name": "B.md", "type": "file"}]
    return {
        "tool_call_id": tool_call_id,
        "tool_name": "workspace.list_files",
        "model_action": {
            "action_type": "call_tool",
            "tool_name": "workspace.list_files",
            "arguments": {"path": "."},
            "reason": "列出",
        },
        "ok": True,
        "summary": f"{len(entries)} 条目",
        "data": {"entries": entries},
    }


def _search_obs(tool_call_id="tc-search", data=None) -> dict:
    if data is None:
        data = {
            "returned_matches": 1,
            "truncated": False,
            "truncation_reasons": [],
            "matches": [{"name": "README.md", "path": "docs/README.md", "type": "file"}],
        }
    return {
        "tool_call_id": tool_call_id,
        "tool_name": "workspace.search_files",
        "model_action": {
            "action_type": "call_tool",
            "tool_name": "workspace.search_files",
            "arguments": {"query": "readme", "path": "."},
            "reason": "查找文件",
        },
        "ok": True,
        "summary": "搜索完成",
        "data": data,
    }


def _search_text_obs(tool_call_id="tc-search-text", data=None) -> dict:
    if data is None:
        data = {
            "query": "CreateTask",
            "returned_matches": 1,
            "truncated": False,
            "truncation_reasons": [],
            "matches": [{
                "path": "apps/gateway/internal/api/handlers/task.go",
                "line_number": 42,
                "preview": "func (h *Handler) CreateTask(...) {",
            }],
        }
    return {
        "tool_call_id": tool_call_id,
        "tool_name": "workspace.search_text",
        "model_action": {
            "action_type": "call_tool",
            "tool_name": "workspace.search_text",
            "arguments": {"query": "CreateTask", "path": ".", "source_only": True},
            "reason": "定位源码实现",
        },
        "ok": True,
        "summary": "正文搜索完成",
        "data": data,
    }


def _info_obs(tool_call_id="tc-info", data=None) -> dict:
    if data is None:
        data = {
            "name": "README.md",
            "path": "README.md",
            "type": "file",
            "size_bytes": 42,
            "modified_at": "2026-07-17T00:00:00+00:00",
        }
    return {
        "tool_call_id": tool_call_id,
        "tool_name": "workspace.get_file_info",
        "model_action": {
            "action_type": "call_tool",
            "tool_name": "workspace.get_file_info",
            "arguments": {"path": "README.md"},
            "reason": "查看元信息",
        },
        "ok": True,
        "summary": "元信息查询完成",
        "data": data,
    }


def _rag_obs(tool_call_id="tc-rag") -> dict:
    return {
        "tool_call_id": tool_call_id,
        "tool_name": "rag.search",
        "model_action": {
            "action_type": "call_tool",
            "tool_name": "rag.search",
            "arguments": {"query": "vector database"},
            "reason": "检索专业资料",
        },
        "ok": True,
        "summary": "检索完成",
        "data": {
            "query": "vector database",
            "results": [{
                "chunk_id": "11111111-1111-4111-8111-111111111111",
                "document_id": "22222222-2222-4222-8222-222222222222",
                "document_title": "RAG Paper",
                "source_artifact_id": "33333333-3333-4333-8333-333333333333",
                "chunks": [{
                    "chunk_id": "11111111-1111-4111-8111-111111111111",
                    "role": "primary",
                    "content": "retrieved evidence",
                    "source_locator": {"page_start": 3},
                }],
                "elements": [],
            }],
            "truncated": False,
            "evidence_assessment": {
                "schema": "rag-evidence-assessment-v1",
                "sufficient": False,
                "reason_code": "REQUESTED_DOCUMENT_COVERAGE_INCOMPLETE",
                "evidence_count": 1,
                "covered_document_count": 1,
                "requested_document_count": 2,
            },
            "document_coverage": {
                "requested_count": 2,
                "covered_count": 1,
                "complete": False,
            },
        },
    }


def _arxiv_obs(tool_call_id="tc-arxiv", data=None) -> dict:
    return {
        "tool_call_id": tool_call_id,
        "tool_name": "literature.search_arxiv",
        "model_action": {
            "action_type": "call_tool", "tool_name": "literature.search_arxiv",
            "arguments": {"query": "AI agent memory", "max_results": 1},
        },
        "ok": True,
        "summary": "arXiv 检索完成：1 条未收录结果",
        "data": data or {},
    }


def _download_obs() -> dict:
    return {
        "tool_call_id": "tc-download",
        "tool_name": "literature.download_arxiv_pdf",
        "model_action": {
            "action_type": "call_tool",
            "tool_name": "literature.download_arxiv_pdf",
            "arguments": {"arxiv_id": "2401.12345v2"},
        },
        "ok": True,
        "summary": "已下载 arXiv 文献 2401.12345v2",
        "artifact_ids": ["11111111-1111-1111-1111-111111111111"],
        "data": {"arxiv_id": "2401.12345v2", "sha256": "a" * 64, "path": "secret.pdf"},
    }


# ============================================================
# 合法消息顺序
# ============================================================

class TestRoleOrder:
    def test_no_obs_system_user(self, builder):
        msgs = builder.build_messages("x")
        assert [m.role for m in msgs] == ["system", "user"]

    def test_one_obs_system_user_assistant_tool(self, builder):
        msgs = builder.build_messages("x", observations=[_read_obs()])
        assert [m.role for m in msgs] == ["system", "user", "assistant", "tool"]

    def test_two_obs_two_pairs(self, builder):
        obs = [_read_obs("tc-1", "a"), _read_obs("tc-2", "b")]
        msgs = builder.build_messages("x", observations=obs)
        assert [m.role for m in msgs] == [
            "system", "user",
            "assistant", "tool",
            "assistant", "tool",
        ]

    def test_assistant_tool_same_id(self, builder):
        msgs = builder.build_messages("x", observations=[_read_obs("tc-42")])
        a, t = msgs[2], msgs[3]
        assert a.role == "assistant"
        assert t.role == "tool"
        assert a.tool_call_id == t.tool_call_id == "tc-42"

    def test_valid_obs_always_produces_pair(self, builder):
        """合法 observation 始终生成严格的 assistant/tool 对。"""
        for _ in range(3):
            msgs = builder.build_messages("x", observations=[_read_obs()])
            assert len(msgs) == 4
            assert msgs[2].role == "assistant"
            assert msgs[3].role == "tool"


class TestWorkflowToolProjection:
    def test_download_exposes_trusted_artifact_id_but_not_path(self, builder):
        message = builder.build_messages("x", observations=[_download_obs()])[-1]
        data = json.loads(message.content)["data"]
        assert data["artifact_id"] == "11111111-1111-1111-1111-111111111111"
        assert data["arxiv_id"] == "2401.12345v2"
        assert "path" not in data


# ============================================================
# system/user 内容
# ============================================================

class TestSystemUserContent:
    def test_system_has_action_schema(self, builder):
        assert "call_tool" in builder.build_messages("x")[0].content

    def test_system_has_security_rules(self, builder):
        assert "不可信" in builder.build_messages("x")[0].content

    def test_system_no_workspace_root(self, builder):
        """安全指令明确告知模型不得自行提供 workspace_root。"""
        system = builder.build_messages("列出文件")[0].content
        assert "不得提供 workspace_root" in system or "不要输出系统级路径参数" in system or "workspace_root 由系统自动注入" in system

    def test_user_only_goal(self, builder):
        goal = "总结 AGENTS.md"
        msgs = builder.build_messages(goal)
        assert goal in msgs[1].content
        assert "不可信" not in msgs[1].content

    def test_user_no_tool_data(self, builder):
        obs = [_read_obs(content="SECRET")]
        msgs = builder.build_messages("x", observations=obs)
        assert "SECRET" not in msgs[1].content

    def test_tool_result_not_in_system(self, builder):
        obs = [_read_obs(content="SECRET")]
        msgs = builder.build_messages("x", observations=obs)
        assert "SECRET" not in msgs[0].content


# ============================================================
# assistant content
# ============================================================

class TestAssistantContent:
    def test_valid_json(self, builder):
        msgs = builder.build_messages("x", observations=[_read_obs()])
        parsed = json.loads(msgs[2].content)
        assert parsed["action_type"] == "call_tool"
        assert parsed["tool_name"] == "workspace.read_file"

    def test_no_workspace_root(self, builder):
        msgs = builder.build_messages("x", observations=[_read_obs()])
        parsed = json.loads(msgs[2].content)
        assert "workspace_root" not in parsed.get("arguments", {})

    def test_name_matches(self, builder):
        msgs = builder.build_messages("x", observations=[_read_obs()])
        assert msgs[2].name == "workspace.read_file"

    def test_reason_truncated(self, builder):
        obs = _read_obs()
        obs["model_action"]["reason"] = "x" * (MAX_REASON_LENGTH + 100)
        msgs = builder.build_messages("x", observations=[obs])
        parsed = json.loads(msgs[2].content)
        assert len(parsed["reason"]) <= MAX_REASON_LENGTH


# ============================================================
# tool content
# ============================================================

class TestToolContent:
    def test_valid_json(self, builder):
        msgs = builder.build_messages("x", observations=[_read_obs()])
        parsed = json.loads(msgs[3].content)
        assert parsed["tool_name"] == "workspace.read_file"
        assert parsed["ok"] is True

    def test_read_file_content_in_tool(self, builder):
        obs = [_read_obs(content="# AGENTS\nRules")]
        msgs = builder.build_messages("x", observations=obs)
        assert "# AGENTS" in msgs[3].content

    def test_read_file_truncated(self, builder):
        obs = _read_obs(content="x" * (MAX_READ_FILE_CHARS + 100))
        msgs = builder.build_messages("x", observations=[obs])
        parsed = json.loads(msgs[3].content)
        assert parsed["data"]["truncated"]
        assert len(parsed["data"]["content"]) <= MAX_READ_FILE_CHARS

    def test_list_files_entries_truncated(self, builder):
        many = [{"name": f"f_{i}", "type": "file"} for i in range(MAX_LIST_FILES_ENTRIES + 5)]
        msgs = builder.build_messages("x", observations=[_list_obs(entries=many)])
        parsed = json.loads(msgs[3].content)
        assert parsed["data"]["truncated"]
        assert len(parsed["data"]["entries"]) == MAX_LIST_FILES_ENTRIES


# ============================================================
# 字段边界
# ============================================================

class TestFieldBounds:
    def test_summary_truncated(self, builder):
        obs = _read_obs()
        obs["summary"] = "x" * (MAX_SUMMARY_LENGTH + 100)
        parsed = json.loads(builder.build_messages("x", observations=[obs])[3].content)
        assert len(parsed["summary"]) <= MAX_SUMMARY_LENGTH

    def test_path_truncated(self, builder):
        obs = _read_obs(path="d/" * (MAX_PATH_LENGTH + 10))
        parsed = json.loads(builder.build_messages("x", observations=[obs])[3].content)
        assert len(parsed["data"]["path"]) <= MAX_PATH_LENGTH

    def test_entry_name_truncated(self, builder):
        obs = _list_obs(entries=[{"name": "f" * (MAX_ENTRY_NAME_LENGTH + 50), "type": "file"}])
        parsed = json.loads(builder.build_messages("x", observations=[obs])[3].content)
        assert len(parsed["data"]["entries"][0]["name"]) <= MAX_ENTRY_NAME_LENGTH

    def test_error_message_truncated(self, builder):
        obs = {
            "tool_call_id": "tc-1",
            "tool_name": "workspace.read_file",
            "model_action": {"action_type": "call_tool", "tool_name": "workspace.read_file", "arguments": {"path": "x"}},
            "ok": False,
            "summary": "fail",
            "error": {"code": "ERR", "message": "e" * (MAX_ERROR_MESSAGE_LENGTH + 50)},
        }
        parsed = json.loads(builder.build_messages("x", observations=[obs])[3].content)
        assert len(parsed["error"]["message"]) <= MAX_ERROR_MESSAGE_LENGTH

    def test_observations_bound(self, builder):
        obs = [_read_obs(f"tc-{i:03d}") for i in range(MAX_OBSERVATIONS + 5)]
        msgs = builder.build_messages("x", observations=obs)
        non_sys_user = msgs[2:]
        assert len(non_sys_user) <= MAX_OBSERVATIONS * 2


# ============================================================
# 原子消息对：缺少字段 → PromptBuildError
# ============================================================

class TestAtomicPairValidation:
    """observation 无效时抛出 PromptBuildError，不产生孤立消息。"""

    def test_obs_not_dict(self, builder):
        with pytest.raises(PromptBuildError, match="dict"):
            builder.build_messages("x", observations=["not_dict"])

    def test_missing_model_action(self, builder):
        obs = {"tool_call_id": "tc-1", "tool_name": "w.rf", "ok": True}
        with pytest.raises(PromptBuildError, match="model_action"):
            builder.build_messages("x", observations=[obs])

    def test_model_action_not_dict(self, builder):
        obs = {"tool_call_id": "tc-1", "tool_name": "w.rf", "ok": True, "model_action": "bad"}
        with pytest.raises(PromptBuildError, match="model_action"):
            builder.build_messages("x", observations=[obs])

    def test_missing_tool_call_id(self, builder):
        obs = {
            "tool_name": "w.rf",
            "model_action": {"action_type": "call_tool", "tool_name": "w.rf", "arguments": {}},
            "ok": True,
        }
        with pytest.raises(PromptBuildError, match="tool_call_id"):
            builder.build_messages("x", observations=[obs])

    def test_tool_call_id_empty(self, builder):
        obs = _read_obs()
        obs["tool_call_id"] = ""
        with pytest.raises(PromptBuildError, match="tool_call_id"):
            builder.build_messages("x", observations=[obs])

    def test_missing_tool_name(self, builder):
        obs = {
            "tool_call_id": "tc-1",
            "model_action": {"action_type": "call_tool", "tool_name": "w.rf", "arguments": {}},
            "ok": True,
        }
        with pytest.raises(PromptBuildError, match="tool_name"):
            builder.build_messages("x", observations=[obs])

    def test_tool_name_empty(self, builder):
        obs = _read_obs()
        obs["tool_name"] = ""
        with pytest.raises(PromptBuildError, match="tool_name"):
            builder.build_messages("x", observations=[obs])

    def test_model_action_type_not_call_tool(self, builder):
        obs = _read_obs()
        obs["model_action"]["action_type"] = "finish"
        with pytest.raises(PromptBuildError, match="call_tool"):
            builder.build_messages("x", observations=[obs])

    def test_model_action_tool_name_mismatch(self, builder):
        obs = _read_obs()
        obs["model_action"]["tool_name"] = "workspace.list_files"
        with pytest.raises(PromptBuildError, match="不一致"):
            builder.build_messages("x", observations=[obs])

    def test_model_action_arguments_not_dict(self, builder):
        obs = _read_obs()
        obs["model_action"]["arguments"] = "not_dict"
        with pytest.raises(PromptBuildError, match="arguments"):
            builder.build_messages("x", observations=[obs])

    def test_ok_not_bool(self, builder):
        obs = _read_obs()
        obs["ok"] = "yes"  # not bool
        with pytest.raises(PromptBuildError, match="ok"):
            builder.build_messages("x", observations=[obs])

    def test_no_orphan_tool_on_error(self, builder):
        """任一验证失败都不会产生部分消息。"""
        for bad_obs in [
            {"tool_call_id": "tc", "tool_name": "w", "ok": True},  # 缺 model_action
        ]:
            with pytest.raises(PromptBuildError):
                builder.build_messages("x", observations=[bad_obs])
            # build_messages 抛出异常 → 不会返回部分结果


# ============================================================
# 递归 JSON-safe sanitizer
# ============================================================

class TestRecursiveSanitizer:
    def test_top_level_string_truncated(self, builder):
        obs = _read_obs()
        obs["model_action"]["arguments"]["path"] = "p" * (MAX_ARGUMENT_STRING_LENGTH + 100)
        msgs = builder.build_messages("x", observations=[obs])
        parsed = json.loads(msgs[2].content)
        assert len(parsed["arguments"]["path"]) <= MAX_ARGUMENT_STRING_LENGTH

    def test_nested_dict_string_truncated(self, builder):
        obs = _read_obs()
        obs["model_action"]["arguments"]["nested"] = {"deep": "d" * (MAX_ARGUMENT_STRING_LENGTH + 50)}
        msgs = builder.build_messages("x", observations=[obs])
        parsed = json.loads(msgs[2].content)
        assert len(parsed["arguments"]["nested"]["deep"]) <= MAX_ARGUMENT_STRING_LENGTH

    def test_nested_list_string_truncated(self, builder):
        obs = _read_obs()
        obs["model_action"]["arguments"]["items"] = ["i" * (MAX_ARGUMENT_STRING_LENGTH + 50)]
        msgs = builder.build_messages("x", observations=[obs])
        parsed = json.loads(msgs[2].content)
        assert len(parsed["arguments"]["items"][0]) <= MAX_ARGUMENT_STRING_LENGTH

    def test_max_depth_exceeded(self, builder):
        obs = _read_obs()
        nested = {}
        curr = nested
        for _ in range(MAX_ARGUMENT_DEPTH + 2):
            curr["next"] = {}
            curr = curr["next"]
        obs["model_action"]["arguments"]["deep"] = nested
        with pytest.raises(PromptBuildError, match="深度"):
            builder.build_messages("x", observations=[obs])

    def test_dict_items_truncated(self, builder):
        obs = _read_obs()
        big = {f"k{i}": i for i in range(MAX_ARGUMENT_ITEMS + 10)}
        obs["model_action"]["arguments"]["big"] = big
        msgs = builder.build_messages("x", observations=[obs])
        parsed = json.loads(msgs[2].content)
        assert len(parsed["arguments"]["big"]) <= MAX_ARGUMENT_ITEMS

    def test_list_items_truncated(self, builder):
        obs = _read_obs()
        obs["model_action"]["arguments"]["items"] = list(range(MAX_ARGUMENT_ITEMS + 10))
        msgs = builder.build_messages("x", observations=[obs])
        parsed = json.loads(msgs[2].content)
        assert len(parsed["arguments"]["items"]) <= MAX_ARGUMENT_ITEMS

    def test_long_dict_key_truncated(self, builder):
        obs = _read_obs()
        long_key = "k" * (MAX_ARGUMENT_KEY_LENGTH + 50)
        obs["model_action"]["arguments"][long_key] = "v"
        msgs = builder.build_messages("x", observations=[obs])
        parsed = json.loads(msgs[2].content)
        keys = list(parsed["arguments"].keys())
        assert all(len(k) <= MAX_ARGUMENT_KEY_LENGTH for k in keys)

    def test_non_string_dict_key_rejected(self, builder):
        obs = _read_obs()
        obs["model_action"]["arguments"][42] = "v"  # int key
        with pytest.raises(PromptBuildError, match="key"):
            builder.build_messages("x", observations=[obs])

    def test_set_rejected(self, builder):
        obs = _read_obs()
        obs["model_action"]["arguments"]["bad"] = {1, 2}
        with pytest.raises(PromptBuildError, match="set"):
            builder.build_messages("x", observations=[obs])

    def test_bytes_rejected(self, builder):
        obs = _read_obs()
        obs["model_action"]["arguments"]["bad"] = b"bytes"
        with pytest.raises(PromptBuildError, match="bytes"):
            builder.build_messages("x", observations=[obs])

    def test_custom_object_rejected(self, builder):
        obs = _read_obs()
        obs["model_action"]["arguments"]["bad"] = object()
        with pytest.raises(PromptBuildError):
            builder.build_messages("x", observations=[obs])

    def test_nan_rejected(self, builder):
        obs = _read_obs()
        obs["model_action"]["arguments"]["bad"] = float("nan")
        with pytest.raises(PromptBuildError, match="非有限"):
            builder.build_messages("x", observations=[obs])

    def test_inf_rejected(self, builder):
        obs = _read_obs()
        obs["model_action"]["arguments"]["bad"] = float("inf")
        with pytest.raises(PromptBuildError, match="非有限"):
            builder.build_messages("x", observations=[obs])

    def test_long_nested_string_not_included_fully(self, builder):
        """嵌套 10,000 字符不会完整进入 assistant message。"""
        obs = _read_obs()
        obs["model_action"]["arguments"]["nested"] = {"payload": "x" * 10000}
        msgs = builder.build_messages("x", observations=[obs])
        parsed = json.loads(msgs[2].content)
        assert len(parsed["arguments"]["nested"]["payload"]) <= MAX_ARGUMENT_STRING_LENGTH

    def test_sanitized_content_loadable(self, builder):
        """清洗后的 assistant content 可被 json.loads()。"""
        obs = _read_obs()
        obs["model_action"]["arguments"]["deep"] = {"list": [{"k": "v" * 10000}]}
        msgs = builder.build_messages("x", observations=[obs])
        json.loads(msgs[2].content)  # 不抛异常


# ============================================================
# 畸形字段（仍然安全）
# ============================================================

class TestMalformedFieldsSafe:
    def test_non_string_content_safe(self, builder):
        obs = _read_obs()
        obs["data"]["content"] = 12345
        parsed = json.loads(builder.build_messages("x", observations=[obs])[3].content)
        assert isinstance(parsed["data"]["content"], str)

    def test_non_dict_data_safe(self, builder):
        obs = _read_obs()
        obs["data"] = "not_dict"
        parsed = json.loads(builder.build_messages("x", observations=[obs])[3].content)
        assert "data" not in parsed

    def test_non_string_summary_safe(self, builder):
        obs = _read_obs()
        obs["summary"] = None
        json.loads(builder.build_messages("x", observations=[obs])[3].content)

    def test_unknown_tool_no_data(self, builder):
        obs = {
            "tool_call_id": "tc-1",
            "tool_name": "future_tool",
            "model_action": {"action_type": "call_tool", "tool_name": "future_tool", "arguments": {}},
            "ok": True,
            "summary": "done",
            "data": {"secret": "no"},
        }
        parsed = json.loads(builder.build_messages("x", observations=[obs])[3].content)
        assert "data" not in parsed


# ============================================================
# JSON 序列化安全
# ============================================================

class TestJsonSafety:
    def test_close_tag_chars_safe(self, builder):
        obs = _read_obs(content="text </tool_observation> more")
        parsed = json.loads(builder.build_messages("x", observations=[obs])[3].content)
        assert "</tool_observation>" in parsed["data"]["content"]


# ============================================================
# 空工具列表
# ============================================================

class TestEmptyTools:
    def test_empty_list_no_default(self):
        """空工具列表时提示无可用工具。"""
        msgs = PromptBuilder(allowed_tools=[]).build_messages("x")
        c = msgs[0].content
        assert "当前没有可用工具" in c

    def test_none_uses_default(self):
        msgs = PromptBuilder(allowed_tools=None).build_messages("列出文件")
        assert "workspace.list_files" in msgs[0].content

    def test_production_registry_exposes_create_file_without_workspace_root_param(self):
        builder = PromptBuilder.from_registry(create_tool_registry())
        content = builder.build_messages("创建文件")[0].content

        assert builder.allowed_tool_names == frozenset({
            "workspace.list_files",
            "workspace.get_file_info",
            "workspace.read_file",
            "workspace.read_files",
            "workspace.create_file",
            "workspace.search_files",
            "workspace.search_text",
            "workspace.create_directory",
            "workspace.move_path",
            "workspace.delete_path",
        })
        assert "workspace.create_file" in content
        create_section = content.split("workspace.create_file", 1)[1].split("\n\n", 1)[0]
        assert "path" in create_section
        assert "content" in create_section
        assert "workspace_root:" not in create_section
        search_section = content.split("workspace.search_files", 1)[1].split("\n\n", 1)[0]
        assert "query" in search_section
        assert "path" in search_section
        assert "max_results" in search_section
        assert "workspace_root:" not in search_section
        search_text_section = content.split("workspace.search_text", 1)[1].split("\n\n", 1)[0]
        assert "query" in search_text_section
        assert "source_only" in search_text_section
        assert "workspace_root:" not in search_text_section
        info_section = content.split("workspace.get_file_info", 1)[1].split("\n\n", 1)[0]
        assert "path" in info_section
        assert "workspace_root:" not in info_section
        directory_section = content.split("workspace.create_directory", 1)[1].split("\n\n", 1)[0]
        assert "path" in directory_section
        assert "workspace_root:" not in directory_section
        move_section = content.split("workspace.move_path", 1)[1].split("\n\n", 1)[0]
        assert "source_path" in move_section
        assert "destination_path" in move_section
        assert "workspace_root:" not in move_section
        delete_section = content.split("workspace.delete_path", 1)[1].split("\n\n", 1)[0]
        assert "path" in delete_section
        assert "workspace_root:" not in delete_section

    def test_knowledge_provenance_is_runtime_managed_and_hidden_from_model(self):
        registry = create_tool_registry(knowledge_executor=lambda _request: None)
        builder = PromptBuilder.from_registry(registry)
        content = builder.build_messages("保存知识笔记")[0].content

        knowledge_section = content.split("knowledge.create_document", 1)[1].split(
            "\n\n", 1
        )[0]
        assert "title" in knowledge_section
        assert "content" in knowledge_section
        assert "provenance_links" not in knowledge_section


# ============================================================
# Phase 6C: 动态 call_tool 示例
# ============================================================

def _parse_example_json(content: str) -> dict | None:
    """从 system prompt 中提取 call_tool 示例 JSON 并解析。"""
    marker = "call_tool 示例：\n"
    idx = content.find(marker)
    if idx < 0:
        return None
    candidate = content[idx + len(marker):].split("\n\n")[0].strip()
    return json.loads(candidate)


class TestDynamicCallToolExample:
    def test_default_builder_example_is_list_files(self):
        """默认 PromptBuilder() 示例为 workspace.list_files。"""
        c = PromptBuilder().build_messages("x")[0].content
        parsed = _parse_example_json(c)
        assert parsed is not None, "应能提取示例 JSON"
        assert parsed["tool_name"] == "workspace.list_files"
        assert parsed["arguments"] == {}

    def test_full_registry_always_includes_exact_rag_await_example(self):
        registry = create_tool_registry(
            rag_search_executor=lambda _request: None,
            rag_ingestion_executor=lambda _request: None,
            rag_await_ingestion_executor=lambda _request: None,
        )
        content = PromptBuilder.from_registry(registry).build_messages(
            "把论文处理到可检索后告诉我"
        )[0].content
        marker = (
            "关键链路 call_tool 精确示例（tool_name 必须逐字匹配，"
            "每轮仍只能输出一个 JSON object）：\n"
        )

        exact_example = json.loads(content.split(marker, 1)[1].splitlines()[0])

        assert exact_example["action_type"] == "call_tool"
        assert exact_example["tool_name"] == "rag.await_ingestion"
        assert exact_example["arguments"] == {
            "job_id": "00000000-0000-4000-8000-000000000000",
        }
        assert exact_example["reason"] == "用户要求在论文真正可检索后再告知"
        assert "必须使用其 ToolResult.data.job_id" in content
        assert "不得自造同义工具名" in content
        assert "不得输出供应商原生 tool_calls" in content
        assert "DSML 标记" in content

    def test_list_files_only_example_parsable(self):
        b = PromptBuilder(allowed_tools=[{
            "name": "workspace.list_files", "description": "list", "parameters": {"path": "subdir"},
        }])
        c = b.build_messages("x")[0].content
        assert "workspace.list_files" in c
        assert "workspace.read_file" not in c
        parsed = _parse_example_json(c)
        assert parsed["tool_name"] == "workspace.list_files"
        assert parsed["arguments"] == {}

    def test_read_file_only_example_parsable(self):
        b = PromptBuilder(allowed_tools=[{
            "name": "workspace.read_file", "description": "read", "parameters": {"path": "relative path"},
        }])
        c = b.build_messages("x")[0].content
        assert "workspace.read_file" in c
        assert "workspace.list_files" not in c
        parsed = _parse_example_json(c)
        assert parsed["tool_name"] == "workspace.read_file"
        assert parsed["arguments"] == {"path": "README.md"}
        assert not parsed["arguments"]["path"].startswith("/")
        assert "workspace_root" not in parsed["arguments"]

    def test_search_files_only_example_and_behavior_guide(self):
        b = PromptBuilder(allowed_tools=[{
            "name": "workspace.search_files",
            "description": "search names",
            "parameters": {"query": "substring", "path": "relative path"},
        }])
        content = b.build_messages("查找 Markdown 文件")[0].content
        parsed = _parse_example_json(content)

        assert parsed["tool_name"] == "workspace.search_files"
        assert parsed["arguments"]["query"] == ".md"
        assert "workspace_root" not in parsed["arguments"]
        assert "不搜索文件正文" in content
        assert "不是 regex/glob" in content
        assert "找到候选文件后" in content
        assert "workspace.read_file" in content
        assert "不要继续用同类宽泛关键词重复搜索" in content

    def test_get_file_info_only_example_and_behavior_guide(self):
        b = PromptBuilder(allowed_tools=[{
            "name": "workspace.get_file_info",
            "description": "metadata only",
            "parameters": {"path": "relative path"},
        }])
        content = b.build_messages("查看 README.md 大小和修改时间")[0].content
        parsed = _parse_example_json(content)

        assert parsed["tool_name"] == "workspace.get_file_info"
        assert parsed["arguments"] == {"path": "README.md"}
        assert "workspace_root" not in parsed["arguments"]
        assert "不要读取文件正文" in content
        assert "当前 Task" in content
        assert "不得用历史" in content

    @pytest.mark.parametrize(
        ("tool_name", "parameters", "expected_arguments"),
        [
            ("workspace.create_directory", {"path": "relative path"}, {"path": "notes"}),
            ("workspace.move_path", {"source_path": "source", "destination_path": "target"}, {"source_path": "draft.txt", "destination_path": "archive/draft.txt"}),
            ("workspace.delete_path", {"path": "relative path"}, {"path": "obsolete.txt"}),
        ],
    )
    def test_workspace_mutation_only_examples_are_safe(self, tool_name, parameters, expected_arguments):
        content = PromptBuilder(allowed_tools=[{
            "name": tool_name,
            "description": "mutation",
            "parameters": parameters,
        }]).build_messages("执行文件操作")[0].content
        parsed = _parse_example_json(content)

        assert parsed["tool_name"] == tool_name
        assert parsed["arguments"] == expected_arguments
        assert "workspace_root" not in parsed["arguments"]

    def test_filesystem_history_is_not_current_truth(self):
        content = PromptBuilder().build_messages(
            "继续查看 docs 目录信息",
            history_messages=[
                {"role": "user", "content": "查看 README.md 信息"},
                {"role": "assistant", "content": "README.md 是普通文件"},
            ],
        )[0].content

        assert "只代表过去状态" in content
        assert "必须在当前 Task 重新调用对应工具" in content

    def test_custom_tool_no_example_but_has_universal_guide(self):
        """未知工具不生成具体示例，但保留通用后处理指南。"""
        b = PromptBuilder(allowed_tools=[{
            "name": "custom.tool", "description": "自定义工具", "parameters": {},
        }])
        c = b.build_messages("x")[0].content
        assert "custom.tool" in c
        parsed = _parse_example_json(c)
        assert parsed is None, "未知工具不应生成 call_tool 示例"
        # 通用后处理指南：不依赖工具特定关键词
        assert "工具执行后，系统会将结果返回给你" in c
        assert "根据工具结果继续决策或 finish" in c
        assert "只有任务不需要工具或已经完成时才能 finish" in c

    def test_empty_tools_no_example_and_no_tool_names(self):
        b = PromptBuilder(allowed_tools=[])
        c = b.build_messages("x")[0].content
        assert "workspace.list_files" not in c
        assert "workspace.read_file" not in c
        assert "不要尝试调用任何工具" in c
        assert _parse_example_json(c) is None


class TestSearchFilesObservation:
    def test_bounded_matches_are_visible_to_next_model_call(self):
        matches = [
            {"name": f"file-{index}.md", "path": f"docs/file-{index}.md", "type": "file"}
            for index in range(25)
        ]
        messages = PromptBuilder().build_messages(
            "查找文件",
            observations=[_search_obs(data={
                "returned_matches": 25,
                "truncated": False,
                "truncation_reasons": [],
                "matches": matches,
                "workspace_root": "/sensitive/root",
            })],
        )
        tool_data = json.loads(messages[-1].content)["data"]

        assert len(tool_data["matches"]) == 20
        assert tool_data["returned_matches"] == 25
        assert tool_data["truncated"] is True
        assert "workspace_root" not in json.dumps(tool_data)

    @pytest.mark.parametrize(
        "data",
        [
            {"matches": "bad", "returned_matches": "bad", "truncation_reasons": None},
            {"matches": [], "returned_matches": True, "truncation_reasons": "max_depth"},
            {"matches": [], "returned_matches": -1, "truncation_reasons": [None, "unknown"]},
        ],
    )
    def test_malformed_metadata_safely_degrades(self, data):
        messages = PromptBuilder().build_messages(
            "查找文件",
            observations=[_search_obs(data=data)],
        )
        tool_data = json.loads(messages[-1].content)["data"]

        assert tool_data["matches"] == []
        assert 0 <= tool_data["returned_matches"] <= 100
        assert isinstance(tool_data["truncation_reasons"], list)


class TestSearchTextObservation:
    def test_bounded_source_matches_are_visible_without_private_fields(self):
        matches = [{
            "path": f"src/file-{index}.py",
            "line_number": index + 1,
            "preview": "P" * 700,
            "private_root": "/sensitive/root",
        } for index in range(25)]
        messages = PromptBuilder().build_messages(
            "定位实现",
            observations=[_search_text_obs(data={
                "search_path": "src",
                "query": "CreateTask",
                "source_only": True,
                "matches": matches,
                "candidate_matches": 25,
                "matching_lines": 42,
                "matched_files": 9,
                "searched_files": 705,
                "scanned_bytes": 5_615_731,
                "scan_complete": True,
                "result_window_truncated": True,
                "truncated": False,
                "truncation_reasons": ["max_results", "unknown"],
                "workspace_root": "/sensitive/root",
            })],
        )
        tool_data = json.loads(messages[-1].content)["data"]

        assert len(tool_data["matches"]) == 20
        assert len(tool_data["matches"][0]["preview"]) == 600
        assert tool_data["truncated"] is True
        assert tool_data["truncation_reasons"] == ["max_results"]
        assert tool_data["search_path"] == "src"
        assert tool_data["source_only"] is True
        assert tool_data["candidate_matches"] == 25
        assert tool_data["matching_lines"] == 42
        assert tool_data["matched_files"] == 9
        assert tool_data["searched_files"] == 705
        assert tool_data["scan_complete"] is True
        assert tool_data["result_window_truncated"] is True
        assert "workspace_root" not in json.dumps(tool_data)

    def test_production_prompt_guides_code_research_to_text_search_then_read(self):
        content = PromptBuilder.from_registry(create_tool_registry()).build_messages(
            "解释代码调用链"
        )[0].content

        assert "正文中定位概念、符号、配置或实现" in content
        assert "代码取证设置 source_only=true" in content
        assert "不要把正文关键词传给" in content
        assert "读取少量权威文件" in content
        assert "不存在分页游标" in content
        assert "Workspace 正文取证算法" in content
        assert "同一文件的多个命中合并为一个行范围" in content
        assert "workspace.read_files 一次读取" in content
        assert "path 必须原样复制 ToolResult" in content
        assert "ToolResult 含 suggested_paths" in content
        assert "不得把候选当作已读取证据" in content
        assert "先搜索并读取用户指定的起点和终点锚点" in content
        assert "每条边必须有调用点、发布/消费或 dispatch 直接证据" in content
        assert "外层循环/调度器实际调用终点" in content
        assert "精确标识符搜索只用于找到第一份锚点" in content
        assert "至少取得两个不同文件的成功正文 ToolResult" in content
        assert "不要预设业务文件名、文档类型或唯一答案路径" in content

    def test_batch_read_observation_is_bounded_and_keeps_item_errors(self):
        messages = PromptBuilder.from_registry(create_tool_registry()).build_messages(
            "审查多个文件",
            observations=[_read_files_obs()],
        )
        tool_data = json.loads(messages[-1].content)["data"]

        assert tool_data["requested_files"] == 2
        assert tool_data["succeeded_files"] == 1
        assert len(tool_data["files"]) == 2
        assert len(tool_data["files"][0]["content"]) == MAX_READ_FILE_CHARS
        assert tool_data["files"][0]["truncated"] is True
        assert tool_data["files"][0]["start_line"] == 10
        assert tool_data["files"][1]["error"]["code"] == "FILE_NOT_FOUND"
        assert tool_data["files"][1]["suggested_paths"] == [
            "src/runtime/b.py",
            "src/domain/b.py",
        ]

    def test_failed_path_suggestions_are_bounded_before_model_projection(self, builder):
        observation = {
            "tool_call_id": "tc-miss",
            "tool_name": "workspace.read_file",
            "model_action": {
                "action_type": "call_tool",
                "tool_name": "workspace.read_file",
                "arguments": {"path": "src/application/task_service.py"},
                "reason": "读取实现",
            },
            "ok": False,
            "summary": "文件不存在",
            "data": {
                "requested_path": "src/application/task_service.py",
                "suggested_paths": [
                    "src/runtime/tasks/service.py",
                    *[f"private/{index}.py" for index in range(10)],
                ],
                "workspace_root": "/private/workspace",
                "content": "must-not-leak",
            },
            "error": {"code": "FILE_NOT_FOUND", "message": "missing"},
        }

        messages = builder.build_messages("定位实现", observations=[observation])
        tool_data = json.loads(messages[-1].content)["data"]

        assert tool_data["requested_path"] == "src/application/task_service.py"
        assert len(tool_data["suggested_paths"]) == 5
        assert tool_data["suggested_paths"][0] == "src/runtime/tasks/service.py"
        assert "workspace_root" not in tool_data
        assert "content" not in tool_data

    def test_finish_only_prompt_hides_tools_and_examples(self):
        messages = PromptBuilder.from_registry(create_tool_registry()).build_messages(
            "根据已有证据收口",
            observations=[_read_obs("tc-finish", "证据", "evidence.md")],
            finish_only=True,
        )
        content = messages[0].content

        assert "终态收口模式" in content
        assert "不能替代 caller/dispatcher/executor 证据" in content
        assert "外层循环或实际调用点未被读取" in content
        assert "唯一合法 action 是 finish" in content
        assert "当前允许的工具列表：" not in content
        assert "workspace.list_files" not in content
        assert "workspace.search_text" not in content
        assert "call_tool 示例" not in content
        assert "call_tool" not in content
        assert "citations 是 RAG 专用字段" in content
        assert "Workspace 文件路径、行号或搜索结果必须写在 final_message" in content
        assert "没有 rag.search chunk_id 时必须使用空数组" in content
        assert [message.role for message in messages[-2:]] == ["user", "user"]
        assert "Runtime ToolResult" in messages[-1].content
        assert "action_type" not in messages[-2].content


class TestGetFileInfoObservation:
    def test_only_public_bounded_metadata_is_injected(self):
        messages = PromptBuilder().build_messages(
            "查看文件信息",
            observations=[_info_obs(data={
                "name": "R" * 300,
                "path": "docs/README.md",
                "type": "file",
                "size_bytes": 42,
                "modified_at": "2026-07-17T00:00:00+00:00",
                "workspace_root": "/sensitive/root",
                "mode": "0777",
                "owner": "private-user",
            })],
        )
        tool_data = json.loads(messages[-1].content)["data"]

        assert len(tool_data["name"]) == MAX_ENTRY_NAME_LENGTH
        assert tool_data["path"] == "docs/README.md"
        assert tool_data["type"] == "file"
        assert tool_data["size_bytes"] == 42
        assert "workspace_root" not in tool_data
        assert "mode" not in tool_data
        assert "owner" not in tool_data

    @pytest.mark.parametrize("size_bytes", [True, -1, "42", None])
    def test_malformed_size_is_omitted(self, size_bytes):
        messages = PromptBuilder().build_messages(
            "查看文件信息",
            observations=[_info_obs(data={
                "name": "README.md",
                "path": "README.md",
                "type": "unexpected",
                "size_bytes": size_bytes,
                "modified_at": 123,
            })],
        )
        tool_data = json.loads(messages[-1].content)["data"]

        assert tool_data["type"] == "other"
        assert "size_bytes" not in tool_data
        assert "modified_at" not in tool_data


class TestArxivObservation:
    def test_bounded_public_metadata_is_visible_to_next_model_call(self):
        messages = PromptBuilder().build_messages(
            "生成来源报告",
            observations=[_arxiv_obs(data={
                "source": "arxiv", "query": "AI agent memory", "known_source_count": 0,
                "results": [{
                    "arxiv_id": "2605.26252v1", "title": "Agent Memory",
                    "authors": ["Alice"], "abstract": "A" * 3000,
                    "published": "2026-05-25T00:00:00Z", "updated": "2026-05-25T00:00:00Z",
                    "primary_category": "cs.AI",
                    "abstract_url": "https://arxiv.org/abs/2605.26252v1",
                    "pdf_url": "https://arxiv.org/pdf/2605.26252v1",
                    "content_sha256": "a" * 64,
                    "download": {
                        "available": True,
                        "reference": "2605.26252v1",
                        "mime_type": "application/pdf",
                        "url": "https://arxiv.org/pdf/2605.26252v1",
                    },
                    "private_path": "/secret",
                }],
                "attribution": "arXiv attribution", "private": "secret",
            })],
        )
        tool_data = json.loads(messages[-1].content)["data"]
        assert tool_data["results"][0]["arxiv_id"] == "2605.26252v1"
        assert tool_data["results"][0]["abstract_url"].endswith("2605.26252v1")
        assert "abstract" not in tool_data["results"][0]
        assert tool_data["results"][0]["source_id"] == "arxiv:2605.26252v1"
        assert tool_data["results"][0]["source_type"] == "literature"
        assert tool_data["results"][0]["canonical_url"].endswith("2605.26252v1")
        assert tool_data["results"][0]["content_scope"] == "abstract"
        assert tool_data["results"][0]["content_text"] == "A" * 1600
        assert tool_data["results"][0]["content_locators"] == ["abstract"]
        assert tool_data["results"][0]["content_sha256"] == "a" * 64
        assert tool_data["results"][0]["download"] == {
            "available": True,
            "reference": "2605.26252v1",
            "mime_type": "application/pdf",
            "url": "https://arxiv.org/pdf/2605.26252v1",
        }
        assert "private_path" not in tool_data["results"][0]
        assert "private" not in tool_data


class TestRagObservation:
    def test_bounded_retrieval_evidence_is_visible_to_next_model_call(self):
        messages = PromptBuilder().build_messages(
            "回答专业问题",
            observations=[_rag_obs()],
        )
        tool_data = json.loads(messages[-1].content)["data"]

        assert tool_data["query"] == "vector database"
        assert tool_data["results"][0]["document_title"] == "RAG Paper"
        assert tool_data["results"][0]["chunks"][0]["content"] == "retrieved evidence"
        assert tool_data["results"][0]["chunks"][0]["source_locator"] == {
            "page_start": 3
        }


class TestRuntimeFeedback:
    def test_trusted_runtime_feedback_is_injected_into_system_message(self):
        messages = PromptBuilder().build_messages(
            "创建文件",
            runtime_feedback=[
                "上一次 finish 被拒绝：必须先调用 workspace.create_file。",
            ],
        )

        assert "Runtime 校验反馈（可信系统状态）" in messages[0].content
        assert "必须先调用 workspace.create_file" in messages[0].content

    def test_runtime_feedback_is_bounded_and_ignores_non_strings(self):
        messages = PromptBuilder().build_messages(
            "x",
            runtime_feedback=["A" * 2000, 123, "B", "C", "D"],  # type: ignore[list-item]
        )
        system_content = messages[0].content

        feedback_section = system_content.split("Runtime 校验反馈（可信系统状态）：", 1)[1]
        assert "A" * 800 in feedback_section
        assert "A" * 801 not in feedback_section
        assert "- B" in feedback_section
        assert "- C" in feedback_section
        assert "- D" not in feedback_section


class TestRagCitationContract:
    def test_successful_rag_search_adds_dynamic_required_citation_contract(self):
        messages = PromptBuilder().build_messages(
            "根据论文回答并给出页码",
            observations=[_rag_obs()],
            finish_only=True,
        )

        system_content = messages[0].content
        assert "Runtime RAG 引用契约" in system_content
        assert "省略 citations 或返回 citations=[] 都是非法的" in system_content
        assert "11111111-1111-4111-8111-111111111111 (p.3)" in system_content
        assert (
            '"citations":[{"chunk_id":"11111111-1111-4111-8111-111111111111"}]'
            in system_content
        )
        tool_payload = json.loads(messages[-1].content.split("\n", 1)[1])
        assert tool_payload["data"]["evidence_assessment"]["sufficient"] is False
        assert tool_payload["data"]["document_coverage"]["complete"] is False

    def test_non_rag_observation_does_not_add_citation_contract(self):
        messages = PromptBuilder().build_messages(
            "列出文件",
            observations=[_list_obs()],
        )

        assert "Runtime RAG 引用契约" not in messages[0].content

    def test_rag_contract_survives_more_than_four_later_observations(self):
        messages = PromptBuilder().build_messages(
            "综合已有证据回答",
            observations=[_rag_obs(), *[_list_obs() for _ in range(5)]],
            finish_only=True,
        )

        assert "Runtime RAG 引用契约" in messages[0].content
        assert "11111111-1111-4111-8111-111111111111 (p.3)" in messages[0].content
