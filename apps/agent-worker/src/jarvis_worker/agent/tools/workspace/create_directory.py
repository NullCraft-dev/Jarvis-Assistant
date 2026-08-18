"""workspace.create_directory capability executor。"""

from __future__ import annotations

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


def execute_workspace_create_directory(request: ToolRequest) -> ToolResult:
    """在 workspace 内创建一个新空目录，不递归创建父目录。

    路径的每个父组件都从可信 workspace root fd 开始以 ``O_NOFOLLOW``
    打开。目录创建使用 parent dir-fd，目标已存在时绝不覆盖或合并。
    """
    if not path_policy._supports_safe_mutation_dir_fd(os.mkdir):
        return _error("UNSUPPORTED_PLATFORM", "当前平台不支持安全目录创建", category="tool", recoverable=False)

    args = request.arguments or {}
    workspace_root = args.get("workspace_root")
    if not isinstance(workspace_root, str) or not workspace_root:
        return _error("WORKSPACE_ROOT_REQUIRED", "workspace_root 是必须参数", category="permission", recoverable=False)

    try:
        display_path, parent_components, directory_name = path_policy._parse_workspace_leaf_path(args.get("path"))
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
            return _error("PARENT_DIR_NOT_FOUND", "父目录不存在，workspace.create_directory 不会自动创建目录", category="validation", recoverable=True)
        except (path_policy._DirectorySymlinkError, NotADirectoryError, OSError):
            return _error("WORKSPACE_ACCESS_DENIED", "无法安全访问父目录", category="permission", recoverable=False)

        try:
            os.mkdir(directory_name, mode=0o755, dir_fd=parent_fd)
        except FileExistsError:
            try:
                target_stat = os.stat(directory_name, dir_fd=parent_fd, follow_symlinks=False)
            except OSError:
                return _error("PATH_ALREADY_EXISTS", "目标路径已存在，不会覆盖", category="validation", recoverable=True)
            if stat.S_ISLNK(target_stat.st_mode):
                return _error("WORKSPACE_ACCESS_DENIED", "目标路径是符号链接，拒绝创建", category="permission", recoverable=False)
            return _error("PATH_ALREADY_EXISTS", "目标路径已存在，workspace.create_directory 不会合并或覆盖", category="validation", recoverable=True)
        except PermissionError:
            return _error("PERMISSION_DENIED", "没有权限在目标位置创建目录", category="permission", recoverable=True)
        except OSError:
            return _error("CREATE_DIRECTORY_FAILED", "无法创建目录", category="tool", recoverable=True)

        log.info("workspace.create_directory: path=%s", display_path)
        return ToolResult(
            ok=True,
            kind="json",
            summary=f"Created directory: {display_path}",
            data={"created": True, "path": display_path, "type": "directory"},
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
