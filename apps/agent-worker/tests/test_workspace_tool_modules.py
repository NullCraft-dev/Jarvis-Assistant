"""Workspace capability tool package 的结构边界回归测试。"""

from __future__ import annotations

import jarvis_worker.agent.tools.workspace as workspace
from jarvis_worker.agent.tools.workspace import (
    create_directory,
    delete_path,
    get_file_info,
    move_path,
    path_policy,
    search_files,
    search_text,
)


def test_workspace_facade_exports_only_stable_executors() -> None:
    assert workspace.__all__ == [
        "execute_workspace_create_directory",
        "execute_workspace_create_file",
        "execute_workspace_delete_path",
        "execute_workspace_get_file_info",
        "execute_workspace_list_files",
        "execute_workspace_move_path",
        "execute_workspace_read_file",
        "execute_workspace_read_files",
        "execute_workspace_search_files",
        "execute_workspace_search_text",
    ]


def test_each_executor_is_owned_by_its_tool_module() -> None:
    expected_modules = {
        "execute_workspace_create_directory": "agent.tools.workspace.create_directory",
        "execute_workspace_create_file": "agent.tools.workspace.create_file",
        "execute_workspace_delete_path": "agent.tools.workspace.delete_path",
        "execute_workspace_get_file_info": "agent.tools.workspace.get_file_info",
        "execute_workspace_list_files": "agent.tools.workspace.list_files",
        "execute_workspace_move_path": "agent.tools.workspace.move_path",
        "execute_workspace_read_file": "agent.tools.workspace.read_file",
        "execute_workspace_read_files": "agent.tools.workspace.read_files",
        "execute_workspace_search_files": "agent.tools.workspace.search_files",
        "execute_workspace_search_text": "agent.tools.workspace.search_text",
    }

    for name, module_suffix in expected_modules.items():
        executor = getattr(workspace, name)
        assert executor.__module__.endswith(module_suffix)


def test_fd_based_tools_share_one_path_policy_owner() -> None:
    assert create_directory.path_policy is path_policy
    assert delete_path.path_policy is path_policy
    assert get_file_info.path_policy is path_policy
    assert move_path.path_policy is path_policy
    assert search_files.path_policy is path_policy
    assert search_text.path_policy is path_policy
