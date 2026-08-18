"""把分阶段指标转化为待隐私复核的真实失败候选。"""

from __future__ import annotations

from dataclasses import dataclass

from .contracts import (
    EvaluationStage,
    FailureSeverity,
    RagEvaluationSample,
    RagFailureCandidate,
    RagFailureType,
    RagMetricResult,
)


@dataclass(frozen=True, slots=True)
class FailureMiningPolicy:
    retrieval_cutoff: int = 5
    minimum_candidate_recall: float = 1.0
    minimum_reranker_recall: float = 1.0
    minimum_context_recall: float = 1.0
    minimum_embedding_margin: float = 0.05
    minimum_must_keep_rate: float = 0.9


def mine_failures(
    sample: RagEvaluationSample,
    metrics: tuple[RagMetricResult, ...],
    *,
    policy: FailureMiningPolicy | None = None,
) -> tuple[RagFailureCandidate, ...]:
    selected = policy or FailureMiningPolicy()
    by_id = {metric.metric_id: metric for metric in metrics}
    failures: list[RagFailureCandidate] = []
    candidate_id = f"candidate.recall@{selected.retrieval_cutoff}"
    reranker_id = f"reranker.recall@{selected.retrieval_cutoff}"
    candidate = by_id.get(candidate_id)
    reranker = by_id.get(reranker_id)
    context = by_id.get("context.evidence_recall")
    margin = by_id.get("embedding.positive_negative_margin")
    must_keep = by_id.get("chunk.must_keep_pass_rate")

    if candidate and candidate.value < selected.minimum_candidate_recall:
        failures.append(
            _failure(
                sample,
                RagFailureType.CANDIDATE_EVIDENCE_MISSED,
                EvaluationStage.CANDIDATE_RECALL,
                FailureSeverity.HIGH,
                candidate,
            )
        )
    if (
        candidate
        and reranker
        and candidate.value >= selected.minimum_candidate_recall
        and reranker.value < selected.minimum_reranker_recall
    ):
        failures.append(
            _failure(
                sample,
                RagFailureType.RERANKER_EVIDENCE_DROPPED,
                EvaluationStage.RERANKER,
                FailureSeverity.HIGH,
                reranker,
            )
        )
    if (
        reranker
        and context
        and reranker.value >= selected.minimum_reranker_recall
        and context.value < selected.minimum_context_recall
    ):
        failures.append(
            _failure(
                sample,
                RagFailureType.CONTEXT_EVIDENCE_DROPPED,
                EvaluationStage.CONTEXT_ASSEMBLY,
                FailureSeverity.HIGH,
                context,
            )
        )
    if by_id.get("context.truncated", None) and by_id["context.truncated"].value > 0:
        failures.append(
            _failure(
                sample,
                RagFailureType.CONTEXT_TRUNCATED,
                EvaluationStage.CONTEXT_ASSEMBLY,
                FailureSeverity.MEDIUM,
                by_id["context.truncated"],
            )
        )
    if margin and margin.value < selected.minimum_embedding_margin:
        failures.append(
            _failure(
                sample,
                RagFailureType.EMBEDDING_MARGIN_LOW,
                EvaluationStage.EMBEDDING,
                FailureSeverity.MEDIUM,
                margin,
            )
        )
    if must_keep and must_keep.value < selected.minimum_must_keep_rate:
        failures.append(
            _failure(
                sample,
                RagFailureType.CHUNK_SEMANTIC_SPLIT,
                EvaluationStage.CHUNKING,
                FailureSeverity.HIGH,
                must_keep,
            )
        )
    return tuple(failures)


def _failure(
    sample: RagEvaluationSample,
    failure_type: RagFailureType,
    stage: EvaluationStage,
    severity: FailureSeverity,
    metric: RagMetricResult,
) -> RagFailureCandidate:
    return RagFailureCandidate.detected(
        sample=sample,
        failure_type=failure_type,
        stage=stage,
        severity=severity,
        metric_ids=(metric.metric_id,),
        evidence={"metric_value": metric.value},
    )
