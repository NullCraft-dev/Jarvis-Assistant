"""不依赖 Judge 模型的分片、Embedding、召回、Reranker 与 Context 指标。"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence

from .contracts import EvaluationStage, RagEvaluationSample, RagMetricResult, RankedChunk


def evaluate_chunking_stage(
    *,
    chunk_node_ids: Sequence[Sequence[str]],
    token_counts: Sequence[int],
    must_keep_groups: Sequence[Sequence[str]],
    min_tokens: int,
    max_tokens: int,
) -> tuple[RagMetricResult, ...]:
    if len(chunk_node_ids) != len(token_counts):
        raise ValueError("Chunk node/token 数量不一致")
    memberships = [set(values) for values in chunk_node_ids]
    passed = sum(
        any(set(group).issubset(membership) for membership in memberships)
        for group in must_keep_groups
    )
    total_groups = len(must_keep_groups)
    total_chunks = len(token_counts)
    return (
        RagMetricResult(
            "chunk.must_keep_pass_rate",
            EvaluationStage.CHUNKING,
            _ratio(passed, total_groups),
            sample_count=max(total_groups, 1),
            details={"passed": passed, "total": total_groups},
        ),
        RagMetricResult(
            "chunk.max_token_violation_rate",
            EvaluationStage.CHUNKING,
            _ratio(sum(value > max_tokens for value in token_counts), total_chunks),
            sample_count=max(total_chunks, 1),
        ),
        RagMetricResult(
            "chunk.short_chunk_rate",
            EvaluationStage.CHUNKING,
            _ratio(sum(value < min_tokens for value in token_counts), total_chunks),
            sample_count=max(total_chunks, 1),
        ),
        RagMetricResult(
            "chunk.traceability_rate",
            EvaluationStage.CHUNKING,
            _ratio(sum(bool(values) for values in chunk_node_ids), len(chunk_node_ids)),
            sample_count=max(len(chunk_node_ids), 1),
        ),
    )


def evaluate_embedding_stage(
    *,
    query_vector: Sequence[float],
    positive_vectors: Mapping[str, Sequence[float]],
    hard_negative_vectors: Mapping[str, Sequence[float]],
) -> tuple[RagMetricResult, ...]:
    if not positive_vectors:
        raise ValueError("Embedding 评估至少需要一个 positive vector")
    positives = [_cosine(query_vector, value) for value in positive_vectors.values()]
    negatives = [_cosine(query_vector, value) for value in hard_negative_vectors.values()]
    pair_count = len(positives) * len(negatives)
    pairwise = (
        sum(positive > negative for positive in positives for negative in negatives)
        / pair_count
        if pair_count
        else 1.0
    )
    positive_mean = sum(positives) / len(positives)
    negative_mean = sum(negatives) / len(negatives) if negatives else 0.0
    return (
        RagMetricResult(
            "embedding.positive_similarity_mean",
            EvaluationStage.EMBEDDING,
            positive_mean,
            sample_count=len(positives),
        ),
        RagMetricResult(
            "embedding.hard_negative_similarity_mean",
            EvaluationStage.EMBEDDING,
            negative_mean,
            sample_count=max(len(negatives), 1),
        ),
        RagMetricResult(
            "embedding.positive_negative_margin",
            EvaluationStage.EMBEDDING,
            positive_mean - negative_mean,
            sample_count=max(pair_count, 1),
        ),
        RagMetricResult(
            "embedding.pairwise_ranking_accuracy",
            EvaluationStage.EMBEDDING,
            pairwise,
            sample_count=max(pair_count, 1),
        ),
    )


def evaluate_retrieval_stages(
    sample: RagEvaluationSample,
    *,
    cutoffs: Sequence[int] = (1, 3, 5, 10),
) -> tuple[RagMetricResult, ...]:
    if not sample.positive_chunk_ids:
        raise ValueError("召回评估需要 positive_chunk_ids")
    normalized_cutoffs = tuple(sorted({value for value in cutoffs if value > 0}))
    if not normalized_cutoffs:
        raise ValueError("cutoffs 至少需要一个正整数")
    metrics = [
        *_ranking_metrics(
            sample.candidate_ranking,
            sample.positive_chunk_ids,
            sample.hard_negative_chunk_ids,
            prefix="candidate",
            stage=EvaluationStage.CANDIDATE_RECALL,
            cutoffs=normalized_cutoffs,
        ),
        *_ranking_metrics(
            sample.reranked_ranking,
            sample.positive_chunk_ids,
            sample.hard_negative_chunk_ids,
            prefix="reranker",
            stage=EvaluationStage.RERANKER,
            cutoffs=normalized_cutoffs,
        ),
    ]
    candidate_mrr = _reciprocal_rank(sample.candidate_ranking, sample.positive_chunk_ids)
    reranker_mrr = _reciprocal_rank(sample.reranked_ranking, sample.positive_chunk_ids)
    candidate_positive = {
        value.chunk_id for value in sample.candidate_ranking
    } & sample.positive_chunk_ids
    reranked_positive = {
        value.chunk_id for value in sample.reranked_ranking
    } & sample.positive_chunk_ids
    context_positive = set(sample.context_chunk_ids) & sample.positive_chunk_ids
    metrics.extend(
        (
            RagMetricResult(
                "reranker.mrr_delta",
                EvaluationStage.RERANKER,
                reranker_mrr - candidate_mrr,
            ),
            RagMetricResult(
                "reranker.evidence_drop_rate",
                EvaluationStage.RERANKER,
                _ratio(len(candidate_positive - reranked_positive), len(candidate_positive)),
            ),
            RagMetricResult(
                "context.evidence_recall",
                EvaluationStage.CONTEXT_ASSEMBLY,
                _ratio(len(context_positive), len(sample.positive_chunk_ids)),
            ),
            RagMetricResult(
                "context.truncated",
                EvaluationStage.CONTEXT_ASSEMBLY,
                float(sample.context_truncated),
            ),
        )
    )
    return tuple(metrics)


def _ranking_metrics(
    ranking: Sequence[RankedChunk],
    positives: frozenset[str],
    hard_negatives: frozenset[str],
    *,
    prefix: str,
    stage: EvaluationStage,
    cutoffs: Sequence[int],
) -> list[RagMetricResult]:
    results: list[RagMetricResult] = []
    for cutoff in cutoffs:
        selected = ranking[:cutoff]
        ids = {value.chunk_id for value in selected}
        hits = len(ids & positives)
        results.extend(
            (
                RagMetricResult(
                    f"{prefix}.recall@{cutoff}",
                    stage,
                    _ratio(hits, len(positives)),
                ),
                RagMetricResult(
                    f"{prefix}.precision@{cutoff}",
                    stage,
                    _ratio(hits, len(selected)),
                ),
                RagMetricResult(
                    f"{prefix}.hit_rate@{cutoff}",
                    stage,
                    float(hits > 0),
                ),
                RagMetricResult(
                    f"{prefix}.hard_negative_intrusion@{cutoff}",
                    stage,
                    _ratio(len(ids & hard_negatives), len(selected)),
                ),
            )
        )
    results.extend(
        (
            RagMetricResult(
                f"{prefix}.mrr",
                stage,
                _reciprocal_rank(ranking, positives),
            ),
            RagMetricResult(
                f"{prefix}.ndcg@{cutoffs[-1]}",
                stage,
                _ndcg(ranking, positives, cutoffs[-1]),
            ),
        )
    )
    return results


def _reciprocal_rank(ranking: Sequence[RankedChunk], positives: frozenset[str]) -> float:
    rank = next(
        (index for index, value in enumerate(ranking, start=1) if value.chunk_id in positives),
        None,
    )
    return 1.0 / rank if rank is not None else 0.0


def _ndcg(ranking: Sequence[RankedChunk], positives: frozenset[str], cutoff: int) -> float:
    gains = [
        1.0 / math.log2(index + 1)
        for index, value in enumerate(ranking[:cutoff], start=1)
        if value.chunk_id in positives
    ]
    ideal_count = min(len(positives), cutoff)
    ideal = sum(1.0 / math.log2(index + 1) for index in range(1, ideal_count + 1))
    return sum(gains) / ideal if ideal else 0.0


def _cosine(first: Sequence[float], second: Sequence[float]) -> float:
    if not first or len(first) != len(second):
        raise ValueError("Embedding vectors 必须非空且维度一致")
    values = [float(left) * float(right) for left, right in zip(first, second, strict=True)]
    first_norm = math.sqrt(sum(float(value) ** 2 for value in first))
    second_norm = math.sqrt(sum(float(value) ** 2 for value in second))
    if not first_norm or not second_norm:
        return 0.0
    result = sum(values) / (first_norm * second_norm)
    if not math.isfinite(result):
        raise ValueError("Embedding cosine 结果必须有限")
    return result


def _ratio(numerator: int, denominator: int) -> float:
    return numerator / denominator if denominator else 1.0
