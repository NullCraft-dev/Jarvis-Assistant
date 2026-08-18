"""Workspace 边界内的检索、重排与上下文组装。"""

from jarvis_worker.agent.rag.retrieval.config import HnswSearchConfig
from jarvis_worker.agent.rag.retrieval.contracts import (
    RETRIEVAL_POLICY_VERSION,
    RagContextChunk,
    RagContextElement,
    RagContextItem,
    RagContextPackage,
    RagPipelineTrace,
    RagPreparedQuery,
    RagQueryPlan,
    RagRankedCandidateTrace,
    RagRerankerStepTrace,
    RagRetrievalQuery,
)

__all__ = [
    "RETRIEVAL_POLICY_VERSION",
    "RagContextChunk",
    "RagContextElement",
    "RagContextItem",
    "RagContextPackage",
    "RagPipelineTrace",
    "RagPreparedQuery",
    "RagQueryPlan",
    "RagRankedCandidateTrace",
    "RagRerankerStepTrace",
    "RagRetrievalQuery",
    "HnswSearchConfig",
    "RagRetrievalPipeline",
    "RagRetrievalService",
]


def __getattr__(name: str):
    if name == "RagRetrievalPipeline":
        from jarvis_worker.agent.rag.retrieval.pipeline import RagRetrievalPipeline

        return RagRetrievalPipeline
    if name == "RagRetrievalService":
        from jarvis_worker.agent.rag.retrieval.service import RagRetrievalService

        return RagRetrievalService
    raise AttributeError(name)
