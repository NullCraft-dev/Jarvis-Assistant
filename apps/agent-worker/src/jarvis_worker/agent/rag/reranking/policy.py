"""最终业务策略选择；不拥有相关性算法。"""

from __future__ import annotations

from collections import defaultdict
from time import perf_counter

from jarvis_worker.agent.rag.reranking.contracts import RagRerankResult
from jarvis_worker.agent.rag.retrieval.contracts import RagRerankerStepTrace
from jarvis_worker.agent.rag.retrieval.coverage import document_coverage_candidates


class PolicySelector:
    stage_id = "policy-selector-v2"

    async def rerank(self, *, plan, candidates, request) -> RagRerankResult:
        del plan
        started = perf_counter()
        selected = []
        selected_chunk_ids = set()
        seen_hashes: set[str] = set()
        per_document = defaultdict(int)

        # A selected multi-document scope is a user constraint, not a ranking hint.
        # Reserve the best surviving candidate from every requested document before
        # filling the remaining top-k slots.  Runtime raises top_k to at least the
        # selected document count, so this remains bounded by the request contract.
        if request.document_ids:
            for candidate in document_coverage_candidates(
                candidates, request.document_ids
            ):
                selected.append(candidate)
                selected_chunk_ids.add(candidate.chunk_id)
                seen_hashes.add(candidate.content_hash)
                per_document[candidate.document_id] += 1
                if len(selected) >= request.top_k:
                    break

        for candidate in candidates:
            if len(selected) >= request.top_k:
                break
            if candidate.chunk_id in selected_chunk_ids:
                continue
            if candidate.content_hash in seen_hashes:
                continue
            if per_document[candidate.document_id] >= request.max_chunks_per_document:
                continue
            selected.append(candidate)
            selected_chunk_ids.add(candidate.chunk_id)
            seen_hashes.add(candidate.content_hash)
            per_document[candidate.document_id] += 1
        return RagRerankResult(
            candidates=tuple(selected),
            steps=(
                RagRerankerStepTrace(
                    stage_id=self.stage_id,
                    status="applied" if candidates else "skipped",
                    provider="deterministic-policy",
                    model=self.stage_id,
                    input_count=len(candidates),
                    output_count=len(selected),
                    latency_ms=max(0, round((perf_counter() - started) * 1_000)),
                ),
            ),
        )
