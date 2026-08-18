"""workspace.read_files capability executor。"""

from __future__ import annotations

import logging
import re
from typing import Any

from jarvis_worker.agent.tool_gateway.contracts import ToolRequest, ToolResult

from .read_file import execute_workspace_read_file

log = logging.getLogger("jarvis_worker.tool.workspace")

_MAX_FILES = 6
_MAX_LINES_PER_FILE = 400
_MAX_CHARS_PER_FILE = 12_000
_MAX_TOTAL_RETURNED_CHARS = 60_000
_MAX_BYTES_PER_FILE = 262_144
_PATH_RANGE_SHORTHAND = re.compile(r"^(?P<path>.+):(?P<start>[1-9][0-9]*):(?P<end>[1-9][0-9]*)$")


def execute_workspace_read_files(request: ToolRequest) -> ToolResult:
    """批量读取多个已定位的 Workspace UTF-8 文件片段。

    每个条目都委托 ``workspace.read_file`` 的同一安全实现处理，因此不会绕过
    workspace boundary、symlink、普通文件、UTF-8 或文件大小限制。
    """
    args = request.arguments if request.arguments else {}
    workspace_root = args.get("workspace_root")
    if not isinstance(workspace_root, str) or not workspace_root:
        return _validation_error(
            "WORKSPACE_ROOT_REQUIRED",
            "workspace_root 缺失，无法确定允许访问的根目录",
            recoverable=False,
        )

    files = args.get("files")
    if not isinstance(files, list) or not 1 <= len(files) <= _MAX_FILES:
        return _validation_error(
            "INVALID_BATCH_REQUEST",
            f"files 必须是包含 1 到 {_MAX_FILES} 个条目的数组",
        )

    normalized: list[dict[str, Any]] = []
    for index, item in enumerate(files):
        # LLM 在只需要整段默认读取时常会输出字符串数组。字符串只是
        # {"path": ...} 的无损简写，仍然进入与 object 条目完全相同的
        # workspace boundary / symlink / size / encoding 校验链路。
        if isinstance(item, str):
            shorthand = _PATH_RANGE_SHORTHAND.fullmatch(item.strip())
            if shorthand is None:
                item = {"path": item}
            else:
                start_line = int(shorthand.group("start"))
                end_line = int(shorthand.group("end"))
                if end_line < start_line:
                    return _validation_error(
                        "INVALID_BATCH_REQUEST",
                        f"files[{index}] 的范围终点必须 >= 起点",
                    )
                item = {
                    "path": shorthand.group("path"),
                    "start_line": start_line,
                    "max_lines": end_line - start_line + 1,
                }
        if not isinstance(item, dict):
            return _validation_error(
                "INVALID_BATCH_REQUEST",
                f"files[{index}] 必须是 path 字符串或 object",
            )
        unknown = set(item) - {"path", "start_line", "max_lines"}
        if unknown:
            return _validation_error(
                "INVALID_BATCH_REQUEST",
                f"files[{index}] 包含未声明字段",
            )
        path = item.get("path")
        if not isinstance(path, str) or not path.strip():
            return _validation_error(
                "INVALID_BATCH_REQUEST",
                f"files[{index}].path 必须是非空相对路径",
            )
        try:
            start_line = int(item.get("start_line", 1))
            max_lines = int(item.get("max_lines", 200))
        except (TypeError, ValueError):
            return _validation_error(
                "INVALID_BATCH_REQUEST",
                f"files[{index}] 的 start_line/max_lines 必须是正整数",
            )
        if start_line < 1 or not 1 <= max_lines <= _MAX_LINES_PER_FILE:
            return _validation_error(
                "INVALID_BATCH_REQUEST",
                (
                    f"files[{index}] 的 start_line 必须 >= 1，max_lines 必须介于 "
                    f"1 和 {_MAX_LINES_PER_FILE} 之间"
                ),
            )
        normalized.append({"path": path.strip(), "start_line": start_line, "max_lines": max_lines})

    results: list[dict[str, Any]] = []
    succeeded = 0
    failed = 0
    returned_chars = 0
    output_truncated = False

    for item in normalized:
        child_request = ToolRequest(
            task_id=request.task_id,
            run_id=request.run_id,
            tool_name="workspace.read_file",
            arguments={
                "workspace_root": workspace_root,
                "path": item["path"],
                "max_bytes": _MAX_BYTES_PER_FILE,
                "max_chars": _MAX_CHARS_PER_FILE,
                "start_line": item["start_line"],
                "max_lines": item["max_lines"],
            },
            authorization_scope=request.authorization_scope,
        )
        result = execute_workspace_read_file(child_request)
        if not result.ok:
            failed += 1
            error = result.error if isinstance(result.error, dict) else {}
            child_data = result.data if isinstance(result.data, dict) else {}
            suggested_paths = child_data.get("suggested_paths", [])
            if not isinstance(suggested_paths, list):
                suggested_paths = []
            results.append(
                {
                    "path": item["path"],
                    "ok": False,
                    "suggested_paths": [
                        value for value in suggested_paths if isinstance(value, str)
                    ][:5],
                    "error": {
                        "code": str(error.get("code", "READ_FILE_FAILED"))[:128],
                        "message": str(error.get("message", result.summary))[:500],
                    },
                }
            )
            continue

        data = result.data if isinstance(result.data, dict) else {}
        content = data.get("content", "")
        if not isinstance(content, str):
            content = str(content) if content else ""
        remaining = max(0, _MAX_TOTAL_RETURNED_CHARS - returned_chars)
        if len(content) > remaining:
            content = content[:remaining]
            output_truncated = True
        returned_chars += len(content)
        succeeded += 1
        results.append(
            {
                "path": str(data.get("path", item["path"])),
                "ok": True,
                "content": content,
                "size_bytes": int(data.get("size_bytes", 0)),
                "chars_read": len(content),
                "start_line": int(data.get("start_line", item["start_line"])),
                "end_line": int(data.get("end_line", item["start_line"])),
                "total_lines": int(data.get("total_lines", 0)),
                "truncated": bool(data.get("truncated", False)) or output_truncated,
            }
        )

    summary = (
        f"Batch read: {succeeded}/{len(normalized)} files succeeded; "
        f"{returned_chars} chars returned"
    )
    log.info(
        "workspace.read_files: requested=%d succeeded=%d failed=%d chars=%d truncated=%s",
        len(normalized),
        succeeded,
        failed,
        returned_chars,
        output_truncated,
    )
    if succeeded == 0:
        return ToolResult(
            ok=False,
            kind="empty",
            summary=summary,
            data={
                "requested_files": len(normalized),
                "succeeded_files": 0,
                "failed_files": failed,
                "files": results,
                "truncated": output_truncated,
            },
            error={
                "code": "BATCH_READ_FAILED",
                "message": "所有批量文件读取都失败；请根据每个条目的错误调整路径或范围",
                "category": "tool",
                "recoverable": True,
            },
        )
    return ToolResult(
        ok=True,
        kind="json",
        summary=summary,
        data={
            "requested_files": len(normalized),
            "succeeded_files": succeeded,
            "failed_files": failed,
            "returned_chars": returned_chars,
            "files": results,
            "truncated": output_truncated,
        },
    )


def _validation_error(code: str, message: str, *, recoverable: bool = True) -> ToolResult:
    return ToolResult(
        ok=False,
        kind="empty",
        summary=message,
        error={
            "code": code,
            "message": message,
            "category": "validation" if recoverable else "permission",
            "recoverable": recoverable,
        },
    )
