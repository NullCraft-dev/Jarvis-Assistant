"""Jarvis RAG 数据飞轮的框架无关评估契约与确定性指标。"""

from .contracts import (
    EvaluationStage,
    FailureSeverity,
    RagEvaluationSample,
    RagFailureCandidate,
    RagFailureType,
    RagMetricResult,
    RankedChunk,
)
from .failure_mining import FailureMiningPolicy, mine_failures
from .metrics import (
    evaluate_chunking_stage,
    evaluate_embedding_stage,
    evaluate_retrieval_stages,
)
from .orchestrator import RagEvaluationOutcome, evaluate_labeled_trace
from .projector import evaluation_sample_from_context, evaluation_sample_from_trace
from .regression_gate import (
    FlywheelGatePolicy,
    MetricGate,
    build_review_queue,
    evaluate_regression_gate,
)

__all__ = [
    "EvaluationStage",
    "FailureMiningPolicy",
    "FailureSeverity",
    "RagEvaluationSample",
    "RagFailureCandidate",
    "RagFailureType",
    "RagMetricResult",
    "RankedChunk",
    "evaluate_chunking_stage",
    "evaluate_embedding_stage",
    "evaluate_retrieval_stages",
    "evaluation_sample_from_context",
    "evaluation_sample_from_trace",
    "evaluate_labeled_trace",
    "RagEvaluationOutcome",
    "mine_failures",
    "FlywheelGatePolicy",
    "MetricGate",
    "build_review_queue",
    "evaluate_regression_gate",
]
