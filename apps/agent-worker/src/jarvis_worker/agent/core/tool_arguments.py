"""模型工具参数的窄范围、无歧义规范化。

这里只修复 JSON 形状上可以唯一解释的值，不做语义推断，也不替代
ToolGateway 的 schema 校验。
"""

from __future__ import annotations

from typing import Any


def normalize_tool_arguments(
    tool_name: str,
    arguments: dict[str, Any],
) -> dict[str, Any]:
    normalized = dict(arguments)
    if tool_name != "rag.search" or "document_ids" not in normalized:
        return normalized

    document_ids = normalized["document_ids"]
    if document_ids is None:
        normalized.pop("document_ids")
    elif isinstance(document_ids, str) and document_ids.strip():
        normalized["document_ids"] = [document_ids.strip()]
    return normalized
