"""数据飞轮的稳定、可序列化领域契约。"""

from __future__ import annotations

import hashlib
import math
from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class EvaluationStage(str, Enum):
    PREPROCESSING = "preprocessing"
    CHUNKING = "chunking"
    EMBEDDING = "embedding"
    CANDIDATE_RECALL = "candidate_recall"
    RERANKER = "reranker"
    CONTEXT_ASSEMBLY = "context_assembly"
    GENERATION = "generation"
    CITATION = "citation"
    END_TO_END = "end_to_end"


class FailureSeverity(str, Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


class RagFailureType(str, Enum):
    CHUNK_SEMANTIC_SPLIT = "chunk_semantic_split"
    EMBEDDING_MARGIN_LOW = "embedding_margin_low"
    CANDIDATE_EVIDENCE_MISSED = "candidate_evidence_missed"
    RERANKER_EVIDENCE_DROPPED = "reranker_evidence_dropped"
    CONTEXT_EVIDENCE_DROPPED = "context_evidence_dropped"
    CONTEXT_TRUNCATED = "context_truncated"


@dataclass(frozen=True, slots=True)
class RankedChunk:
    chunk_id: str
    rank: int
    score: float
    document_id: str = ""
    sources: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not self.chunk_id.strip() or self.rank < 1:
            raise ValueError("RankedChunk 必须包含 chunk_id 和正整数 rank")
        if not math.isfinite(self.score):
            raise ValueError("RankedChunk score 必须是有限数值")


@dataclass(frozen=True, slots=True)
class RagEvaluationSample:
    trace_id: str
    query_id: str
    query: str
    positive_chunk_ids: frozenset[str]
    hard_negative_chunk_ids: frozenset[str] = frozenset()
    candidate_ranking: tuple[RankedChunk, ...] = ()
    reranked_ranking: tuple[RankedChunk, ...] = ()
    context_chunk_ids: tuple[str, ...] = ()
    context_truncated: bool = False
    pipeline_versions: dict[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.trace_id.strip() or not self.query_id.strip() or not self.query.strip():
            raise ValueError("EvaluationSample trace/query 标识和 query 不能为空")
        if self.positive_chunk_ids & self.hard_negative_chunk_ids:
            raise ValueError("positive 与 hard-negative chunk 不得重叠")


@dataclass(frozen=True, slots=True)
class RagMetricResult:
    metric_id: str
    stage: EvaluationStage
    value: float
    sample_count: int = 1
    details: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.metric_id.strip() or not math.isfinite(self.value):
            raise ValueError("MetricResult metric_id 不能为空且 value 必须有限")
        if self.sample_count < 1:
            raise ValueError("MetricResult sample_count 必须大于 0")


@dataclass(frozen=True, slots=True)
class RagFailureCandidate:
    candidate_id: str
    trace_id: str
    query_id: str
    failure_type: RagFailureType
    suspected_stage: EvaluationStage
    severity: FailureSeverity
    metric_ids: tuple[str, ...]
    evidence: dict[str, Any] = field(default_factory=dict)
    review_status: str = "detected"
    privacy_status: str = "pending"

    @classmethod
    def detected(
        cls,
        *,
        sample: RagEvaluationSample,
        failure_type: RagFailureType,
        stage: EvaluationStage,
        severity: FailureSeverity,
        metric_ids: tuple[str, ...],
        evidence: dict[str, Any] | None = None,
    ) -> "RagFailureCandidate":
        material = "|".join(
            (sample.trace_id, sample.query_id, failure_type.value, stage.value)
        )
        return cls(
            candidate_id=hashlib.sha256(material.encode("utf-8")).hexdigest(),
            trace_id=sample.trace_id,
            query_id=sample.query_id,
            failure_type=failure_type,
            suspected_stage=stage,
            severity=severity,
            metric_ids=metric_ids,
            evidence=evidence or {},
        )
