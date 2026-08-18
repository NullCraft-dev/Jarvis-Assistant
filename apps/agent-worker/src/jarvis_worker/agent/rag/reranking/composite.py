"""按顺序组合多个 Reranker，并在每个插件边界验证候选集合。"""

from __future__ import annotations

from jarvis_worker.agent.rag.reranking.contracts import RagRerankResult
from jarvis_worker.agent.rag.retrieval.contracts import RagQueryPlan, RagRetrievalQuery
from jarvis_worker.agent.rag.retrieval.repository import RagCandidateRecord


class CompositeReranker:
    stage_id = "retrieval-rerank-pipeline-v1"

    def __init__(self, *rerankers) -> None:
        if not rerankers:
            raise ValueError("CompositeReranker 至少需要一个阶段")
        self._rerankers = rerankers

    async def rerank(
        self,
        *,
        plan: RagQueryPlan,
        candidates: list[RagCandidateRecord],
        request: RagRetrievalQuery,
    ) -> RagRerankResult:
        current = list(candidates)
        steps = []
        for reranker in self._rerankers:
            allowed_ids = {candidate.chunk_id for candidate in current}
            result = await reranker.rerank(
                plan=plan,
                candidates=current,
                request=request,
            )
            output_ids = [candidate.chunk_id for candidate in result.candidates]
            if len(output_ids) != len(set(output_ids)) or any(
                chunk_id not in allowed_ids for chunk_id in output_ids
            ):
                raise ValueError("Reranker 阶段返回了重复或非输入候选")
            current = list(result.candidates)
            steps.extend(result.steps)
        return RagRerankResult(candidates=tuple(current), steps=tuple(steps))
