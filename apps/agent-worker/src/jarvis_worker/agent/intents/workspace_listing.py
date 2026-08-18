"""显式 Workspace 列举目标的高置信度类型提取。"""

from __future__ import annotations

import re

_DIRECTORY_TARGET = re.compile(
    r"(?:(?:列出|列一下|列|查看|看看|告诉我|有哪些|哪些)"
    r"[^，,。！？!?；;\n]{0,40}(?:一级)?(?:目录|文件夹)|"
    r"(?:list|show|what|which)[^,.;!?\n]{0,40}(?:directories|folders))",
    re.IGNORECASE,
)
_FILE_TARGET = re.compile(
    r"(?:(?:列出|列一下|列|查看|看看|告诉我|有哪些|哪些)"
    r"[^，,。！？!?；;\n]{0,40}文件(?!夹)|"
    r"(?:list|show|what|which)[^,.;!?\n]{0,40}files?)",
    re.IGNORECASE,
)
_SYMLINK_TARGET = re.compile(
    r"(?:(?:列出|列一下|列|查看|看看|告诉我|有哪些|哪些)"
    r"[^，,。！？!?；;\n]{0,40}(?:符号链接|软链接)|"
    r"(?:list|show|what|which)[^,.;!?\n]{0,40}(?:symbolic links?|symlinks?))",
    re.IGNORECASE,
)


def explicit_workspace_listing_entry_types(user_goal: str) -> tuple[str, ...]:
    """只提取显式列举宾语；否定的副作用名词不会被提升为结果类型。"""
    if not isinstance(user_goal, str) or not user_goal.strip():
        return ()
    goal = user_goal[:10_000]
    result: list[str] = []
    for entry_type, pattern in (
        ("file", _FILE_TARGET),
        ("dir", _DIRECTORY_TARGET),
        ("symlink", _SYMLINK_TARGET),
    ):
        if pattern.search(goal):
            result.append(entry_type)
    return tuple(result)
