"""Provider 无关的语义重排与稳健排名融合。"""

from __future__ import annotations

import logging
from dataclasses import dataclass, replace
from time import perf_counter

from jarvis_worker.agent.rag.reranking.contracts import (
    RagRerankResult,
    RerankDocument,
    RerankerProvider,
    RerankerProviderError,
)
from jarvis_worker.agent.rag.retrieval.contracts import (
    RagQueryPlan,
    RagRerankerStepTrace,
    RagRetrievalQuery,
)
from jarvis_worker.agent.rag.retrieval.coverage import select_with_document_coverage
from jarvis_worker.agent.rag.retrieval.repository import RagCandidateRecord

log = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class SemanticRerankerConfig:
    semantic_weight: float = 0.75
    retrieval_weight: float = 0.25
    max_candidates: int = 30
    max_content_chars: int = 3_200
    max_heading_chars: int = 500
    fallback_on_error: bool = True

    def __post_init__(self) -> None:
        if self.semantic_weight < 0 or self.retrieval_weight < 0:
            raise ValueError("Reranker 融合权重不能为负数")
        if self.semantic_weight + self.retrieval_weight <= 0:
            raise ValueError("Reranker 融合权重之和必须大于 0")
        if not 1 <= self.max_candidates <= 100:
            raise ValueError("Reranker max_candidates 必须在 1..100")
        if not 256 <= self.max_content_chars <= 20_000:
            raise ValueError("Reranker max_content_chars 必须在 256..20000")
        if not 0 <= self.max_heading_chars <= 2_000:
            raise ValueError("Reranker max_heading_chars 必须在 0..2000")


class SemanticReranker:
    stage_id = "cross-encoder-rank-fusion-v1"

    def __init__(
        self,
        provider: RerankerProvider,
        *,
        config: SemanticRerankerConfig | None = None,
    ) -> None:
        self._provider = provider
        self._config = config or SemanticRerankerConfig()

    async def rerank(
        self,
        *,
        plan: RagQueryPlan,
        candidates: list[RagCandidateRecord],
        request: RagRetrievalQuery,
    ) -> RagRerankResult:
        if not candidates:
            return RagRerankResult(
                candidates=(),
                steps=(self._trace("skipped", 0, 0, 0),),
            )
        if len(candidates) > self._config.max_candidates:
            raise ValueError("Reranker 候选数量超过配置上限")

        documents = tuple(
            self._document(candidate, query=plan.original_query) for candidate in candidates
        )
        started = perf_counter()
        try:
            scores = await self._provider.score(
                query=plan.original_query,
                documents=documents,
            )
            score_by_chunk = _validate_scores(documents, scores)
        except Exception as exc:
            if not self._config.fallback_on_error:
                raise
            latency_ms = _elapsed_ms(started)
            failure_code = (
                exc.code if isinstance(exc, RerankerProviderError) else "RERANKER_PROVIDER_FAILED"
            )
            log.warning(
                "RAG semantic reranker degraded: provider=%s model=%s code=%s",
                self._provider.provider_name,
                self._provider.model_name,
                failure_code,
            )
            return RagRerankResult(
                candidates=tuple(candidates),
                steps=(
                    self._trace(
                        "degraded",
                        len(candidates),
                        len(candidates),
                        latency_ms,
                        failure_code=failure_code,
                    ),
                ),
            )

        semantic_order = sorted(
            enumerate(candidates),
            key=lambda item: (
                -score_by_chunk[item[1].chunk_id],
                item[0],
                str(item[1].chunk_id),
            ),
        )
        semantic_rank = {
            candidate.chunk_id: rank
            for rank, (_index, candidate) in enumerate(semantic_order, start=1)
        }
        count = len(candidates)
        semantic_weight, retrieval_weight = self._normalized_weights()
        blended = []
        for retrieval_rank, candidate in enumerate(candidates, start=1):
            fused_score = semantic_weight * _rank_score(
                semantic_rank[candidate.chunk_id], count
            ) + retrieval_weight * _rank_score(retrieval_rank, count)
            blended.append(
                replace(
                    candidate,
                    score=fused_score,
                    trace=replace(
                        candidate.trace,
                        cross_encoder_score=score_by_chunk[candidate.chunk_id],
                        fused_score=fused_score,
                    ),
                )
            )
        blended.sort(
            key=lambda candidate: (
                -candidate.score,
                semantic_rank[candidate.chunk_id],
                str(candidate.chunk_id),
            )
        )
        selected = select_with_document_coverage(
            blended,
            limit=request.effective_cross_encoder_limit,
            document_ids=request.document_ids,
        )
        return RagRerankResult(
            candidates=tuple(selected),
            steps=(
                self._trace(
                    "applied",
                    count,
                    len(selected),
                    _elapsed_ms(started),
                ),
            ),
        )

    def _document(self, candidate: RagCandidateRecord, *, query: str) -> RerankDocument:
        raw_heading = candidate.source_locator.get("heading_path", ())
        heading_path = (
            tuple(str(value).strip() for value in raw_heading if str(value).strip())
            if isinstance(raw_heading, (list, tuple))
            else ()
        )
        heading_budget = self._config.max_heading_chars
        bounded_headings: list[str] = []
        for heading in heading_path:
            if heading_budget <= 0:
                break
            bounded = heading[:heading_budget]
            bounded_headings.append(bounded)
            heading_budget -= len(bounded)
        return RerankDocument(
            chunk_id=candidate.chunk_id,
            document_title=candidate.document_title,
            heading_path=tuple(bounded_headings),
            content=_compact_content(
                candidate.content,
                query=query,
                max_chars=self._config.max_content_chars,
            ),
        )

    def _normalized_weights(self) -> tuple[float, float]:
        total = self._config.semantic_weight + self._config.retrieval_weight
        return (
            self._config.semantic_weight / total,
            self._config.retrieval_weight / total,
        )

    def _trace(
        self,
        status: str,
        input_count: int,
        output_count: int,
        latency_ms: int,
        *,
        failure_code: str = "",
    ) -> RagRerankerStepTrace:
        return RagRerankerStepTrace(
            stage_id=self.stage_id,
            status=status,
            provider=self._provider.provider_name,
            model=self._provider.model_name,
            input_count=input_count,
            output_count=output_count,
            latency_ms=latency_ms,
            failure_code=failure_code,
        )


def _validate_scores(documents, scores) -> dict:
    expected = {document.chunk_id for document in documents}
    actual = [score.chunk_id for score in scores]
    if len(actual) != len(set(actual)) or set(actual) != expected:
        raise RerankerProviderError("RERANKER_PROVIDER_CONTRACT_VIOLATION")
    return {score.chunk_id: float(score.score) for score in scores}


def _rank_score(rank: int, count: int) -> float:
    if count <= 1:
        return 1.0
    return 1.0 - (rank - 1) / (count - 1)


def _elapsed_ms(started: float) -> int:
    return max(0, round((perf_counter() - started) * 1_000))


def _compact_content(content: str, *, query: str, max_chars: int) -> str:
    """压缩异常超长候选；保留首尾并优先保留精确查询命中附近的证据。"""

    normalized = content.strip()
    if len(normalized) <= max_chars:
        return normalized
    marker = "\n…\n"
    usable = max_chars - len(marker) * 2
    head_budget = usable // 3
    tail_budget = usable // 4
    middle_budget = usable - head_budget - tail_budget
    query_position = normalized.casefold().find(query.strip().casefold()) if query.strip() else -1
    if query_position < 0:
        middle_start = max(head_budget, (len(normalized) - middle_budget) // 2)
    else:
        middle_start = max(head_budget, query_position - middle_budget // 2)
    middle_start = min(middle_start, len(normalized) - tail_budget - middle_budget)
    return (
        normalized[:head_budget].rstrip()
        + marker
        + normalized[middle_start : middle_start + middle_budget].strip()
        + marker
        + normalized[-tail_budget:].lstrip()
    )
