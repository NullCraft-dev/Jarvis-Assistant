"""WorkspacePolicy — 任务工作区允许范围的唯一应用层校验入口。"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from jarvis_worker.shared.errors.application import AppError


def _canonical_directory(path: str) -> str:
    """返回已展开、绝对化并解析符号链接的目录路径。"""
    return str(Path(path).expanduser().resolve(strict=False))


def _unique_paths(paths: Iterable[str]) -> tuple[str, ...]:
    result: list[str] = []
    for raw in paths:
        value = raw.strip()
        if not value:
            continue
        canonical = _canonical_directory(value)
        if canonical not in result:
            result.append(canonical)
    return tuple(result)


@dataclass(frozen=True)
class WorkspacePolicy:
    """校验 Task.workspace_path 只能落在服务端配置的允许根目录中。"""

    default_workspace_path: str | None
    allowed_workspace_paths: tuple[str, ...]

    @classmethod
    def from_env(cls) -> "WorkspacePolicy":
        default_raw = os.getenv("JARVIS_WORKSPACE_ROOT", "").strip()
        allowed_raw = os.getenv("JARVIS_ALLOWED_WORKSPACE_PATHS", "")
        configured = allowed_raw.split(os.pathsep) if allowed_raw else []
        if default_raw:
            configured.insert(0, default_raw)

        allowed = _unique_paths(configured)
        default = _canonical_directory(default_raw) if default_raw else None
        return cls(default_workspace_path=default, allowed_workspace_paths=allowed)

    def resolve(self, requested_path: str | None) -> str | None:
        """解析任务工作区；未指定时使用默认值，越界时 fail closed。"""
        requested = (requested_path or "").strip()
        selected = requested or self.default_workspace_path
        if not selected:
            return None

        candidate = _canonical_directory(selected)
        if not self.allowed_workspace_paths:
            raise AppError(
                code="WORKSPACE_ACCESS_DENIED",
                message="服务端尚未配置允许访问的工作区",
                category="permission",
                recoverable=False,
            )

        if not any(_is_within(candidate, root) for root in self.allowed_workspace_paths):
            raise AppError(
                code="WORKSPACE_ACCESS_DENIED",
                message="所选工作区不在服务端允许范围内",
                category="permission",
                recoverable=False,
            )

        if not Path(candidate).is_dir():
            raise AppError(
                code="WORKSPACE_NOT_FOUND",
                message="所选工作区不存在或不是目录",
                category="validation",
                recoverable=False,
            )
        return candidate


def _is_within(candidate: str, root: str) -> bool:
    try:
        return os.path.commonpath((candidate, root)) == root
    except ValueError:
        return False
