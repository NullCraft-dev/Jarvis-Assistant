"""经 Runtime 校验并用可信检索元数据归一化的 RAG 回答。"""

from __future__ import annotations

from dataclasses import dataclass, field
from uuid import UUID


@dataclass(frozen=True, slots=True)
class RagCitation:
    chunk_id: UUID
    document_id: UUID
    document_title: str
    source_artifact_id: UUID
    source_locator: dict = field(default_factory=dict)
    evidence_excerpt: str = ""


@dataclass(frozen=True, slots=True)
class RagAnswer:
    answer: str
    citations: tuple[RagCitation, ...] = ()
    insufficient_evidence: bool = False
