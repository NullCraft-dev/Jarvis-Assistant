"""格式无关的多模态文档预处理契约。"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Awaitable, Callable, Protocol


_HASH = re.compile(r"^[0-9a-f]{64}$")


class DocumentNodeType(str, Enum):
    HEADING = "heading"
    PARAGRAPH = "paragraph"
    LIST = "list"
    CODE = "code"
    TABLE = "table"
    IMAGE = "image"
    CHART = "chart"
    FORMULA = "formula"
    CAPTION = "caption"


class NodeExtractionMethod(str, Enum):
    NATIVE = "native"
    PADDLEOCR_VL = "paddleocr_vl"
    HYBRID = "hybrid"


class PageRoutingReason(str, Enum):
    OCR_REQUIRED = "ocr_required"
    COMPLEX_IMAGE = "complex_image"
    COMPLEX_TABLE = "complex_table"


@dataclass(frozen=True, slots=True)
class VisualRegion:
    """仅对页面中的一个复杂区域执行视觉解析。"""

    bounding_box: tuple[float, float, float, float]
    reasons: tuple[PageRoutingReason, ...]

    def __post_init__(self) -> None:
        x0, y0, x1, y1 = self.bounding_box
        if not (0 <= x0 < x1 and 0 <= y0 < y1):
            raise ValueError("视觉区域 bounding_box 无效")
        if not self.reasons or len(set(self.reasons)) != len(self.reasons):
            raise ValueError("视觉区域必须包含唯一的路由原因")


@dataclass(frozen=True, slots=True)
class PageRoutingDecision:
    page_number: int
    reasons: tuple[PageRoutingReason, ...]
    regions: tuple[VisualRegion, ...] = ()

    def __post_init__(self) -> None:
        if self.page_number < 1 or not self.reasons:
            raise ValueError("视觉页面路由决策必须包含页码和原因")
        if len(set(self.reasons)) != len(self.reasons):
            raise ValueError("视觉页面路由原因不能重复")
        if any(not set(region.reasons).issubset(self.reasons) for region in self.regions):
            raise ValueError("视觉区域原因必须属于页面路由原因")


@dataclass(frozen=True, slots=True)
class DocumentNode:
    node_id: str
    node_type: DocumentNodeType
    page_number: int
    order_index: int
    bounding_box: tuple[float, float, float, float]
    page_width: float
    page_height: float
    extraction_method: NodeExtractionMethod
    extraction_version: str
    confidence: float
    text: str = ""
    structured_data: dict = field(default_factory=dict)
    asset_bytes: bytes | None = None
    asset_mime_type: str | None = None
    parent_node_id: str | None = None
    related_node_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not _HASH.fullmatch(self.node_id):
            raise ValueError("Document node_id 必须是 64 位小写 SHA-256")
        if self.page_number < 1 or self.order_index < 0:
            raise ValueError("Document node 页码和顺序无效")
        if self.page_width <= 0 or self.page_height <= 0:
            raise ValueError("Document node 页面尺寸必须大于 0")
        x0, y0, x1, y1 = self.bounding_box
        if not (0 <= x0 < x1 <= self.page_width and 0 <= y0 < y1 <= self.page_height):
            raise ValueError("Document node bounding_box 超出页面")
        if not self.extraction_version.strip():
            raise ValueError("Document node extraction_version 不能为空")
        if not 0 <= self.confidence <= 1:
            raise ValueError("Document node confidence 必须在 0..1")
        if not self.text.strip() and self.asset_bytes is None and not self.structured_data:
            raise ValueError("Document node 必须包含文本、结构数据或受控二进制内容")
        if (self.asset_bytes is None) is not (self.asset_mime_type is None):
            raise ValueError("Document node asset bytes/mime_type 必须同时提供")


@dataclass(frozen=True, slots=True)
class PreprocessedDocument:
    page_count: int
    nodes: tuple[DocumentNode, ...]
    native_parser_version: str
    preprocessing_policy_version: str
    structure_provider: str | None = None
    structure_provider_version: str | None = None
    pages_processed_by_structure_model: tuple[int, ...] = ()
    page_routing_decisions: tuple[PageRoutingDecision, ...] = ()

    def __post_init__(self) -> None:
        if self.page_count < 1:
            raise ValueError("Preprocessed document 必须至少包含一页")
        if not self.native_parser_version.strip() or not self.preprocessing_policy_version.strip():
            raise ValueError("Preprocessed document 版本不能为空")
        if len({node.node_id for node in self.nodes}) != len(self.nodes):
            raise ValueError("Preprocessed document node_id 必须唯一")
        if any(node.page_number > self.page_count for node in self.nodes):
            raise ValueError("Preprocessed document node 页码超出范围")
        if bool(self.structure_provider) is not bool(self.structure_provider_version):
            raise ValueError("Structure provider 和版本必须同时提供")
        decision_pages = tuple(item.page_number for item in self.page_routing_decisions)
        if len(set(decision_pages)) != len(decision_pages):
            raise ValueError("视觉页面路由决策页码不能重复")
        if any(page > self.page_count for page in decision_pages):
            raise ValueError("视觉页面路由决策页码超出范围")


@dataclass(frozen=True, slots=True)
class StructurePageResult:
    page_number: int
    nodes: tuple[DocumentNode, ...]
    provider: str
    provider_version: str


@dataclass(frozen=True, slots=True)
class PreprocessingProgress:
    page_count: int
    native_extraction_done: bool
    visual_pages_total: int
    visual_pages_completed: int
    active_executor: str
    visual_route_counts: dict[str, int] = field(default_factory=dict)


PreprocessingProgressCallback = Callable[[PreprocessingProgress], Awaitable[None]]


class DocumentStructureProvider(Protocol):
    provider_name: str
    provider_version: str

    async def analyze_page(
        self,
        *,
        image: bytes,
        mime_type: str,
        page_number: int,
        page_width: float,
        page_height: float,
    ) -> StructurePageResult: ...


class StructureResultCache(Protocol):
    async def get(self, cache_key: str) -> StructurePageResult | None: ...

    async def put(self, cache_key: str, result: StructurePageResult) -> None: ...


class DocumentPreprocessor(Protocol):
    async def preprocess_pdf(
        self,
        content: bytes,
        *,
        progress_callback: PreprocessingProgressCallback | None = None,
    ) -> PreprocessedDocument: ...
