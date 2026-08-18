"""默认 ContextAssembler 的纯函数实现。"""

from __future__ import annotations

import json
from collections import defaultdict
from dataclasses import dataclass, field
from uuid import UUID

from jarvis_worker.agent.rag.chunking import estimate_tokens
from jarvis_worker.agent.rag.retrieval.keyword import build_keyword_terms
from jarvis_worker.agent.rag.retrieval.contracts import (
    RagContextChunk,
    RagContextElement,
    RagContextItem,
)
from jarvis_worker.agent.rag.retrieval.repository import (
    RagCandidateRecord,
    RagElementEvidenceRecord,
    RagNeighborRecord,
)


def build_context_items(
    candidates: list[RagCandidateRecord],
    neighbors: list[RagNeighborRecord],
    elements: list[RagElementEvidenceRecord],
    *,
    query: str,
    token_budget: int,
) -> tuple[tuple[RagContextItem, ...], int, bool]:
    neighbors_by_document: dict[UUID, list[RagNeighborRecord]] = defaultdict(list)
    for neighbor in neighbors:
        neighbors_by_document[neighbor.document_id].append(neighbor)
    elements_by_chunk: dict[UUID, list[RagElementEvidenceRecord]] = defaultdict(list)
    for element in elements:
        elements_by_chunk[element.chunk_id].append(element)

    if not candidates:
        return (), 0, False

    primary_ids = {candidate.chunk_id for candidate in candidates}
    expanded_ids: set[UUID] = set()
    active_candidates = candidates[: max(1, token_budget // 32)]
    drafts = [_ContextItemDraft(candidate=candidate) for candidate in active_candidates]
    remaining = token_budget
    was_truncated = len(active_candidates) < len(candidates)
    fair_share = max(32, token_budget // len(active_candidates))
    primary_cap = fair_share

    # 第一轮先为每个主证据保留公平预算，避免高排名长 Chunk 独占全部上下文。
    for draft in drafts:
        candidate = draft.candidate
        available = min(remaining, primary_cap)
        primary, consumed = _context_chunk(
            chunk_id=candidate.chunk_id,
            role="primary",
            ordinal=candidate.ordinal,
            content=candidate.content,
            token_count=candidate.token_count,
            source_locator=candidate.source_locator,
            available=available,
            query=query,
        )
        remaining -= consumed
        draft.chunks.append(primary)
        expanded_ids.add(candidate.chunk_id)
        was_truncated = was_truncated or primary.truncated

    # 第二轮按候选轮转补充相邻 Chunk；每个邻居有独立上限，仍保留自然阅读顺序。
    neighbor_cap = max(32, fair_share // 2)
    for draft in drafts:
        candidate = draft.candidate
        nearby = sorted(
            neighbors_by_document[candidate.document_id],
            key=lambda item: (abs(item.ordinal - candidate.ordinal), item.ordinal),
        )
        for neighbor in nearby:
            if (
                neighbor.chunk_id in primary_ids
                or neighbor.chunk_id in expanded_ids
                or neighbor.ordinal == candidate.ordinal
            ):
                continue
            if remaining < 32:
                was_truncated = True
                break
            role = "previous" if neighbor.ordinal < candidate.ordinal else "next"
            context, used = _context_chunk(
                chunk_id=neighbor.chunk_id,
                role=role,
                ordinal=neighbor.ordinal,
                content=neighbor.content,
                token_count=neighbor.token_count,
                source_locator=neighbor.source_locator,
                available=min(remaining, neighbor_cap),
                query=query,
            )
            draft.chunks.append(context)
            expanded_ids.add(neighbor.chunk_id)
            remaining -= used
            was_truncated = was_truncated or context.truncated

    # 第三轮再加入表格、公式、图片 OCR/描述，避免视觉元素挤掉所有主证据。
    element_cap = max(16, fair_share // 3)
    for draft in drafts:
        candidate = draft.candidate
        for evidence in elements_by_chunk[candidate.chunk_id]:
            if remaining < 16:
                was_truncated = True
                break
            text = _element_text(evidence)
            estimated = estimate_tokens(text)
            bounded, used, truncated = _bounded_text(
                text,
                estimated,
                min(remaining, element_cap),
                query=query,
            )
            draft.elements.append(
                RagContextElement(
                    element_id=evidence.element_id,
                    element_type=evidence.element_type,
                    page_number=evidence.page_number,
                    text=bounded,
                    confidence=evidence.confidence,
                    asset_ids=evidence.asset_ids,
                    truncated=truncated,
                )
            )
            remaining -= used
            was_truncated = was_truncated or truncated

    items: list[RagContextItem] = []
    for draft in drafts:
        ordered_chunks = _deduplicate_ordered_chunks(draft.chunks)
        item_tokens = sum(chunk.token_count for chunk in ordered_chunks) + sum(
            estimate_tokens(element.text) for element in draft.elements
        )
        candidate = draft.candidate
        items.append(
            RagContextItem(
                chunk_id=candidate.chunk_id,
                document_id=candidate.document_id,
                document_title=candidate.document_title,
                source_artifact_id=candidate.source_artifact_id,
                score=candidate.score,
                chunks=tuple(ordered_chunks),
                elements=tuple(draft.elements),
                token_count=item_tokens,
            )
        )
    total_tokens = sum(item.token_count for item in items)
    return tuple(items), total_tokens, was_truncated


@dataclass(slots=True)
class _ContextItemDraft:
    candidate: RagCandidateRecord
    chunks: list[RagContextChunk] = field(default_factory=list)
    elements: list[RagContextElement] = field(default_factory=list)


def _context_chunk(
    *, chunk_id, role, ordinal, content, token_count, source_locator, available, query
) -> tuple[RagContextChunk, int]:
    bounded, used, truncated = _bounded_text(
        content,
        token_count,
        available,
        query=query,
    )
    return (
        RagContextChunk(
            chunk_id=chunk_id,
            role=role,
            ordinal=ordinal,
            content=bounded,
            token_count=used,
            source_locator=dict(source_locator),
            truncated=truncated,
        ),
        used,
    )


def _element_text(evidence: RagElementEvidenceRecord) -> str:
    parts = [
        value.strip()
        for value in (
            evidence.caption_text,
            evidence.ocr_text,
            evidence.derived_description,
        )
        if value and value.strip()
    ]
    if evidence.structured_data:
        parts.append(
            json.dumps(
                evidence.structured_data,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
        )
    text = "\n".join(dict.fromkeys(parts)) or f"[{evidence.element_type}]"
    return text[:16_000]


def _bounded_text(
    text: str,
    estimated_tokens: int,
    available: int,
    *,
    query: str,
) -> tuple[str, int, bool]:
    if estimated_tokens <= available:
        return text, estimated_tokens, False
    bounded_tokens = max(1, available)
    bounded = _query_aware_window(text, query=query, token_budget=bounded_tokens)
    return bounded, min(bounded_tokens, estimate_tokens(bounded)), True


def _query_aware_window(text: str, *, query: str, token_budget: int) -> str:
    if estimate_tokens(text) <= token_budget:
        return text
    terms = (query.strip(), *build_keyword_terms(query))
    folded = text.casefold()
    positions = [folded.find(term.casefold()) for term in terms if term.strip()]
    match = next((position for position in positions if position >= 0), -1)
    anchor = match if match >= 0 else 0
    low = 1
    high = len(text)
    while low < high:
        size = (low + high + 1) // 2
        start = max(0, min(anchor - size // 3, len(text) - size))
        if estimate_tokens(text[start : start + size]) <= token_budget:
            low = size
        else:
            high = size - 1
    start = max(0, min(anchor - low // 3, len(text) - low))
    bounded = text[start : start + low].strip()
    prefix = "…" if start > 0 else ""
    suffix = "…" if start + low < len(text) else ""
    while bounded and estimate_tokens(prefix + bounded + suffix) > token_budget:
        bounded = bounded[:-1].rstrip()
    return prefix + bounded + suffix


def _deduplicate_ordered_chunks(chunks: list[RagContextChunk]) -> list[RagContextChunk]:
    result: list[RagContextChunk] = []
    previous_content = ""
    for chunk in chunks:
        content = _remove_prefix_overlap(previous_content, chunk.content)
        if not content:
            continue
        token_count = estimate_tokens(content)
        result.append(
            RagContextChunk(
                chunk_id=chunk.chunk_id,
                role=chunk.role,
                ordinal=chunk.ordinal,
                content=content,
                token_count=token_count,
                source_locator=chunk.source_locator,
                truncated=chunk.truncated,
            )
        )
        previous_content = content
    return result


def _remove_prefix_overlap(previous: str, current: str) -> str:
    if not previous or not current:
        return current
    maximum = min(len(previous), len(current), 1_024)
    for size in range(maximum, 31, -1):
        if previous[-size:] == current[:size]:
            return current[size:].lstrip()
    return current
