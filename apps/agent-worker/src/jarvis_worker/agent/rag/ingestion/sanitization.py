"""RAG 文档进入 PostgreSQL 前的最小文本规范化。"""

from __future__ import annotations

from typing import Any


def remove_nul(value: str) -> str:
    """移除 PostgreSQL UTF-8 text/JSONB 不接受的 U+0000。"""

    return value.replace("\x00", "")


def remove_nul_from_json(value: Any) -> Any:
    """递归清理 JSON-like 数据中的 U+0000，不改变其他字符。"""

    if isinstance(value, str):
        return remove_nul(value)
    if isinstance(value, dict):
        cleaned: dict[Any, Any] = {}
        for key, item in value.items():
            clean_key = remove_nul(key) if isinstance(key, str) else key
            if clean_key in cleaned and clean_key != key:
                raise ValueError("RAG 结构数据清理后出现重复字段")
            cleaned[clean_key] = remove_nul_from_json(item)
        return cleaned
    if isinstance(value, list):
        return [remove_nul_from_json(item) for item in value]
    if isinstance(value, tuple):
        return tuple(remove_nul_from_json(item) for item in value)
    return value
