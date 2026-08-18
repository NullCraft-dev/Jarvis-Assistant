"""语言无关且有界的 RAG 关键词词项准备。"""

from __future__ import annotations

import re
import unicodedata


_TOKEN = re.compile(r"[a-z0-9][a-z0-9_+.#/-]{1,63}|[\u3400-\u9fff]{2,48}")
_CJK = re.compile(r"^[\u3400-\u9fff]+$")
_MAX_TERMS = 16


def build_keyword_terms(query: str) -> tuple[str, ...]:
    """保留精确术语，并为较长中文片段生成少量四字窗口。"""

    normalized = unicodedata.normalize("NFKC", query).casefold()
    candidates: list[str] = []
    for match in _TOKEN.finditer(normalized):
        token = match.group(0).strip("_+.#/-")
        if len(token) < 2:
            continue
        candidates.append(token)
        if _CJK.fullmatch(token) and len(token) > 8:
            candidates.extend(token[offset : offset + 4] for offset in range(0, len(token) - 3, 2))

    unique: list[str] = []
    seen: set[str] = set()
    for term in sorted(candidates, key=lambda value: (-len(value), value)):
        if term in seen:
            continue
        seen.add(term)
        unique.append(term)
        if len(unique) >= _MAX_TERMS:
            break
    return tuple(unique)
