"""可插拔 RAG 检索流水线。

稳定阶段边界：
QueryRewriter -> CandidateRetriever -> Reranker -> ContextAssembler

当前默认使用向量 + PostgreSQL 有界关键词召回与 RRF。未来的多路 Query Rewrite、
专用 BM25 或 Cross-Encoder Reranker 只需替换对应端口；Task/Workspace 信任边界和
Tool 契约保持不变。
"""

from __future__ import annotations

import re
from abc import ABC, abstractmethod
from dataclasses import replace
from typing import Any
from uuid import UUID

from jarvis_worker.agent.rag.embedding.openai import OpenAIEmbeddingError
from jarvis_worker.agent.rag.reranking.contracts import RagRerankResult
from jarvis_worker.agent.rag.reranking.policy import PolicySelector
from jarvis_worker.agent.rag.retrieval.contracts import (
    RETRIEVAL_POLICY_VERSION,
    RagContextItem,
    RagContextPackage,
    RagPipelineTrace,
    RagPreparedQuery,
    RagQueryPlan,
    RagRankedCandidateTrace,
    RagRetrievalQuery,
)
from jarvis_worker.agent.rag.retrieval.coverage import select_with_document_coverage
from jarvis_worker.agent.rag.retrieval.fusion import reciprocal_rank_fuse
from jarvis_worker.agent.rag.retrieval.keyword import build_keyword_terms
from jarvis_worker.agent.rag.retrieval.repository import (
    RagCandidateRecord,
    RagRetrievalRepository,
)
from jarvis_worker.agent.rag.retrieval.stages import build_context_items
from jarvis_worker.database.unit_of_work import PostgresUnitOfWork
from jarvis_worker.shared.domain.models import WorkspaceStatus
from jarvis_worker.shared.errors.application import AppError


class RagQueryRewriter(ABC):
    stage_id = "query-rewriter"

    @abstractmethod
    async def rewrite(self, request: RagRetrievalQuery) -> RagQueryPlan: ...


class RagCandidateRetriever(ABC):
    stage_id = "candidate-retriever"

    @abstractmethod
    async def prepare(self, plan: RagQueryPlan) -> RagPreparedQuery: ...

    @abstractmethod
    async def retrieve(
        self,
        repository: RagRetrievalRepository,
        *,
        workspace_id: UUID,
        prepared: RagPreparedQuery,
        request: RagRetrievalQuery,
    ) -> list[RagCandidateRecord]: ...


class RagReranker(ABC):
    stage_id = "reranker"

    @abstractmethod
    async def rerank(
        self,
        *,
        plan: RagQueryPlan,
        candidates: list[RagCandidateRecord],
        request: RagRetrievalQuery,
    ) -> RagRerankResult: ...


class RagContextAssembler(ABC):
    stage_id = "context-assembler"

    @abstractmethod
    async def assemble(
        self,
        repository: RagRetrievalRepository,
        *,
        workspace_id: UUID,
        candidates: list[RagCandidateRecord],
        request: RagRetrievalQuery,
    ) -> tuple[tuple[RagContextItem, ...], int, bool]: ...


class IdentityQueryRewriter(RagQueryRewriter):
    stage_id = "identity-query-v1"

    async def rewrite(self, request: RagRetrievalQuery) -> RagQueryPlan:
        query = request.query.strip()
        return RagQueryPlan(original_query=query, queries=(query,))


class BoundedQueryPlanner(RagQueryRewriter):
    """Create a small, deterministic multi-query plan for compound questions.

    The original user query is always first and remains immutable.  Additional
    queries are only independently meaningful clauses; this avoids an LLM
    rewrite adding facts, changing document scope, or making retrieval itself
    another unbounded agent loop.
    """

    stage_id = "bounded-query-plan-v1"
    _CLAUSE_BOUNDARY = re.compile(
        r"(?:[；;。!?！？]\s*|\s+(?:and|versus|vs\.?|compared\s+with)\s+)",
        re.IGNORECASE,
    )
    _LEADING_CONNECTOR = re.compile(
        r"^(?:并且|以及|同时|然后|另外|and|also|then)\s*",
        re.IGNORECASE,
    )
    _COMPARISON_DIMENSIONS = re.compile(
        r"(?:在|从|围绕)\s*(?P<axes>[^。！？!?；;]{2,160}?)\s*"
        r"(?:上|方面)?(?:的)?(?:差异|异同|比较|对比|关注点|侧重点)",
        re.IGNORECASE,
    )
    _DIMENSION_SEPARATOR = re.compile(r"\s*(?:、|，|,|以及|和|及)\s*")

    def __init__(self, *, max_queries: int = 6, min_clause_chars: int = 8) -> None:
        if not 1 <= max_queries <= 8:
            raise ValueError("RAG query plan 上限必须在 1..8")
        if not 4 <= min_clause_chars <= 100:
            raise ValueError("RAG query plan 最短子句必须在 4..100")
        self._max_queries = max_queries
        self._min_clause_chars = min_clause_chars

    async def rewrite(self, request: RagRetrievalQuery) -> RagQueryPlan:
        original = request.query.strip()
        queries = [original]
        seen = {_query_fingerprint(original)}
        for match in self._COMPARISON_DIMENSIONS.finditer(original):
            for raw_axis in self._DIMENSION_SEPARATOR.split(match.group("axes")):
                axis = raw_axis.strip(" ,，:：-—")
                fingerprint = _query_fingerprint(axis)
                if len(axis) < 2 or fingerprint in seen:
                    continue
                queries.append(axis)
                seen.add(fingerprint)
                if len(queries) >= self._max_queries:
                    return RagQueryPlan(original_query=original, queries=tuple(queries))
        for raw_clause in self._CLAUSE_BOUNDARY.split(original):
            clause = self._LEADING_CONNECTOR.sub("", raw_clause).strip(" ,，:：-—")
            fingerprint = _query_fingerprint(clause)
            if (
                len(clause) < self._min_clause_chars
                or fingerprint in seen
                or fingerprint == _query_fingerprint(original)
            ):
                continue
            queries.append(clause)
            seen.add(fingerprint)
            if len(queries) >= self._max_queries:
                break
        return RagQueryPlan(original_query=original, queries=tuple(queries))


def _query_fingerprint(query: str) -> str:
    return " ".join(query.casefold().split()).strip(" ,，。;；:：!?！？")


class PgVectorCandidateRetriever(RagCandidateRetriever):
    stage_id = "pgvector-cosine-v1"

    def __init__(self, embedding_provider) -> None:
        self._embedding_provider = embedding_provider

    async def prepare(self, plan: RagQueryPlan) -> RagPreparedQuery:
        try:
            vectors = [await self._embedding_provider.embed_query(query) for query in plan.queries]
        except OpenAIEmbeddingError as exc:
            raise _error(exc.code, str(exc), "provider", exc.recoverable) from exc
        except Exception as exc:
            raise _error(
                "RAG_QUERY_EMBEDDING_FAILED",
                "查询向量化失败",
                "provider",
                True,
            ) from exc
        return RagPreparedQuery(
            plan=plan,
            vectors=tuple(tuple(float(value) for value in vector) for vector in vectors),
            metadata={
                "provider_name": self._embedding_provider.provider_name,
                "model_name": self._embedding_provider.model_name,
            },
        )

    async def retrieve(
        self,
        repository: RagRetrievalRepository,
        *,
        workspace_id: UUID,
        prepared: RagPreparedQuery,
        request: RagRetrievalQuery,
    ) -> list[RagCandidateRecord]:
        provider_name = str(prepared.metadata.get("provider_name", ""))
        model_name = str(prepared.metadata.get("model_name", ""))
        by_chunk: dict[UUID, RagCandidateRecord] = {}
        for vector in prepared.vectors:
            candidates = await repository.search_candidates(
                workspace_id=workspace_id,
                query_vector=vector,
                provider_name=provider_name,
                model_name=model_name,
                document_ids=request.document_ids,
                limit=request.candidate_limit,
            )
            for candidate in candidates:
                previous = by_chunk.get(candidate.chunk_id)
                if previous is None or candidate.score > previous.score:
                    by_chunk[candidate.chunk_id] = candidate
        return sorted(
            by_chunk.values(),
            key=lambda candidate: (-candidate.score, str(candidate.chunk_id)),
        )


class PostgresKeywordCandidateRetriever(RagCandidateRetriever):
    stage_id = "postgres-keyword-v1"

    async def prepare(self, plan: RagQueryPlan) -> RagPreparedQuery:
        return RagPreparedQuery(
            plan=plan,
            metadata={"keyword_terms": tuple(build_keyword_terms(query) for query in plan.queries)},
        )

    async def retrieve(
        self,
        repository: RagRetrievalRepository,
        *,
        workspace_id: UUID,
        prepared: RagPreparedQuery,
        request: RagRetrievalQuery,
    ) -> list[RagCandidateRecord]:
        terms_by_query = prepared.metadata.get("keyword_terms")
        if not isinstance(terms_by_query, tuple):
            raise _error(
                "RAG_KEYWORD_PREPARATION_INVALID",
                "关键词 Retriever 缺少可信词项",
                "runtime",
            )
        by_chunk: dict[UUID, RagCandidateRecord] = {}
        for raw_terms in terms_by_query:
            if not isinstance(raw_terms, tuple):
                raise _error(
                    "RAG_KEYWORD_PREPARATION_INVALID",
                    "关键词 Retriever 词项结构无效",
                    "runtime",
                )
            if not raw_terms:
                continue
            candidates = await repository.search_keyword_candidates(
                workspace_id=workspace_id,
                query_terms=raw_terms,
                document_ids=request.document_ids,
                limit=request.candidate_limit,
            )
            for candidate in candidates:
                previous = by_chunk.get(candidate.chunk_id)
                if previous is None or candidate.score > previous.score:
                    by_chunk[candidate.chunk_id] = candidate
        return sorted(
            by_chunk.values(),
            key=lambda candidate: (-candidate.score, str(candidate.chunk_id)),
        )


class HybridRrfCandidateRetriever(RagCandidateRetriever):
    """组合语义与关键词 Retriever；两路原始分数不直接比较。"""

    stage_id = "semantic-keyword-rrf-v1"

    def __init__(
        self,
        semantic: RagCandidateRetriever,
        keyword: RagCandidateRetriever,
    ) -> None:
        self._semantic = semantic
        self._keyword = keyword

    async def prepare(self, plan: RagQueryPlan) -> RagPreparedQuery:
        semantic = await self._semantic.prepare(plan)
        keyword = await self._keyword.prepare(plan)
        return RagPreparedQuery(
            plan=plan,
            vectors=semantic.vectors,
            metadata={**semantic.metadata, **keyword.metadata},
        )

    async def retrieve(
        self,
        repository: RagRetrievalRepository,
        *,
        workspace_id: UUID,
        prepared: RagPreparedQuery,
        request: RagRetrievalQuery,
    ) -> list[RagCandidateRecord]:
        semantic = await self._semantic.retrieve(
            repository,
            workspace_id=workspace_id,
            prepared=prepared,
            request=request,
        )
        keyword = await self._keyword.retrieve(
            repository,
            workspace_id=workspace_id,
            prepared=prepared,
            request=request,
        )
        semantic = [item for item in semantic if item.score >= request.min_score]
        keyword = [item for item in keyword if item.score >= request.min_score]
        covered_documents = {item.document_id for item in (*semantic, *keyword)}
        for document_id in request.document_ids:
            if document_id in covered_documents:
                continue
            scoped_request = replace(request, document_ids=(document_id,))
            scoped_semantic = await self._semantic.retrieve(
                repository,
                workspace_id=workspace_id,
                prepared=prepared,
                request=scoped_request,
            )
            scoped_keyword = await self._keyword.retrieve(
                repository,
                workspace_id=workspace_id,
                prepared=prepared,
                request=scoped_request,
            )
            semantic.extend(
                item for item in scoped_semantic if item.score >= request.min_score
            )
            keyword.extend(
                item for item in scoped_keyword if item.score >= request.min_score
            )

        fused = reciprocal_rank_fuse(
            {
                "semantic": _deduplicate_candidates(semantic),
                "keyword": _deduplicate_candidates(keyword),
            }
        )
        return select_with_document_coverage(
            fused,
            limit=request.candidate_limit,
            document_ids=request.document_ids,
        )


class PolicyReranker(PolicySelector):
    """兼容旧测试/显式 Pipeline；生产默认使用 PolicySelector。"""

    stage_id = "policy-score-v1"


def _deduplicate_candidates(
    candidates: list[RagCandidateRecord],
) -> list[RagCandidateRecord]:
    result: list[RagCandidateRecord] = []
    seen: set[UUID] = set()
    for candidate in candidates:
        if candidate.chunk_id in seen:
            continue
        result.append(candidate)
        seen.add(candidate.chunk_id)
    return result

class EvidenceContextAssembler(RagContextAssembler):
    stage_id = "fair-neighbor-multimodal-budget-v2"

    async def assemble(
        self,
        repository: RagRetrievalRepository,
        *,
        workspace_id: UUID,
        candidates: list[RagCandidateRecord],
        request: RagRetrievalQuery,
    ) -> tuple[tuple[RagContextItem, ...], int, bool]:
        neighbors = await repository.load_neighbors(
            workspace_id=workspace_id,
            centers=tuple((candidate.document_id, candidate.ordinal) for candidate in candidates),
            radius=request.neighbor_window,
        )
        elements = await repository.load_elements(
            workspace_id=workspace_id,
            chunk_ids=tuple(candidate.chunk_id for candidate in candidates),
        )
        return build_context_items(
            candidates,
            neighbors,
            elements,
            query=request.query,
            token_budget=request.token_budget,
        )


class RagRetrievalPipeline:
    def __init__(
        self,
        uow_factory,
        *,
        query_rewriter: RagQueryRewriter,
        retriever: RagCandidateRetriever,
        reranker: RagReranker,
        context_assembler: RagContextAssembler,
        unit_of_work=PostgresUnitOfWork,
    ) -> None:
        self._uow_factory = uow_factory
        self._query_rewriter = query_rewriter
        self._retriever = retriever
        self._reranker = reranker
        self._context_assembler = context_assembler
        self._unit_of_work = unit_of_work

    async def run(self, *, workspace_id: UUID, request: RagRetrievalQuery) -> RagContextPackage:
        await self._require_workspace(workspace_id)
        plan = await self._query_rewriter.rewrite(request)
        if plan.original_query != request.query.strip():
            raise _error(
                "RAG_QUERY_REWRITER_CONTRACT_VIOLATION",
                "QueryRewriter 改写了不可变的原始查询",
                "runtime",
            )
        prepared = await self._retriever.prepare(plan)

        async with self._transaction() as tx:
            await _require_active_workspace(tx, workspace_id)
            candidates = await self._retriever.retrieve(
                tx.rag_retrieval,
                workspace_id=workspace_id,
                prepared=prepared,
                request=request,
            )
            _validate_retrieved_candidates(candidates, workspace_id)

        rerank_result = await self._reranker.rerank(
            plan=plan,
            candidates=candidates,
            request=request,
        )
        ranked = list(rerank_result.candidates)
        _validate_ranked_candidates(ranked, candidates, request)

        async with self._transaction() as tx:
            await _require_active_workspace(tx, workspace_id)
            items, total_tokens, truncated = await self._context_assembler.assemble(
                tx.rag_retrieval,
                workspace_id=workspace_id,
                candidates=ranked,
                request=request,
            )
            ranked_ids = {candidate.chunk_id for candidate in ranked}
            if any(item.chunk_id not in ranked_ids for item in items):
                raise _error(
                    "RAG_CONTEXT_ASSEMBLER_CONTRACT_VIOLATION",
                    "ContextAssembler 返回了未经过 Ranker 的 Chunk",
                    "runtime",
                )

        return RagContextPackage(
            query=plan.original_query,
            workspace_id=workspace_id,
            policy_version=RETRIEVAL_POLICY_VERSION,
            items=items,
            candidate_count=len(candidates),
            total_tokens=total_tokens,
            token_budget=request.token_budget,
            truncated=truncated,
            pipeline=RagPipelineTrace(
                query_rewriter=self._query_rewriter.stage_id,
                retriever=self._retriever.stage_id,
                reranker=self._reranker.stage_id,
                context_assembler=self._context_assembler.stage_id,
                queries=plan.queries,
                retrieved_candidates=_ranked_trace(candidates),
                reranked_candidates=_ranked_trace(ranked),
                context_chunk_ids=tuple(
                    dict.fromkeys(chunk.chunk_id for item in items for chunk in item.chunks)
                ),
                reranker_steps=rerank_result.steps,
            ),
        )

    async def _require_workspace(self, workspace_id: UUID) -> None:
        async with self._transaction() as tx:
            await _require_active_workspace(tx, workspace_id)

    def _transaction(self):
        session_factory = self._uow_factory()
        return _UowTransaction(session_factory, self._unit_of_work)


def _ranked_trace(
    candidates: list[RagCandidateRecord],
) -> tuple[RagRankedCandidateTrace, ...]:
    return tuple(
        RagRankedCandidateTrace(
            chunk_id=candidate.chunk_id,
            document_id=candidate.document_id,
            rank=rank,
            score=candidate.score,
            content_hash=candidate.content_hash,
            sources=candidate.trace.sources,
            semantic_rank=candidate.trace.semantic_rank,
            keyword_rank=candidate.trace.keyword_rank,
            rrf_score=candidate.trace.rrf_score,
            feature_score=candidate.trace.feature_score,
            cross_encoder_score=candidate.trace.cross_encoder_score,
            fused_score=candidate.trace.fused_score,
            mmr_score=candidate.trace.mmr_score,
        )
        for rank, candidate in enumerate(candidates, start=1)
    )


class _UowTransaction:
    def __init__(self, session_factory, unit_of_work) -> None:
        self._session_factory = session_factory
        self._unit_of_work = unit_of_work
        self._session_context = None
        self._transaction_context = None

    async def __aenter__(self):
        self._session_context = self._session_factory()
        session = await self._session_context.__aenter__()
        self._transaction_context = self._unit_of_work(session).transaction()
        return await self._transaction_context.__aenter__()

    async def __aexit__(self, exc_type, exc, traceback):
        try:
            return await self._transaction_context.__aexit__(exc_type, exc, traceback)
        finally:
            await self._session_context.__aexit__(exc_type, exc, traceback)


async def _require_active_workspace(tx: Any, workspace_id: UUID) -> None:
    workspace = await tx.workspaces.get(workspace_id)
    if workspace is None or workspace.status is not WorkspaceStatus.ACTIVE:
        raise _error("RAG_WORKSPACE_UNAVAILABLE", "Workspace 不存在或已撤销", "validation")


def _validate_retrieved_candidates(
    candidates: list[RagCandidateRecord], workspace_id: UUID
) -> None:
    if len(candidates) > 800:
        raise _error(
            "RAG_RETRIEVER_LIMIT_VIOLATION",
            "Retriever 返回的候选数量超过流水线上限",
            "runtime",
        )
    if any(candidate.workspace_id != workspace_id for candidate in candidates):
        raise _error(
            "RAG_RETRIEVER_SCOPE_VIOLATION",
            "Retriever 返回了当前 Workspace 之外的候选",
            "security",
        )


def _validate_ranked_candidates(
    ranked: list[RagCandidateRecord],
    retrieved: list[RagCandidateRecord],
    request: RagRetrievalQuery,
) -> None:
    retrieved_ids = {candidate.chunk_id for candidate in retrieved}
    ranked_ids = [candidate.chunk_id for candidate in ranked]
    if (
        len(ranked) > request.top_k
        or len(set(ranked_ids)) != len(ranked_ids)
        or any(chunk_id not in retrieved_ids for chunk_id in ranked_ids)
    ):
        raise _error(
            "RAG_RERANKER_CONTRACT_VIOLATION",
            "Reranker 返回了重复、越界或非召回候选",
            "runtime",
        )


def _error(code: str, message: str, category: str, recoverable: bool = False) -> AppError:
    return AppError(
        code=code,
        message=message,
        category=category,
        recoverable=recoverable,
    )
