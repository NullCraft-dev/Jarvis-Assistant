"""Pure-semantic filenames for Jarvis-managed knowledge documents."""

from __future__ import annotations

import re
from pathlib import PurePosixPath

from jarvis_worker.shared.errors.application import AppError

KIND_DIRECTORIES = {"report": "Reports", "note": "Notes", "source": "Sources"}
MAX_FILENAME_BYTES = 220


class KnowledgeDocumentNamingPolicy:
    def relative_path(self, *, title: str, kind: str) -> str:
        directory = KIND_DIRECTORIES.get(kind)
        if directory is None:
            raise AppError("VALIDATION_ERROR", "不支持的知识文档类型", "validation")
        filename = self._semantic_filename(title)
        return PurePosixPath(directory, f"{filename}.md").as_posix()

    @staticmethod
    def _semantic_filename(title: str) -> str:
        normalized = re.sub(r"\s+", " ", title).strip()
        normalized = re.sub(r"[\\/:*?\"<>|#^[\]]+", "-", normalized).strip(" .-")
        normalized = _truncate_utf8(normalized, MAX_FILENAME_BYTES).strip(" .-")
        return normalized or "未命名"


def _truncate_utf8(value: str, maximum_bytes: int) -> str:
    encoded = value.encode("utf-8")
    if len(encoded) <= maximum_bytes:
        return value
    return encoded[:maximum_bytes].decode("utf-8", errors="ignore")
