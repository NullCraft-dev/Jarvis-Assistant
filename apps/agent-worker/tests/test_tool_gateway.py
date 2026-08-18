"""ToolGateway MVP 第一刀 — 单元测试（修复版：P0 安全边界修正）。

覆盖：
- ToolRegistry 注册与查找
- PermissionManager L0 auto allow / 非 L0 deny
- workspace.list_files: workspace_root + path 严格分离
- workspace.list_files: ../ 拒绝、/etc 拒绝、symlink 逃逸拒绝
- workspace.list_files: 排除 node_modules/.git 等目录
- ToolGateway execute 成功/失败路径
- mock runner tool scenario 发出 started/finished
- tool 失败时发 failed
- mock runner workspace_path 作为 workspace_root 传递
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path
from unittest.mock import MagicMock

import pytest

import jarvis_worker.agent.tools.workspace.path_policy as workspace_path_policy
from jarvis_worker.agent.permissions.manager import PermissionManager
from jarvis_worker.agent.tool_gateway.contracts import (
    PermissionApproval,
    ToolManifest,
    ToolRequest,
    ToolResult,
)
from jarvis_worker.agent.tool_gateway.gateway import ToolGateway
from jarvis_worker.agent.tool_gateway.registry import ToolRegistry
from jarvis_worker.agent.tools.workspace import (
    create_file as workspace_create_file_tools,
)
from jarvis_worker.agent.tools.workspace import (
    execute_workspace_create_file,
    execute_workspace_get_file_info,
    execute_workspace_list_files,
    execute_workspace_read_file,
    execute_workspace_read_files,
    execute_workspace_search_files,
    execute_workspace_search_text,
)
from jarvis_worker.agent.tools.workspace import (
    search_files as workspace_search_file_tools,
)
from jarvis_worker.agent.tools.workspace import (
    search_text as workspace_search_text_tools,
)
from jarvis_worker.agent.tools.workspace.path_policy import (
    _EXCLUDED_DIRS,
    _is_excluded,
    _resolve_safe_target,
)
from jarvis_worker.bootstrap.tool_registry import create_tool_registry
from jarvis_worker.runtime_bus.messages import RunJobMessage
from tests.testing_doubles import MockRunner

# ============================================================
# Helpers
# ============================================================

def _make_job(user_goal: str = "test", workspace_path: str = "") -> RunJobMessage:
    return RunJobMessage(
        job_id="job-1",
        trace_id="trace-1",
        task_id="task-1",
        run_id="run-1",
        user_goal=user_goal,
        created_at="2026-07-08T00:00:00Z",
        workspace_path=workspace_path,
    )


def _make_tool_gateway() -> ToolGateway:
    """创建最小 ToolGateway（注册 workspace.list_files）。"""
    registry = ToolRegistry()
    registry.register(
        ToolManifest(
            name="workspace.list_files",
            provider="native",
            description="list workspace files",
            risk_level_default="L0",
            permission_scope="workspace",
        ),
        execute_workspace_list_files,
    )
    return ToolGateway(registry, PermissionManager())


def _make_tool_request(workspace_root: str, path: str = ".") -> ToolRequest:
    """创建带 workspace_root 的工具请求。"""
    return ToolRequest(
        task_id="t1",
        run_id="r1",
        tool_name="workspace.list_files",
        arguments={"workspace_root": workspace_root, "path": path},
    )


# ============================================================
# ToolRegistry
# ============================================================

class TestToolRegistry:
    """ToolRegistry 注册与查找。"""

    def test_register_and_lookup(self):
        registry = ToolRegistry()
        manifest = ToolManifest(name="test.tool", provider="native", risk_level_default="L0")

        def fake_executor(req: ToolRequest) -> ToolResult:
            return ToolResult(ok=True, kind="text", summary="ok")

        registry.register(manifest, fake_executor)

        assert registry.has("test.tool")
        assert registry.get_manifest("test.tool") is manifest
        assert registry.get_executor("test.tool") is fake_executor
        assert registry.tool_count == 1

    def test_register_duplicate_raises(self):
        registry = ToolRegistry()
        manifest = ToolManifest(name="test.tool")

        def fake_executor(req: ToolRequest) -> ToolResult:
            return ToolResult(ok=True)

        registry.register(manifest, fake_executor)
        with pytest.raises(ValueError, match="已注册"):
            registry.register(manifest, fake_executor)

    def test_lookup_missing_returns_none(self):
        registry = ToolRegistry()
        assert registry.get_manifest("nonexistent") is None
        assert registry.get_executor("nonexistent") is None
        assert not registry.has("nonexistent")

    def test_list_manifests(self):
        registry = ToolRegistry()
        m1 = ToolManifest(name="tool.a")
        m2 = ToolManifest(name="tool.b")

        def fake_executor(req: ToolRequest) -> ToolResult:
            return ToolResult(ok=True)

        registry.register(m1, fake_executor)
        registry.register(m2, fake_executor)

        manifests = registry.list_manifests()
        assert len(manifests) == 2
        names = {m.name for m in manifests}
        assert names == {"tool.a", "tool.b"}

    def test_workspace_list_files_is_registered(self):
        """workspace.list_files 能被注册并查找。"""
        registry = ToolRegistry()
        manifest = ToolManifest(
            name="workspace.list_files",
            provider="native",
            risk_level_default="L0",
        )
        registry.register(manifest, execute_workspace_list_files)

        assert registry.has("workspace.list_files")
        found = registry.get_manifest("workspace.list_files")
        assert found is not None
        assert found.name == "workspace.list_files"
        assert found.risk_level_default == "L0"
        assert found.provider == "native"


# ============================================================
# PermissionManager
# ============================================================

class TestPermissionManager:
    """PermissionManager 自动允许、用户确认和禁止策略。"""

    def test_l0_auto_allow(self):
        perm = PermissionManager()
        manifest = ToolManifest(
            name="workspace.list_files",
            risk_level_default="L0",
        )
        request = ToolRequest(
            task_id="t1",
            run_id="r1",
            tool_name="workspace.list_files",
        )

        result = perm.check(manifest, request)
        assert result.allowed is True
        assert result.decision == "allow"
        assert result.needs_user_approval is False
        assert result.risk_level == "L0"

    def test_non_l0_requires_user_approval(self):
        perm = PermissionManager()
        manifest = ToolManifest(
            name="some.tool",
            risk_level_default="L3",
        )
        request = ToolRequest(
            task_id="t1",
            run_id="r1",
            tool_name="some.tool",
        )

        result = perm.check(manifest, request)
        assert result.allowed is False
        assert result.decision == "ask_user"
        assert result.needs_user_approval is True
        assert result.allowed_decisions == ["allow_once", "deny"]

    def test_l0_not_in_whitelist_deny(self):
        """L0 但不在白名单中的工具应被拒绝。"""
        perm = PermissionManager()
        manifest = ToolManifest(
            name="unknown.l0.tool",
            risk_level_default="L0",
        )
        request = ToolRequest(
            task_id="t1",
            run_id="r1",
            tool_name="unknown.l0.tool",
        )

        result = perm.check(manifest, request)
        assert result.allowed is False

    def test_classify_risk(self):
        perm = PermissionManager()
        manifest = ToolManifest(name="test.tool", risk_level_default="L2")
        assert perm.classify_risk(manifest) == "L2"

    def test_l1_auto_allow(self):
        perm = PermissionManager()
        manifest = ToolManifest(
            name="workspace.list_files",
            risk_level_default="L1",  # 即使名在白名单，risk 也不是 L0
        )
        request = ToolRequest(task_id="t1", run_id="r1", tool_name="workspace.list_files")
        result = perm.check(manifest, request)
        assert result.allowed is True
        assert result.decision == "allow"

    def test_scheduled_task_can_only_authorize_knowledge_write(self):
        perm = PermissionManager()
        scope = {
            "type": "scheduled_task",
            "scheduled_task_id": "schedule-1",
            "authorized_tools": ["knowledge.create_document"],
        }
        knowledge = ToolManifest(
            name="knowledge.create_document", risk_level_default="L2",
        )
        allowed = perm.check(knowledge, ToolRequest(
            task_id="t1", run_id="r1", tool_name=knowledge.name,
            authorization_scope=scope,
        ))
        assert allowed.allowed is True
        assert allowed.needs_user_approval is False

        unrelated = ToolManifest(name="workspace.create_file", risk_level_default="L2")
        denied = perm.check(unrelated, ToolRequest(
            task_id="t1", run_id="r1", tool_name=unrelated.name,
            authorization_scope=scope,
        ))
        assert denied.allowed is False
        assert denied.needs_user_approval is True

    def test_ordinary_knowledge_write_still_requires_confirmation(self):
        manifest = ToolManifest(
            name="knowledge.create_document", risk_level_default="L2",
        )
        result = PermissionManager().check(manifest, ToolRequest(
            task_id="t1", run_id="r1", tool_name=manifest.name,
        ))
        assert result.allowed is False
        assert result.needs_user_approval is True


# ============================================================
# _resolve_safe_target
# ============================================================

class TestResolveSafeTarget:
    """_resolve_safe_target 安全边界测试。"""

    def test_root_itself(self):
        """workspace_root + path="." 解析到 root 本身。"""
        result = _resolve_safe_target("/home/user/project", ".")
        assert result == os.path.realpath("/home/user/project")

    def test_subdir(self):
        """workspace_root + path="subdir" 解析到子目录。"""
        result = _resolve_safe_target("/home/user/project", "src")
        assert result == os.path.realpath("/home/user/project/src")

    def test_absolute_etc_rejected(self):
        """path="/etc" 即使 join 到 root 也会被 commonpath 拒绝。"""
        with pytest.raises(ValueError, match="超出 workspace"):
            _resolve_safe_target("/home/user/project", "/etc")

    def test_absolute_system_path_rejected(self):
        """path="/Users" 不在 workspace root 内。"""
        with pytest.raises(ValueError, match="超出 workspace"):
            _resolve_safe_target("/home/user/project", "/Users")

    def test_parent_traversal_rejected(self):
        """path="../outside" 路径穿越被拒绝。"""
        with pytest.raises(ValueError, match="超出 workspace"):
            _resolve_safe_target("/home/user/project", "../outside")

    def test_multilevel_traversal_rejected(self):
        """path="../../etc" 多层穿越被拒绝。"""
        with pytest.raises(ValueError, match="超出 workspace"):
            _resolve_safe_target("/home/user/project", "../../etc")

    def test_symlink_outside_rejected(self):
        """symlink 指向 workspace 外部被拒绝。"""
        with tempfile.TemporaryDirectory() as tmpdir:
            # 设置 workspace root
            ws_root = os.path.join(tmpdir, "workspace")
            os.makedirs(ws_root, exist_ok=True)

            # 创建一个指向外部的 symlink
            outside_dir = os.path.join(tmpdir, "outside")
            os.makedirs(outside_dir, exist_ok=True)
            symlink_path = os.path.join(ws_root, "link_to_outside")
            os.symlink(outside_dir, symlink_path)

            # path="link_to_outside" 应该解析到外部目录并被拒绝
            with pytest.raises(ValueError, match="超出 workspace"):
                _resolve_safe_target(ws_root, "link_to_outside")

    def test_symlink_inside_allowed(self):
        """symlink 指向 workspace 内部应允许。"""
        with tempfile.TemporaryDirectory() as tmpdir:
            ws_root = os.path.join(tmpdir, "workspace")
            os.makedirs(ws_root, exist_ok=True)

            inner_dir = os.path.join(ws_root, "inner")
            os.makedirs(inner_dir, exist_ok=True)
            symlink_path = os.path.join(ws_root, "link_to_inner")
            os.symlink(inner_dir, symlink_path)

            # path="link_to_inner" 应在 workspace 内
            result = _resolve_safe_target(ws_root, "link_to_inner")
            assert result == os.path.realpath(inner_dir)


# ============================================================
# workspace.list_files
# ============================================================

class TestWorkspaceListFiles:
    """workspace.list_files 执行器测试（修复版：workspace_root + path 分离）。"""

    def test_workspace_root_default_path_success(self):
        """workspace_root + 默认 path="." 成功。"""
        with tempfile.TemporaryDirectory() as tmpdir:
            (Path(tmpdir) / "readme.md").write_text("hello")
            (Path(tmpdir) / "src").mkdir()

            request = _make_tool_request(workspace_root=tmpdir)
            result = execute_workspace_list_files(request)

            assert result.ok is True
            assert result.kind == "json"
            entries = result.data["entries"]
            names = {e["name"] for e in entries}
            assert "readme.md" in names
            assert "src" in names

    def test_workspace_root_with_subdir_path(self):
        """workspace_root + path="subdir" 成功列出子目录。"""
        with tempfile.TemporaryDirectory() as tmpdir:
            (Path(tmpdir) / "subdir").mkdir()
            (Path(tmpdir) / "subdir" / "child.txt").write_text("child")
            (Path(tmpdir) / "root_file.txt").write_text("root")

            # 列出 subdir，只看到 child.txt
            request = _make_tool_request(workspace_root=tmpdir, path="subdir")
            result = execute_workspace_list_files(request)

            assert result.ok is True
            entries = result.data["entries"]
            names = {e["name"] for e in entries}
            assert "child.txt" in names
            assert "root_file.txt" not in names  # 不在 subdir 中

    def test_workspace_root_missing_fail_closed(self):
        """workspace_root 缺失返回 WORKSPACE_ROOT_REQUIRED。"""
        request = ToolRequest(
            task_id="t1",
            run_id="r1",
            tool_name="workspace.list_files",
            arguments={},
        )
        result = execute_workspace_list_files(request)

        assert result.ok is False
        assert result.error["code"] == "WORKSPACE_ROOT_REQUIRED"

    def test_path_etc_rejected(self):
        """path="/etc" 被 WORKSPACE_ACCESS_DENIED 拒绝。"""
        with tempfile.TemporaryDirectory() as tmpdir:
            request = ToolRequest(
                task_id="t1",
                run_id="r1",
                tool_name="workspace.list_files",
                arguments={"workspace_root": tmpdir, "path": "/etc"},
            )
            result = execute_workspace_list_files(request)

            assert result.ok is False
            assert result.error["code"] == "WORKSPACE_ACCESS_DENIED"

    def test_path_parent_traversal_rejected(self):
        """path="../outside" 被 WORKSPACE_ACCESS_DENIED 拒绝。"""
        with tempfile.TemporaryDirectory() as tmpdir:
            request = ToolRequest(
                task_id="t1",
                run_id="r1",
                tool_name="workspace.list_files",
                arguments={"workspace_root": tmpdir, "path": "../outside"},
            )
            result = execute_workspace_list_files(request)

            assert result.ok is False
            assert result.error["code"] == "WORKSPACE_ACCESS_DENIED"

    def test_lists_files_in_temp_dir(self):
        """在临时目录创建文件，验证 list_files 返回正确条目。"""
        with tempfile.TemporaryDirectory() as tmpdir:
            (Path(tmpdir) / "readme.md").write_text("hello")
            (Path(tmpdir) / "script.py").write_text("print(1)")
            (Path(tmpdir) / "src").mkdir()

            request = _make_tool_request(workspace_root=tmpdir)
            result = execute_workspace_list_files(request)

            assert result.ok is True
            assert result.kind == "json"
            entries = result.data["entries"]
            names = {e["name"] for e in entries}
            assert "readme.md" in names
            assert "script.py" in names
            assert "src" in names
            # 目录在前
            assert entries[0]["type"] == "dir"
            assert entries[0]["name"] == "src"

    def test_excludes_noise_dirs(self):
        """验证 node_modules、.git 等噪声目录被排除。"""
        with tempfile.TemporaryDirectory() as tmpdir:
            (Path(tmpdir) / "node_modules").mkdir()
            (Path(tmpdir) / ".git").mkdir()
            (Path(tmpdir) / "dist").mkdir()
            (Path(tmpdir) / "build").mkdir()
            (Path(tmpdir) / "__pycache__").mkdir()
            (Path(tmpdir) / "src").mkdir()
            (Path(tmpdir) / "readme.md").write_text("hello")

            request = _make_tool_request(workspace_root=tmpdir)
            result = execute_workspace_list_files(request)

            assert result.ok is True
            entries = result.data["entries"]
            names = {e["name"] for e in entries}

            # 噪声目录被排除
            for noise in _EXCLUDED_DIRS:
                assert noise not in names, f"{noise} 应被排除"

            # 正常文件/目录保留
            assert "src" in names
            assert "readme.md" in names

    def test_hidden_files_excluded(self):
        """验证 .开头文件被排除。"""
        with tempfile.TemporaryDirectory() as tmpdir:
            (Path(tmpdir) / ".env").write_text("secret")
            (Path(tmpdir) / ".DS_Store").touch()
            (Path(tmpdir) / "normal.txt").write_text("ok")

            request = _make_tool_request(workspace_root=tmpdir)
            result = execute_workspace_list_files(request)

            entries = result.data["entries"]
            names = {e["name"] for e in entries}
            assert ".env" not in names
            assert ".DS_Store" not in names
            assert "normal.txt" in names

    def test_max_entries_limit(self):
        """验证最多返回 100 条。"""
        with tempfile.TemporaryDirectory() as tmpdir:
            for i in range(150):
                (Path(tmpdir) / f"file_{i:03d}.txt").write_text(str(i))

            request = _make_tool_request(workspace_root=tmpdir)
            result = execute_workspace_list_files(request)

            assert result.ok is True
            assert len(result.data["entries"]) <= 100
            assert result.data["truncated"] is True

    def test_nonexistent_path(self):
        """验证不存在子路径返回错误。"""
        with tempfile.TemporaryDirectory() as tmpdir:
            request = _make_tool_request(workspace_root=tmpdir, path="nonexistent")
            result = execute_workspace_list_files(request)

            assert result.ok is False
            assert result.error["code"] == "PATH_NOT_FOUND"

    def test_nonexistent_path_returns_bounded_existing_directory_suggestions(self):
        """可恢复路径错误只返回已有目录候选，不隐式改变目标。"""
        with tempfile.TemporaryDirectory() as tmpdir:
            (Path(tmpdir) / "src" / "runtime").mkdir(parents=True)

            request = _make_tool_request(workspace_root=tmpdir, path="src/runtim")
            result = execute_workspace_list_files(request)

            assert result.ok is False
            assert result.error["code"] == "PATH_NOT_FOUND"
            assert result.data["requested_path"] == "src/runtim"
            assert result.data["suggested_paths"] == ["src/runtime"]

    def test_not_a_directory(self):
        """验证非目录路径返回错误。"""
        with tempfile.TemporaryDirectory() as tmpdir:
            file_path = os.path.join(tmpdir, "test.txt")
            Path(file_path).write_text("hello")

            request = _make_tool_request(workspace_root=tmpdir, path="test.txt")
            result = execute_workspace_list_files(request)

            # test.txt 在 workspace root 下但不是目录
            # 注意：path="test.txt" 指向文件，不是目录
            assert result.ok is False
            assert result.error["code"] == "NOT_A_DIRECTORY"

    def test_returns_file_metadata(self):
        """验证返回文件元数据（size、modified_at）。"""
        with tempfile.TemporaryDirectory() as tmpdir:
            f = Path(tmpdir) / "data.txt"
            f.write_text("hello world")

            request = _make_tool_request(workspace_root=tmpdir)
            result = execute_workspace_list_files(request)

            entries = result.data["entries"]
            data_entry = next(e for e in entries if e["name"] == "data.txt")
            assert data_entry["type"] == "file"
            assert data_entry["size"] == 11
            assert data_entry["modified_at"] is not None

    def test_empty_directory(self):
        """空目录返回空列表。"""
        with tempfile.TemporaryDirectory() as tmpdir:
            request = _make_tool_request(workspace_root=tmpdir)
            result = execute_workspace_list_files(request)

            assert result.ok is True
            assert result.data["entries"] == []
            assert "0 条可见条目" in result.summary

    def test_entries_sorted_dirs_first(self):
        """验证目录在前，文件在后，同类型按名称排序。"""
        with tempfile.TemporaryDirectory() as tmpdir:
            (Path(tmpdir) / "z_dir").mkdir()
            (Path(tmpdir) / "a_dir").mkdir()
            (Path(tmpdir) / "z_file.txt").write_text("z")
            (Path(tmpdir) / "a_file.txt").write_text("a")

            request = _make_tool_request(workspace_root=tmpdir)
            result = execute_workspace_list_files(request)

            entries = result.data["entries"]
            types = [e["type"] for e in entries]
            names = [e["name"] for e in entries]

            # 前两个应为目录（字母序）
            assert types[0] == "dir"
            assert types[1] == "dir"
            assert names[0] == "a_dir"
            assert names[1] == "z_dir"
            # 后两个应为文件
            assert types[2] == "file"
            assert types[3] == "file"
            assert names[2] == "a_file.txt"
            assert names[3] == "z_file.txt"

    def test_result_includes_workspace_root(self):
        """结果 data 中包含 workspace_root 字段。"""
        with tempfile.TemporaryDirectory() as tmpdir:
            (Path(tmpdir) / "file.txt").write_text("x")
            request = _make_tool_request(workspace_root=tmpdir)
            result = execute_workspace_list_files(request)

            assert result.ok is True
            assert result.data["workspace_root"] == tmpdir


# ============================================================
# ToolGateway execute
# ============================================================

class TestToolGatewayExecute:
    """ToolGateway execute 方法测试。"""

    def test_execute_successful_tool(self):
        gateway = _make_tool_gateway()

        with tempfile.TemporaryDirectory() as tmpdir:
            (Path(tmpdir) / "hello.txt").write_text("world")

            request = _make_tool_request(workspace_root=tmpdir)
            result = gateway.execute(request)

            assert result.ok is True
            assert result.kind == "json"
            assert "hello.txt" in str(result.data)

    def test_execute_unknown_tool(self):
        gateway = _make_tool_gateway()
        request = ToolRequest(
            task_id="t1",
            run_id="r1",
            tool_name="nonexistent.tool",
        )
        result = gateway.execute(request)

        assert result.ok is False
        assert result.error["code"] == "TOOL_NOT_FOUND"

    def test_execute_disabled_tool(self):
        registry = ToolRegistry()
        manifest = ToolManifest(
            name="disabled.tool",
            risk_level_default="L0",
            enabled=False,
        )

        def fake_executor(req: ToolRequest) -> ToolResult:
            return ToolResult(ok=True)

        registry.register(manifest, fake_executor)
        perm = PermissionManager()
        perm.L0_ALLOWED_TOOLS = {"disabled.tool"}
        gateway = ToolGateway(registry, perm)

        request = ToolRequest(
            task_id="t1",
            run_id="r1",
            tool_name="disabled.tool",
        )
        result = gateway.execute(request)

        assert result.ok is False
        assert result.error["code"] == "TOOL_DISABLED"

    def test_execute_permission_required(self):
        """L2-L4 工具在没有批准时返回可恢复的权限请求。"""
        registry = ToolRegistry()
        manifest = ToolManifest(
            name="risky.tool",
            risk_level_default="L3",
        )

        def fake_executor(req: ToolRequest) -> ToolResult:
            return ToolResult(ok=True)

        registry.register(manifest, fake_executor)
        gateway = ToolGateway(registry)

        request = ToolRequest(
            task_id="t1",
            run_id="r1",
            tool_name="risky.tool",
        )
        result = gateway.execute(request)

        assert result.ok is False
        assert result.error["code"] == "PERMISSION_REQUIRED"
        assert result.error["recoverable"] is True

    def test_execute_l2_with_verified_once_approval(self):
        registry = ToolRegistry()
        called = []
        registry.register(
            ToolManifest(name="risky.tool", risk_level_default="L2"),
            lambda request: (called.append(request.tool_name) or ToolResult(ok=True)),
        )
        gateway = ToolGateway(registry)
        request = ToolRequest(task_id="t1", run_id="r1", tool_name="risky.tool")

        result = gateway.execute(
            request,
            approval=PermissionApproval(request_id="permission-1", decision="allow_once"),
        )

        assert result.ok is True
        assert called == ["risky.tool"]

    def test_manifest_argument_schema_rejects_unknown_field(self):
        registry = ToolRegistry()
        registry.register(
            ToolManifest(
                name="schema.tool",
                risk_level_default="L2",
                input_schema={
                    "type": "object",
                    "properties": {"path": {"type": "string"}},
                    "required": ["path"],
                    "additionalProperties": False,
                },
            ),
            lambda _request: ToolResult(ok=True),
        )
        result = ToolGateway(registry).execute(
            ToolRequest(
                task_id="t1", run_id="r1", tool_name="schema.tool",
                arguments={"path": "a", "unexpected": True},
            )
        )
        assert result.ok is False
        assert result.error["code"] == "TOOL_ARGUMENTS_INVALID"

    def test_registry_and_permission_manager_access(self):
        gateway = _make_tool_gateway()
        assert gateway.registry is not None
        assert gateway.permission_manager is not None
        assert gateway.registry.has("workspace.list_files")


# ============================================================
# MockRunner tool scenario
# ============================================================

class TestMockRunnerToolScenario:
    """mock runner tool scenario 集成测试。"""

    def test_detects_tool_scenario_keywords(self):
        """验证关键词检测。"""
        runner = MockRunner(worker_id="w1", tool_gateway=_make_tool_gateway())

        # 中文关键词
        assert runner._is_tool_scenario(_make_job("列出文件"))
        assert runner._is_tool_scenario(_make_job("查看目录内容"))
        assert runner._is_tool_scenario(_make_job("查看文件列表"))

        # 英文关键词
        assert runner._is_tool_scenario(_make_job("list files in workspace"))
        assert runner._is_tool_scenario(_make_job("show me the workspace files"))

        # 不匹配
        assert not runner._is_tool_scenario(_make_job("hello world"))
        assert not runner._is_tool_scenario(_make_job("execute permission test"))
        assert not runner._is_tool_scenario(_make_job("run a simple task"))

    def test_tool_scenario_with_workspace_path_as_root(self):
        """job.workspace_path 作为 workspace_root 传给工具。"""
        gateway = _make_tool_gateway()

        with tempfile.TemporaryDirectory() as tmpdir:
            (Path(tmpdir) / "project.md").write_text("project")

            runner = MockRunner(worker_id="w1", tool_gateway=gateway)
            job = _make_job("list workspace files", workspace_path=tmpdir)
            envelopes = runner.run(job)

            # 验证 finished event
            finished = next(e for e in envelopes if e.event_type == "tool.call.finished")
            tc = finished.runtime_event["payload"]["tool_call"]
            assert tc["status"] == "completed"
            assert tc["result"]["summary"] is not None

    def test_tool_scenario_with_default_workspace_root(self):
        """default_workspace_root 配置作为 workspace_root 传给工具。"""
        gateway = _make_tool_gateway()

        with tempfile.TemporaryDirectory() as tmpdir:
            (Path(tmpdir) / "readme.md").write_text("hello")

            runner = MockRunner(
                worker_id="w1",
                tool_gateway=gateway,
                default_workspace_root=tmpdir,
            )
            # job.workspace_path 为空，使用 default_workspace_root
            job = _make_job("列出文件", workspace_path="")
            envelopes = runner.run(job)

            # 验证 finished event
            finished = next(e for e in envelopes if e.event_type == "tool.call.finished")
            tc = finished.runtime_event["payload"]["tool_call"]
            assert tc["status"] == "completed"

    def test_tool_scenario_no_workspace_root_fails(self):
        """无 workspace_path 无 default_workspace_root → 工具失败。"""
        gateway = _make_tool_gateway()
        runner = MockRunner(worker_id="w1", tool_gateway=gateway)
        # 不传 default_workspace_root，job 也无 workspace_path
        job = _make_job("列出文件", workspace_path="")
        envelopes = runner.run(job)

        # 应得到 tool.call.failed + agent.run.failed
        event_types = [e.event_type for e in envelopes]
        assert "tool.call.started" in event_types
        assert "tool.call.failed" in event_types
        assert "agent.run.failed" in event_types

        failed = next(e for e in envelopes if e.event_type == "tool.call.failed")
        tc = failed.runtime_event["payload"]["tool_call"]
        assert tc["error"]["code"] == "WORKSPACE_ROOT_REQUIRED"

    def test_tool_scenario_produces_started_and_finished(self):
        """tool scenario 产生 tool.call.started + tool.call.finished + agent.run.completed。"""
        with tempfile.TemporaryDirectory() as tmpdir:
            gateway = _make_tool_gateway()
            runner = MockRunner(
                worker_id="w1",
                tool_gateway=gateway,
                default_workspace_root=tmpdir,
            )

            job = _make_job("列出当前 workspace 文件")
            envelopes = runner.run(job)

            assert len(envelopes) >= 4

            event_types = [e.event_type for e in envelopes]
            assert "agent.run.started" in event_types
            assert "tool.call.started" in event_types
            assert "tool.call.finished" in event_types
            assert "agent.run.completed" in event_types

            # 验证 tool.call.started payload
            started = next(e for e in envelopes if e.event_type == "tool.call.started")
            tc = started.runtime_event["payload"]["tool_call"]
            assert tc["tool_name"] == "workspace.list_files"
            assert tc["risk_level"] == "L0"
            assert tc["status"] == "running"
            # arguments_summary 应包含 workspace_root
            args = tc["arguments_summary"]
            assert args["workspace_root"] == tmpdir

            # 验证 tool.call.finished payload
            finished = next(e for e in envelopes if e.event_type == "tool.call.finished")
            tc = finished.runtime_event["payload"]["tool_call"]
            assert tc["tool_name"] == "workspace.list_files"
            assert tc["status"] == "completed"
            assert tc["result"] is not None

    def test_tool_scenario_permission_denied_tool(self):
        """非白名单工具应产失败事件。"""
        with tempfile.TemporaryDirectory() as tmpdir:
            registry = ToolRegistry()
            manifest = ToolManifest(
                name="workspace.list_files",
                risk_level_default="L2",  # 非 L0
            )
            registry.register(manifest, execute_workspace_list_files)
            gateway = ToolGateway(registry)

            runner = MockRunner(
                worker_id="w1",
                tool_gateway=gateway,
                default_workspace_root=tmpdir,
            )
            job = _make_job("列出文件")
            envelopes = runner.run(job)

            event_types = [e.event_type for e in envelopes]
            assert "tool.call.started" in event_types
            assert "tool.call.failed" in event_types
            assert "agent.run.failed" in event_types

            # 验证 tool.call.failed payload
            failed = next(e for e in envelopes if e.event_type == "tool.call.failed")
            tc = failed.runtime_event["payload"]["tool_call"]
            assert tc["tool_name"] == "workspace.list_files"
            assert tc["status"] == "failed"
            assert tc["error"] is not None
            assert tc["error"]["code"] == "PERMISSION_REQUIRED"

    def test_tool_scenario_cancel_before_tool_call(self):
        """cancel 在 tool.call.started 之后应中断并发出 agent.run.cancelled。"""
        with tempfile.TemporaryDirectory() as tmpdir:
            gateway = _make_tool_gateway()
            runner = MockRunner(
                worker_id="w1",
                tool_gateway=gateway,
                default_workspace_root=tmpdir,
            )
            job = _make_job("列出文件")

            cancel_after = 1  # 在第二个检查点（tool.call.started 之后）取消
            call_count = [0]

            def cancel_check():
                call_count[0] += 1
                return call_count[0] > cancel_after

            envelopes = runner.run_with_cancel_check(job, cancel_check=cancel_check)

            event_types = [e.event_type for e in envelopes]
            assert "agent.run.started" in event_types
            assert "tool.call.started" in event_types
            assert "agent.run.cancelled" in event_types
            assert "agent.run.completed" not in event_types
            assert "tool.call.finished" not in event_types

    def test_tool_scenario_simple_success_not_affected(self):
        """普通任务仍走 simple_success。"""
        gateway = _make_tool_gateway()
        runner = MockRunner(worker_id="w1", tool_gateway=gateway)
        job = _make_job("hello world")
        envelopes = runner.run(job)

        event_types = [e.event_type for e in envelopes]
        assert "model.delta" in event_types
        assert "agent.run.completed" in event_types
        assert "tool.call.started" not in event_types

    def test_permission_scenario_not_affected(self):
        """permission scenario 不受 tool scenario 影响。"""
        gateway = _make_tool_gateway()
        runner = MockRunner(worker_id="w1", tool_gateway=gateway)

        job = _make_job("test permission scenario")
        envelopes = runner.run_with_cancel_check(
            job,
            wait_decision=lambda req_id: "deny",
            prepare_wait=lambda req_id: None,
        )

        event_types = [e.event_type for e in envelopes]
        assert "permission.required" in event_types
        assert "tool.call.started" in event_types

    def test_tool_scenario_without_gateway_falls_back(self):
        """没有 tool_gateway 时 tool 关键词仍走 simple_success。"""
        runner = MockRunner(worker_id="w1")  # 不传 tool_gateway
        job = _make_job("列出文件")
        envelopes = runner.run(job)

        event_types = [e.event_type for e in envelopes]
        assert "model.delta" in event_types
        assert "agent.run.completed" in event_types

    def test_envelope_validation(self):
        """tool scenario 产生的事件通过 envelope 校验。"""
        with tempfile.TemporaryDirectory() as tmpdir:
            gateway = _make_tool_gateway()
            runner = MockRunner(
                worker_id="w1",
                tool_gateway=gateway,
                default_workspace_root=tmpdir,
            )
            job = _make_job("列出文件")
            envelopes = runner.run(job)

            for env in envelopes:
                env.validate()
                assert env.event_type == env.runtime_event["type"]
                assert env.task_id == env.runtime_event["task_id"]
                assert env.run_id == env.runtime_event["run_id"]


# ============================================================
# workspace.create_file — L2 Scoped Write
# ============================================================

class TestWorkspaceCreateFile:
    """安全创建新文件的执行器契约。"""

    @staticmethod
    def _request(workspace_root: str, path: str, content: str = "hello") -> ToolRequest:
        return ToolRequest(
            task_id="t1",
            run_id="r1",
            tool_name="workspace.create_file",
            arguments={
                "workspace_root": workspace_root,
                "path": path,
                "content": content,
            },
        )

    def test_success_creates_utf8_file_and_returns_metadata(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            (Path(tmpdir) / "notes").mkdir()
            result = execute_workspace_create_file(
                self._request(tmpdir, "notes/你好.txt", "你好 Jarvis")
            )

            assert result.ok is True
            assert result.kind == "file"
            assert (Path(tmpdir) / "notes" / "你好.txt").read_text() == "你好 Jarvis"
            assert result.data["path"] == os.path.join("notes", "你好.txt")
            assert result.data["size_bytes"] == len("你好 Jarvis".encode("utf-8"))
            assert len(result.data["sha256"]) == 64
            assert len(result.deliverables) == 1
            deliverable = result.deliverables[0]
            assert deliverable.path == os.path.join("notes", "你好.txt")
            assert deliverable.mime_type == "text/plain; charset=utf-8"
            assert deliverable.content_hash == result.data["sha256"]

    def test_existing_file_is_not_overwritten(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            target = Path(tmpdir) / "exists.txt"
            target.write_text("old")

            result = execute_workspace_create_file(
                self._request(tmpdir, "exists.txt", "new")
            )

            assert result.ok is False
            assert result.error["code"] == "FILE_ALREADY_EXISTS"
            assert target.read_text() == "old"

    def test_existing_directory_has_specific_error(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            (Path(tmpdir) / "target").mkdir()

            result = execute_workspace_create_file(
                self._request(tmpdir, "target")
            )

            assert result.ok is False
            assert result.error["code"] == "PATH_IS_DIRECTORY"

    @pytest.mark.parametrize("path", ["../escape.txt", "/tmp/escape.txt"])
    def test_workspace_escape_is_rejected(self, path):
        with tempfile.TemporaryDirectory() as tmpdir:
            result = execute_workspace_create_file(self._request(tmpdir, path))

            assert result.ok is False
            assert result.error["code"] in {
                "TOOL_ARGUMENTS_INVALID",
                "WORKSPACE_ACCESS_DENIED",
            }

    def test_missing_parent_is_not_created(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            result = execute_workspace_create_file(
                self._request(tmpdir, "missing/file.txt")
            )

            assert result.ok is False
            assert result.error["code"] == "PARENT_DIR_NOT_FOUND"
            assert not (Path(tmpdir) / "missing").exists()

    def test_parent_symlink_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmpdir, tempfile.TemporaryDirectory() as outside:
            try:
                (Path(tmpdir) / "link").symlink_to(outside, target_is_directory=True)
            except (OSError, NotImplementedError):
                pytest.skip("当前平台不支持 symlink")

            result = execute_workspace_create_file(
                self._request(tmpdir, "link/escape.txt")
            )

            assert result.ok is False
            assert result.error["code"] == "WORKSPACE_ACCESS_DENIED"
            assert not (Path(outside) / "escape.txt").exists()

    def test_final_symlink_is_rejected_without_touching_target(self):
        with tempfile.TemporaryDirectory() as tmpdir, tempfile.TemporaryDirectory() as outside:
            outside_file = Path(outside) / "outside.txt"
            outside_file.write_text("safe")
            try:
                (Path(tmpdir) / "link.txt").symlink_to(outside_file)
            except (OSError, NotImplementedError):
                pytest.skip("当前平台不支持 symlink")

            result = execute_workspace_create_file(
                self._request(tmpdir, "link.txt", "overwrite")
            )

            assert result.ok is False
            assert result.error["code"] == "WORKSPACE_ACCESS_DENIED"
            assert outside_file.read_text() == "safe"

    def test_partial_writes_are_retried_until_complete(self, monkeypatch):
        with tempfile.TemporaryDirectory() as tmpdir:
            original_write = os.write
            calls = 0

            def partial_write(fd, data):
                nonlocal calls
                calls += 1
                return original_write(fd, data[:1])

            monkeypatch.setattr(workspace_create_file_tools.os, "write", partial_write)
            result = execute_workspace_create_file(
                self._request(tmpdir, "partial.txt", "abcdef")
            )

            assert result.ok is True
            assert calls == 6
            assert (Path(tmpdir) / "partial.txt").read_text() == "abcdef"

    @pytest.mark.parametrize("failing_call", ["write", "fsync"])
    def test_write_failure_cleans_file_and_closes_all_opened_fds(
        self, monkeypatch, failing_call,
    ):
        with tempfile.TemporaryDirectory() as tmpdir:
            original_open = os.open
            original_close = os.close
            opened: list[int] = []
            closed: list[int] = []

            def tracked_open(*args, **kwargs):
                fd = original_open(*args, **kwargs)
                opened.append(fd)
                return fd

            def tracked_close(fd):
                closed.append(fd)
                return original_close(fd)

            def fail(*_args, **_kwargs):
                raise OSError("forced failure")

            monkeypatch.setattr(workspace_create_file_tools, "_supports_safe_dir_fd", lambda: True)
            monkeypatch.setattr(workspace_create_file_tools.os, "open", tracked_open)
            monkeypatch.setattr(workspace_create_file_tools.os, "close", tracked_close)
            monkeypatch.setattr(workspace_create_file_tools.os, failing_call, fail)

            result = execute_workspace_create_file(
                self._request(tmpdir, f"{failing_call}.txt", "content")
            )

            assert result.ok is False
            assert result.error["code"] == "CREATE_FILE_FAILED"
            assert not (Path(tmpdir) / f"{failing_call}.txt").exists()
            assert set(opened) <= set(closed)

    def test_content_size_limit_is_enforced(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            result = execute_workspace_create_file(
                self._request(tmpdir, "large.txt", "x" * (1024 * 1024 + 1))
            )

            assert result.ok is False
            assert result.error["code"] == "FILE_TOO_LARGE"
            assert not (Path(tmpdir) / "large.txt").exists()


# ============================================================
# workspace.read_file — Phase 6A
# ============================================================

class TestWorkspaceReadFile:
    """workspace.read_file 执行器测试。"""

    def _make_read_request(self, workspace_root: str, path: str,
                           max_bytes: int | None = None,
                           max_chars: int | None = None,
                           start_line: int | None = None,
                           max_lines: int | None = None) -> ToolRequest:
        args: dict = {"workspace_root": workspace_root, "path": path}
        if max_bytes is not None:
            args["max_bytes"] = max_bytes
        if max_chars is not None:
            args["max_chars"] = max_chars
        if start_line is not None:
            args["start_line"] = start_line
        if max_lines is not None:
            args["max_lines"] = max_lines
        return ToolRequest(
            task_id="t1",
            run_id="r1",
            tool_name="workspace.read_file",
            arguments=args,
        )

    def test_success_read_text_file(self):
        """成功读取 workspace 内文本文件。"""
        with tempfile.TemporaryDirectory() as tmpdir:
            (Path(tmpdir) / "readme.md").write_text("# Hello World\n\nThis is a test file.")

            request = self._make_read_request(workspace_root=tmpdir, path="readme.md")
            result = execute_workspace_read_file(request)

            assert result.ok is True
            assert result.kind == "text"
            assert "# Hello World" in result.data["content"]
            assert result.data["path"] == "readme.md"
            assert result.data["size_bytes"] > 0
            assert result.data["chars_read"] > 0
            assert result.data["truncated"] is False
            assert "Read file: readme.md" in result.summary

    def test_path_in_subdir(self):
        """读取子目录下的文件。"""
        with tempfile.TemporaryDirectory() as tmpdir:
            subdir = Path(tmpdir) / "docs"
            subdir.mkdir()
            (subdir / "README.md").write_text("# Docs")

            request = self._make_read_request(workspace_root=tmpdir, path="docs/README.md")
            result = execute_workspace_read_file(request)

            assert result.ok is True
            assert "# Docs" in result.data["content"]
            assert result.data["path"] == "docs/README.md"

    def test_workspace_root_missing(self):
        """workspace_root 缺失返回 WORKSPACE_ROOT_REQUIRED。"""
        request = ToolRequest(
            task_id="t1", run_id="r1",
            tool_name="workspace.read_file",
            arguments={"path": "readme.md"},
        )
        result = execute_workspace_read_file(request)
        assert result.ok is False
        assert result.error["code"] == "WORKSPACE_ROOT_REQUIRED"

    def test_path_missing(self):
        """path 缺失返回 FILE_NOT_FOUND。"""
        with tempfile.TemporaryDirectory() as tmpdir:
            request = ToolRequest(
                task_id="t1", run_id="r1",
                tool_name="workspace.read_file",
                arguments={"workspace_root": tmpdir},
            )
            result = execute_workspace_read_file(request)
            assert result.ok is False
            assert result.error["code"] == "FILE_NOT_FOUND"

    def test_absolute_path_rejected(self):
        """绝对路径被拒绝。"""
        with tempfile.TemporaryDirectory() as tmpdir:
            request = self._make_read_request(workspace_root=tmpdir, path="/etc/passwd")
            result = execute_workspace_read_file(request)

            assert result.ok is False
            assert result.error["code"] == "WORKSPACE_ACCESS_DENIED"

    def test_parent_traversal_rejected(self):
        """../ 路径穿越被拒绝。"""
        with tempfile.TemporaryDirectory() as tmpdir:
            request = self._make_read_request(workspace_root=tmpdir, path="../outside.txt")
            result = execute_workspace_read_file(request)

            assert result.ok is False
            assert result.error["code"] == "WORKSPACE_ACCESS_DENIED"

    def test_symlink_outside_rejected(self):
        """symlink 指向 workspace 外部被拒绝。"""
        with tempfile.TemporaryDirectory() as tmpdir:
            ws_root = os.path.join(tmpdir, "workspace")
            os.makedirs(ws_root, exist_ok=True)
            outside_dir = os.path.join(tmpdir, "outside")
            os.makedirs(outside_dir, exist_ok=True)
            (Path(outside_dir) / "secret.txt").write_text("secret")
            symlink_path = os.path.join(ws_root, "link_to_secret")
            os.symlink(outside_dir, symlink_path)

            request = self._make_read_request(workspace_root=ws_root, path="link_to_secret")
            result = execute_workspace_read_file(request)

            assert result.ok is False
            assert result.error["code"] == "WORKSPACE_ACCESS_DENIED"

    def test_symlink_inside_allowed(self):
        """symlink 指向 workspace 内部允许。"""
        with tempfile.TemporaryDirectory() as tmpdir:
            ws_root = os.path.join(tmpdir, "workspace")
            os.makedirs(ws_root, exist_ok=True)
            inner_dir = os.path.join(ws_root, "inner")
            os.makedirs(inner_dir, exist_ok=True)
            (Path(inner_dir) / "target.txt").write_text("inner content")
            symlink_path = os.path.join(ws_root, "link_to_inner")
            os.symlink(inner_dir, symlink_path)

            request = self._make_read_request(workspace_root=ws_root, path="link_to_inner/target.txt")
            result = execute_workspace_read_file(request)

            assert result.ok is True
            assert "inner content" in result.data["content"]

    def test_file_not_found(self):
        """文件不存在返回 FILE_NOT_FOUND。"""
        with tempfile.TemporaryDirectory() as tmpdir:
            request = self._make_read_request(workspace_root=tmpdir, path="nonexistent.txt")
            result = execute_workspace_read_file(request)

            assert result.ok is False
            assert result.error["code"] == "FILE_NOT_FOUND"

    def test_file_not_found_returns_ranked_existing_file_suggestions(self):
        """猜错架构路径时返回有界候选，但不自动读取候选正文。"""
        with tempfile.TemporaryDirectory() as tmpdir:
            actual = Path(tmpdir) / "src" / "runtime" / "tasks" / "service.py"
            actual.parent.mkdir(parents=True)
            actual.write_text("SECRET_EVIDENCE")

            result = execute_workspace_read_file(
                self._make_read_request(
                    workspace_root=tmpdir,
                    path="src/application/task_service.py",
                )
            )

            assert result.ok is False
            assert result.error["code"] == "FILE_NOT_FOUND"
            assert result.data["requested_path"] == "src/application/task_service.py"
            assert "src/runtime/tasks/service.py" in result.data["suggested_paths"]
            assert len(result.data["suggested_paths"]) <= 5
            assert "SECRET_EVIDENCE" not in str(result.data)

    def test_path_is_directory(self):
        """path 是目录返回 PATH_IS_DIRECTORY。"""
        with tempfile.TemporaryDirectory() as tmpdir:
            (Path(tmpdir) / "subdir").mkdir()
            request = self._make_read_request(workspace_root=tmpdir, path="subdir")
            result = execute_workspace_read_file(request)

            assert result.ok is False
            assert result.error["code"] == "PATH_IS_DIRECTORY"

    def test_file_too_large(self):
        """文件超过 max_bytes 返回 FILE_TOO_LARGE。"""
        with tempfile.TemporaryDirectory() as tmpdir:
            file_path = Path(tmpdir) / "large.txt"
            # 写入 2000 字节
            file_path.write_text("x" * 2000)

            request = self._make_read_request(workspace_root=tmpdir, path="large.txt", max_bytes=1000)
            result = execute_workspace_read_file(request)

            assert result.ok is False
            assert result.error["code"] == "FILE_TOO_LARGE"
            assert result.error["category"] == "validation"
            assert result.error["recoverable"] is True

    def test_binary_file_rejected(self):
        """二进制文件返回 UNSUPPORTED_FILE_TYPE。"""
        with tempfile.TemporaryDirectory() as tmpdir:
            file_path = Path(tmpdir) / "binary.bin"
            # 写入非 UTF-8 字节
            file_path.write_bytes(b'\x00\x01\x02\x80\xff\xfe\xfd')

            request = self._make_read_request(workspace_root=tmpdir, path="binary.bin")
            result = execute_workspace_read_file(request)

            assert result.ok is False
            assert result.error["code"] == "UNSUPPORTED_FILE_TYPE"

    def test_partial_invalid_utf8_rejected(self):
        """少量非法 UTF-8 字节也必须失败，不做容错替换。"""
        with tempfile.TemporaryDirectory() as tmpdir:
            file_path = Path(tmpdir) / "partial_utf8.txt"
            # 大部分是有效 UTF-8，插入了少量非法字节 \x80
            file_path.write_bytes(b"hello\x80world")

            request = self._make_read_request(workspace_root=tmpdir, path="partial_utf8.txt")
            result = execute_workspace_read_file(request)

            assert result.ok is False
            assert result.error["code"] == "UNSUPPORTED_FILE_TYPE"
            assert result.error["category"] == "validation"
            assert result.error["recoverable"] is True

    def test_max_chars_truncation(self):
        """字符数超过 max_chars 时截断返回。"""
        with tempfile.TemporaryDirectory() as tmpdir:
            content = "Hello World! " * 500  # ~6500 chars
            (Path(tmpdir) / "long.txt").write_text(content)

            request = self._make_read_request(workspace_root=tmpdir, path="long.txt", max_chars=1000)
            result = execute_workspace_read_file(request)

            assert result.ok is True
            assert result.data["truncated"] is True
            assert result.data["chars_read"] == 1000
            assert len(result.data["content"]) == 1000
            assert "（截断后" in result.summary or "truncated" in result.summary

    def test_default_max_bytes_respected(self):
        """默认 max_bytes 限制生效。"""
        with tempfile.TemporaryDirectory() as tmpdir:
            file_path = Path(tmpdir) / "huge.txt"
            # 写入 128KB（超过默认 65536）
            file_path.write_text("A" * 131072)

            request = self._make_read_request(workspace_root=tmpdir, path="huge.txt")
            result = execute_workspace_read_file(request)

            assert result.ok is False
            assert result.error["code"] == "FILE_TOO_LARGE"

    def test_absolute_max_bytes_capped(self):
        """max_bytes 超过安全上限时被截断为 _ABSOLUTE_MAX_BYTES 范围。"""
        with tempfile.TemporaryDirectory() as tmpdir:
            file_path = Path(tmpdir) / "medium.txt"
            # 写 100K 字符 — 在 max_bytes cap 范围（262144）内，但在默认 max_chars（20000）外
            # 需要同时提高 max_chars 以验证 max_bytes 未被 cap 到默认值
            file_path.write_text("B" * 100000)

            request = self._make_read_request(
                workspace_root=tmpdir, path="medium.txt",
                max_bytes=500000, max_chars=100000,
            )
            result = execute_workspace_read_file(request)

            assert result.ok is True
            assert result.data["size_bytes"] == 100000
            assert result.data["chars_read"] == 100000

    def test_empty_file(self):
        """空文件成功返回空内容。"""
        with tempfile.TemporaryDirectory() as tmpdir:
            (Path(tmpdir) / "empty.txt").write_text("")

            request = self._make_read_request(workspace_root=tmpdir, path="empty.txt")
            result = execute_workspace_read_file(request)

            assert result.ok is True
            assert result.data["content"] == ""
            assert result.data["size_bytes"] == 0
            assert result.data["chars_read"] == 0
            assert "空文件" in result.summary

    def test_chinese_utf8_content(self):
        """中文 UTF-8 内容正常读取。"""
        with tempfile.TemporaryDirectory() as tmpdir:
            (Path(tmpdir) / "zh.md").write_text("# 你好世界\n\n这是一个中文测试文件。")

            request = self._make_read_request(workspace_root=tmpdir, path="zh.md")
            result = execute_workspace_read_file(request)

            assert result.ok is True
            assert "你好世界" in result.data["content"]
            assert "中文测试文件" in result.data["content"]

    def test_reads_requested_line_range_instead_of_file_prefix(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            lines = [f"line-{index}\n" for index in range(1, 1001)]
            (Path(tmpdir) / "large.py").write_text("".join(lines))

            request = self._make_read_request(
                workspace_root=tmpdir,
                path="large.py",
                start_line=790,
                max_lines=25,
            )
            result = execute_workspace_read_file(request)

            assert result.ok is True
            assert result.data["content"].startswith("line-790\n")
            assert "line-814" in result.data["content"]
            assert "line-789" not in result.data["content"]
            assert result.data["start_line"] == 790
            assert result.data["end_line"] == 814
            assert result.data["total_lines"] == 1000
            assert result.data["truncated"] is True

    def test_rejects_out_of_bounds_line_range(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            (Path(tmpdir) / "short.txt").write_text("one\ntwo\n")

            result = execute_workspace_read_file(
                self._make_read_request(
                    workspace_root=tmpdir,
                    path="short.txt",
                    start_line=10,
                    max_lines=2,
                )
            )

            assert result.ok is False
            assert result.error["code"] == "LINE_RANGE_OUT_OF_BOUNDS"


class TestWorkspaceReadFiles:
    def _request(self, workspace_root: str, files: object) -> ToolRequest:
        return ToolRequest(
            task_id="t1",
            run_id="r1",
            tool_name="workspace.read_files",
            arguments={"workspace_root": workspace_root, "files": files},
        )

    def test_reads_multiple_files_and_targeted_ranges_in_one_call(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            (Path(tmpdir) / "entry.ts").write_text("a\nb\nc\nd\n")
            (Path(tmpdir) / "worker.py").write_text(
                "".join(f"line-{index}\n" for index in range(1, 501))
            )

            result = execute_workspace_read_files(
                self._request(
                    tmpdir,
                    [
                        {"path": "entry.ts", "start_line": 2, "max_lines": 2},
                        {"path": "worker.py", "start_line": 450, "max_lines": 20},
                    ],
                )
            )

            assert result.ok is True
            assert result.data["succeeded_files"] == 2
            assert result.data["failed_files"] == 0
            assert result.data["files"][0]["content"] == "b\nc\n"
            assert result.data["files"][1]["content"].startswith("line-450\n")
            assert result.data["files"][1]["end_line"] == 469

    def test_accepts_plain_path_strings_as_default_range_shorthand(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            (Path(tmpdir) / "first.txt").write_text("first evidence\n")
            (Path(tmpdir) / "second.txt").write_text("second evidence\n")

            result = execute_workspace_read_files(
                self._request(tmpdir, ["first.txt", "second.txt"])
            )

            assert result.ok is True
            assert result.data["succeeded_files"] == 2
            assert result.data["files"][0]["content"] == "first evidence\n"
            assert result.data["files"][1]["start_line"] == 1

    def test_accepts_path_start_end_range_shorthand(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            (Path(tmpdir) / "source.py").write_text(
                "".join(f"line-{index}\n" for index in range(1, 101))
            )

            result = execute_workspace_read_files(
                self._request(tmpdir, ["source.py:40:49"])
            )

            assert result.ok is True
            assert result.data["files"][0]["start_line"] == 40
            assert result.data["files"][0]["end_line"] == 49
            assert result.data["files"][0]["content"].startswith("line-40\n")

    def test_rejects_reversed_path_range_shorthand(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            result = execute_workspace_read_files(
                self._request(tmpdir, ["source.py:49:40"])
            )

            assert result.ok is False
            assert result.error["code"] == "INVALID_BATCH_REQUEST"

    def test_partial_success_preserves_per_file_error(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            (Path(tmpdir) / "good.txt").write_text("evidence")

            result = execute_workspace_read_files(
                self._request(
                    tmpdir,
                    [{"path": "good.txt"}, {"path": "missing.txt"}],
                )
            )

            assert result.ok is True
            assert result.data["succeeded_files"] == 1
            assert result.data["failed_files"] == 1
            assert result.data["files"][1]["error"]["code"] == "FILE_NOT_FOUND"

    def test_partial_success_preserves_bounded_path_suggestions(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            (Path(tmpdir) / "good.txt").write_text("evidence")
            actual = Path(tmpdir) / "src" / "runtime" / "tasks" / "service.py"
            actual.parent.mkdir(parents=True)
            actual.write_text("runtime evidence")

            result = execute_workspace_read_files(
                self._request(
                    tmpdir,
                    ["good.txt", "src/application/task_service.py"],
                )
            )

            assert result.ok is True
            failed = result.data["files"][1]
            assert failed["error"]["code"] == "FILE_NOT_FOUND"
            assert "src/runtime/tasks/service.py" in failed["suggested_paths"]

    def test_all_fail_returns_batch_error_without_hiding_item_errors(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            result = execute_workspace_read_files(
                self._request(tmpdir, [{"path": "missing.txt"}])
            )

            assert result.ok is False
            assert result.error["code"] == "BATCH_READ_FAILED"
            assert result.data["files"][0]["error"]["code"] == "FILE_NOT_FOUND"

    def test_each_path_keeps_workspace_boundary_enforcement(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            result = execute_workspace_read_files(
                self._request(tmpdir, [{"path": "../outside.txt"}])
            )

            assert result.ok is False
            assert result.data["files"][0]["error"]["code"] == "WORKSPACE_ACCESS_DENIED"

    def test_rejects_more_than_six_items(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            result = execute_workspace_read_files(
                self._request(
                    tmpdir,
                    [{"path": f"{index}.txt"} for index in range(7)],
                )
            )

            assert result.ok is False
            assert result.error["code"] == "INVALID_BATCH_REQUEST"


# ============================================================
# _is_excluded
# ============================================================

class TestIsExcluded:
    """_is_excluded 排除规则测试。"""

    def test_excludes_known_dirs(self):
        for name in _EXCLUDED_DIRS:
            assert _is_excluded(name), f"{name} 应被排除"

    def test_excludes_hidden(self):
        assert _is_excluded(".env")
        assert _is_excluded(".gitignore")
        assert _is_excluded(".DS_Store")

    def test_does_not_exclude_normal(self):
        assert not _is_excluded("src")
        assert not _is_excluded("readme.md")
        assert not _is_excluded("package.json")


# ============================================================
# workspace.search_files
# ============================================================

class TestWorkspaceSearchFiles:
    """workspace.search_files 安全、边界与结果契约。"""

    @staticmethod
    def _request(workspace_root: str = "", **arguments) -> ToolRequest:
        return ToolRequest(
            task_id="t-search",
            run_id="r-search",
            tool_name="workspace.search_files",
            arguments={"workspace_root": workspace_root, **arguments},
        )

    def test_recursive_casefold_search_returns_relative_posix_paths(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            docs = Path(tmpdir) / "Docs"
            docs.mkdir()
            (docs / "README.MD").write_text("body")

            result = execute_workspace_search_files(
                self._request(tmpdir, query="readme", path=".")
            )

            assert result.ok is True
            assert result.data["matches"][0]["path"] == "Docs/README.MD"
            assert result.data["matches"][0]["type"] == "file"
            assert not os.path.isabs(result.data["matches"][0]["path"])

    def test_path_is_canonicalized_before_results_are_built(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            docs = Path(tmpdir) / "docs"
            docs.mkdir()
            (docs / "README.md").write_text("body")

            result = execute_workspace_search_files(
                self._request(tmpdir, query="readme", path="missing/../docs")
            )

            assert result.ok is True
            assert result.data["search_path"] == "docs"
            assert result.data["matches"][0]["path"] == "docs/README.md"
            assert ".." not in result.data["matches"][0]["path"]

    @pytest.mark.parametrize("query", ["", "   ", None])
    def test_query_required(self, query):
        result = execute_workspace_search_files(self._request("/tmp", query=query))
        assert result.ok is False
        assert result.error["code"] == "SEARCH_QUERY_REQUIRED"

    def test_query_length_limit(self):
        result = execute_workspace_search_files(
            self._request("/tmp", query="x" * 201)
        )
        assert result.ok is False
        assert result.error["code"] == "SEARCH_QUERY_TOO_LONG"

    @pytest.mark.parametrize("path", ["/etc", "../outside"])
    def test_absolute_and_escape_paths_are_denied(self, path):
        with tempfile.TemporaryDirectory() as tmpdir:
            result = execute_workspace_search_files(
                self._request(tmpdir, query="x", path=path)
            )
        assert result.ok is False
        assert result.error["code"] == "WORKSPACE_ACCESS_DENIED"

    def test_missing_and_non_directory_search_root(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            missing = execute_workspace_search_files(
                self._request(tmpdir, query="x", path="missing")
            )
            (Path(tmpdir) / "file.txt").write_text("x")
            file_result = execute_workspace_search_files(
                self._request(tmpdir, query="x", path="file.txt")
            )

        assert missing.error["code"] == "PATH_NOT_FOUND"
        assert file_result.error["code"] == "NOT_A_DIRECTORY"

    def test_hidden_noise_and_symlink_target_are_not_traversed(self):
        with tempfile.TemporaryDirectory() as tmpdir, tempfile.TemporaryDirectory() as outside:
            (Path(tmpdir) / ".hidden").mkdir()
            (Path(tmpdir) / ".hidden" / "secret.txt").write_text("hidden")
            (Path(tmpdir) / "node_modules").mkdir()
            (Path(tmpdir) / "node_modules" / "secret.txt").write_text("noise")
            (Path(outside) / "outside-secret.txt").write_text("outside")
            (Path(tmpdir) / "outside-link").symlink_to(outside, target_is_directory=True)

            result = execute_workspace_search_files(
                self._request(tmpdir, query="secret")
            )

            assert result.ok is True
            assert result.data["matches"] == []
            assert result.data["excluded_entries"] == 2

    def test_symlink_replacement_between_queue_and_open_cannot_escape(
        self, monkeypatch
    ):
        with tempfile.TemporaryDirectory() as tmpdir, tempfile.TemporaryDirectory() as outside:
            child = Path(tmpdir) / "child"
            child.mkdir()
            (Path(outside) / "outside-secret.txt").write_text("outside")
            original = workspace_path_policy._open_workspace_directory_fd
            child_open_attempted = False

            def replace_before_child_open(root_fd, components):
                nonlocal child_open_attempted
                if components == ("child",) and not child_open_attempted:
                    child_open_attempted = True
                    child.rmdir()
                    child.symlink_to(outside, target_is_directory=True)
                return original(root_fd, components)

            monkeypatch.setattr(
                workspace_path_policy,
                "_open_workspace_directory_fd",
                replace_before_child_open,
            )
            result = execute_workspace_search_files(
                self._request(tmpdir, query="outside-secret")
            )

            assert result.ok is True
            assert result.data["matches"] == []
            assert result.data["skipped_directories"] == 1

    def test_root_scandir_permission_error_is_failure(self, monkeypatch):
        monkeypatch.setattr(workspace_path_policy, "_supports_safe_search_dir_fd", lambda: True)
        monkeypatch.setattr(
            workspace_search_file_tools.os,
            "scandir",
            MagicMock(side_effect=PermissionError("sensitive detail")),
        )
        result = execute_workspace_search_files(
            self._request("/tmp", query="x")
        )
        assert result.ok is False
        assert result.error["code"] == "PERMISSION_DENIED"
        assert "sensitive detail" not in result.error["message"]

    def test_scan_budget_bounds_materialized_entries(self, monkeypatch):
        monkeypatch.setattr(workspace_search_file_tools, "_SEARCH_MAX_SCANNED", 2)
        with tempfile.TemporaryDirectory() as tmpdir:
            for name in ("a.txt", "b.txt", "c.txt"):
                (Path(tmpdir) / name).write_text(name)
            result = execute_workspace_search_files(
                self._request(tmpdir, query=".txt", max_results=100)
            )

        assert result.ok is True
        assert result.data["scanned_entries"] == 2
        assert len(result.data["matches"]) == 2
        assert result.data["truncated"] is True
        assert "max_scanned_entries" in result.data["truncation_reasons"]

    def test_depth_limit_marks_truncation(self, monkeypatch):
        monkeypatch.setattr(workspace_search_file_tools, "_SEARCH_MAX_DEPTH", 1)
        with tempfile.TemporaryDirectory() as tmpdir:
            nested = Path(tmpdir) / "d0" / "d1" / "d2"
            nested.mkdir(parents=True)
            result = execute_workspace_search_files(
                self._request(tmpdir, query="never")
            )

        assert result.ok is True
        assert result.data["truncated"] is True
        assert "max_depth" in result.data["truncation_reasons"]

    def test_max_results_bool_uses_default_and_real_limit_truncates(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            for index in range(3):
                (Path(tmpdir) / f"match-{index}.txt").write_text("x")
            bool_result = execute_workspace_search_files(
                self._request(tmpdir, query="match", max_results=True)
            )
            limited_result = execute_workspace_search_files(
                self._request(tmpdir, query="match", max_results=1)
            )

        assert bool_result.data["max_results"] == 50
        assert len(bool_result.data["matches"]) == 3
        assert len(limited_result.data["matches"]) == 1
        assert "max_results" in limited_result.data["truncation_reasons"]

    def test_registry_manifest_and_l0_permission(self):
        registry = create_tool_registry()
        manifest = registry.get_manifest("workspace.search_files")
        assert manifest is not None
        assert manifest.provider == "native"
        assert manifest.risk_level_default == "L0"
        assert manifest.permission_scope == "workspace"
        assert manifest.input_schema["required"] == ["workspace_root", "query"]
        assert manifest.input_schema["additionalProperties"] is False

        decision = PermissionManager().check(
            manifest,
            self._request("/tmp", query="x"),
        )
        assert decision.allowed is True
        assert decision.needs_user_approval is False

# ============================================================
# workspace.search_text
# ============================================================


class TestWorkspaceSearchText:
    """workspace.search_text 的正文检索、安全边界与容量契约。"""

    @staticmethod
    def _request(workspace_root: str = "", **arguments) -> ToolRequest:
        return ToolRequest(
            task_id="t-search-text",
            run_id="r-search-text",
            tool_name="workspace.search_text",
            arguments={"workspace_root": workspace_root, **arguments},
        )

    def test_casefold_content_search_returns_line_and_bounded_preview(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            docs = Path(tmpdir) / "docs"
            docs.mkdir()
            (docs / "release.md").write_text(
                "第一行\nRC2 发布门禁已经完成\n第三行",
                encoding="utf-8",
            )

            result = execute_workspace_search_text(self._request(tmpdir, query="rc2", path="docs"))

        assert result.ok is True
        assert result.data["matches"] == [
            {
                "path": "docs/release.md",
                "line_number": 2,
                "preview": "RC2 发布门禁已经完成",
            }
        ]
        assert result.data["searched_files"] == 1
        assert result.data["scan_complete"] is True
        assert result.data["result_window_truncated"] is False
        assert result.data["truncated"] is False

    def test_single_file_path_is_searched_without_following_symlinks(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            docs = Path(tmpdir) / "docs"
            docs.mkdir()
            target = docs / "progress.md"
            target.write_text("RC2 ready\nother\nRC2 risk\n", encoding="utf-8")

            result = execute_workspace_search_text(
                self._request(tmpdir, query="rc2", path="docs/progress.md")
            )

        assert result.ok is True
        assert [item["line_number"] for item in result.data["matches"]] == [1, 3]
        assert {item["path"] for item in result.data["matches"]} == {"docs/progress.md"}
        assert result.data["searched_files"] == 1

    def test_zero_match_summary_explains_literal_search_recovery(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            (Path(tmpdir) / "release.md").write_text("RC2 candidate", encoding="utf-8")
            result = execute_workspace_search_text(
                self._request(tmpdir, query="RC2 发布门禁", path="release.md")
            )

        assert result.ok is True
        assert result.data["matches"] == []
        assert "精确子串" in result.summary
        assert "较短关键词" in result.summary

    @pytest.mark.parametrize("path", ["/etc", "../outside"])
    def test_absolute_and_escape_paths_are_denied(self, path):
        with tempfile.TemporaryDirectory() as tmpdir:
            result = execute_workspace_search_text(self._request(tmpdir, query="secret", path=path))
        assert result.ok is False
        assert result.error["code"] == "WORKSPACE_ACCESS_DENIED"

    def test_hidden_binary_large_and_symlink_content_are_not_exposed(self):
        with tempfile.TemporaryDirectory() as tmpdir, tempfile.TemporaryDirectory() as outside:
            root = Path(tmpdir)
            (root / ".hidden.md").write_text("secret", encoding="utf-8")
            (root / "binary.txt").write_bytes(b"secret\x00value")
            (root / "large.md").write_text("secret" * 200_000, encoding="utf-8")
            outside_file = Path(outside) / "outside.md"
            outside_file.write_text("secret", encoding="utf-8")
            (root / "outside-link.md").symlink_to(outside_file)

            result = execute_workspace_search_text(self._request(tmpdir, query="secret"))

        assert result.ok is True
        assert result.data["matches"] == []
        assert result.data["excluded_entries"] == 1
        assert result.data["skipped_files"] == 2

    def test_result_and_total_byte_budgets_are_hard_bounded(self, monkeypatch):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            (root / "a.md").write_text("match\nmatch", encoding="utf-8")
            (root / "b.md").write_text("match", encoding="utf-8")
            limited = execute_workspace_search_text(
                self._request(tmpdir, query="match", max_results=1)
            )
            monkeypatch.setattr(workspace_search_text_tools, "_MAX_TOTAL_BYTES", 8)
            byte_limited = execute_workspace_search_text(
                self._request(tmpdir, query="never", max_results=50)
            )

        assert len(limited.data["matches"]) == 1
        assert "max_results" in limited.data["truncation_reasons"]
        assert limited.data["scan_complete"] is True
        assert limited.data["result_window_truncated"] is True
        assert "同 query/path 重试不会翻页" in limited.summary
        assert byte_limited.data["truncated"] is True
        assert "max_total_bytes" in byte_limited.data["truncation_reasons"]
        assert byte_limited.data["scan_complete"] is False

    def test_per_file_match_cap_preserves_path_diversity(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            (root / "a.md").write_text("\n".join(["RC2"] * 10), encoding="utf-8")
            (root / "b.md").write_text("RC2", encoding="utf-8")

            result = execute_workspace_search_text(
                self._request(tmpdir, query="RC2", max_results=20)
            )

        paths = [item["path"] for item in result.data["matches"]]
        assert paths.count("a.md") == 3
        assert "b.md" in paths
        assert result.data["matched_files"] == 2
        assert result.data["matching_lines"] == 11
        assert result.data["result_window_truncated"] is True

    def test_source_only_excludes_docs_tests_and_non_source_files(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            (root / "docs").mkdir()
            (root / "tests").mkdir()
            source = root / "src" / "agent" / "core"
            source.mkdir(parents=True)
            (root / "docs" / "architecture.md").write_text(
                "AgentRunner design", encoding="utf-8"
            )
            (root / "tests" / "test_runner.py").write_text(
                "AgentRunner test", encoding="utf-8"
            )
            (source / "runner.py").write_text(
                "class AgentRunner:", encoding="utf-8"
            )
            (source / "notes.md").write_text(
                "AgentRunner note", encoding="utf-8"
            )

            result = execute_workspace_search_text(
                self._request(
                    tmpdir,
                    query="AgentRunner",
                    path=".",
                    max_results=50,
                    source_only=True,
                )
            )

        assert result.ok is True
        assert result.data["source_only"] is True
        assert [item["path"] for item in result.data["matches"]] == [
            "src/agent/core/runner.py"
        ]

    def test_source_only_ranks_runtime_behavior_ahead_of_secondary_references(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            source = root / "apps" / "worker" / "src" / "runtime" / "audit"
            source.mkdir(parents=True)
            scripts = root / "scripts"
            scripts.mkdir()
            web = root / "apps" / "web" / "src"
            web.mkdir(parents=True)
            (scripts / "audit_drill.py").write_text(
                "from app import AuditLog\n" * 30, encoding="utf-8"
            )
            (web / "audit.ts").write_text(
                "export type AuditLog = unknown\n" * 30, encoding="utf-8"
            )
            (source / "postgres_repository.py").write_text(
                "class PostgresAuditLogRepository:\n"
                "    async def create(self, log: AuditLog):\n"
                "        # AuditLog 持久化 adapter\n"
                "        return await self.session.save(log)\n",
                encoding="utf-8",
            )

            result = execute_workspace_search_text(
                self._request(
                    tmpdir,
                    query="AuditLog",
                    path=".",
                    max_results=2,
                    source_only=True,
                )
            )

        assert result.ok is True
        assert result.data["truncated"] is True
        assert "max_results" in result.data["truncation_reasons"]
        assert [item["path"] for item in result.data["matches"]] == [
            "apps/worker/src/runtime/audit/postgres_repository.py",
            "apps/worker/src/runtime/audit/postgres_repository.py",
        ]
        assert all("scripts/" not in item["path"] for item in result.data["matches"])

    def test_registry_manifest_and_l0_permission(self):
        registry = create_tool_registry()
        manifest = registry.get_manifest("workspace.search_text")
        assert manifest is not None
        assert manifest.provider == "native"
        assert manifest.risk_level_default == "L0"
        assert manifest.input_schema["required"] == ["workspace_root", "query"]
        assert "source_only" in manifest.input_schema["properties"]
        decision = PermissionManager().check(
            manifest,
            self._request("/tmp", query="RC2"),
        )
        assert decision.allowed is True
        assert decision.needs_user_approval is False


# ============================================================
# workspace.get_file_info
# ============================================================

class TestWorkspaceGetFileInfo:
    """workspace.get_file_info 有限元信息、安全边界与 L0 契约。"""

    @staticmethod
    def _request(workspace_root: str = "", path: object = "README.md") -> ToolRequest:
        return ToolRequest(
            task_id="t-info",
            run_id="r-info",
            tool_name="workspace.get_file_info",
            arguments={"workspace_root": workspace_root, "path": path},
        )

    def test_regular_file_returns_relative_bounded_metadata(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            file_path = Path(tmpdir) / "README.md"
            file_path.write_text("hello", encoding="utf-8")

            result = execute_workspace_get_file_info(
                self._request(tmpdir, "README.md")
            )

        assert result.ok is True
        assert result.kind == "json"
        assert result.data["name"] == "README.md"
        assert result.data["path"] == "README.md"
        assert result.data["type"] == "file"
        assert result.data["size_bytes"] == 5
        assert result.data["modified_at"].endswith("+00:00")
        assert "workspace_root" not in result.data
        assert "mode" not in result.data

    def test_directory_and_workspace_root_do_not_report_file_size(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            (Path(tmpdir) / "docs").mkdir()
            directory = execute_workspace_get_file_info(
                self._request(tmpdir, "docs")
            )
            root = execute_workspace_get_file_info(self._request(tmpdir, "."))

        assert directory.data["type"] == "dir"
        assert directory.data["path"] == "docs"
        assert "size_bytes" not in directory.data
        assert root.data["type"] == "dir"
        assert root.data["path"] == "."
        assert "size_bytes" not in root.data

    def test_final_symlink_reports_itself_without_following_target(self):
        with tempfile.TemporaryDirectory() as tmpdir, tempfile.TemporaryDirectory() as outside:
            outside_file = Path(outside) / "secret.txt"
            outside_file.write_text("sensitive body", encoding="utf-8")
            (Path(tmpdir) / "outside-link").symlink_to(outside_file)

            result = execute_workspace_get_file_info(
                self._request(tmpdir, "outside-link")
            )

        assert result.ok is True
        assert result.data == {
            "name": "outside-link",
            "path": "outside-link",
            "type": "symlink",
            "modified_at": result.data["modified_at"],
        }
        assert "target" not in result.data
        assert "sensitive body" not in str(result.data)

    def test_parent_symlink_is_denied(self):
        with tempfile.TemporaryDirectory() as tmpdir, tempfile.TemporaryDirectory() as outside:
            (Path(outside) / "secret.txt").write_text("outside")
            (Path(tmpdir) / "linked-dir").symlink_to(outside, target_is_directory=True)

            result = execute_workspace_get_file_info(
                self._request(tmpdir, "linked-dir/secret.txt")
            )

        assert result.ok is False
        assert result.error["code"] == "WORKSPACE_ACCESS_DENIED"

    def test_parent_replaced_with_symlink_before_open_cannot_escape(self, monkeypatch):
        with tempfile.TemporaryDirectory() as tmpdir, tempfile.TemporaryDirectory() as outside:
            child = Path(tmpdir) / "child"
            child.mkdir()
            (child / "inside.txt").write_text("inside")
            (Path(outside) / "inside.txt").write_text("outside")
            original = workspace_path_policy._open_workspace_directory_fd
            replaced = False

            def replace_before_open(root_fd, components):
                nonlocal replaced
                if components == ("child",) and not replaced:
                    replaced = True
                    (child / "inside.txt").unlink()
                    child.rmdir()
                    child.symlink_to(outside, target_is_directory=True)
                return original(root_fd, components)

            monkeypatch.setattr(
                workspace_path_policy,
                "_open_workspace_directory_fd",
                replace_before_open,
            )
            result = execute_workspace_get_file_info(
                self._request(tmpdir, "child/inside.txt")
            )

        assert result.ok is False
        assert result.error["code"] == "WORKSPACE_ACCESS_DENIED"

    @pytest.mark.parametrize("path", ["/etc/passwd", "../outside", "\x00bad"])
    def test_absolute_escape_and_nul_paths_are_denied(self, path):
        with tempfile.TemporaryDirectory() as tmpdir:
            result = execute_workspace_get_file_info(self._request(tmpdir, path))
        assert result.ok is False
        assert result.error["code"] == "WORKSPACE_ACCESS_DENIED"

    @pytest.mark.parametrize("path", ["", "   ", None, 123])
    def test_path_is_required_string(self, path):
        result = execute_workspace_get_file_info(self._request("/tmp", path))
        assert result.ok is False
        assert result.error["code"] == "TOOL_ARGUMENTS_INVALID"

    def test_missing_path_and_non_directory_parent_are_structured(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            missing = execute_workspace_get_file_info(
                self._request(tmpdir, "missing.txt")
            )
            (Path(tmpdir) / "file.txt").write_text("x")
            bad_parent = execute_workspace_get_file_info(
                self._request(tmpdir, "file.txt/child")
            )

        assert missing.error["code"] == "PATH_NOT_FOUND"
        assert bad_parent.error["code"] == "NOT_A_DIRECTORY"

    def test_generic_os_error_does_not_leak_details(self, monkeypatch):
        monkeypatch.setattr(workspace_path_policy, "_supports_safe_file_info_dir_fd", lambda: True)
        monkeypatch.setattr(
            workspace_path_policy,
            "_open_workspace_directory_fd",
            MagicMock(side_effect=OSError("sensitive absolute detail")),
        )
        result = execute_workspace_get_file_info(
            self._request("/tmp", "child/file.txt")
        )
        assert result.ok is False
        assert result.error["code"] == "GET_FILE_INFO_FAILED"
        assert "sensitive" not in result.error["message"]

    def test_registry_manifest_and_l0_permission(self):
        registry = create_tool_registry()
        manifest = registry.get_manifest("workspace.get_file_info")
        assert manifest is not None
        assert manifest.provider == "native"
        assert manifest.risk_level_default == "L0"
        assert manifest.permission_scope == "workspace"
        assert manifest.input_schema["required"] == ["workspace_root", "path"]
        assert manifest.input_schema["additionalProperties"] is False

        decision = PermissionManager().check(
            manifest,
            self._request("/tmp", "README.md"),
        )
        assert decision.allowed is True
        assert decision.needs_user_approval is False
