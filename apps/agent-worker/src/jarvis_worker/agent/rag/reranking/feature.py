"""低成本、可解释的候选特征重排。"""

from __future__ import annotations

import unicodedata
from dataclasses import replace
from time import perf_counter

from jarvis_worker.agent.rag.reranking.contracts import RagRerankResult
from jarvis_worker.agent.rag.retrieval.contracts import RagRerankerStepTrace
from jarvis_worker.agent.rag.retrieval.coverage import select_with_document_coverage
from jarvis_worker.agent.rag.retrieval.keyword import build_keyword_terms


class FeatureReranker:
    stage_id = "feature-rank-v1"

    async def rerank(self, *, plan, candidates, request) -> RagRerankResult:
        started = perf_counter()
        if not candidates:
            return RagRerankResult(
                candidates=(),
                steps=(self._trace("skipped", 0, 0, started),),
            )

        query = _normalize(plan.original_query)
        terms = build_keyword_terms(query)
        count = len(candidates)
        scored = []
        for rank, candidate in enumerate(candidates, start=1):
            content = _normalize(candidate.content)
            title = _normalize(candidate.document_title)
            heading = _normalize(_heading_text(candidate.source_locator))
            coverage = _coverage(terms, f"{title}\n{heading}\n{content}")
            title_match = _coverage(terms, title)
            heading_match = _coverage(terms, heading)
            exact_phrase = float(len(query) >= 2 and query in f"{title}\n{heading}\n{content}")
            quality = _content_quality(candidate.token_count, content)
            retrieval_rank = _rank_score(rank, count)
            feature_score = (
                0.30 * retrieval_rank
                + 0.25 * coverage
                + 0.15 * title_match
                + 0.15 * heading_match
                + 0.10 * exact_phrase
                + 0.05 * quality
            )
            scored.append(
                replace(
                    candidate,
                    score=feature_score,
                    trace=replace(candidate.trace, feature_score=feature_score),
                )
            )
        scored.sort(key=lambda item: (-item.score, str(item.chunk_id)))
        selected = select_with_document_coverage(
            scored,
            limit=request.effective_feature_limit,
            document_ids=request.document_ids,
        )
        return RagRerankResult(
            candidates=tuple(selected),
            steps=(self._trace("applied", count, len(selected), started),),
        )

    def _trace(self, status, input_count, output_count, started):
        return RagRerankerStepTrace(
            stage_id=self.stage_id,
            status=status,
            provider="deterministic-feature",
            model=self.stage_id,
            input_count=input_count,
            output_count=output_count,
            latency_ms=max(0, round((perf_counter() - started) * 1_000)),
        )


def _normalize(value: str) -> str:
    return unicodedata.normalize("NFKC", value).casefold().strip()


def _heading_text(locator: dict) -> str:
    value = locator.get("heading_path", ())
    if not isinstance(value, (list, tuple)):
        return ""
    return " ".join(str(item) for item in value if str(item).strip())


def _coverage(terms: tuple[str, ...], text: str) -> float:
    if not terms or not text:
        return 0.0
    return sum(term in text for term in terms) / len(terms)


def _content_quality(token_count: int, content: str) -> float:
    if not content:
        return 0.0
    if token_count < 8:
        return 0.25
    if token_count < 24:
        return 0.7
    return 1.0


def _rank_score(rank: int, count: int) -> float:
    if count <= 1:
        return 1.0
    return 1.0 - (rank - 1) / (count - 1)
