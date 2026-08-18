"""RAG 在线检索输入、证据与 Context Package 契约。"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal
from uuid import UUID

RETRIEVAL_POLICY_VERSION = "rag-hybrid-retrieval-v6"

RagRerankerStatus = Literal["applied", "degraded", "skipped"]


@dataclass(frozen=True, slots=True)
class RagRetrievalQuery:
    query: str
    top_k: int = 8
    candidate_limit: int = 50
    feature_limit: int = 30
    cross_encoder_limit: int = 16
    diversity_limit: int = 10
    token_budget: int = 4_000
    document_ids: tuple[UUID, ...] = ()
    min_score: float = 0.15
    neighbor_window: int = 1
    max_chunks_per_document: int = 3

    def __post_init__(self) -> None:
        cleaned = self.query.strip()
        if not cleaned or len(cleaned) > 2_000:
            raise ValueError("RAG query 长度必须在 1..2000")
        if not 1 <= self.top_k <= 20:
            raise ValueError("RAG top_k 必须在 1..20")
        if not self.top_k <= self.candidate_limit <= 100:
            raise ValueError("RAG candidate_limit 必须在 top_k..100")
        if any(
            not 1 <= value <= 100
            for value in (self.feature_limit, self.cross_encoder_limit, self.diversity_limit)
        ):
            raise ValueError("RAG 阶段候选数量必须在 1..100")
        if not 256 <= self.token_budget <= 16_000:
            raise ValueError("RAG token_budget 必须在 256..16000")
        if len(self.document_ids) > 50 or len(set(self.document_ids)) != len(self.document_ids):
            raise ValueError("RAG document_ids 必须唯一且不超过 50 个")
        if not 0.0 <= self.min_score <= 1.0:
            raise ValueError("RAG min_score 必须在 0..1")
        if not 0 <= self.neighbor_window <= 2:
            raise ValueError("RAG neighbor_window 必须在 0..2")
        if not 1 <= self.max_chunks_per_document <= 10:
            raise ValueError("RAG max_chunks_per_document 必须在 1..10")

    @property
    def effective_feature_limit(self) -> int:
        return max(self.top_k, min(self.feature_limit, self.candidate_limit))

    @property
    def effective_cross_encoder_limit(self) -> int:
        return max(self.top_k, min(self.cross_encoder_limit, self.effective_feature_limit))

    @property
    def effective_diversity_limit(self) -> int:
        return max(self.top_k, min(self.diversity_limit, self.effective_cross_encoder_limit))


@dataclass(frozen=True, slots=True)
class RagQueryPlan:
    original_query: str
    queries: tuple[str, ...]

    def __post_init__(self) -> None:
        if not self.original_query.strip():
            raise ValueError("RAG original_query 不能为空")
        if not self.queries or len(self.queries) > 8:
            raise ValueError("RAG queries 必须包含 1..8 项")
        if any(not query.strip() or len(query) > 2_000 for query in self.queries):
            raise ValueError("RAG rewritten query 长度必须在 1..2000")


@dataclass(frozen=True, slots=True)
class RagPreparedQuery:
    plan: RagQueryPlan
    vectors: tuple[tuple[float, ...], ...] = ()
    metadata: dict = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class RagRankedCandidateTrace:
    """不含正文的检索阶段排序快照，供可观察性与离线评估使用。"""

    chunk_id: UUID
    document_id: UUID
    rank: int
    score: float
    content_hash: str
    sources: tuple[Literal["semantic", "keyword"], ...] = ()
    semantic_rank: int | None = None
    keyword_rank: int | None = None
    rrf_score: float | None = None
    feature_score: float | None = None
    cross_encoder_score: float | None = None
    fused_score: float | None = None
    mmr_score: float | None = None

    def __post_init__(self) -> None:
        if self.rank < 1:
            raise ValueError("RAG candidate trace rank 必须大于 0")
        if not 0.0 <= self.score <= 1.0:
            raise ValueError("RAG candidate trace score 必须在 0..1")
        if len(self.content_hash) != 64:
            raise ValueError("RAG candidate trace content_hash 必须是 SHA-256")


@dataclass(frozen=True, slots=True)
class RagRerankerStepTrace:
    stage_id: str
    status: RagRerankerStatus
    provider: str
    model: str
    input_count: int
    output_count: int
    latency_ms: int
    failure_code: str = ""

    def __post_init__(self) -> None:
        if not self.stage_id.strip() or not self.provider.strip():
            raise ValueError("RAG reranker trace stage/provider 不能为空")
        if min(self.input_count, self.output_count, self.latency_ms) < 0:
            raise ValueError("RAG reranker trace 计数和耗时不能为负数")
        if self.output_count > self.input_count:
            raise ValueError("RAG reranker trace 输出不能超过输入")


@dataclass(frozen=True, slots=True)
class RagPipelineTrace:
    query_rewriter: str
    retriever: str
    reranker: str
    context_assembler: str
    queries: tuple[str, ...]
    retrieved_candidates: tuple[RagRankedCandidateTrace, ...] = ()
    reranked_candidates: tuple[RagRankedCandidateTrace, ...] = ()
    context_chunk_ids: tuple[UUID, ...] = ()
    reranker_steps: tuple[RagRerankerStepTrace, ...] = ()


@dataclass(frozen=True, slots=True)
class RagContextChunk:
    chunk_id: UUID
    role: Literal["primary", "previous", "next"]
    ordinal: int
    content: str
    token_count: int
    source_locator: dict = field(default_factory=dict)
    truncated: bool = False


@dataclass(frozen=True, slots=True)
class RagContextElement:
    element_id: UUID
    element_type: str
    page_number: int
    text: str
    confidence: float
    asset_ids: tuple[UUID, ...] = ()
    truncated: bool = False


@dataclass(frozen=True, slots=True)
class RagContextItem:
    chunk_id: UUID
    document_id: UUID
    document_title: str
    source_artifact_id: UUID
    score: float
    chunks: tuple[RagContextChunk, ...]
    elements: tuple[RagContextElement, ...]
    token_count: int


@dataclass(frozen=True, slots=True)
class RagContextPackage:
    query: str
    workspace_id: UUID
    policy_version: str
    items: tuple[RagContextItem, ...]
    candidate_count: int
    total_tokens: int
    token_budget: int
    truncated: bool
    pipeline: RagPipelineTrace | None = None
