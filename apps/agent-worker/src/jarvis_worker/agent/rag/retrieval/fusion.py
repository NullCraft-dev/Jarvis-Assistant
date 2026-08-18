"""检索候选融合算法。"""

from __future__ import annotations

from dataclasses import replace
from uuid import UUID

from jarvis_worker.agent.rag.retrieval.repository import (
    RagCandidateRecord,
    RagCandidateTrace,
    RagRetrievalSource,
)


def reciprocal_rank_fuse(
    rankings: dict[RagRetrievalSource, list[RagCandidateRecord]],
    *,
    rank_constant: int = 60,
) -> list[RagCandidateRecord]:
    """用归一化 RRF 融合异构分数，避免直接比较 cosine 与关键词分数。"""

    if rank_constant < 1:
        raise ValueError("RRF rank_constant 必须大于 0")
    if not rankings:
        return []
    route_count = len(rankings)
    maximum = route_count / (rank_constant + 1)
    records: dict[UUID, RagCandidateRecord] = {}
    contributions: dict[UUID, float] = {}
    traces: dict[UUID, dict[str, int | float]] = {}

    for source, candidates in rankings.items():
        for rank, candidate in enumerate(candidates, start=1):
            records.setdefault(candidate.chunk_id, candidate)
            contributions[candidate.chunk_id] = contributions.get(candidate.chunk_id, 0.0) + (
                1 / (rank_constant + rank)
            )
            traces.setdefault(candidate.chunk_id, {})[f"{source}_rank"] = rank
            traces[candidate.chunk_id][f"{source}_score"] = candidate.score

    fused: list[RagCandidateRecord] = []
    for chunk_id, candidate in records.items():
        values = traces[chunk_id]
        sources = tuple(source for source in ("semantic", "keyword") if f"{source}_rank" in values)
        score = min(1.0, contributions[chunk_id] / maximum)
        fused.append(
            replace(
                candidate,
                score=score,
                trace=RagCandidateTrace(
                    sources=sources,
                    semantic_rank=_optional_int(values.get("semantic_rank")),
                    semantic_score=_optional_float(values.get("semantic_score")),
                    keyword_rank=_optional_int(values.get("keyword_rank")),
                    keyword_score=_optional_float(values.get("keyword_score")),
                    rrf_score=score,
                ),
            )
        )
    return sorted(fused, key=lambda candidate: (-candidate.score, str(candidate.chunk_id)))


def _optional_int(value: int | float | None) -> int | None:
    return int(value) if value is not None else None


def _optional_float(value: int | float | None) -> float | None:
    return float(value) if value is not None else None
