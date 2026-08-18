"""统一多模态预处理与本地结构模型 adapters。"""

from jarvis_worker.agent.rag.preprocessing.contracts import (
    DocumentNode,
    DocumentNodeType,
    DocumentPreprocessor,
    DocumentStructureProvider,
    NodeExtractionMethod,
    PageRoutingDecision,
    PageRoutingReason,
    PreprocessedDocument,
    PreprocessingProgress,
    StructurePageResult,
    StructureResultCache,
    VisualRegion,
)
from jarvis_worker.agent.rag.preprocessing.cache import LocalStructureResultCache
from jarvis_worker.agent.rag.preprocessing.orchestrator import (
    MultimodalDocumentPreprocessor,
)
from jarvis_worker.agent.rag.preprocessing.policy import PageRoutingPolicy

__all__ = [
    "DocumentNode",
    "DocumentNodeType",
    "DocumentPreprocessor",
    "DocumentStructureProvider",
    "NodeExtractionMethod",
    "PageRoutingDecision",
    "PageRoutingReason",
    "MultimodalDocumentPreprocessor",
    "PageRoutingPolicy",
    "PreprocessedDocument",
    "PreprocessingProgress",
    "StructurePageResult",
    "StructureResultCache",
    "VisualRegion",
    "LocalStructureResultCache",
]
