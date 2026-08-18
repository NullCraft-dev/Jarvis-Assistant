"""RAG Reranker 的 Provider 与阶段结果契约。"""

from __future__ import annotations

import math
from abc import ABC, abstractmethod
from dataclasses import dataclass
from uuid import UUID

from jarvis_worker.agent.rag.retrieval.contracts import RagRerankerStepTrace
from jarvis_worker.agent.rag.retrieval.repository import RagCandidateRecord


@dataclass(frozen=True, slots=True)
class RerankDocument:
    chunk_id: UUID
    document_title: str
    heading_path: tuple[str, ...]
    content: str

    def __post_init__(self) -> None:
        if not self.document_title.strip():
            raise ValueError("Rerank document_title 不能为空")
        if not self.content.strip():
            raise ValueError("Rerank content 不能为空")


@dataclass(frozen=True, slots=True)
class RerankScore:
    chunk_id: UUID
    score: float

    def __post_init__(self) -> None:
        if not math.isfinite(self.score):
            raise ValueError("Rerank score 必须是有限数值")


class RerankerProviderError(RuntimeError):
    def __init__(self, code: str, *, recoverable: bool = True) -> None:
        super().__init__(code)
        self.code = code
        self.recoverable = recoverable


class RerankerProvider(ABC):
    provider_name = "reranker"
    model_name = "unknown"

    @abstractmethod
    async def score(
        self,
        *,
        query: str,
        documents: tuple[RerankDocument, ...],
    ) -> tuple[RerankScore, ...]: ...


@dataclass(frozen=True, slots=True)
class RagRerankResult:
    candidates: tuple[RagCandidateRecord, ...]
    steps: tuple[RagRerankerStepTrace, ...]
