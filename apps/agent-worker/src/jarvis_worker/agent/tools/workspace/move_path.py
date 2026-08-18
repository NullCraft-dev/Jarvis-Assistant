"""workspace.move_path capability executor。"""

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


def _entry_type(mode: int) -> str | None:
    if stat.S_ISREG(mode):
        return "file"
    if stat.S_ISDIR(mode):
        return "directory"
    if stat.S_ISLNK(mode):
        return "symlink"
    return None


def execute_workspace_move_path(request: ToolRequest) -> ToolResult:
    """在 workspace 内原子移动文件、空/非空目录或符号链接。

    不支持跨 workspace/cross-device copy fallback；目标必须不存在。移动操作只使用
    平台 no-replace rename 原语，避免 ``os.rename`` 默认覆盖目标的行为。
    """
    if not path_policy._supports_safe_mutation_dir_fd(os.rename):
        return _error("UNSUPPORTED_PLATFORM", "当前平台不支持安全路径移动", category="tool", recoverable=False)

    args = request.arguments or {}
    workspace_root = args.get("workspace_root")
    if not isinstance(workspace_root, str) or not workspace_root:
        return _error("WORKSPACE_ROOT_REQUIRED", "workspace_root 是必须参数", category="permission", recoverable=False)

    try:
        source_path, source_parent_components, source_name = path_policy._parse_workspace_leaf_path(args.get("source_path"))
        destination_path, destination_parent_components, destination_name = path_policy._parse_workspace_leaf_path(args.get("destination_path"))
    except ValueError as exc:
        return _error("TOOL_ARGUMENTS_INVALID", str(exc), category="validation", recoverable=True)

    if source_path == destination_path:
        return _error("SOURCE_DESTINATION_SAME", "源路径与目标路径相同，无需移动", category="validation", recoverable=True)

    try:
        root_fd = path_policy._open_workspace_root_fd(workspace_root)
    except (OSError, NotImplementedError):
        return _error("WORKSPACE_ACCESS_DENIED", "无法安全打开 workspace 根目录", category="permission", recoverable=False)

    source_parent_fd: int | None = None
    destination_parent_fd: int | None = None
    try:
        try:
            source_parent_fd = path_policy._open_workspace_directory_fd(root_fd, source_parent_components)
        except FileNotFoundError:
            return _error("PATH_NOT_FOUND", "源路径的父目录不存在", category="validation", recoverable=True)
        except (path_policy._DirectorySymlinkError, NotADirectoryError, OSError):
            return _error("WORKSPACE_ACCESS_DENIED", "无法安全访问源路径父目录", category="permission", recoverable=False)

        try:
            destination_parent_fd = path_policy._open_workspace_directory_fd(root_fd, destination_parent_components)
        except FileNotFoundError:
            return _error("PARENT_DIR_NOT_FOUND", "目标父目录不存在，workspace.move_path 不会自动创建目录", category="validation", recoverable=True)
        except (path_policy._DirectorySymlinkError, NotADirectoryError, OSError):
            return _error("WORKSPACE_ACCESS_DENIED", "无法安全访问目标父目录", category="permission", recoverable=False)

        try:
            source_stat = os.stat(source_name, dir_fd=source_parent_fd, follow_symlinks=False)
        except FileNotFoundError:
            return _error("PATH_NOT_FOUND", "源路径不存在", category="validation", recoverable=True)
        except OSError:
            return _error("WORKSPACE_ACCESS_DENIED", "无法安全访问源路径", category="permission", recoverable=False)

        source_type = _entry_type(source_stat.st_mode)
        if source_type is None:
            return _error("UNSUPPORTED_PATH_TYPE", "仅支持移动普通文件、目录或符号链接", category="validation", recoverable=True)

        source_components = source_parent_components + (source_name,)
        destination_components = destination_parent_components + (destination_name,)
        if source_type == "directory" and destination_components[:len(source_components)] == source_components:
            return _error("INVALID_MOVE", "不能将目录移动到自身或其子目录中", category="validation", recoverable=True)

        try:
            os.stat(destination_name, dir_fd=destination_parent_fd, follow_symlinks=False)
        except FileNotFoundError:
            pass
        except OSError:
            return _error("WORKSPACE_ACCESS_DENIED", "无法安全访问目标路径", category="permission", recoverable=False)
        else:
            return _error("PATH_ALREADY_EXISTS", "目标路径已存在，workspace.move_path 不覆盖已有路径", category="validation", recoverable=True)

        try:
            path_policy._rename_no_replace(
                source_parent_fd,
                source_name,
                destination_parent_fd,
                destination_name,
            )
        except NotImplementedError:
            return _error("UNSUPPORTED_PLATFORM", "当前平台不支持不覆盖目标的安全路径移动", category="tool", recoverable=False)
        except OSError as exc:
            if exc.errno == errno.EEXIST:
                return _error("PATH_ALREADY_EXISTS", "目标路径已存在，workspace.move_path 不覆盖已有路径", category="validation", recoverable=True)
            if exc.errno == errno.ENOENT:
                return _error("PATH_NOT_FOUND", "源路径已不存在或父目录已变化", category="validation", recoverable=True)
            if exc.errno in (errno.EINVAL, errno.ENOTEMPTY):
                return _error("INVALID_MOVE", "该移动会使目录结构无效", category="validation", recoverable=True)
            if exc.errno == errno.EXDEV:
                return _error("CROSS_DEVICE_MOVE_NOT_SUPPORTED", "不支持跨设备移动，未执行复制或删除", category="validation", recoverable=True)
            return _error("MOVE_PATH_FAILED", "无法移动路径", category="tool", recoverable=True)

        log.info("workspace.move_path: source=%s destination=%s type=%s", source_path, destination_path, source_type)
        return ToolResult(
            ok=True,
            kind="json",
            summary=f"Moved {source_type}: {source_path} -> {destination_path}",
            data={
                "moved": True,
                "source_path": source_path,
                "destination_path": destination_path,
                "type": source_type,
            },
        )
    finally:
        if destination_parent_fd is not None:
            try:
                os.close(destination_parent_fd)
            except OSError:
                pass
        if source_parent_fd is not None:
            try:
                os.close(source_parent_fd)
            except OSError:
                pass
        try:
            os.close(root_fd)
        except OSError:
            pass
