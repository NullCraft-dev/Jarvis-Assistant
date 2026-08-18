"""Workspace 写入结构工具的安全与权限契约测试。"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

import pytest

from jarvis_worker.bootstrap.tool_registry import create_tool_registry
from jarvis_worker.agent.permissions.manager import PermissionManager
from jarvis_worker.agent.tool_gateway.gateway import ToolGateway
from jarvis_worker.agent.tools.workspace import (
    execute_workspace_create_directory,
    execute_workspace_delete_path,
    execute_workspace_move_path,
)
from jarvis_worker.agent.tool_gateway.contracts import PermissionApproval, ToolRequest


def _request(tool_name: str, workspace_root: str, **arguments: object) -> ToolRequest:
    return ToolRequest(
        task_id="task-1",
        run_id="run-1",
        tool_name=tool_name,
        arguments={"workspace_root": workspace_root, **arguments},
    )


class TestWorkspaceCreateDirectory:
    def test_creates_new_directory_and_returns_relative_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            (Path(tmpdir) / "parent").mkdir()
            result = execute_workspace_create_directory(
                _request("workspace.create_directory", tmpdir, path="parent/notes")
            )

            assert result.ok is True
            assert (Path(tmpdir) / "parent" / "notes").is_dir()
            assert result.data == {"created": True, "path": "parent/notes", "type": "directory"}

    def test_does_not_merge_existing_path_or_create_missing_parent(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            (Path(tmpdir) / "exists").mkdir()
            exists = execute_workspace_create_directory(
                _request("workspace.create_directory", tmpdir, path="exists")
            )
            missing_parent = execute_workspace_create_directory(
                _request("workspace.create_directory", tmpdir, path="missing/new")
            )

            assert exists.error["code"] == "PATH_ALREADY_EXISTS"
            assert missing_parent.error["code"] == "PARENT_DIR_NOT_FOUND"
            assert not (Path(tmpdir) / "missing").exists()

    @pytest.mark.parametrize("path", [".", "../escape", "/tmp/escape"])
    def test_rejects_root_and_escape_paths(self, path: str) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            result = execute_workspace_create_directory(
                _request("workspace.create_directory", tmpdir, path=path)
            )
            assert result.ok is False
            assert result.error["code"] == "TOOL_ARGUMENTS_INVALID"

    def test_rejects_symlink_parent(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir, tempfile.TemporaryDirectory() as outside:
            try:
                (Path(tmpdir) / "link").symlink_to(outside, target_is_directory=True)
            except (OSError, NotImplementedError):
                pytest.skip("当前平台不支持 symlink")

            result = execute_workspace_create_directory(
                _request("workspace.create_directory", tmpdir, path="link/new")
            )
            assert result.error["code"] == "WORKSPACE_ACCESS_DENIED"
            assert not (Path(outside) / "new").exists()


class TestWorkspaceMovePath:
    def test_moves_file_without_overwriting_target(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            (root / "archive").mkdir()
            (root / "draft.txt").write_text("draft")

            result = execute_workspace_move_path(
                _request("workspace.move_path", tmpdir, source_path="draft.txt", destination_path="archive/final.txt")
            )

            assert result.ok is True
            assert not (root / "draft.txt").exists()
            assert (root / "archive" / "final.txt").read_text() == "draft"
            assert result.data["type"] == "file"

            (root / "second.txt").write_text("second")
            existing = execute_workspace_move_path(
                _request("workspace.move_path", tmpdir, source_path="second.txt", destination_path="archive/final.txt")
            )
            assert existing.error["code"] == "PATH_ALREADY_EXISTS"
            assert (root / "second.txt").read_text() == "second"
            assert (root / "archive" / "final.txt").read_text() == "draft"

    def test_rejects_root_missing_parent_and_directory_self_descendant(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            (root / "folder").mkdir()
            root_move = execute_workspace_move_path(
                _request("workspace.move_path", tmpdir, source_path=".", destination_path="renamed")
            )
            missing_parent = execute_workspace_move_path(
                _request("workspace.move_path", tmpdir, source_path="folder", destination_path="missing/folder")
            )
            descendant = execute_workspace_move_path(
                _request("workspace.move_path", tmpdir, source_path="folder", destination_path="folder/child")
            )

            assert root_move.error["code"] == "TOOL_ARGUMENTS_INVALID"
            assert missing_parent.error["code"] == "PARENT_DIR_NOT_FOUND"
            assert descendant.error["code"] == "INVALID_MOVE"
            assert (root / "folder").is_dir()

    def test_moves_symlink_itself_without_following_target(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir, tempfile.TemporaryDirectory() as outside:
            root = Path(tmpdir)
            outside_file = Path(outside) / "outside.txt"
            outside_file.write_text("safe")
            try:
                (root / "link.txt").symlink_to(outside_file)
            except (OSError, NotImplementedError):
                pytest.skip("当前平台不支持 symlink")

            result = execute_workspace_move_path(
                _request("workspace.move_path", tmpdir, source_path="link.txt", destination_path="moved-link.txt")
            )
            assert result.ok is True
            assert (root / "moved-link.txt").is_symlink()
            assert outside_file.read_text() == "safe"


class TestWorkspaceDeletePath:
    def test_deletes_file_and_empty_directory_only(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            (root / "file.txt").write_text("remove")
            (root / "empty").mkdir()
            (root / "nonempty").mkdir()
            (root / "nonempty" / "child.txt").write_text("keep")

            deleted_file = execute_workspace_delete_path(
                _request("workspace.delete_path", tmpdir, path="file.txt")
            )
            deleted_directory = execute_workspace_delete_path(
                _request("workspace.delete_path", tmpdir, path="empty")
            )
            nonempty = execute_workspace_delete_path(
                _request("workspace.delete_path", tmpdir, path="nonempty")
            )

            assert deleted_file.ok is True
            assert deleted_file.data["type"] == "file"
            assert deleted_directory.ok is True
            assert deleted_directory.data["type"] == "directory"
            assert nonempty.error["code"] == "DIRECTORY_NOT_EMPTY"
            assert (root / "nonempty" / "child.txt").read_text() == "keep"

    def test_deletes_symlink_itself_and_rejects_root(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir, tempfile.TemporaryDirectory() as outside:
            root = Path(tmpdir)
            outside_file = Path(outside) / "outside.txt"
            outside_file.write_text("safe")
            try:
                (root / "link.txt").symlink_to(outside_file)
            except (OSError, NotImplementedError):
                pytest.skip("当前平台不支持 symlink")

            deleted = execute_workspace_delete_path(
                _request("workspace.delete_path", tmpdir, path="link.txt")
            )
            root_delete = execute_workspace_delete_path(
                _request("workspace.delete_path", tmpdir, path=".")
            )
            assert deleted.ok is True
            assert deleted.data["type"] == "symlink"
            assert outside_file.read_text() == "safe"
            assert root_delete.error["code"] == "TOOL_ARGUMENTS_INVALID"


class TestWorkspaceMutationRegistryAndPermissions:
    @pytest.mark.parametrize(
        ("tool_name", "risk", "arguments"),
        [
            ("workspace.create_directory", "L2", {"path": "notes"}),
            ("workspace.move_path", "L3", {"source_path": "a", "destination_path": "b"}),
            ("workspace.delete_path", "L4", {"path": "obsolete.txt"}),
        ],
    )
    def test_manifest_and_permission_are_explicitly_confirmed(self, tool_name, risk, arguments) -> None:
        registry = create_tool_registry()
        manifest = registry.get_manifest(tool_name)
        assert manifest is not None
        assert manifest.risk_level_default == risk
        assert manifest.allowed_decisions == ["allow_once", "deny"]
        check = PermissionManager().check(manifest, _request(tool_name, "/tmp", **arguments))
        assert check.needs_user_approval is True
        assert check.allowed_decisions == ["allow_once", "deny"]

    def test_delete_requires_gateway_approval_before_executor_runs(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            target = Path(tmpdir) / "obsolete.txt"
            target.write_text("remove")
            gateway = ToolGateway(create_tool_registry())
            request = _request("workspace.delete_path", tmpdir, path="obsolete.txt")

            pending = gateway.execute(request)
            assert pending.error["code"] == "PERMISSION_REQUIRED"
            assert target.exists()

            approved = gateway.execute(
                request,
                approval=PermissionApproval(request_id="permission-1", decision="allow_once"),
            )

            assert approved.ok is True
            assert not target.exists()
