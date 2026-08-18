"""Bounded, read-only path suggestions for recoverable Workspace misses."""

from __future__ import annotations

import re
from difflib import SequenceMatcher
from pathlib import PurePosixPath

from jarvis_worker.agent.tool_gateway.contracts import ToolRequest

from .search_files import execute_workspace_search_files

_MAX_SUGGESTIONS = 5
_MAX_SEARCH_RESULTS = 100
_MAX_QUERY_TOKENS = 2
_MIN_SCORE = 0.34
_TOKEN_SPLIT = re.compile(r"[^a-zA-Z0-9]+")


def find_path_suggestions(
    *,
    workspace_root: str,
    requested_path: str,
    expected_type: str,
) -> list[str]:
    """Return ranked existing relative paths without auto-selecting one.

    The helper reuses the bounded, dir-fd based filename search capability. It
    only runs after a recoverable miss, returns at most five paths, and never
    changes the requested target or performs an implicit read.
    """
    if expected_type not in {"file", "dir"}:
        return []
    tokens = _query_tokens(requested_path)
    if not tokens:
        return []

    matches: dict[str, dict[str, object]] = {}
    for token in tokens[:_MAX_QUERY_TOKENS]:
        result = execute_workspace_search_files(
            ToolRequest(
                task_id="path-suggestion",
                run_id="path-suggestion",
                tool_name="workspace.search_files",
                arguments={
                    "workspace_root": workspace_root,
                    "query": token,
                    "path": ".",
                    "max_results": _MAX_SEARCH_RESULTS,
                },
                requested_by="system",
            )
        )
        data = result.data if result.ok and isinstance(result.data, dict) else {}
        raw_matches = data.get("matches", [])
        if not isinstance(raw_matches, list):
            continue
        for item in raw_matches:
            if not isinstance(item, dict) or item.get("type") != expected_type:
                continue
            path = item.get("path")
            if isinstance(path, str) and path:
                matches[path] = item
        # The strongest leaf token usually resolves the miss. Avoid rescanning
        # the same workspace with a weaker fallback token once typed matches
        # already exist; the second bounded scan is only a recovery path.
        if matches:
            break

    ranked = sorted(
        ((_path_similarity(requested_path, candidate), candidate) for candidate in matches),
        key=lambda item: (-item[0], item[1].casefold(), item[1]),
    )
    return [path for score, path in ranked if score >= _MIN_SCORE][:_MAX_SUGGESTIONS]


def _query_tokens(path: str) -> list[str]:
    normalized = path.replace("\\", "/").strip("/")
    leaf = PurePosixPath(normalized).name
    stem = PurePosixPath(leaf).stem
    raw = [stem, *_TOKEN_SPLIT.split(stem), *reversed(PurePosixPath(normalized).parts)]
    result: list[str] = []
    for value in raw:
        token = value.casefold().strip()
        if len(token) < 3 or token in result:
            continue
        result.append(token)
    # Prefer semantic leaf tokens over a compound name that may not exist.
    result.sort(key=lambda value: ("_" in value or "-" in value, -len(value), value))
    return result


def _path_similarity(requested: str, candidate: str) -> float:
    requested_cf = requested.replace("\\", "/").casefold().strip("/")
    candidate_cf = candidate.casefold().strip("/")
    requested_path = PurePosixPath(requested_cf)
    candidate_path = PurePosixPath(candidate_cf)
    requested_leaf = requested_path.name
    candidate_leaf = candidate_path.name
    requested_stem = requested_path.stem
    candidate_stem = candidate_path.stem

    requested_tokens = set(_TOKEN_SPLIT.split(requested_cf)) - {""}
    candidate_tokens = set(_TOKEN_SPLIT.split(candidate_cf)) - {""}
    union = requested_tokens | candidate_tokens
    token_overlap = len(requested_tokens & candidate_tokens) / len(union) if union else 0.0

    requested_parts = requested_path.parts
    candidate_parts = candidate_path.parts
    common_suffix = 0
    for left, right in zip(reversed(requested_parts), reversed(candidate_parts)):
        if left != right:
            break
        common_suffix += 1
    suffix_score = common_suffix / max(1, min(len(requested_parts), len(candidate_parts)))

    return (
        0.35 * SequenceMatcher(None, requested_leaf, candidate_leaf).ratio()
        + 0.25 * SequenceMatcher(None, requested_stem, candidate_stem).ratio()
        + 0.20 * SequenceMatcher(None, requested_cf, candidate_cf).ratio()
        + 0.15 * token_overlap
        + 0.05 * suffix_score
    )
