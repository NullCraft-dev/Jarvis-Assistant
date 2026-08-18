"""Deterministic coverage selection for Runtime-scoped RAG documents."""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from typing import TypeVar
from uuid import UUID

from jarvis_worker.agent.rag.retrieval.repository import RagCandidateRecord

CandidateT = TypeVar("CandidateT", bound=RagCandidateRecord)


def document_coverage_candidates(
    candidates: Sequence[CandidateT],
    document_ids: Iterable[UUID],
) -> list[CandidateT]:
    """Return the highest-ranked surviving candidate for each requested document."""
    selected: list[CandidateT] = []
    selected_chunks: set[UUID] = set()
    for document_id in document_ids:
        candidate = next(
            (
                item
                for item in candidates
                if item.document_id == document_id and item.chunk_id not in selected_chunks
            ),
            None,
        )
        if candidate is None:
            continue
        selected.append(candidate)
        selected_chunks.add(candidate.chunk_id)
    return selected


def select_with_document_coverage(
    candidates: Sequence[CandidateT],
    *,
    limit: int,
    document_ids: Iterable[UUID],
) -> list[CandidateT]:
    """Reserve one candidate per selected document, then fill by existing rank."""
    if limit <= 0:
        return []
    selected = document_coverage_candidates(candidates, document_ids)[:limit]
    selected_chunks = {item.chunk_id for item in selected}
    for candidate in candidates:
        if len(selected) >= limit:
            break
        if candidate.chunk_id in selected_chunks:
            continue
        selected.append(candidate)
        selected_chunks.add(candidate.chunk_id)
    return selected
