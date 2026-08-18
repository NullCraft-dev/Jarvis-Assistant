"""模型调用前的确定性硬过滤与完全去重。"""

from __future__ import annotations

from time import perf_counter

from jarvis_worker.agent.rag.reranking.contracts import RagRerankResult
from jarvis_worker.agent.rag.retrieval.contracts import RagRerankerStepTrace


class HardFilter:
    stage_id = "hard-filter-v1"

    async def rerank(self, *, plan, candidates, request) -> RagRerankResult:
        del plan
        started = perf_counter()
        selected = []
        seen_hashes: set[object] = set()
        for candidate in candidates:
            if candidate.score < request.min_score:
                continue
            dedupe_key: object = (
                (candidate.document_id, candidate.content_hash)
                if request.document_ids
                else candidate.content_hash
            )
            if not candidate.content.strip() or dedupe_key in seen_hashes:
                continue
            seen_hashes.add(dedupe_key)
            selected.append(candidate)
            if len(selected) >= request.candidate_limit:
                break
        return RagRerankResult(
            candidates=tuple(selected),
            steps=(
                RagRerankerStepTrace(
                    stage_id=self.stage_id,
                    status="applied",
                    provider="deterministic-filter",
                    model=self.stage_id,
                    input_count=len(candidates),
                    output_count=len(selected),
                    latency_ms=_elapsed_ms(started),
                ),
            ),
        )


def _elapsed_ms(started: float) -> int:
    return max(0, round((perf_counter() - started) * 1_000))
