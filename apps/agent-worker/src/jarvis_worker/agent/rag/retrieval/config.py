"""PostgreSQL/pgvector 在线检索参数。"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Literal


HnswIterativeScan = Literal["off", "strict_order", "relaxed_order"]


@dataclass(frozen=True, slots=True)
class HnswSearchConfig:
    """单次语义召回使用的事务级 HNSW 搜索策略。"""

    ef_search: int = 100
    iterative_scan: HnswIterativeScan = "relaxed_order"
    max_scan_tuples: int = 20_000
    scan_mem_multiplier: int = 1

    def __post_init__(self) -> None:
        if not 1 <= self.ef_search <= 1_000:
            raise ValueError("RAG HNSW ef_search 必须在 1..1000")
        if self.iterative_scan not in {"off", "strict_order", "relaxed_order"}:
            raise ValueError("RAG HNSW iterative_scan 仅支持 off/strict_order/relaxed_order")
        if not 1 <= self.max_scan_tuples <= 1_000_000:
            raise ValueError("RAG HNSW max_scan_tuples 必须在 1..1000000")
        if not 1 <= self.scan_mem_multiplier <= 1_000:
            raise ValueError("RAG HNSW scan_mem_multiplier 必须在 1..1000")

    @classmethod
    def from_env(cls) -> "HnswSearchConfig":
        iterative_scan = os.getenv("JARVIS_RAG_HNSW_ITERATIVE_SCAN", "relaxed_order").strip()
        if iterative_scan not in {"off", "strict_order", "relaxed_order"}:
            raise ValueError("JARVIS_RAG_HNSW_ITERATIVE_SCAN 仅支持 off/strict_order/relaxed_order")
        return cls(
            ef_search=_parse_int("JARVIS_RAG_HNSW_EF_SEARCH", 100, minimum=1, maximum=1_000),
            iterative_scan=iterative_scan,
            max_scan_tuples=_parse_int(
                "JARVIS_RAG_HNSW_MAX_SCAN_TUPLES",
                20_000,
                minimum=1,
                maximum=1_000_000,
            ),
            scan_mem_multiplier=_parse_int(
                "JARVIS_RAG_HNSW_SCAN_MEM_MULTIPLIER",
                1,
                minimum=1,
                maximum=1_000,
            ),
        )

    def transaction_settings(self) -> tuple[tuple[str, str], ...]:
        return (
            ("hnsw.ef_search", str(self.ef_search)),
            ("hnsw.iterative_scan", self.iterative_scan),
            ("hnsw.max_scan_tuples", str(self.max_scan_tuples)),
            ("hnsw.scan_mem_multiplier", str(self.scan_mem_multiplier)),
        )


def _parse_int(name: str, default: int, *, minimum: int, maximum: int) -> int:
    raw = os.getenv(name)
    if raw is None:
        return default
    try:
        value = int(raw)
    except ValueError:
        raise ValueError(f"{name} 必须是整数，当前: {raw!r}") from None
    if value < minimum or value > maximum:
        raise ValueError(f"{name} 必须在 {minimum}..{maximum}，当前: {value}")
    return value
