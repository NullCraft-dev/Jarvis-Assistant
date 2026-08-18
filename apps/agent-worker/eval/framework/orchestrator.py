"""真实 RAG 轨迹的统一分阶段评估入口。"""

from __future__ import annotations

from dataclasses import dataclass

from .contracts import RagEvaluationSample, RagFailureCandidate, RagMetricResult
from .failure_mining import FailureMiningPolicy, mine_failures
from .metrics import evaluate_retrieval_stages


@dataclass(frozen=True, slots=True)
class RagEvaluationOutcome:
    sample: RagEvaluationSample
    metrics: tuple[RagMetricResult, ...]
    failures: tuple[RagFailureCandidate, ...]


def evaluate_labeled_trace(
    sample: RagEvaluationSample,
    *,
    cutoffs: tuple[int, ...] = (1, 3, 5, 10),
    policy: FailureMiningPolicy | None = None,
    additional_metrics: tuple[RagMetricResult, ...] = (),
) -> RagEvaluationOutcome:
    """评估真实链路快照，并把指标归因成待复核失败候选。"""
    retrieval_metrics = evaluate_retrieval_stages(sample, cutoffs=cutoffs)
    metrics = (*additional_metrics, *retrieval_metrics)
    return RagEvaluationOutcome(
        sample=sample,
        metrics=metrics,
        failures=mine_failures(sample, metrics, policy=policy),
    )
