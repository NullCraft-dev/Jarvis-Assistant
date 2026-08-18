"""带文档配额的确定性 MMR 多样性选择。"""

from __future__ import annotations

import re
import unicodedata
from collections import defaultdict
from dataclasses import dataclass, replace
from time import perf_counter

from jarvis_worker.agent.rag.reranking.contracts import RagRerankResult
from jarvis_worker.agent.rag.retrieval.contracts import RagRerankerStepTrace
from jarvis_worker.agent.rag.retrieval.coverage import document_coverage_candidates

_TOKEN = re.compile(r"[a-z0-9_+.#/-]{2,64}|[\u3400-\u9fff]{2,8}")


@dataclass(frozen=True, slots=True)
class MmrDiversityConfig:
    relevance_weight: float = 0.8

    def __post_init__(self) -> None:
        if not 0.5 <= self.relevance_weight <= 1.0:
            raise ValueError("MMR relevance_weight 必须在 0.5..1.0")


class QuotaAwareMmrReranker:
    stage_id = "quota-mmr-v1"

    def __init__(self, config: MmrDiversityConfig | None = None) -> None:
        self._config = config or MmrDiversityConfig()

    async def rerank(self, *, plan, candidates, request) -> RagRerankResult:
        del plan
        started = perf_counter()
        remaining = list(candidates)
        selected = []
        per_document = defaultdict(int)
        token_sets = {item.chunk_id: _tokens(item.content) for item in remaining}
        weight = self._config.relevance_weight

        for candidate in document_coverage_candidates(remaining, request.document_ids):
            if len(selected) >= request.effective_diversity_limit:
                break
            score = _mmr_score(candidate, selected, token_sets, weight)
            selected.append(
                replace(
                    candidate,
                    trace=replace(candidate.trace, mmr_score=score),
                )
            )
            per_document[candidate.document_id] += 1
            remaining.remove(candidate)

        while remaining and len(selected) < request.effective_diversity_limit:
            eligible = [
                item
                for item in remaining
                if per_document[item.document_id] < request.max_chunks_per_document
            ]
            if not eligible:
                break
            best = max(
                eligible,
                key=lambda item: (
                    _mmr_score(item, selected, token_sets, weight),
                    item.score,
                    str(item.chunk_id),
                ),
            )
            score = _mmr_score(best, selected, token_sets, weight)
            selected.append(
                replace(
                    best,
                    trace=replace(best.trace, mmr_score=score),
                )
            )
            per_document[best.document_id] += 1
            remaining.remove(best)

        return RagRerankResult(
            candidates=tuple(selected),
            steps=(
                RagRerankerStepTrace(
                    stage_id=self.stage_id,
                    status="applied" if candidates else "skipped",
                    provider="deterministic-mmr",
                    model=f"lexical-jaccard-lambda-{weight:.2f}",
                    input_count=len(candidates),
                    output_count=len(selected),
                    latency_ms=max(0, round((perf_counter() - started) * 1_000)),
                ),
            ),
        )


def _mmr_score(candidate, selected, token_sets, weight: float) -> float:
    if not selected:
        return candidate.score
    redundancy = max(
        _jaccard(token_sets[candidate.chunk_id], token_sets[item.chunk_id]) for item in selected
    )
    return weight * candidate.score - (1.0 - weight) * redundancy


def _tokens(content: str) -> frozenset[str]:
    normalized = unicodedata.normalize("NFKC", content).casefold()
    return frozenset(match.group(0) for match in _TOKEN.finditer(normalized))


def _jaccard(left: frozenset[str], right: frozenset[str]) -> float:
    if not left or not right:
        return 0.0
    return len(left & right) / len(left | right)
