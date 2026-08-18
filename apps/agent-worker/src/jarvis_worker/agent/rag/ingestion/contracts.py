"""RAG 来源读取与 PDF 原生解析的纯领域契约。"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum

from jarvis_worker.agent.rag.contracts import RagElementType


class PdfBlockType(str, Enum):
    HEADING = "heading"
    PARAGRAPH = "paragraph"
    LIST = "list"
    CODE = "code"
    TABLE_PROXY = "table_proxy"


@dataclass(frozen=True, slots=True)
class PdfExtractionPolicy:
    max_pages: int = 500
    max_extracted_chars: int = 5_000_000
    min_native_chars_per_page: int = 40
    max_replacement_char_ratio: float = 0.05
    scanned_image_coverage_threshold: float = 0.5
    max_native_asset_bytes: int = 8 * 1024 * 1024
    parser_version: str = "pymupdf-native-v1"

    def __post_init__(self) -> None:
        if self.max_pages < 1 or self.max_extracted_chars < 1:
            raise ValueError("PDF extraction limits 必须大于 0")
        if self.min_native_chars_per_page < 0 or self.max_native_asset_bytes < 1:
            raise ValueError("PDF extraction policy 数值无效")
        if not 0 <= self.max_replacement_char_ratio <= 1:
            raise ValueError("PDF replacement ratio 必须在 0..1")
        if not 0 <= self.scanned_image_coverage_threshold <= 1:
            raise ValueError("PDF image coverage threshold 必须在 0..1")


@dataclass(frozen=True, slots=True)
class DocumentBlock:
    page_number: int
    order_index: int
    block_type: PdfBlockType
    text: str
    bounding_box: tuple[float, float, float, float]
    page_width: float
    page_height: float
    heading_level: int | None = None
    font_size: float = 0.0
    bold: bool = False


@dataclass(frozen=True, slots=True)
class ExtractedElement:
    page_number: int
    order_index: int
    element_type: RagElementType
    bounding_box: tuple[float, float, float, float]
    page_width: float
    page_height: float
    content_hash: str
    caption_text: str = ""
    structured_data: dict = field(default_factory=dict)
    asset_bytes: bytes | None = None
    asset_mime_type: str | None = None
    asset_width: int | None = None
    asset_height: int | None = None
    confidence: float = 1.0


@dataclass(frozen=True, slots=True)
class NativePageMetrics:
    """PyMuPDF 原生读取后的逐页质量指标；不得受后续元素去重影响。"""

    page_number: int
    native_char_count: int
    replacement_char_ratio: float
    image_coverage: float

    def __post_init__(self) -> None:
        if self.page_number < 1 or self.native_char_count < 0:
            raise ValueError("PDF 页面原生指标无效")
        if not 0 <= self.replacement_char_ratio <= 1:
            raise ValueError("PDF 页面乱码比例必须在 0..1")
        if not 0 <= self.image_coverage <= 1:
            raise ValueError("PDF 页面图片覆盖率必须在 0..1")


@dataclass(frozen=True, slots=True)
class ParsedPdfDocument:
    page_count: int
    blocks: tuple[DocumentBlock, ...]
    elements: tuple[ExtractedElement, ...]
    pages_requiring_ocr: tuple[int, ...]
    native_char_count: int
    parser_version: str
    page_metrics: tuple[NativePageMetrics, ...] = ()
