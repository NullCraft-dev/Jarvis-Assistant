"""workspace.get_file_info capability executor。"""

from __future__ import annotations

import logging
import os
import stat
from datetime import datetime, timezone

from jarvis_worker.agent.tool_gateway.contracts import ToolRequest, ToolResult

from . import path_policy

log = logging.getLogger("jarvis_worker.tool.workspace")

# ============================================================
# workspace.get_file_info — L0 有限元信息查询
# ============================================================


def _file_info_type(mode: int) -> str:
    """把 lstat mode 映射为稳定的公开类型。"""
    if stat.S_ISREG(mode):
        return "file"
    if stat.S_ISDIR(mode):
        return "dir"
    if stat.S_ISLNK(mode):
        return "symlink"
    return "other"


def _file_info_error(exc: BaseException) -> ToolResult:
    """把有限元信息查询错误映射为稳定、无系统详情的 ToolResult。"""
    if isinstance(exc, FileNotFoundError):
        return ToolResult(
            ok=False, kind="empty", summary="路径不存在",
            error={"code": "PATH_NOT_FOUND", "message": "请求的路径不存在",
                   "category": "tool", "recoverable": True},
        )
    if isinstance(exc, NotADirectoryError):
        return ToolResult(
            ok=False, kind="empty", summary="路径组件不是目录",
            error={"code": "NOT_A_DIRECTORY", "message": "路径中包含非目录组件",
                   "category": "validation", "recoverable": True},
        )
    if isinstance(exc, PermissionError):
        return ToolResult(
            ok=False, kind="empty", summary="权限不足，无法获取路径信息",
            error={"code": "PERMISSION_DENIED", "message": "没有权限获取路径信息",
                   "category": "permission", "recoverable": True},
        )
    if isinstance(exc, path_policy._DirectorySymlinkError):
        return ToolResult(
            ok=False, kind="empty", summary="拒绝符号链接目录组件",
            error={"code": "WORKSPACE_ACCESS_DENIED",
                   "message": "路径中包含符号链接目录，拒绝访问",
                   "category": "permission", "recoverable": False},
        )
    return ToolResult(
        ok=False, kind="empty", summary="获取路径信息失败",
        error={"code": "GET_FILE_INFO_FAILED",
               "message": "获取路径信息时发生系统错误",
               "category": "tool", "recoverable": True},
    )


def execute_workspace_get_file_info(request: ToolRequest) -> ToolResult:
    """获取 workspace 内单个路径的有限元信息，不读取正文、不跟随 symlink。

    arguments 契约：
        - workspace_root: str（必须）由 AgentRunner 从可信状态注入
        - path: str（必须）相对 workspace_root 的路径；`.` 表示根目录

    公开结果只包含 name/path/type/modified_at，以及普通文件的 size_bytes。
    """
    if not path_policy._supports_safe_file_info_dir_fd():
        return ToolResult(
            ok=False, kind="empty", summary="当前平台不支持安全元信息查询",
            error={"code": "UNSUPPORTED_PLATFORM",
                   "message": "当前平台不支持 dir_fd 安全元信息查询",
                   "category": "tool", "recoverable": False},
        )

    args = request.arguments if request.arguments else {}
    workspace_root = args.get("workspace_root", "")
    if not isinstance(workspace_root, str) or not workspace_root:
        return ToolResult(
            ok=False, kind="empty", summary="workspace_root 缺失",
            error={"code": "WORKSPACE_ROOT_REQUIRED",
                   "message": "workspace_root 是必须参数",
                   "category": "permission", "recoverable": False},
        )

    path_raw = args.get("path", "")
    if not isinstance(path_raw, str) or not path_raw.strip():
        return ToolResult(
            ok=False, kind="empty", summary="path 参数缺失",
            error={"code": "TOOL_ARGUMENTS_INVALID",
                   "message": "path 是必须参数，请提供 workspace 相对路径",
                   "category": "validation", "recoverable": True},
        )
    path = path_raw.strip()
    if os.path.isabs(path):
        return ToolResult(
            ok=False, kind="empty", summary="拒绝访问绝对路径",
            error={"code": "WORKSPACE_ACCESS_DENIED",
                   "message": "不允许使用绝对路径，请使用 workspace 相对路径",
                   "category": "permission", "recoverable": False},
        )

    try:
        canonical_path, components = path_policy._normalize_workspace_path(path)
    except ValueError:
        return ToolResult(
            ok=False, kind="empty", summary="拒绝访问 workspace 外路径",
            error={"code": "WORKSPACE_ACCESS_DENIED",
                   "message": "请求的路径不在允许的 workspace 范围内",
                   "category": "permission", "recoverable": False},
        )

    try:
        real_root = os.path.realpath(workspace_root)
        root_fd = os.open(
            real_root,
            os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
        )
    except (OSError, ValueError, NotImplementedError):
        return ToolResult(
            ok=False, kind="empty", summary="无法打开 workspace 根目录",
            error={"code": "WORKSPACE_ACCESS_DENIED",
                   "message": "无法安全打开 workspace 根目录",
                   "category": "permission", "recoverable": False},
        )

    parent_fd: int | None = None
    try:
        if not components:
            target_stat = os.fstat(root_fd)
            name = os.path.basename(real_root) or "."
        else:
            parent_fd = path_policy._open_workspace_directory_fd(root_fd, components[:-1])
            name = components[-1]
            target_stat = os.stat(
                name,
                dir_fd=parent_fd,
                follow_symlinks=False,
            )
    except (OSError, NotImplementedError) as exc:
        return _file_info_error(exc)
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

    entry_type = _file_info_type(target_stat.st_mode)
    try:
        modified_at = datetime.fromtimestamp(
            target_stat.st_mtime, tz=timezone.utc,
        ).isoformat()
    except (OverflowError, OSError, ValueError):
        modified_at = None

    data: dict[str, object] = {
        "name": name,
        "path": canonical_path,
        "type": entry_type,
    }
    if entry_type == "file":
        data["size_bytes"] = target_stat.st_size
    if modified_at is not None:
        data["modified_at"] = modified_at

    size_summary = (
        f", {target_stat.st_size} bytes" if entry_type == "file" else ""
    )
    log.info(
        "workspace.get_file_info: path=%s type=%s size=%s",
        canonical_path,
        entry_type,
        target_stat.st_size if entry_type == "file" else "-",
    )
    return ToolResult(
        ok=True,
        kind="json",
        summary=f"Path info: {canonical_path} ({entry_type}{size_summary})",
        data=data,
    )
