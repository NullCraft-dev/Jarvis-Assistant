"""workspace.search_files capability executor。"""

from __future__ import annotations

import logging
import os
from collections import deque
from datetime import datetime, timezone

from jarvis_worker.agent.tool_gateway.contracts import ToolRequest, ToolResult

from . import path_policy

log = logging.getLogger("jarvis_worker.tool.workspace")

_SEARCH_MAX_RESULTS = 100
_SEARCH_DEFAULT_RESULTS = 50
_SEARCH_MAX_SCANNED = 10000
_SEARCH_MAX_DEPTH = 20
_SEARCH_MAX_QUERY_LENGTH = 200

def _search_root_open_error(exc: BaseException) -> ToolResult:
    """把搜索根目录打开/读取错误映射为稳定的 ToolResult。"""
    if isinstance(exc, FileNotFoundError):
        return ToolResult(
            ok=False,
            kind="empty",
            summary="搜索起始路径不存在",
            error={
                "code": "PATH_NOT_FOUND",
                "message": "搜索起始路径不存在",
                "category": "tool",
                "recoverable": True,
            },
        )
    if isinstance(exc, NotADirectoryError):
        return ToolResult(
            ok=False,
            kind="empty",
            summary="搜索起始路径不是目录",
            error={
                "code": "NOT_A_DIRECTORY",
                "message": "搜索起始路径不是目录",
                "category": "tool",
                "recoverable": True,
            },
        )
    if isinstance(exc, PermissionError):
        return ToolResult(
            ok=False,
            kind="empty",
            summary="权限不足，无法搜索目录",
            error={
                "code": "PERMISSION_DENIED",
                "message": "没有权限读取搜索起始目录",
                "category": "permission",
                "recoverable": True,
            },
        )
    if isinstance(exc, path_policy._DirectorySymlinkError):
        return ToolResult(
            ok=False,
            kind="empty",
            summary="拒绝符号链接搜索路径",
            error={
                "code": "WORKSPACE_ACCESS_DENIED",
                "message": "搜索起始路径包含符号链接目录",
                "category": "permission",
                "recoverable": False,
            },
        )
    return ToolResult(
        ok=False,
        kind="empty",
        summary="搜索目录失败",
        error={
            "code": "SEARCH_FAILED",
            "message": "读取搜索起始目录时发生系统错误",
            "category": "tool",
            "recoverable": True,
        },
    )


def execute_workspace_search_files(request: ToolRequest) -> ToolResult:
    """执行 workspace.search_files — 在 Workspace 内递归搜索匹配文件/目录名。

    L0 只读：不读取文件正文，不跟随 symlink 目录。

    arguments 契约：
        - workspace_root: str（**必须**）由 AgentRunner 从可信状态注入
        - query: str（**必须**）大小写不敏感 substring 匹配，最大 200 字符
        - path: str（可选，默认 "."）搜索起始子目录的相对路径
        - max_results: int（可选，1-100，默认 50）

    资源限制：
        - max_results: 1-100
        - 最大扫描条目: 10000
        - 最大递归深度: 20
    """
    if not path_policy._supports_safe_search_dir_fd():
        return ToolResult(
            ok=False,
            kind="empty",
            summary="当前平台不支持安全目录搜索",
            error={
                "code": "UNSUPPORTED_PLATFORM",
                "message": "当前平台不支持 dir_fd 安全目录搜索",
                "category": "tool",
                "recoverable": False,
            },
        )

    args = request.arguments if request.arguments else {}

    # ── 1. workspace_root ──
    workspace_root: str = args.get("workspace_root", "")
    if not workspace_root or not isinstance(workspace_root, str):
        return ToolResult(ok=False, kind="empty", summary="workspace_root 缺失",
                          error={"code": "WORKSPACE_ROOT_REQUIRED",
                                 "message": "workspace_root 是必须参数",
                                 "category": "permission", "recoverable": False})

    # ── 2. query ──
    query_raw = args.get("query", "")
    if not query_raw or not isinstance(query_raw, str) or not query_raw.strip():
        return ToolResult(ok=False, kind="empty", summary="query 参数缺失",
                          error={"code": "SEARCH_QUERY_REQUIRED",
                                 "message": "query 是必须参数，请提供要搜索的文件名或关键词",
                                 "category": "validation", "recoverable": True})
    query = query_raw.strip()
    if len(query) > _SEARCH_MAX_QUERY_LENGTH:
        return ToolResult(ok=False, kind="empty",
                          summary=f"query 超过 {_SEARCH_MAX_QUERY_LENGTH} 字符上限",
                          error={"code": "SEARCH_QUERY_TOO_LONG",
                                 "message": f"query 不能超过 {_SEARCH_MAX_QUERY_LENGTH} 个字符",
                                 "category": "validation", "recoverable": True})
    query_cf = query.casefold()

    # ── 3. path ──
    search_path_raw = args.get("path", ".")
    if not isinstance(search_path_raw, str):
        search_path_raw = "."
    search_path = search_path_raw.strip() or "."

    if os.path.isabs(search_path):
        return ToolResult(ok=False, kind="empty", summary="绝对路径不允许",
                          error={"code": "WORKSPACE_ACCESS_DENIED",
                                 "message": "不允许使用绝对路径作为搜索起始目录",
                                 "category": "permission", "recoverable": True})

    try:
        canonical_search_path, search_components = path_policy._normalize_workspace_path(search_path)
    except ValueError:
        return ToolResult(
            ok=False,
            kind="empty",
            summary="拒绝: 路径超出 workspace 范围",
            error={
                "code": "WORKSPACE_ACCESS_DENIED",
                "message": "搜索起始路径不在允许的 workspace 范围内",
                "category": "permission",
                "recoverable": False,
            },
        )

    # ── 4. max_results ──
    max_results_raw = args.get("max_results", _SEARCH_DEFAULT_RESULTS)
    # 拒绝 bool（bool 是 int 的子类）
    if isinstance(max_results_raw, bool):
        max_results = _SEARCH_DEFAULT_RESULTS
    else:
        try:
            max_results = int(max_results_raw)
        except (ValueError, TypeError):
            max_results = _SEARCH_DEFAULT_RESULTS
    max_results = max(1, min(max_results, _SEARCH_MAX_RESULTS))

    # ── 5. 安全解析起始路径 ──
    try:
        path_policy._resolve_safe_target(workspace_root, canonical_search_path)
    except ValueError:
        return ToolResult(ok=False, kind="empty", summary="拒绝: 路径超出 workspace 范围",
                          error={"code": "WORKSPACE_ACCESS_DENIED",
                                 "message": "搜索起始路径不在允许的 workspace 范围内",
                                 "category": "permission", "recoverable": False})
    except OSError:
        return ToolResult(ok=False, kind="empty", summary="路径解析失败",
                          error={"code": "PATH_RESOLVE_ERROR",
                                 "message": "无法解析搜索起始路径",
                                 "category": "tool", "recoverable": True})

    # ── 6. 从可信 workspace root fd 打开搜索根目录 ──
    try:
        real_root = os.path.realpath(workspace_root)
        workspace_fd = os.open(
            real_root,
            os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
        )
    except (OSError, ValueError, NotImplementedError) as exc:
        return _search_root_open_error(exc)

    try:
        try:
            search_root_fd = path_policy._open_workspace_directory_fd(workspace_fd, search_components)
        except (OSError, NotImplementedError) as exc:
            return _search_root_open_error(exc)
    finally:
        try:
            os.close(workspace_fd)
        except OSError:
            pass

    # ── 7. 递归搜索 ──
    matches: list[dict] = []
    scanned = 0
    excluded = 0
    skipped_dirs = 0
    truncated = False
    truncation_reasons: list[str] = []

    # 队列只保存相对目录 components，不持有大量 FD。每次从固定的
    # search_root_fd 逐级 O_NOFOLLOW 打开，避免目录被替换为 symlink 后逃逸。
    queue: deque[tuple[tuple[str, ...], int, str]] = deque()
    queue.append(((), 0, "" if canonical_search_path == "." else canonical_search_path))
    stop_search = False

    try:
        while queue and not stop_search:
            dir_components, depth, rel_prefix = queue.popleft()
            current_fd: int | None = None
            entries: list[dict[str, object]] = []
            scan_budget_exhausted = False

            try:
                current_fd = path_policy._open_workspace_directory_fd(search_root_fd, dir_components)
                remaining_scan_budget = _SEARCH_MAX_SCANNED - scanned
                if remaining_scan_budget <= 0:
                    scan_budget_exhausted = True
                else:
                    raw_entries: list[os.DirEntry[str]] = []
                    with os.scandir(current_fd) as dir_it:
                        for entry in dir_it:
                            if len(raw_entries) >= remaining_scan_budget:
                                scan_budget_exhausted = True
                                break
                            raw_entries.append(entry)
                    scanned += len(raw_entries)
                    raw_entries.sort(key=lambda entry: (entry.name.casefold(), entry.name))

                    # DirEntry 的 stat 可能依赖仍打开的目录 FD，因此在关闭 FD 前
                    # 快照所有后续需要的有界元数据。
                    for entry in raw_entries:
                        snapshot: dict[str, object] = {"name": entry.name}
                        try:
                            is_symlink = entry.is_symlink()
                            is_dir = entry.is_dir(follow_symlinks=False)
                            is_file = entry.is_file(follow_symlinks=False)
                        except OSError:
                            is_symlink = False
                            is_dir = False
                            is_file = False
                        if is_symlink:
                            snapshot["type"] = "symlink"
                        elif is_dir:
                            snapshot["type"] = "dir"
                        elif is_file:
                            snapshot["type"] = "file"
                        else:
                            snapshot["type"] = "unknown"
                        try:
                            entry_stat = entry.stat(follow_symlinks=False)
                            snapshot["modified_at"] = datetime.fromtimestamp(
                                entry_stat.st_mtime,
                                tz=timezone.utc,
                            ).isoformat()
                            if is_file:
                                snapshot["size"] = entry_stat.st_size
                        except OSError:
                            pass
                        entries.append(snapshot)
            except (PermissionError, path_policy._DirectorySymlinkError) as exc:
                if depth == 0:
                    return _search_root_open_error(exc)
                log.warning("workspace.search_files 跳过无权限或 symlink 目录")
                skipped_dirs += 1
                continue
            except OSError as exc:
                if depth == 0:
                    return _search_root_open_error(exc)
                log.warning(
                    "workspace.search_files 跳过无法读取的目录: type=%s",
                    type(exc).__name__,
                )
                skipped_dirs += 1
                continue
            finally:
                if current_fd is not None:
                    try:
                        os.close(current_fd)
                    except OSError:
                        pass

            for entry in entries:
                name = str(entry["name"])

                if path_policy._is_excluded(name):
                    excluded += 1
                    continue

                rel_path_raw = os.path.join(rel_prefix, name) if rel_prefix else name
                rel_path = rel_path_raw.replace(os.sep, "/")

                entry_type = str(entry.get("type", "unknown"))
                is_symlink = entry_type == "symlink"

                if query_cf in name.casefold() or query_cf in rel_path.casefold():
                    if len(matches) >= max_results:
                        truncated = True
                        if "max_results" not in truncation_reasons:
                            truncation_reasons.append("max_results")
                        stop_search = True
                        break

                    match: dict = {"name": name, "path": rel_path, "type": entry_type}
                    if entry_type == "file" and "size" in entry:
                        match["size"] = entry["size"]
                    if "modified_at" in entry:
                        match["modified_at"] = entry["modified_at"]
                    matches.append(match)

                if entry_type == "dir" and not is_symlink:
                    if depth >= _SEARCH_MAX_DEPTH:
                        truncated = True
                        if "max_depth" not in truncation_reasons:
                            truncation_reasons.append("max_depth")
                    else:
                        queue.append((dir_components + (name,), depth + 1, rel_path_raw))

            if scan_budget_exhausted:
                truncated = True
                if "max_scanned_entries" not in truncation_reasons:
                    truncation_reasons.append("max_scanned_entries")
                break
    finally:
        try:
            os.close(search_root_fd)
        except OSError:
            pass

    # ── 8. 确定性排序（按 rel_path casefold） ──
    matches.sort(key=lambda m: m["path"].casefold())

    # ── 9. 构造结果 ──
    summary_parts = [f"搜索完成: query='{query}'"]
    summary_parts.append(f"匹配 {len(matches)} 条")
    if truncated:
        summary_parts.append("(结果已截断)")
    if skipped_dirs:
        summary_parts.append(f"跳过 {skipped_dirs} 个目录")

    log.info(
        "workspace.search_files: query=%s results=%d scanned=%d excluded=%d skipped=%d truncated=%s",
        query, len(matches), scanned, excluded, skipped_dirs, truncated,
    )

    return ToolResult(
        ok=True, kind="json",
        summary="; ".join(summary_parts),
        data={
            "search_path": canonical_search_path,
            "query": query,
            "returned_matches": len(matches),
            "scanned_entries": scanned,
            "excluded_entries": excluded,
            "skipped_directories": skipped_dirs,
            "max_results": max_results,
            "truncated": truncated,
            "truncation_reasons": truncation_reasons,
            "matches": matches,
        },
    )
