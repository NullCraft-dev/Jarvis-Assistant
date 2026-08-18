"""workspace.search_text capability executor。"""

from __future__ import annotations

import logging
import os
import stat
from collections import deque

from jarvis_worker.agent.tool_gateway.contracts import ToolRequest, ToolResult

from . import path_policy

log = logging.getLogger("jarvis_worker.tool.workspace")

_DEFAULT_MAX_RESULTS = 20
_MAX_RESULTS = 50
_MAX_QUERY_LENGTH = 200
_MAX_SCANNED_ENTRIES = 10_000
_MAX_SCANNED_FILES = 2_000
_MAX_FILE_BYTES = 1_048_576
_MAX_TOTAL_BYTES = 16 * 1024 * 1024
_MAX_DEPTH = 20
_MAX_PREVIEW_CHARS = 600
_MAX_MATCHES_PER_FILE = 3
_SCAN_TRUNCATION_REASONS = frozenset(
    {"max_scanned_entries", "max_scanned_files", "max_total_bytes", "max_depth"}
)
_SOURCE_EXTENSIONS = frozenset(
    {
        ".c",
        ".cc",
        ".cpp",
        ".cs",
        ".go",
        ".h",
        ".hpp",
        ".java",
        ".js",
        ".jsx",
        ".kt",
        ".kts",
        ".mjs",
        ".php",
        ".py",
        ".rb",
        ".rs",
        ".sh",
        ".swift",
        ".ts",
        ".tsx",
        ".vue",
    }
)
_SOURCE_EXCLUDED_DIRECTORIES = frozenset(
    {
        "doc",
        "docs",
        "example",
        "examples",
        "fixture",
        "fixtures",
        "script",
        "scripts",
        "test",
        "tests",
        "__tests__",
    }
)
_TEXT_EXTENSIONS = frozenset(
    {
        ".cfg",
        ".conf",
        ".css",
        ".csv",
        ".go",
        ".graphql",
        ".html",
        ".ini",
        ".js",
        ".json",
        ".jsx",
        ".md",
        ".mjs",
        ".py",
        ".rst",
        ".scss",
        ".sh",
        ".sql",
        ".toml",
        ".ts",
        ".tsx",
        ".txt",
        ".vue",
        ".xml",
        ".yaml",
        ".yml",
    }
)
_TEXT_FILENAMES = frozenset(
    {
        "dockerfile",
        "makefile",
        "readme",
        "license",
        "changelog",
        "agents.md",
    }
)


def _error(
    code: str, message: str, *, category: str = "tool", recoverable: bool = True
) -> ToolResult:
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


def _is_text_candidate(name: str) -> bool:
    lowered = name.casefold()
    return lowered in _TEXT_FILENAMES or os.path.splitext(lowered)[1] in _TEXT_EXTENSIONS


def _is_source_candidate(name: str) -> bool:
    lowered = name.casefold()
    stem, extension = os.path.splitext(lowered)
    if extension not in _SOURCE_EXTENSIONS:
        return False
    return not (
        stem.startswith("test_")
        or stem.endswith("_test")
        or stem.endswith(".test")
        or stem.endswith(".spec")
    )


def _source_match_priority(match: dict[str, object], query_cf: str) -> tuple[int, int, str, int]:
    """让 production owner / 行为证据优先于引用与展示层命中。"""
    path = str(match.get("path", ""))
    path_cf = f"/{path.casefold().strip('/')}"
    preview = str(match.get("preview", ""))
    preview_cf = preview.casefold()
    production_rank = 0 if "/src/" in path_cf or "/internal/" in path_cf else 1
    behavior_markers = (
        "repository",
        "persist",
        "持久化",
        ".create(",
        ".save(",
        "insert(",
        "update(",
        "execute(",
    )
    declaration_markers = (
        f"class {query_cf}",
        f"def {query_cf}",
        f"interface {query_cf}",
        f"type {query_cf}",
        f"struct {query_cf}",
    )
    if any(marker in preview_cf for marker in behavior_markers):
        evidence_rank = 0
    elif any(marker in preview_cf for marker in declaration_markers):
        evidence_rank = 1
    elif preview_cf.startswith(("import ", "from ", "//", "#", "/*", "*")):
        evidence_rank = 3
    else:
        evidence_rank = 2
    return production_rank, evidence_rank, path_cf, int(match.get("line_number", 0))


def _bounded_int(value: object, default: int, maximum: int) -> int:
    if isinstance(value, bool):
        return default
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return max(1, min(parsed, maximum))


def _line_preview(line: str, query_cf: str) -> str:
    compact = " ".join(line.strip().split())
    if len(compact) <= _MAX_PREVIEW_CHARS:
        return compact
    position = compact.casefold().find(query_cf)
    if position < 0:
        return compact[:_MAX_PREVIEW_CHARS]
    start = max(0, position - _MAX_PREVIEW_CHARS // 3)
    end = min(len(compact), start + _MAX_PREVIEW_CHARS)
    prefix = "…" if start else ""
    suffix = "…" if end < len(compact) else ""
    return prefix + compact[start:end] + suffix


def _search_single_file(
    root_fd: int,
    components: tuple[str, ...],
    canonical_path: str,
    query: str,
    query_cf: str,
    max_results: int,
    source_only: bool,
) -> ToolResult:
    """通过可信 parent dir FD 搜索一个显式指定的普通文本文件。"""
    if not components:
        return _error("NOT_A_DIRECTORY", "搜索起始路径不是目录或文本文件")
    parent_fd: int | None = None
    file_fd: int | None = None
    try:
        parent_fd = path_policy._open_workspace_directory_fd(root_fd, components[:-1])
        name = components[-1]
        file_stat = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        if stat.S_ISLNK(file_stat.st_mode):
            return _error(
                "WORKSPACE_ACCESS_DENIED",
                "搜索文件不能是符号链接",
                category="permission",
                recoverable=False,
            )
        if not stat.S_ISREG(file_stat.st_mode):
            return _error("NOT_A_TEXT_FILE", "搜索起始路径不是普通文本文件")
        if not _is_text_candidate(name):
            return _error("NOT_A_TEXT_FILE", "搜索起始路径不是受支持的文本文件")
        if source_only and not _is_source_candidate(name):
            return _error("NOT_A_SOURCE_FILE", "仅源码检索不支持该文件类型")
        if file_stat.st_size > _MAX_FILE_BYTES:
            return _error("FILE_TOO_LARGE", "搜索文件超过正文检索大小上限")
        file_fd = os.open(
            name,
            os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC,
            dir_fd=parent_fd,
        )
        opened_stat = os.fstat(file_fd)
        if not stat.S_ISREG(opened_stat.st_mode) or opened_stat.st_size > _MAX_FILE_BYTES:
            return _error("FILE_CHANGED", "搜索文件在打开期间发生变化")
        raw = os.read(file_fd, _MAX_FILE_BYTES + 1)
    except FileNotFoundError:
        return _error("PATH_NOT_FOUND", "搜索起始路径不存在")
    except (PermissionError, path_policy._DirectorySymlinkError):
        return _error(
            "WORKSPACE_ACCESS_DENIED",
            "无法安全打开搜索文件",
            category="permission",
            recoverable=False,
        )
    except OSError:
        return _error("SEARCH_FAILED", "读取搜索文件时发生系统错误")
    finally:
        if file_fd is not None:
            try:
                os.close(file_fd)
            except OSError:
                pass
        if parent_fd is not None:
            try:
                os.close(parent_fd)
            except OSError:
                pass

    if b"\x00" in raw:
        return _error("NOT_A_TEXT_FILE", "搜索起始路径不是 UTF-8 文本文件")
    try:
        content = raw.decode("utf-8")
    except UnicodeDecodeError:
        return _error("NOT_A_TEXT_FILE", "搜索起始路径不是 UTF-8 文本文件")

    matches: list[dict[str, object]] = []
    matching_lines = 0
    stored_limit = min(max_results, _MAX_MATCHES_PER_FILE)
    for line_number, line in enumerate(content.splitlines(), 1):
        if query_cf not in line.casefold():
            continue
        matching_lines += 1
        if len(matches) < stored_limit:
            matches.append(
                {
                    "path": canonical_path,
                    "line_number": line_number,
                    "preview": _line_preview(line, query_cf),
                }
            )
    reasons: list[str] = []
    if matching_lines > max_results:
        reasons.append("max_results")
    if matching_lines > _MAX_MATCHES_PER_FILE:
        reasons.append("max_matches_per_file")
    result_window_truncated = matching_lines > len(matches)
    summary = (
        f"正文搜索完成: query='{query}'; 返回 {len(matches)} 条，正文共命中 "
        f"{matching_lines} 行；扫描范围完整"
    )
    if not matches:
        summary += "; 未发现精确子串匹配，不代表文件中没有相关概念，可改用较短关键词"
    elif result_window_truncated:
        summary += "; 返回窗口已截断，同 query/path 重试不会翻页"
    return ToolResult(
        ok=True,
        kind="json",
        summary=summary,
        data={
            "search_path": canonical_path,
            "query": query,
            "source_only": source_only,
            "matches": matches,
            "returned_matches": len(matches),
            "candidate_matches": len(matches),
            "matching_lines": matching_lines,
            "matched_files": 1 if matching_lines else 0,
            "scanned_entries": 1,
            "scanned_files": 1,
            "searched_files": 1,
            "scanned_bytes": len(raw),
            "excluded_entries": 0,
            "skipped_files": 0,
            "scan_complete": True,
            "result_window_truncated": result_window_truncated,
            "truncated": result_window_truncated,
            "truncation_reasons": reasons,
        },
    )


def execute_workspace_search_text(request: ToolRequest) -> ToolResult:
    """在 workspace UTF-8 文本文件正文中执行有界 substring 搜索。

    该 L0 工具不跟随符号链接，只扫描受支持的文本扩展名；单文件、总读取量、
    扫描文件数、递归深度和返回结果数均有硬上限。
    """
    if not path_policy._supports_safe_search_dir_fd():
        return _error(
            "UNSUPPORTED_PLATFORM",
            "当前平台不支持安全正文搜索",
            recoverable=False,
        )

    args = request.arguments if request.arguments else {}
    workspace_root = args.get("workspace_root")
    if not isinstance(workspace_root, str) or not workspace_root:
        return _error(
            "WORKSPACE_ROOT_REQUIRED",
            "workspace_root 是必须参数",
            category="permission",
            recoverable=False,
        )

    query_raw = args.get("query")
    if not isinstance(query_raw, str) or not query_raw.strip():
        return _error(
            "SEARCH_QUERY_REQUIRED",
            "query 是必须参数，请提供要搜索的正文关键词",
            category="validation",
        )
    query = query_raw.strip()
    if len(query) > _MAX_QUERY_LENGTH:
        return _error(
            "SEARCH_QUERY_TOO_LONG",
            f"query 不能超过 {_MAX_QUERY_LENGTH} 个字符",
            category="validation",
        )
    query_cf = query.casefold()
    source_only = args.get("source_only") is True

    search_path_raw = args.get("path", ".")
    search_path = search_path_raw.strip() if isinstance(search_path_raw, str) else "."
    search_path = search_path or "."
    if os.path.isabs(search_path):
        return _error(
            "WORKSPACE_ACCESS_DENIED",
            "不允许使用绝对路径作为搜索起始目录",
            category="permission",
        )
    try:
        canonical_path, search_components = path_policy._normalize_workspace_path(search_path)
        path_policy._resolve_safe_target(workspace_root, canonical_path)
    except ValueError:
        return _error(
            "WORKSPACE_ACCESS_DENIED",
            "搜索起始路径不在允许的 workspace 范围内",
            category="permission",
            recoverable=False,
        )
    except OSError:
        return _error("PATH_RESOLVE_ERROR", "无法解析搜索起始路径")

    if any(path_policy._is_excluded(component) for component in search_components):
        return _error(
            "WORKSPACE_ACCESS_DENIED",
            "搜索起始路径包含隐藏或排除项",
            category="permission",
            recoverable=False,
        )

    max_results = _bounded_int(args.get("max_results"), _DEFAULT_MAX_RESULTS, _MAX_RESULTS)
    try:
        root_fd = path_policy._open_workspace_root_fd(workspace_root)
        search_root_fd = path_policy._open_workspace_directory_fd(root_fd, search_components)
    except FileNotFoundError:
        return _error("PATH_NOT_FOUND", "搜索起始路径不存在")
    except NotADirectoryError:
        return _search_single_file(
            root_fd,
            search_components,
            canonical_path,
            query,
            query_cf,
            max_results,
            source_only,
        )
    except (PermissionError, path_policy._DirectorySymlinkError):
        return _error(
            "WORKSPACE_ACCESS_DENIED",
            "无法安全打开搜索起始目录",
            category="permission",
            recoverable=False,
        )
    except OSError:
        return _error("SEARCH_FAILED", "读取搜索起始目录时发生系统错误")
    finally:
        if "root_fd" in locals():
            try:
                os.close(root_fd)
            except OSError:
                pass

    matches: list[dict[str, object]] = []
    queue: deque[tuple[tuple[str, ...], int, str]] = deque(
        [((), 0, "" if canonical_path == "." else canonical_path)]
    )
    scanned_entries = 0
    scanned_files = 0
    searched_files = 0
    matched_files = 0
    matching_lines = 0
    scanned_bytes = 0
    excluded_entries = 0
    skipped_files = 0
    truncated = False
    truncation_reasons: list[str] = []

    def mark_truncated(reason: str) -> None:
        nonlocal truncated
        truncated = True
        if reason not in truncation_reasons:
            truncation_reasons.append(reason)

    stop = False
    try:
        while queue and not stop:
            components, depth, rel_prefix = queue.popleft()
            current_fd: int | None = None
            try:
                current_fd = path_policy._open_workspace_directory_fd(search_root_fd, components)
                with os.scandir(current_fd) as iterator:
                    entries = list(iterator)
                entries.sort(key=lambda item: (item.name.casefold(), item.name))
                for entry in entries:
                    if scanned_entries >= _MAX_SCANNED_ENTRIES:
                        mark_truncated("max_scanned_entries")
                        stop = True
                        break
                    scanned_entries += 1
                    name = entry.name
                    if path_policy._is_excluded(name):
                        excluded_entries += 1
                        continue
                    if source_only and name.casefold() in _SOURCE_EXCLUDED_DIRECTORIES:
                        excluded_entries += 1
                        continue
                    try:
                        is_symlink = entry.is_symlink()
                        is_dir = entry.is_dir(follow_symlinks=False)
                        is_file = entry.is_file(follow_symlinks=False)
                    except OSError:
                        skipped_files += 1
                        continue
                    rel_path_raw = os.path.join(rel_prefix, name) if rel_prefix else name
                    rel_path = rel_path_raw.replace(os.sep, "/")
                    if is_dir and not is_symlink:
                        if depth >= _MAX_DEPTH:
                            mark_truncated("max_depth")
                        else:
                            queue.append((components + (name,), depth + 1, rel_path_raw))
                        continue
                    if (
                        is_symlink
                        or not is_file
                        or not _is_text_candidate(name)
                        or (source_only and not _is_source_candidate(name))
                    ):
                        continue
                    if scanned_files >= _MAX_SCANNED_FILES:
                        mark_truncated("max_scanned_files")
                        stop = True
                        break
                    scanned_files += 1
                    file_fd: int | None = None
                    try:
                        file_fd = os.open(
                            name,
                            os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC,
                            dir_fd=current_fd,
                        )
                        file_stat = os.fstat(file_fd)
                        if (
                            not stat.S_ISREG(file_stat.st_mode)
                            or file_stat.st_size > _MAX_FILE_BYTES
                        ):
                            skipped_files += 1
                            continue
                        if scanned_bytes + file_stat.st_size > _MAX_TOTAL_BYTES:
                            mark_truncated("max_total_bytes")
                            stop = True
                            break
                        raw = os.read(file_fd, _MAX_FILE_BYTES + 1)
                    except OSError:
                        skipped_files += 1
                        continue
                    finally:
                        if file_fd is not None:
                            try:
                                os.close(file_fd)
                            except OSError:
                                pass
                    scanned_bytes += len(raw)
                    if b"\x00" in raw:
                        skipped_files += 1
                        continue
                    try:
                        content = raw.decode("utf-8")
                    except UnicodeDecodeError:
                        skipped_files += 1
                        continue
                    searched_files += 1
                    matches_in_file = 0
                    for line_number, line in enumerate(content.splitlines(), 1):
                        if query_cf not in line.casefold():
                            continue
                        matches_in_file += 1
                        matching_lines += 1
                        if matches_in_file <= _MAX_MATCHES_PER_FILE:
                            matches.append(
                                {
                                    "path": rel_path,
                                    "line_number": line_number,
                                    "preview": _line_preview(line, query_cf),
                                }
                            )
                    if matches_in_file:
                        matched_files += 1
                    if matches_in_file > _MAX_MATCHES_PER_FILE:
                        mark_truncated("max_matches_per_file")
            except (PermissionError, path_policy._DirectorySymlinkError, OSError):
                if depth == 0:
                    return _error("SEARCH_FAILED", "读取搜索起始目录时发生系统错误")
                skipped_files += 1
            finally:
                if current_fd is not None:
                    try:
                        os.close(current_fd)
                    except OSError:
                        pass
    finally:
        try:
            os.close(search_root_fd)
        except OSError:
            pass

    candidate_matches = len(matches)
    if source_only:
        matches.sort(key=lambda item: _source_match_priority(item, query_cf))
    if candidate_matches > max_results:
        matches = matches[:max_results]
        mark_truncated("max_results")
    scan_complete = not any(reason in _SCAN_TRUNCATION_REASONS for reason in truncation_reasons)
    result_window_truncated = candidate_matches > len(matches) or matching_lines > candidate_matches

    summary = (
        f"正文搜索完成: query='{query}'; 返回 {len(matches)}/{candidate_matches} 个候选，"
        f"命中 {matched_files} 个文件/{matching_lines} 行；"
        + ("扫描范围完整" if scan_complete else "扫描范围未完整")
    )
    if not matches:
        summary += "; 未发现精确子串匹配，不代表目录中没有相关概念，可改用较短关键词"
    elif result_window_truncated:
        summary += "; 返回窗口已截断，同 query/path 重试不会翻页"
    log.info(
        "workspace.search_text: query=%s results=%d files=%d bytes=%d truncated=%s",
        query,
        len(matches),
        searched_files,
        scanned_bytes,
        truncated,
    )
    return ToolResult(
        ok=True,
        kind="json",
        summary=summary,
        data={
            "search_path": canonical_path,
            "query": query,
            "source_only": source_only,
            "matches": matches,
            "returned_matches": len(matches),
            "candidate_matches": candidate_matches,
            "matching_lines": matching_lines,
            "matched_files": matched_files,
            "scanned_entries": scanned_entries,
            "scanned_files": scanned_files,
            "searched_files": searched_files,
            "scanned_bytes": scanned_bytes,
            "excluded_entries": excluded_entries,
            "skipped_files": skipped_files,
            "scan_complete": scan_complete,
            "result_window_truncated": result_window_truncated,
            "truncated": truncated,
            "truncation_reasons": truncation_reasons,
        },
    )
