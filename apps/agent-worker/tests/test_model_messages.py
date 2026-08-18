"""测试 models/messages.py — ModelMessage 运行时校验。"""

from __future__ import annotations

from dataclasses import FrozenInstanceError

import pytest

from jarvis_worker.agent.models.messages import (
    ModelMessage,
    ModelMessageValidationError,
)


# ============================================================
# 合法角色
# ============================================================

class TestValidMessages:
    def test_system(self):
        msg = ModelMessage.system("rules")
        assert msg.role == "system"
        assert msg.content == "rules"
        assert msg.name is None
        assert msg.tool_call_id is None

    def test_user(self):
        msg = ModelMessage.user("goal")
        assert msg.role == "user"

    def test_assistant_tool_call(self):
        msg = ModelMessage.assistant("{}", name="w.rf", tool_call_id="tc-1")
        assert msg.role == "assistant"
        assert msg.name == "w.rf"
        assert msg.tool_call_id == "tc-1"

    def test_assistant_plain(self):
        """不带 name/tool_call_id 的 assistant 合法。"""
        msg = ModelMessage.assistant("{}")
        assert msg.name is None
        assert msg.tool_call_id is None

    def test_tool(self):
        msg = ModelMessage.tool("{}", name="w.rf", tool_call_id="tc-1")
        assert msg.role == "tool"
        assert msg.name == "w.rf"
        assert msg.tool_call_id == "tc-1"

    def test_assistant_tool_same_id(self):
        tc_id = "tool-call-42"
        a = ModelMessage.assistant("{}", tool_call_id=tc_id, name="w.rf")
        t = ModelMessage.tool("{}", name="w.rf", tool_call_id=tc_id)
        assert a.tool_call_id == t.tool_call_id == tc_id


# ============================================================
# 运行时校验 - 通用
# ============================================================

class TestValidationGeneral:
    def test_invalid_role(self):
        with pytest.raises(ModelMessageValidationError):
            ModelMessage(role="admin", content="x")  # type: ignore[arg-type]

    def test_content_not_str(self):
        with pytest.raises(ModelMessageValidationError):
            ModelMessage(role="system", content=123)  # type: ignore[arg-type]

    def test_name_empty_string_rejected(self):
        with pytest.raises(ModelMessageValidationError):
            ModelMessage(role="tool", content="{}", name="", tool_call_id="t1")


# ============================================================
# 角色不变量 - system / user
# ============================================================

class TestSystemUserInvariant:
    def test_system_with_name_rejected(self):
        with pytest.raises(ModelMessageValidationError):
            ModelMessage(role="system", content="x", name="bad")

    def test_system_with_tool_call_id_rejected(self):
        with pytest.raises(ModelMessageValidationError):
            ModelMessage(role="system", content="x", tool_call_id="bad")

    def test_user_with_name_rejected(self):
        with pytest.raises(ModelMessageValidationError):
            ModelMessage(role="user", content="x", name="bad")

    def test_user_with_tool_call_id_rejected(self):
        with pytest.raises(ModelMessageValidationError):
            ModelMessage(role="user", content="x", tool_call_id="bad")


# ============================================================
# 角色不变量 - assistant
# ============================================================

class TestAssistantInvariant:
    def test_assistant_name_only_rejected(self):
        with pytest.raises(ModelMessageValidationError):
            ModelMessage(role="assistant", content="{}", name="w.rf")

    def test_assistant_tool_call_id_only_rejected(self):
        with pytest.raises(ModelMessageValidationError):
            ModelMessage(role="assistant", content="{}", tool_call_id="tc")

    def test_assistant_both_ok(self):
        msg = ModelMessage(role="assistant", content="{}", name="w.rf", tool_call_id="tc")
        assert msg.name == "w.rf"
        assert msg.tool_call_id == "tc"


# ============================================================
# 角色不变量 - tool
# ============================================================

class TestToolInvariant:
    def test_tool_missing_name_rejected(self):
        with pytest.raises(ModelMessageValidationError):
            ModelMessage(role="tool", content="{}", tool_call_id="tc")

    def test_tool_empty_name_rejected(self):
        with pytest.raises(ModelMessageValidationError):
            ModelMessage.tool("{}", "", "tc")

    def test_tool_missing_tool_call_id_rejected(self):
        with pytest.raises(ModelMessageValidationError):
            ModelMessage(role="tool", content="{}", name="w.rf")

    def test_tool_empty_tool_call_id_rejected(self):
        with pytest.raises(ModelMessageValidationError):
            ModelMessage.tool("{}", "w.rf", "")


# ============================================================
# 不可变性 (精确断言)
# ============================================================

class TestFrozen:
    def test_cannot_mutate_role(self):
        msg = ModelMessage.system("test")
        with pytest.raises(FrozenInstanceError):
            msg.role = "user"  # type: ignore[misc]

    def test_cannot_mutate_content(self):
        msg = ModelMessage.user("test")
        with pytest.raises(FrozenInstanceError):
            msg.content = "new"  # type: ignore[misc]


# ============================================================
# 无供应商依赖
# ============================================================

class TestNoVendorDeps:
    def test_message_contract_has_no_vendor_or_framework_import(self):
        import ast
        import inspect

        import jarvis_worker.agent.models.messages as messages_module

        tree = ast.parse(inspect.getsource(messages_module))
        imported_roots: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported_roots.update(alias.name.split(".", 1)[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported_roots.add(node.module.split(".", 1)[0])

        assert imported_roots.isdisjoint(
            {"openai", "langchain", "langchain_core", "langchain_openai", "langchain_deepseek"}
        )

    def test_serializable_to_dict(self):
        from dataclasses import asdict
        msg = ModelMessage.tool('{"ok": true}', name="w.rf", tool_call_id="t1")
        d = asdict(msg)
        assert d["role"] == "tool"
        assert d["name"] == "w.rf"
        assert d["tool_call_id"] == "t1"
