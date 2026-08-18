"""`rag.search` 的同步 ToolGateway adapter。"""

from __future__ import annotations

import logging
import re
import unicodedata
from uuid import UUID

from jarvis_worker.agent.rag.evidence import (
    RAG_EVIDENCE_ASSESSMENT_POLICY_VERSION,
    RAG_EVIDENCE_ASSESSMENT_SCHEMA,
)
from jarvis_worker.agent.rag.retrieval import RagRetrievalQuery
from jarvis_worker.agent.tool_gateway.contracts import ToolRequest, ToolResult
from jarvis_worker.shared.errors.application import AppError

log = logging.getLogger("jarvis_worker.rag_search")


class RagSearchToolExecutor:
    def __init__(self, service, async_bridge, *, trace_service=None) -> None:
        self._service = service
        self._bridge = async_bridge
        self._trace_service = trace_service

    def __call__(self, request: ToolRequest) -> ToolResult:
        try:
            task_id = UUID(request.task_id)
            document_ids = _document_ids(request.arguments.get("document_ids", []))
            top_k = request.arguments.get("top_k", 8)
            if isinstance(top_k, bool) or not isinstance(top_k, int) or not 1 <= top_k <= 20:
                raise ValueError("top_k 必须是 1..20 的整数")
            effective_top_k = max(top_k, len(document_ids))
            query = RagRetrievalQuery(
                query=str(request.arguments.get("query", "")),
                top_k=effective_top_k,
                candidate_limit=50,
                feature_limit=30,
                cross_encoder_limit=max(16, top_k),
                diversity_limit=max(10, top_k),
                token_budget=4_000,
                document_ids=document_ids,
            )
            package = self._bridge.run(
                self._service.search_for_task(task_id=task_id, request=query),
                timeout=30,
            )
        except (TypeError, ValueError) as exc:
            return _error(
                "RAG_SEARCH_ARGUMENTS_INVALID",
                str(exc) or "RAG 检索参数无效",
                "validation",
                False,
            )
        except AppError as exc:
            return _error(exc.code, exc.message, exc.category, exc.recoverable)
        except Exception:
            return _error("RAG_SEARCH_FAILED", "RAG 检索暂时不可用", "tool", True)
        evaluation_trace_id = self._capture_trace(request, query, package)
        coverage = _document_coverage(document_ids, package.items)
        data = _package_data(package)
        if coverage is not None:
            data["document_coverage"] = coverage
        assessment = _evidence_assessment(
            package.query,
            package.items,
            coverage,
            planned_queries=(
                package.pipeline.queries if package.pipeline is not None else ()
            ),
        )
        data["evidence_assessment"] = assessment
        if evaluation_trace_id is not None:
            data["evaluation_trace_id"] = evaluation_trace_id
        summary = f"RAG 检索完成：{len(package.items)} 条引用证据"
        if coverage is not None:
            summary += (
                f"；指定文档覆盖 {coverage['covered_count']}/"
                f"{coverage['requested_count']}"
            )
        if not assessment["sufficient"]:
            summary += f"；证据不足（{assessment['reason_code']}）"
        return ToolResult(
            ok=True,
            kind="json",
            summary=summary,
            data=data,
            metadata={
                "workspace_id": str(package.workspace_id),
                "retrieval_policy_version": package.policy_version,
                "result_count": len(package.items),
                "candidate_count": package.candidate_count,
                "total_tokens": package.total_tokens,
                "truncated": package.truncated,
                "evidence_sufficient": assessment["sufficient"],
                "evidence_reason_code": assessment["reason_code"],
                **(
                    {
                        "document_coverage_complete": coverage["complete"],
                        "requested_document_count": coverage["requested_count"],
                        "covered_document_count": coverage["covered_count"],
                    }
                    if coverage is not None
                    else {}
                ),
                "evaluation_trace_id": evaluation_trace_id,
            },
        )

    def _capture_trace(self, request, query, package) -> str | None:
        if self._trace_service is None:
            return None
        try:
            trace = self._bridge.run(
                self._trace_service.capture(
                    task_id=UUID(request.task_id),
                    run_id=UUID(request.run_id),
                    step_id=_optional_uuid(request.step_id),
                    request=query,
                    package=package,
                ),
                timeout=10,
            )
        except Exception:
            # 评估可观察性降级不得改变主 RAG 工具的检索语义。
            log.exception("RAG evaluation trace 持久化失败")
            return None
        return str(trace.id)


def _document_ids(value) -> tuple[UUID, ...]:
    if not isinstance(value, list) or len(value) > 20:
        raise ValueError("document_ids 必须是不超过 20 项的数组")
    try:
        result = tuple(UUID(str(item)) for item in value)
    except (TypeError, ValueError):
        raise ValueError("document_ids 包含无效 ID") from None
    if len(set(result)) != len(result):
        raise ValueError("document_ids 不得重复")
    return result


def _optional_uuid(value: str) -> UUID | None:
    return UUID(value) if value else None


def _package_data(package) -> dict:
    return {
        "query": package.query,
        "workspace_id": str(package.workspace_id),
        "policy_version": package.policy_version,
        "candidate_count": package.candidate_count,
        "total_tokens": package.total_tokens,
        "token_budget": package.token_budget,
        "truncated": package.truncated,
        "pipeline": (
            {
                "query_rewriter": package.pipeline.query_rewriter,
                "retriever": package.pipeline.retriever,
                "reranker": package.pipeline.reranker,
                "context_assembler": package.pipeline.context_assembler,
                "queries": list(package.pipeline.queries),
            }
            if package.pipeline is not None
            else None
        ),
        "results": [
            {
                "chunk_id": str(item.chunk_id),
                "document_id": str(item.document_id),
                "document_title": item.document_title,
                "source_artifact_id": str(item.source_artifact_id),
                "score": round(item.score, 6),
                "token_count": item.token_count,
                "chunks": [
                    {
                        "chunk_id": str(chunk.chunk_id),
                        "role": chunk.role,
                        "ordinal": chunk.ordinal,
                        "content": chunk.content,
                        "token_count": chunk.token_count,
                        "source_locator": chunk.source_locator,
                        "truncated": chunk.truncated,
                    }
                    for chunk in item.chunks
                ],
                "elements": [
                    {
                        "element_id": str(element.element_id),
                        "element_type": element.element_type,
                        "page_number": element.page_number,
                        "text": element.text,
                        "confidence": element.confidence,
                        "asset_ids": [str(asset_id) for asset_id in element.asset_ids],
                        "truncated": element.truncated,
                    }
                    for element in item.elements
                ],
            }
            for item in package.items
        ],
    }


def _document_coverage(document_ids, items) -> dict | None:
    if not document_ids:
        return None
    covered = {item.document_id for item in items}
    missing = [document_id for document_id in document_ids if document_id not in covered]
    return {
        "requested_count": len(document_ids),
        "covered_count": len(document_ids) - len(missing),
        "complete": not missing,
        "uncovered_document_ids": [str(document_id) for document_id in missing],
    }


_NUMBER_OR_IDENTIFIER = re.compile(
    r"(?<![\w.])(?:\d{2,}(?:[.,]\d+)?%?|[A-Za-z]+(?:[-_/][A-Za-z0-9]+)+)(?![\w.])"
)
_LATIN_TERM = re.compile(r"[A-Za-z][A-Za-z'-]{2,}")
_HAN_SPAN = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff]{2,}")
_LATIN_STOPWORDS = frozenset({
    "about", "according", "answer", "authors", "based", "compare", "documents",
    "from", "have", "selected", "tell", "that", "their", "these", "this", "what",
    "when", "where", "which", "with", "would", "your",
})
_HAN_STOP_BIGRAMS = frozenset({
    "作者", "告诉", "根据", "文档", "资料", "选中", "两份", "当前", "什么", "如何",
    "进行", "给出", "说明", "回答", "其中",
})


def _evidence_assessment(
    query: str,
    items,
    coverage: dict | None,
    *,
    planned_queries=(),
) -> dict:
    """Project bounded query-to-evidence sufficiency facts.

    Dense/reranker scores are ranking signals, not proof that retrieved text
    contains the constraints in the question.  Exact numeric/identifier
    constraints therefore fail closed, while lexical anchoring is applied only
    when query and evidence share a script.
    """
    covered_document_ids = {str(item.document_id) for item in items}
    haystack = _evidence_text(items)
    normalized_haystack = _normalize_evidence_text(haystack)
    latin_evidence_terms = frozenset(
        term.casefold() for term in _LATIN_TERM.findall(normalized_haystack)
    )
    planned = tuple(str(value) for value in planned_queries if str(value).strip())
    query_text = "\n".join((query, *planned))
    strict_anchors = _strict_query_anchors(query_text)
    covered_strict = {
        anchor for anchor in strict_anchors if anchor in normalized_haystack
    }
    latin_terms = _latin_relevance_terms(query_text)
    covered_latin = latin_terms & latin_evidence_terms
    han_terms = _han_relevance_terms(query_text)
    evidence_has_han = bool(_HAN_SPAN.search(haystack))
    covered_han = {term for term in han_terms if term in normalized_haystack}
    lexical_gate_applied = bool(
        (len(latin_terms) >= 2 and _LATIN_TERM.search(haystack))
        or (len(han_terms) >= 4 and evidence_has_han)
    )
    lexical_covered_count = len(covered_latin) + len(covered_han)
    if not items:
        reason_code = "NO_EVIDENCE"
    elif coverage is not None and coverage["complete"] is not True:
        reason_code = "REQUESTED_DOCUMENT_COVERAGE_INCOMPLETE"
    elif strict_anchors - covered_strict:
        reason_code = "QUERY_CONSTRAINT_UNCOVERED"
    elif lexical_gate_applied and lexical_covered_count == 0:
        reason_code = "QUERY_EVIDENCE_LEXICAL_MISMATCH"
    else:
        reason_code = "SUFFICIENT"
    return {
        "schema": RAG_EVIDENCE_ASSESSMENT_SCHEMA,
        "policy_version": RAG_EVIDENCE_ASSESSMENT_POLICY_VERSION,
        "sufficient": reason_code == "SUFFICIENT",
        "reason_code": reason_code,
        "evidence_count": len(items),
        "covered_document_count": len(covered_document_ids),
        "requested_document_count": (
            int(coverage["requested_count"]) if coverage is not None else 0
        ),
        "strict_anchor_count": len(strict_anchors),
        "covered_strict_anchor_count": len(covered_strict),
        "lexical_gate_applied": lexical_gate_applied,
        "lexical_term_count": len(latin_terms) + len(han_terms),
        "covered_lexical_term_count": lexical_covered_count,
    }


def _evidence_text(items) -> str:
    values: list[str] = []
    for item in items:
        values.append(str(item.document_title))
        values.extend(str(chunk.content) for chunk in item.chunks)
        values.extend(str(element.text) for element in item.elements)
    return "\n".join(values)


def _normalize_evidence_text(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value).casefold()
    normalized = re.sub(r"(?<=\d),(?=\d)", "", normalized)
    return " ".join(normalized.split())


def _strict_query_anchors(query: str) -> frozenset[str]:
    normalized = _normalize_evidence_text(query)
    return frozenset(
        match.group(0)
        for match in _NUMBER_OR_IDENTIFIER.finditer(normalized)
    )


def _latin_relevance_terms(query: str) -> frozenset[str]:
    return frozenset(
        term.casefold()
        for term in _LATIN_TERM.findall(unicodedata.normalize("NFKC", query))
        if len(term) >= 4 and term.casefold() not in _LATIN_STOPWORDS
    )


def _han_relevance_terms(query: str) -> frozenset[str]:
    terms: set[str] = set()
    for span in _HAN_SPAN.findall(unicodedata.normalize("NFKC", query)):
        for index in range(len(span) - 1):
            term = span[index : index + 2]
            if term not in _HAN_STOP_BIGRAMS:
                terms.add(term)
    return frozenset(terms)


def _error(code: str, message: str, category: str, recoverable: bool) -> ToolResult:
    return ToolResult(
        ok=False,
        summary=message,
        error={
            "code": code,
            "message": message,
            "category": category,
            "recoverable": recoverable,
        },
    )
