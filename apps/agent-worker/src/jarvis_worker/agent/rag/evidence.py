"""Parse bounded, host-owned evidence identities from ``rag.search`` results."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from uuid import UUID

RAG_EVIDENCE_ASSESSMENT_SCHEMA = "rag-evidence-assessment-v1"
RAG_EVIDENCE_ASSESSMENT_POLICY_VERSION = "rag-evidence-sufficiency-v2"


@dataclass(frozen=True, slots=True)
class TrustedRagChunkEvidence:
    """One context chunk returned by the native RAG retrieval pipeline."""

    document_id: UUID
    source_artifact_id: UUID
    chunk_id: UUID
    document_title: str
    role: str
    content: str
    source_locator: dict[str, Any]


def trusted_rag_chunk_evidence(
    observation: dict[str, Any],
    *,
    max_results: int = 20,
    max_chunks_per_result: int = 5,
) -> tuple[TrustedRagChunkEvidence, ...]:
    """Return only valid RAG document/Artifact/Chunk identities.

    The caller owns the observation trust boundary (tool name, success status and
    current-Run scope).  This parser owns the shared structure and UUID checks so
    answer citations and persisted knowledge provenance cannot interpret the same
    native ToolResult differently.
    """
    if max_results < 0 or max_chunks_per_result < 0:
        raise ValueError("RAG evidence limits cannot be negative")
    data = observation.get("data")
    results = data.get("results") if isinstance(data, dict) else None
    if not isinstance(results, list):
        return ()

    evidence: list[TrustedRagChunkEvidence] = []
    for item in results[:max_results]:
        if not isinstance(item, dict):
            continue
        try:
            document_id = UUID(str(item.get("document_id", "")))
            artifact_id = UUID(str(item.get("source_artifact_id", "")))
        except ValueError:
            continue
        chunks = item.get("chunks")
        if not isinstance(chunks, list):
            continue
        for chunk in chunks[:max_chunks_per_result]:
            if not isinstance(chunk, dict):
                continue
            try:
                chunk_id = UUID(str(chunk.get("chunk_id", "")))
            except ValueError:
                continue
            locator = chunk.get("source_locator")
            evidence.append(
                TrustedRagChunkEvidence(
                    document_id=document_id,
                    source_artifact_id=artifact_id,
                    chunk_id=chunk_id,
                    document_title=str(item.get("document_title", ""))[:500],
                    role=str(chunk.get("role", ""))[:20],
                    content=str(chunk.get("content", "")),
                    source_locator=(dict(locator) if isinstance(locator, dict) else {}),
                )
            )
    return tuple(evidence)
