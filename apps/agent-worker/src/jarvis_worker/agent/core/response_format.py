"""面向用户的最终回复规范化。

模型动作仍使用严格 JSON；本模块只处理 ``finish.final_message``，防止模型把
内部 AgentAction 包装或整段 Markdown 围栏泄漏到用户界面。
"""

from __future__ import annotations

import json
import re
from typing import Any


_MARKDOWN_FENCE = re.compile(
    r"\A```(?:markdown|md)\s*\n(?P<body>[\s\S]*?)\n```\s*\Z",
    re.IGNORECASE,
)


def normalize_final_message(value: str) -> str:
    """返回可作为 CommonMark Markdown 展示的最终回复。

    只执行无歧义、可逆的确定性修复：统一换行、解开一层嵌套的 finish
    AgentAction，以及移除包裹整段回答的 markdown fence。普通 JSON、代码块和
    用户要求的结构化内容会被原样保留。
    """

    normalized = value.replace("\r\n", "\n").replace("\r", "\n").strip()
    nested = _nested_finish_message(normalized)
    if nested is not None:
        normalized = nested.replace("\r\n", "\n").replace("\r", "\n").strip()
    match = _MARKDOWN_FENCE.fullmatch(normalized)
    if match is not None:
        normalized = match.group("body").strip()
    return normalized


def _nested_finish_message(value: str) -> str | None:
    try:
        parsed: Any = json.loads(value)
    except (json.JSONDecodeError, TypeError):
        return None
    if not isinstance(parsed, dict) or parsed.get("action_type") != "finish":
        return None
    final_message = parsed.get("final_message")
    return final_message if isinstance(final_message, str) and final_message.strip() else None
