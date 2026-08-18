"""Build trusted source/Artifact/RAG lineage from completed tool observations.

This module does not plan research and does not advance a workflow.  It only
joins host-owned identifiers so a model cannot invent persistence facts when a
human-readable knowledge document refers to independently managed RAG work.
"""

from __future__ import annotations

import re
from typing import Any, Iterable
from uuid import UUID

from jarvis_worker.agent.rag.evidence import (
    TrustedRagChunkEvidence,
    trusted_rag_chunk_evidence,
)

_MAX_PROVENANCE_LINKS = 50
_MAX_MODEL_OBSERVATIONS = 10
_MAX_RAG_RESULTS = 12
_MAX_RAG_CHUNKS_PER_RESULT = 5


def trusted_knowledge_provenance(
    observations: list[dict[str, Any]],
) -> list[dict[str, str]]:
    """Join successful source, Artifact, ingestion and retrieval identities."""
    search_sources: dict[str, dict[str, str]] = {}
    downloads: dict[str, dict[str, str]] = {}
    ingestions: dict[str, dict[str, str]] = {}
    for observation in observations:
        if observation.get("ok") is not True:
            continue
        tool_name = observation.get("tool_name")
        data = observation.get("data")
        if not isinstance(data, dict):
            continue
        if tool_name == "literature.search_arxiv":
            results = data.get("results", [])
            if not isinstance(results, list):
                continue
            for item in results[:10]:
                if not isinstance(item, dict):
                    continue
                arxiv_id = item.get("arxiv_id")
                source_id = item.get("source_id")
                canonical_url = item.get("canonical_url") or item.get("abstract_url")
                if isinstance(arxiv_id, str) and isinstance(source_id, str):
                    source = {
                        "source_id": source_id,
                        "source_url": (
                            canonical_url if isinstance(canonical_url, str) else ""
                        ),
                    }
                    search_sources[arxiv_id] = source
                    search_sources[_arxiv_base_id(arxiv_id)] = source
        elif tool_name == "literature.download_arxiv_pdf":
            artifact_ids = observation.get("artifact_ids", [])
            arxiv_id = data.get("arxiv_id")
            if (
                isinstance(arxiv_id, str)
                and isinstance(artifact_ids, list)
                and artifact_ids
                and isinstance(artifact_ids[0], str)
            ):
                downloads[artifact_ids[0]] = {
                    "arxiv_id": arxiv_id,
                    "artifact_id": artifact_ids[0],
                    "artifact_sha256": str(data.get("sha256", "")),
                }
        elif tool_name == "rag.ingest_artifact":
            artifact_id = data.get("artifact_id")
            if isinstance(artifact_id, str):
                ingestions[artifact_id] = {
                    "rag_document_id": str(data.get("document_id", "")),
                    "rag_job_id": str(data.get("job_id", "")),
                    "rag_status": str(data.get("status", "")),
                }

    links: list[dict[str, str]] = []
    for artifact_id, download in downloads.items():
        source = search_sources.get(
            download["arxiv_id"],
            search_sources.get(_arxiv_base_id(download["arxiv_id"]), {}),
        )
        link = {
            "source_id": source.get(
                "source_id", f"arxiv:{download['arxiv_id']}"
            ),
            "source_url": source.get(
                "source_url", f"https://arxiv.org/abs/{download['arxiv_id']}"
            ),
            "artifact_id": artifact_id,
            "artifact_sha256": download["artifact_sha256"],
        }
        link.update(ingestions.get(artifact_id, {}))
        links.append(link)

    # PromptBuilder exposes only the latest ten observations.  Retrieval
    # provenance therefore uses the same window and records the nested context
    # chunks that the model could actually see, not every candidate considered
    # by the retriever.
    rag_evidence: list[tuple[str, TrustedRagChunkEvidence]] = []
    for observation in observations[-_MAX_MODEL_OBSERVATIONS:]:
        if (
            not isinstance(observation, dict)
            or observation.get("tool_name") != "rag.search"
            or observation.get("ok") is not True
        ):
            continue
        tool_call_id = _uuid_string(observation.get("tool_call_id"))
        if tool_call_id is None:
            continue
        rag_evidence.extend(
            (tool_call_id, item)
            for item in trusted_rag_chunk_evidence(
                observation,
                max_results=_MAX_RAG_RESULTS,
                max_chunks_per_result=_MAX_RAG_CHUNKS_PER_RESULT,
            )
        )

    # Preserve ranked result order while prioritising primary chunks.  A single
    # search can expose 12 results with neighbours (up to 60 chunks), whereas the
    # persisted knowledge contract intentionally caps all provenance at 50 rows.
    rag_evidence.sort(key=lambda pair: pair[1].role != "primary")
    seen: set[tuple[str, UUID, UUID]] = set()
    for tool_call_id, item in rag_evidence:
        key = (tool_call_id, item.document_id, item.chunk_id)
        if key in seen:
            continue
        seen.add(key)
        links.append(
            {
                "artifact_id": str(item.source_artifact_id),
                "rag_document_id": str(item.document_id),
                "rag_search_tool_call_id": tool_call_id,
                "rag_chunk_id": str(item.chunk_id),
            }
        )
        if len(links) >= _MAX_PROVENANCE_LINKS:
            break
    return links[:_MAX_PROVENANCE_LINKS]


def trusted_knowledge_provenance_from_tool_calls(
    tool_calls: Iterable[object],
) -> list[dict[str, str]]:
    """Rebuild provenance from durable, completed ToolCall records.

    Conversation history shown to the model is plain text and is deliberately
    not trusted as a source of host identities.  Follow-up Runs therefore join
    the latest completed assistant turn back to its persisted ToolCalls and
    project those records into the same observation shape used in the live Run.
    """
    observations: list[dict[str, Any]] = []
    for tool_call in tool_calls:
        if getattr(tool_call, "status", None) != "completed":
            continue
        result = getattr(tool_call, "result", None)
        if not isinstance(result, dict):
            continue
        observation: dict[str, Any] = {
            "tool_call_id": str(getattr(tool_call, "id", "")),
            "tool_name": str(getattr(tool_call, "tool_name", "")),
            "ok": True,
            "summary": str(result.get("summary", "")),
        }
        data = result.get("data")
        if isinstance(data, dict):
            observation["data"] = data
        artifact_ids = result.get("artifact_ids")
        if isinstance(artifact_ids, list):
            observation["artifact_ids"] = list(artifact_ids)
        observations.append(observation)
    return trusted_knowledge_provenance(observations)


def merge_trusted_knowledge_provenance(
    *groups: Iterable[dict[str, str]],
) -> list[dict[str, str]]:
    """Merge Runtime-owned lineage without duplicating identical links."""
    merged: list[dict[str, str]] = []
    seen: set[tuple[tuple[str, str], ...]] = set()
    for group in groups:
        for link in group:
            if not isinstance(link, dict):
                continue
            normalized = {
                str(key): str(value)
                for key, value in link.items()
                if isinstance(key, str) and isinstance(value, str) and value
            }
            identity = tuple(sorted(normalized.items()))
            if not identity or identity in seen:
                continue
            seen.add(identity)
            merged.append(normalized)
            if len(merged) >= _MAX_PROVENANCE_LINKS:
                return merged
    return merged


def _arxiv_base_id(value: str) -> str:
    return re.sub(r"v\d+$", "", value.strip(), flags=re.IGNORECASE)


def _uuid_string(value: object) -> str | None:
    try:
        return str(UUID(str(value)))
    except ValueError:
        return None
