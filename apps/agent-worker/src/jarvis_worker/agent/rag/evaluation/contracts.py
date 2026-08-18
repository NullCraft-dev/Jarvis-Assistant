"""生产 RAG 数据飞轮的领域契约。"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Literal
from uuid import UUID, uuid4

from jarvis_worker.agent.rag.retrieval import RagContextPackage, RagRetrievalQuery

PrivacyStatus = Literal["pending", "approved", "rejected"]
LabelStatus = Literal["draft", "confirmed", "rejected", "promoted"]
LabelSource = Literal["user_feedback", "human_review", "citation_validator", "judge"]
FeedbackKind = Literal["helpful", "unhelpful", "citation_incorrect", "evidence_insufficient"]
FeedbackStatus = Literal["pending", "reviewed", "dismissed"]
FailureCategory = Literal[
    "candidate_miss",
    "reranker_miss",
    "context_omission",
    "context_truncated",
    "citation_mismatch",
    "answer_generation",
    "insufficient_evidence",
    "other",
]
QualityGateStatus = Literal["passed", "blocked", "insufficient_evidence"]
QualityTrendDirection = Literal["improved", "stable", "regressed"]
QualityComparisonState = Literal["ready", "insufficient_history"]
QualityAlertSeverity = Literal["warning", "critical"]
FailureClusterPriority = Literal["low", "medium", "high", "critical"]


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


@dataclass(frozen=True, slots=True)
class RagEvaluationTrace:
    id: UUID
    workspace_id: UUID
    task_id: UUID
    run_id: UUID
    step_id: UUID | None
    query: str
    query_hash: str
    request: dict
    pipeline_versions: dict[str, str]
    candidate_ranking: tuple[dict, ...]
    reranked_ranking: tuple[dict, ...]
    context_chunk_ids: tuple[UUID, ...]
    context_truncated: bool
    result_count: int
    privacy_status: PrivacyStatus = "pending"
    created_at: datetime = field(default_factory=_utcnow)

    @classmethod
    def capture(
        cls,
        *,
        task_id: UUID,
        run_id: UUID,
        step_id: UUID | None,
        request: RagRetrievalQuery,
        package: RagContextPackage,
    ) -> "RagEvaluationTrace":
        if package.pipeline is None:
            raise ValueError("RAG evaluation trace 需要 pipeline trace")
        pipeline = package.pipeline
        query = package.query.strip()
        return cls(
            id=uuid4(),
            workspace_id=package.workspace_id,
            task_id=task_id,
            run_id=run_id,
            step_id=step_id,
            query=query,
            query_hash=hashlib.sha256(query.encode("utf-8")).hexdigest(),
            request={
                "top_k": request.top_k,
                "candidate_limit": request.candidate_limit,
                "feature_limit": request.feature_limit,
                "cross_encoder_limit": request.cross_encoder_limit,
                "diversity_limit": request.diversity_limit,
                "token_budget": request.token_budget,
                "document_ids": [str(value) for value in request.document_ids],
                "min_score": request.min_score,
                "neighbor_window": request.neighbor_window,
                "max_chunks_per_document": request.max_chunks_per_document,
            },
            pipeline_versions={
                "retrieval_policy": package.policy_version,
                "query_rewriter": pipeline.query_rewriter,
                "retriever": pipeline.retriever,
                "reranker": pipeline.reranker,
                "reranker_execution": "|".join(
                    f"{step.stage_id}:{step.status}:{step.provider}:{step.model}:"
                    f"{step.failure_code or '-'}"
                    for step in pipeline.reranker_steps
                ),
                "reranker_latency_ms": str(
                    sum(step.latency_ms for step in pipeline.reranker_steps)
                ),
                "context_assembler": pipeline.context_assembler,
            },
            candidate_ranking=tuple(_ranked(value) for value in pipeline.retrieved_candidates),
            reranked_ranking=tuple(_ranked(value) for value in pipeline.reranked_candidates),
            context_chunk_ids=pipeline.context_chunk_ids,
            context_truncated=package.truncated,
            result_count=len(package.items),
        )


@dataclass(frozen=True, slots=True)
class RagEvaluationLabel:
    id: UUID
    trace_id: UUID
    positive_chunk_ids: tuple[UUID, ...]
    hard_negative_chunk_ids: tuple[UUID, ...] = ()
    source: LabelSource = "human_review"
    status: LabelStatus = "draft"
    notes: str = ""
    created_at: datetime = field(default_factory=_utcnow)
    updated_at: datetime = field(default_factory=_utcnow)

    def __post_init__(self) -> None:
        if not self.positive_chunk_ids:
            raise ValueError("RAG evaluation label 至少需要一个 positive chunk")
        if set(self.positive_chunk_ids) & set(self.hard_negative_chunk_ids):
            raise ValueError("positive 与 hard-negative chunk 不得重叠")


@dataclass(frozen=True, slots=True)
class RagEvaluationFeedback:
    """用户反馈候选；它不是金标，必须经开发者复核后才能转成 label。"""

    id: UUID
    trace_id: UUID
    workspace_id: UUID
    task_id: UUID
    run_id: UUID
    message_id: UUID
    kind: FeedbackKind
    citation_chunk_id: UUID | None = None
    status: FeedbackStatus = "pending"
    failure_category: FailureCategory | None = None
    fingerprint: str = ""
    created_at: datetime = field(default_factory=_utcnow)
    updated_at: datetime = field(default_factory=_utcnow)


@dataclass(frozen=True, slots=True)
class RagQualityGateRun:
    """离线发布门禁的脱敏、只读运行摘要。"""

    id: UUID
    gate_id: str
    cohort_id: str
    baseline_id: str
    revision: str
    status: QualityGateStatus
    sample_count: int
    metrics: dict[str, float]
    checks: tuple[dict, ...]
    generated_at: datetime
    failure_targets: tuple[dict, ...] = ()
    created_at: datetime = field(default_factory=_utcnow)


@dataclass(frozen=True, slots=True)
class RagQualityFailureTarget:
    """门禁失败样本的当前、脱敏审核定位。"""

    candidate_id: str
    trace_id: UUID
    workspace_id: UUID
    query_hash: str
    failure_type: str
    suspected_stage: str
    severity: str
    metric_ids: tuple[str, ...]
    privacy_status: str
    label_status: str | None
    label_source: str | None
    review_state: str
    issue: "RagQualityIssue | None" = None


@dataclass(frozen=True, slots=True)
class RagQualityIssue:
    id: UUID
    candidate_id: str
    trace_id: UUID
    gate_id: str
    cohort_id: str
    failure_type: str
    owner: str
    status: str
    occurrence_count: int
    first_seen_run_id: UUID
    last_seen_run_id: UUID
    verified_run_id: UUID | None = None
    resolution_note: str = ""
    version: int = 1
    created_at: datetime = field(default_factory=_utcnow)
    updated_at: datetime = field(default_factory=_utcnow)


@dataclass(frozen=True, slots=True)
class RagQualityIssueLedgerItem:
    """可独立追踪、可重新进入人工审核的质量问题投影。"""

    issue: RagQualityIssue
    workspace_id: UUID
    query_hash: str
    privacy_status: str
    label_status: str | None
    review_state: str
    first_seen_revision: str
    last_seen_revision: str
    verified_revision: str | None


@dataclass(frozen=True, slots=True)
class RagQualityMetricTrend:
    metric_id: str
    current: float
    previous: float
    delta: float
    direction: QualityTrendDirection


@dataclass(frozen=True, slots=True)
class RagQualityAlert:
    code: str
    severity: QualityAlertSeverity
    subject_id: str
    current: float | None = None
    previous: float | None = None
    delta: float | None = None


@dataclass(frozen=True, slots=True)
class RagQualityFailureCluster:
    failure_type: str
    priority: FailureClusterPriority
    latest_rate: float
    latest_count: int
    previous_rate: float | None
    rate_delta: float | None
    occurrence_count: int
    threshold: float
    check_passed: bool


@dataclass(frozen=True, slots=True)
class RagQualityGateInsights:
    comparison_state: QualityComparisonState
    compatible_history_count: int
    previous_run_id: UUID | None
    metric_trends: tuple[RagQualityMetricTrend, ...]
    alerts: tuple[RagQualityAlert, ...]
    failure_clusters: tuple[RagQualityFailureCluster, ...]


def _ranked(value) -> dict:
    return {
        "chunk_id": str(value.chunk_id),
        "document_id": str(value.document_id),
        "rank": value.rank,
        "score": value.score,
        "content_hash": value.content_hash,
        "sources": list(value.sources),
        "semantic_rank": value.semantic_rank,
        "keyword_rank": value.keyword_rank,
        "rrf_score": value.rrf_score,
        "feature_score": value.feature_score,
        "cross_encoder_score": value.cross_encoder_score,
        "fused_score": value.fused_score,
        "mmr_score": value.mmr_score,
    }
