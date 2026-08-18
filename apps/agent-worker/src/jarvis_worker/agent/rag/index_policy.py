"""RAG 索引目标版本与过期判断。"""

from __future__ import annotations

from dataclasses import dataclass

from jarvis_worker.agent.rag.chunking import MultimodalChunkRouter
from jarvis_worker.agent.rag.contracts import RagDocument, RagDocumentStatus
from jarvis_worker.agent.rag.embedding.config import RagEmbeddingConfig
from jarvis_worker.agent.rag.ingestion.contracts import PdfExtractionPolicy
from jarvis_worker.agent.rag.ingestion.service import INGESTION_POLICY_VERSION


@dataclass(frozen=True, slots=True)
class RagIndexTarget:
    ingestion_policy_version: str
    parser_version: str
    chunker_version: str
    embedding_provider: str
    embedding_model: str
    embedding_dimensions: int

    @classmethod
    def current(cls) -> "RagIndexTarget":
        embedding = RagEmbeddingConfig.from_env()
        return cls(
            ingestion_policy_version=INGESTION_POLICY_VERSION,
            parser_version=PdfExtractionPolicy().parser_version,
            chunker_version=MultimodalChunkRouter().version,
            embedding_provider="openai",
            embedding_model=embedding.rag_embedding_model,
            embedding_dimensions=embedding.rag_embedding_dimensions,
        )

    def as_dict(self) -> dict[str, str | int]:
        return {
            "ingestion_policy_version": self.ingestion_policy_version,
            "parser_version": self.parser_version,
            "chunker_version": self.chunker_version,
            "embedding_provider": self.embedding_provider,
            "embedding_model": self.embedding_model,
            "embedding_dimensions": self.embedding_dimensions,
        }


@dataclass(frozen=True, slots=True)
class RagIndexFreshness:
    state: str
    stale_reasons: tuple[str, ...]
    target: RagIndexTarget


def assess_index_freshness(
    document: RagDocument, target: RagIndexTarget
) -> RagIndexFreshness:
    if document.status is RagDocumentStatus.INDEXING:
        return RagIndexFreshness("building", (), target)
    if document.status is RagDocumentStatus.FAILED:
        return RagIndexFreshness("unavailable", (), target)

    checks = (
        (
            "ingestion_policy_version",
            document.ingestion_policy_version,
            target.ingestion_policy_version,
        ),
        ("parser_version", document.parser_version, target.parser_version),
        ("chunker_version", document.chunker_version, target.chunker_version),
        ("embedding_provider", document.embedding_provider, target.embedding_provider),
        ("embedding_model", document.embedding_model, target.embedding_model),
        (
            "embedding_dimensions",
            document.embedding_dimensions,
            target.embedding_dimensions,
        ),
    )
    reasons = tuple(name for name, actual, expected in checks if actual != expected)
    return RagIndexFreshness("stale" if reasons else "current", reasons, target)
