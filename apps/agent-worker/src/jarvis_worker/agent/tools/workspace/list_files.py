"""workspace.list_files capability executor。"""

from __future__ import annotations

import logging
import os
import stat
from datetime import datetime, timezone

from jarvis_worker.agent.tool_gateway.contracts import ToolRequest, ToolResult

from . import path_policy
from .path_suggestions import find_path_suggestions

log = logging.getLogger("jarvis_worker.tool.workspace")

_MAX_ENTRIES = 100

def _get_file_type(path: str) -> str:
    """获取文件类型：file / dir / symlink / unknown"""
    try:
        st = os.lstat(path)
        if stat.S_ISDIR(st.st_mode):
            return "dir"
        elif stat.S_ISLNK(st.st_mode):
            return "symlink"
        elif stat.S_ISREG(st.st_mode):
            return "file"
        else:
            return "unknown"
    except OSError:
        return "unknown"

def _get_size(path: str, entry_type: str) -> int | None:
    """获取文件大小（字节），目录返回 None。"""
    if entry_type == "dir":
        return None
    try:
        return os.path.getsize(path)
    except OSError:
        return None


def _get_modified_at(path: str) -> str | None:
    """获取最后修改时间（ISO 8601）。"""
    try:
        st = os.stat(path)
        return datetime.fromtimestamp(st.st_mtime, tz=timezone.utc).isoformat()
    except OSError:
        return None

def execute_workspace_list_files(request: ToolRequest) -> ToolResult:
    """执行 workspace.list_files。

    arguments 契约：
        - workspace_root: str（**必须**）允许访问的 workspace 根目录
        - path: str（可选，默认 "."）相对 workspace_root 的子路径

    安全策略：
        - workspace_root 缺失 → fail closed (WORKSPACE_ROOT_REQUIRED)
        - 所有 target_path 经 realpath + commonpath 校验
        - 绝对路径 /etc、../ 穿越、symlink 逃逸 → WORKSPACE_ACCESS_DENIED

    Returns:
        ToolResult 包含文件列表或错误信息
    """
    args = request.arguments if request.arguments else {}

    # 1. 获取 workspace_root（必须）
    workspace_root: str = args.get("workspace_root", "")
    if not workspace_root or not isinstance(workspace_root, str):
        return ToolResult(
            ok=False,
            kind="empty",
            summary="workspace_root 缺失，无法确定允许访问的根目录",
            error={
                "code": "WORKSPACE_ROOT_REQUIRED",
                "message": "workspace_root 是必须参数，请配置 JARVIS_WORKSPACE_ROOT 或传入 workspace_path",
                "category": "permission",
                "recoverable": False,
            },
        )

    # 2. 获取 path（默认 "." 表示 workspace root 本身）
    path: str = args.get("path", ".")
    if not isinstance(path, str):
        path = "."
    # 空 path 也视为 "."
    if path.strip() == "":
        path = "."

    # 3. 安全解析 target_path
    try:
        target_path = path_policy._resolve_safe_target(workspace_root, path)
    except ValueError as e:
        log.warning("workspace.list_files 边界拒绝: %s", e)
        return ToolResult(
            ok=False,
            kind="empty",
            summary="拒绝访问: 路径超出 workspace 范围",
            error={
                "code": "WORKSPACE_ACCESS_DENIED",
                "message": "请求的路径不在允许的 workspace 范围内",
                "category": "permission",
                "recoverable": False,
            },
        )
    except OSError as e:
        log.error("workspace.list_files 路径解析失败: %s", e)
        return ToolResult(
            ok=False,
            kind="empty",
            summary="路径解析失败",
            error={
                "code": "PATH_RESOLVE_ERROR",
                "message": "无法解析目标路径",
                "category": "tool",
                "recoverable": True,
            },
        )

    # 4. 检查路径是否存在
    if not os.path.exists(target_path):
        suggestions = find_path_suggestions(
            workspace_root=workspace_root,
            requested_path=path,
            expected_type="dir",
        )
        return ToolResult(
            ok=False,
            kind="empty",
            summary=f"路径不存在: {os.path.basename(target_path)}",
            data={
                "requested_path": path,
                "suggested_paths": suggestions,
            },
            error={
                "code": "PATH_NOT_FOUND",
                "message": "请求的路径不存在",
                "category": "tool",
                "recoverable": True,
            },
        )

    if not os.path.isdir(target_path):
        return ToolResult(
            ok=False,
            kind="empty",
            summary=f"路径不是目录: {os.path.basename(target_path)}",
            error={
                "code": "NOT_A_DIRECTORY",
                "message": "请求的路径不是目录",
                "category": "tool",
                "recoverable": True,
            },
        )

    # 5. 列目录
    try:
        entries_raw = os.listdir(target_path)
    except PermissionError:
        return ToolResult(
            ok=False,
            kind="empty",
            summary="权限不足，无法列出目录",
            error={
                "code": "PERMISSION_DENIED",
                "message": "没有权限访问该目录",
                "category": "permission",
                "recoverable": True,
            },
        )
    except OSError as e:
        return ToolResult(
            ok=False,
            kind="empty",
            summary=f"列出目录失败: {e}",
            error={
                "code": "LIST_DIR_FAILED",
                "message": "列出目录时发生系统错误",
                "category": "tool",
                "recoverable": True,
            },
        )

    # 6. 排序（目录优先，同类型按名称排序）
    entries_raw.sort(key=lambda n: (not os.path.isdir(os.path.join(target_path, n)), n.lower()))

    # 7. 构建结构化结果（不递归，只顶层；最多 100 条）
    entries: list[dict] = []
    for name in entries_raw:
        if path_policy._is_excluded(name):
            continue
        if len(entries) >= _MAX_ENTRIES:
            break

        full_path = os.path.join(target_path, name)
        entry_type = _get_file_type(full_path)

        entry: dict = {
            "name": name,
            "path": full_path,
            "type": entry_type,
        }
        size = _get_size(full_path, entry_type)
        if size is not None:
            entry["size"] = size
        mtime = _get_modified_at(full_path)
        if mtime is not None:
            entry["modified_at"] = mtime

        entries.append(entry)

    # 8. 构造结果摘要
    total_found = len(entries_raw)
    visible = len(entries)
    excluded = sum(1 for n in entries_raw if path_policy._is_excluded(n))
    truncated = (total_found - excluded) > _MAX_ENTRIES

    summary_parts = [f"workspace 目录列表 ({os.path.basename(target_path)})"]
    summary_parts.append(f"{visible} 条可见条目")
    if excluded > 0:
        summary_parts.append(f"（已过滤 {excluded} 个隐藏/噪声目录）")
    if truncated:
        summary_parts.append(f"（超出 {_MAX_ENTRIES} 条上限，已截断）")

    log.info(
        "workspace.list_files: root=%s path=%s target=%s total=%d visible=%d excluded=%d",
        workspace_root,
        path,
        target_path,
        total_found,
        visible,
        excluded,
    )

    return ToolResult(
        ok=True,
        kind="json",
        summary="; ".join(summary_parts),
        data={
            "workspace_path": target_path,
            "workspace_root": workspace_root,
            "total_entries": total_found,
            "visible_entries": visible,
            "excluded_entries": excluded,
            "truncated": truncated,
            "entries": entries,
        },
    )
