"""workspace.delete_path capability executor。"""

from __future__ import annotations

import errno
import logging
import os
import stat

from jarvis_worker.agent.tool_gateway.contracts import ToolRequest, ToolResult
from . import path_policy

log = logging.getLogger("jarvis_worker.tool.workspace")


def _error(code: str, message: str, *, category: str, recoverable: bool) -> ToolResult:
    return ToolResult(
        ok=False,
        kind="empty",
        summary=message,
        error={
            "code": code,
            "message": message,
            "category": category,
            "recoverable": recoverable,
        },
    )


def execute_workspace_delete_path(request: ToolRequest) -> ToolResult:
    """删除 workspace 内一个文件、符号链接或空目录。

    本工具不递归删除目录，不接受 workspace 根目录，并且删除符号链接本身而
    不跟随其 target。所有父目录均通过 dir-fd + ``O_NOFOLLOW`` 访问。
    """
    if not path_policy._supports_safe_mutation_dir_fd(os.unlink, os.rmdir):
        return _error("UNSUPPORTED_PLATFORM", "当前平台不支持安全路径删除", category="tool", recoverable=False)

    args = request.arguments or {}
    workspace_root = args.get("workspace_root")
    if not isinstance(workspace_root, str) or not workspace_root:
        return _error("WORKSPACE_ROOT_REQUIRED", "workspace_root 是必须参数", category="permission", recoverable=False)

    try:
        display_path, parent_components, entry_name = path_policy._parse_workspace_leaf_path(args.get("path"))
    except ValueError as exc:
        return _error("TOOL_ARGUMENTS_INVALID", str(exc), category="validation", recoverable=True)

    try:
        root_fd = path_policy._open_workspace_root_fd(workspace_root)
    except (OSError, NotImplementedError):
        return _error("WORKSPACE_ACCESS_DENIED", "无法安全打开 workspace 根目录", category="permission", recoverable=False)

    parent_fd: int | None = None
    try:
        try:
            parent_fd = path_policy._open_workspace_directory_fd(root_fd, parent_components)
        except FileNotFoundError:
            return _error("PATH_NOT_FOUND", "目标路径的父目录不存在", category="validation", recoverable=True)
        except (path_policy._DirectorySymlinkError, NotADirectoryError, OSError):
            return _error("WORKSPACE_ACCESS_DENIED", "无法安全访问目标父目录", category="permission", recoverable=False)

        try:
            entry_stat = os.stat(entry_name, dir_fd=parent_fd, follow_symlinks=False)
        except FileNotFoundError:
            return _error("PATH_NOT_FOUND", "目标路径不存在", category="validation", recoverable=True)
        except OSError:
            return _error("WORKSPACE_ACCESS_DENIED", "无法安全访问目标路径", category="permission", recoverable=False)

        if stat.S_ISDIR(entry_stat.st_mode):
            entry_type = "directory"
            try:
                os.rmdir(entry_name, dir_fd=parent_fd)
            except OSError as exc:
                if exc.errno in (errno.ENOTEMPTY, errno.EEXIST):
                    return _error("DIRECTORY_NOT_EMPTY", "目录非空；workspace.delete_path 不执行递归删除", category="validation", recoverable=True)
                if exc.errno == errno.ENOENT:
                    return _error("PATH_NOT_FOUND", "目标路径已不存在", category="validation", recoverable=True)
                if exc.errno == errno.EPERM:
                    return _error("PERMISSION_DENIED", "没有权限删除该目录", category="permission", recoverable=True)
                return _error("DELETE_PATH_FAILED", "无法删除目录", category="tool", recoverable=True)
        elif stat.S_ISREG(entry_stat.st_mode) or stat.S_ISLNK(entry_stat.st_mode):
            entry_type = "symlink" if stat.S_ISLNK(entry_stat.st_mode) else "file"
            try:
                os.unlink(entry_name, dir_fd=parent_fd)
            except FileNotFoundError:
                return _error("PATH_NOT_FOUND", "目标路径已不存在", category="validation", recoverable=True)
            except PermissionError:
                return _error("PERMISSION_DENIED", "没有权限删除该路径", category="permission", recoverable=True)
            except OSError:
                return _error("DELETE_PATH_FAILED", "无法删除路径", category="tool", recoverable=True)
        else:
            return _error("UNSUPPORTED_PATH_TYPE", "仅支持删除普通文件、符号链接或空目录", category="validation", recoverable=True)

        log.info("workspace.delete_path: path=%s type=%s", display_path, entry_type)
        return ToolResult(
            ok=True,
            kind="json",
            summary=f"Deleted {entry_type}: {display_path}",
            data={"deleted": True, "path": display_path, "type": entry_type},
        )
    finally:
        if parent_fd is not None:
            try:
                os.close(parent_fd)
            except OSError:
                pass
        try:
            os.close(root_fd)
        except OSError:
            pass
