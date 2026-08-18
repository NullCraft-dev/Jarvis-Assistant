"""受控来源读取、格式解析与多模态元素提取。"""

from importlib import import_module

from jarvis_worker.agent.rag.ingestion.asset_store import LocalRagAssetFileStore
from jarvis_worker.agent.rag.ingestion.contracts import (
    DocumentBlock,
    ExtractedElement,
    ParsedPdfDocument,
    PdfBlockType,
    PdfExtractionPolicy,
)
from jarvis_worker.agent.rag.ingestion.pdf_parser import PdfParseError, PyMuPdfNativeParser

_SERVICE_EXPORTS = {
    "INGESTION_POLICY_VERSION",
    "RagIngestionEnqueueResult",
    "RagIngestionCommandService",
    "RagDocumentMutationResult",
    "RagIngestionError",
    "RagIngestionProcessResult",
    "RagIngestionService",
}


def __getattr__(name: str):
    """延迟导出 application service，避免 contracts 导入触发处理流水线闭环。"""

    if name not in _SERVICE_EXPORTS:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    service_module = import_module("jarvis_worker.agent.rag.ingestion.service")
    value = getattr(service_module, name)
    globals()[name] = value
    return value


__all__ = [
    "DocumentBlock",
    "ExtractedElement",
    "ParsedPdfDocument",
    "PdfBlockType",
    "PdfExtractionPolicy",
    "PdfParseError",
    "PyMuPdfNativeParser",
    "LocalRagAssetFileStore",
    "INGESTION_POLICY_VERSION",
    "RagIngestionEnqueueResult",
    "RagIngestionCommandService",
    "RagDocumentMutationResult",
    "RagIngestionError",
    "RagIngestionProcessResult",
    "RagIngestionService",
]
