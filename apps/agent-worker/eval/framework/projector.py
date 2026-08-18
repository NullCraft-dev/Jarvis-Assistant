"""将真实生产 RagContextPackage 安全投影为评估样本。"""

from __future__ import annotations

from collections.abc import Iterable

from .contracts import RagEvaluationSample, RankedChunk


def evaluation_sample_from_context(
    package,
    *,
    trace_id: str,
    query_id: str,
    positive_chunk_ids: Iterable[str],
    hard_negative_chunk_ids: Iterable[str] = (),
) -> RagEvaluationSample:
    if package.pipeline is None:
        raise ValueError("生产 RagContextPackage 缺少 pipeline trace")
    pipeline = package.pipeline
    return RagEvaluationSample(
        trace_id=trace_id,
        query_id=query_id,
        query=package.query,
        positive_chunk_ids=frozenset(str(value) for value in positive_chunk_ids),
        hard_negative_chunk_ids=frozenset(
            str(value) for value in hard_negative_chunk_ids
        ),
        candidate_ranking=tuple(
            _ranked(value) for value in pipeline.retrieved_candidates
        ),
        reranked_ranking=tuple(
            _ranked(value) for value in pipeline.reranked_candidates
        ),
        context_chunk_ids=tuple(str(value) for value in pipeline.context_chunk_ids),
        context_truncated=package.truncated,
        pipeline_versions={
            "retrieval_policy": package.policy_version,
            "query_rewriter": pipeline.query_rewriter,
            "retriever": pipeline.retriever,
            "reranker": pipeline.reranker,
            "context_assembler": pipeline.context_assembler,
        },
    )


def evaluation_sample_from_trace(trace, label) -> RagEvaluationSample:
    """仅把已确认/已晋升的真实轨迹标签投影为回归样本。"""
    if trace.privacy_status != "approved":
        raise ValueError("只有通过隐私复核的 trace 可以进入评估")
    if label.status not in {"confirmed", "promoted"}:
        raise ValueError("只有 confirmed/promoted 标签可以进入评估")
    if str(label.trace_id) != str(trace.id):
        raise ValueError("RAG trace 与 label 不匹配")
    return RagEvaluationSample(
        trace_id=str(trace.id),
        query_id=str(label.id),
        query=trace.query,
        positive_chunk_ids=frozenset(str(value) for value in label.positive_chunk_ids),
        hard_negative_chunk_ids=frozenset(
            str(value) for value in label.hard_negative_chunk_ids
        ),
        candidate_ranking=tuple(_stored_ranked(value) for value in trace.candidate_ranking),
        reranked_ranking=tuple(_stored_ranked(value) for value in trace.reranked_ranking),
        context_chunk_ids=tuple(str(value) for value in trace.context_chunk_ids),
        context_truncated=trace.context_truncated,
        pipeline_versions=dict(trace.pipeline_versions),
    )


def _ranked(value) -> RankedChunk:
    return RankedChunk(
        chunk_id=str(value.chunk_id),
        document_id=str(value.document_id),
        rank=value.rank,
        score=value.score,
        sources=tuple(value.sources),
    )


def _stored_ranked(value: dict) -> RankedChunk:
    return RankedChunk(
        chunk_id=str(value["chunk_id"]),
        document_id=str(value.get("document_id", "")),
        rank=int(value["rank"]),
        score=float(value["score"]),
        sources=tuple(str(source) for source in value.get("sources", ())),
    )
