"""workspace.read_file capability executor。"""

from __future__ import annotations

import logging
import os

from jarvis_worker.agent.tool_gateway.contracts import ToolRequest, ToolResult

from . import path_policy
from .path_suggestions import find_path_suggestions

log = logging.getLogger("jarvis_worker.tool.workspace")

# ============================================================
# workspace.read_file — Phase 6A
# ============================================================

# 默认最大字节数
_DEFAULT_MAX_BYTES = 65536
# 最大允许字节数（安全上限）
_ABSOLUTE_MAX_BYTES = 262144
# 默认最大字符数
_DEFAULT_MAX_CHARS = 20000
# 最大允许字符数（安全上限）
_ABSOLUTE_MAX_CHARS = 100000
# 单次范围读取的最大行数
_ABSOLUTE_MAX_LINES = 1000


def execute_workspace_read_file(request: ToolRequest) -> ToolResult:
    """执行 workspace.read_file — 安全读取 workspace 内单个文本文件。

    arguments 契约：
        - workspace_root: str（**必须**）允许访问的 workspace 根目录
        - path: str（**必须**）相对 workspace_root 的文件路径
        - max_bytes: int（可选，默认 65536，最大 262144）文件大小上限
        - max_chars: int（可选，默认 20000，最大 100000）字符数上限（支持截断）

    安全策略：
        - workspace_root 缺失 → fail closed (WORKSPACE_ROOT_REQUIRED)
        - path 必须是相对路径，禁止绝对路径
        - 所有 target_path 经 realpath + commonpath 校验
        - 禁止 ../ 穿越、禁止 symlink 逃逸
        - 目录不能读取 → PATH_IS_DIRECTORY
        - 文件不存在 → FILE_NOT_FOUND
        - 文件超过 max_bytes → FILE_TOO_LARGE
        - 二进制/非法 UTF-8 → UNSUPPORTED_FILE_TYPE
        - 字符数超过 max_chars → 截断返回

    Returns:
        ToolResult 包含文件内容或错误信息
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

    # 2. 获取并校验 path（必须，且必须是相对路径）
    path_raw = args.get("path", "")
    if not path_raw or not isinstance(path_raw, str) or not path_raw.strip():
        return ToolResult(
            ok=False,
            kind="empty",
            summary="path 参数缺失",
            error={
                "code": "FILE_NOT_FOUND",
                "message": "path 是必须参数，请提供相对文件路径",
                "category": "validation",
                "recoverable": True,
            },
        )
    path = path_raw.strip()

    # 拒绝绝对路径
    if os.path.isabs(path):
        log.warning("workspace.read_file 拒绝绝对路径: path=%s", path)
        return ToolResult(
            ok=False,
            kind="empty",
            summary="拒绝访问: 不允许绝对路径",
            error={
                "code": "WORKSPACE_ACCESS_DENIED",
                "message": "不允许使用绝对路径，请使用相对 workspace_root 的路径",
                "category": "permission",
                "recoverable": True,
            },
        )

    # 3. 获取大小限制参数
    try:
        max_bytes_raw = args.get("max_bytes", _DEFAULT_MAX_BYTES)
        max_bytes = int(max_bytes_raw) if max_bytes_raw else _DEFAULT_MAX_BYTES
    except (ValueError, TypeError):
        max_bytes = _DEFAULT_MAX_BYTES
    max_bytes = max(1, min(max_bytes, _ABSOLUTE_MAX_BYTES))

    try:
        max_chars_raw = args.get("max_chars", _DEFAULT_MAX_CHARS)
        max_chars = int(max_chars_raw) if max_chars_raw else _DEFAULT_MAX_CHARS
    except (ValueError, TypeError):
        max_chars = _DEFAULT_MAX_CHARS
    max_chars = max(1, min(max_chars, _ABSOLUTE_MAX_CHARS))

    start_line_raw = args.get("start_line")
    max_lines_raw = args.get("max_lines")
    range_requested = start_line_raw is not None or max_lines_raw is not None
    try:
        start_line = int(start_line_raw) if start_line_raw is not None else 1
        max_lines = int(max_lines_raw) if max_lines_raw is not None else 200
    except (TypeError, ValueError):
        return ToolResult(
            ok=False,
            kind="empty",
            summary="行范围参数无效",
            error={
                "code": "INVALID_LINE_RANGE",
                "message": "start_line 和 max_lines 必须是正整数",
                "category": "validation",
                "recoverable": True,
            },
        )
    if start_line < 1 or max_lines < 1 or max_lines > _ABSOLUTE_MAX_LINES:
        return ToolResult(
            ok=False,
            kind="empty",
            summary="行范围参数超出边界",
            error={
                "code": "INVALID_LINE_RANGE",
                "message": (
                    "start_line 必须大于等于 1，max_lines 必须介于 1 和 "
                    f"{_ABSOLUTE_MAX_LINES} 之间"
                ),
                "category": "validation",
                "recoverable": True,
            },
        )

    # 4. 安全解析 target_path
    try:
        target_path = path_policy._resolve_safe_target(workspace_root, path)
    except ValueError as e:
        log.warning("workspace.read_file 边界拒绝: %s", e)
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
        log.error("workspace.read_file 路径解析失败: %s", e)
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

    # 5. 检查路径是否存在
    if not os.path.exists(target_path):
        suggestions = find_path_suggestions(
            workspace_root=workspace_root,
            requested_path=path,
            expected_type="file",
        )
        return ToolResult(
            ok=False,
            kind="empty",
            summary=f"文件不存在: {path}",
            data={
                "requested_path": path,
                "suggested_paths": suggestions,
            },
            error={
                "code": "FILE_NOT_FOUND",
                "message": f"请求的文件不存在: {path}",
                "category": "tool",
                "recoverable": True,
            },
        )

    # 6. 拒绝目录
    if os.path.isdir(target_path):
        return ToolResult(
            ok=False,
            kind="empty",
            summary=f"路径是目录而非文件: {path}",
            error={
                "code": "PATH_IS_DIRECTORY",
                "message": f"请求的路径是一个目录，不能作为文件读取: {path}",
                "category": "validation",
                "recoverable": True,
            },
        )

    # 7. 拒绝非普通文件（symlink 已在 path_policy._resolve_safe_target 中处理，此处防御非普通文件）
    if not os.path.isfile(target_path):
        return ToolResult(
            ok=False,
            kind="empty",
            summary=f"不是普通文件: {path}",
            error={
                "code": "UNSUPPORTED_FILE_TYPE",
                "message": "只能读取普通文件",
                "category": "validation",
                "recoverable": True,
            },
        )

    # 8. 检查文件大小
    try:
        file_size = os.path.getsize(target_path)
    except OSError as e:
        return ToolResult(
            ok=False,
            kind="empty",
            summary="无法获取文件信息",
            error={
                "code": "READ_FILE_FAILED",
                "message": f"无法获取文件大小: {e}",
                "category": "tool",
                "recoverable": True,
            },
        )

    if file_size > max_bytes:
        return ToolResult(
            ok=False,
            kind="empty",
            summary=f"文件过大: {path} ({file_size}B > {max_bytes}B)",
            error={
                "code": "FILE_TOO_LARGE",
                "message": (
                    f"文件大小为 {file_size} 字节，超过上限 {max_bytes} 字节。"
                    f"可通过 max_bytes 参数调大上限（最大 {_ABSOLUTE_MAX_BYTES}）"
                ),
                "category": "validation",
                "recoverable": True,
            },
        )

    if file_size == 0:
        # 空文件 — 返回空内容，不算错误
        log.info("workspace.read_file: 空文件 path=%s", path)
        return ToolResult(
            ok=True,
            kind="text",
            summary=f"Read file: {path}（空文件）",
            data={
                "path": path,
                "content": "",
                "size_bytes": 0,
                "chars_read": 0,
                "truncated": False,
            },
        )

    # 9. 读取并解码
    try:
        with open(target_path, "rb") as f:
            raw_bytes = f.read(max_bytes)
    except OSError as e:
        return ToolResult(
            ok=False,
            kind="empty",
            summary="读取文件失败",
            error={
                "code": "READ_FILE_FAILED",
                "message": f"读取文件时发生系统错误: {e}",
                "category": "tool",
                "recoverable": True,
            },
        )

    # 10. UTF-8 解码 — 严格模式，不做 fallback
    try:
        content = raw_bytes.decode("utf-8")
    except UnicodeDecodeError:
        log.warning("workspace.read_file 非 UTF-8 文件: path=%s", path)
        return ToolResult(
            ok=False,
            kind="empty",
            summary=f"不支持的文件类型: {path}（非有效 UTF-8 文本）",
            error={
                "code": "UNSUPPORTED_FILE_TYPE",
                "message": "文件不是有效的 UTF-8 文本，不支持读取二进制或非 UTF-8 文件",
                "category": "validation",
                "recoverable": True,
            },
        )

    total_lines: int | None = None
    end_line: int | None = None
    range_truncated = False
    if range_requested:
        lines = content.splitlines(keepends=True)
        total_lines = len(lines)
        if start_line > max(1, total_lines):
            return ToolResult(
                ok=False,
                kind="empty",
                summary=f"请求行范围超出文件: {path}",
                error={
                    "code": "LINE_RANGE_OUT_OF_BOUNDS",
                    "message": f"start_line={start_line} 超出文件总行数 {total_lines}",
                    "category": "validation",
                    "recoverable": True,
                },
            )
        selected_lines = lines[start_line - 1 : start_line - 1 + max_lines]
        content = "".join(selected_lines)
        end_line = start_line + len(selected_lines) - 1
        range_truncated = start_line > 1 or end_line < total_lines

    chars_read = len(content)

    # 11. 字符截断
    chars_truncated = chars_read > max_chars
    if chars_truncated:
        content = content[:max_chars]
        chars_read = max_chars
    truncated = range_truncated or chars_truncated

    summary = f"Read file: {path}"
    if range_requested:
        summary += f" (lines {start_line}-{end_line} of {total_lines})"
    if chars_truncated:
        summary += f" ({chars_read} chars, truncated from larger content)"

    log.info(
        "workspace.read_file: path=%s size_bytes=%d chars_read=%d truncated=%s",
        path,
        file_size,
        chars_read,
        truncated,
    )

    return ToolResult(
        ok=True,
        kind="text",
        summary=summary,
        data={
            "path": path,
            "content": content,
            "size_bytes": file_size,
            "chars_read": chars_read,
            "truncated": truncated,
            **(
                {
                    "start_line": start_line,
                    "end_line": end_line,
                    "total_lines": total_lines,
                }
                if range_requested
                else {}
            ),
        },
    )
