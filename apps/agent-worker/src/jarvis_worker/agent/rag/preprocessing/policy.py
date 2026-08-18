"""原生质量优先、可解释且有文档级误判保护的页面路由策略。"""

from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass

from jarvis_worker.agent.rag.contracts import RagElementType
from jarvis_worker.agent.rag.ingestion.contracts import (
    ExtractedElement,
    ParsedPdfDocument,
)
from jarvis_worker.agent.rag.preprocessing.contracts import (
    PageRoutingDecision,
    PageRoutingReason,
    VisualRegion,
)


_IMAGE_TYPES = {
    RagElementType.IMAGE,
    RagElementType.FIGURE,
    RagElementType.CHART,
    RagElementType.DIAGRAM,
}


@dataclass(frozen=True, slots=True)
class PageRoutingPolicy:
    enrich_image_pages: bool = True
    enrich_table_pages: bool = True
    min_semantic_image_area_ratio: float = 0.05
    max_background_image_area_ratio: float = 0.8
    repeated_image_page_threshold: int = 3
    global_image_layer_page_ratio: float = 0.8
    max_regions_per_page: int = 4
    max_region_area_ratio: float = 0.65
    policy_version: str = "native-first-region-routing-v3"

    def __post_init__(self) -> None:
        ratios = (
            self.min_semantic_image_area_ratio,
            self.max_background_image_area_ratio,
            self.global_image_layer_page_ratio,
            self.max_region_area_ratio,
        )
        if any(not 0 <= value <= 1 for value in ratios):
            raise ValueError("RAG 页面路由比例必须在 0..1")
        if self.min_semantic_image_area_ratio >= self.max_background_image_area_ratio:
            raise ValueError("语义图片最小面积必须小于背景图片面积阈值")
        if self.repeated_image_page_threshold < 2:
            raise ValueError("重复图片页数阈值不能小于 2")
        if self.max_regions_per_page < 1:
            raise ValueError("单页视觉区域上限必须大于 0")

    def pages_for_structure_model(self, parsed: ParsedPdfDocument) -> tuple[int, ...]:
        return tuple(item.page_number for item in self.plan(parsed))

    def plan(self, parsed: ParsedPdfDocument) -> tuple[PageRoutingDecision, ...]:
        reasons_by_page: dict[int, set[PageRoutingReason]] = defaultdict(set)
        regions_by_page: dict[int, list[tuple[ExtractedElement, PageRoutingReason]]] = (
            defaultdict(list)
        )
        for page_number in parsed.pages_requiring_ocr:
            reasons_by_page[page_number].add(PageRoutingReason.OCR_REQUIRED)

        if self.enrich_table_pages:
            for element in parsed.elements:
                if (
                    element.element_type is RagElementType.TABLE
                    and not _native_table_is_usable(element.structured_data)
                ):
                    reasons_by_page[element.page_number].add(
                        PageRoutingReason.COMPLEX_TABLE
                    )
                    regions_by_page[element.page_number].append(
                        (element, PageRoutingReason.COMPLEX_TABLE)
                    )

        if self.enrich_image_pages:
            for image in self._semantic_images(parsed):
                reasons_by_page[image.page_number].add(PageRoutingReason.COMPLEX_IMAGE)
                regions_by_page[image.page_number].append(
                    (image, PageRoutingReason.COMPLEX_IMAGE)
                )

        return tuple(
            PageRoutingDecision(
                page_number=page_number,
                reasons=tuple(sorted(reasons, key=lambda item: item.value)),
                regions=(
                    ()
                    if PageRoutingReason.OCR_REQUIRED in reasons
                    else self._bounded_regions(regions_by_page[page_number])
                ),
            )
            for page_number, reasons in sorted(reasons_by_page.items())
        )

    def _semantic_images(self, parsed: ParsedPdfDocument) -> tuple[ExtractedElement, ...]:
        images = [item for item in parsed.elements if item.element_type in _IMAGE_TYPES]
        pages_with_images = {item.page_number for item in images}
        global_image_layer = (
            parsed.page_count > 1
            and len(pages_with_images) / parsed.page_count
            >= self.global_image_layer_page_ratio
        )
        hash_page_counts = Counter(
            (item.content_hash, item.page_number) for item in images
        )
        repeated_hashes = Counter(
            content_hash for content_hash, _ in hash_page_counts
        )
        metrics = {item.page_number: item for item in parsed.page_metrics}
        per_page = Counter(item.page_number for item in images)
        ocr_pages = set(parsed.pages_requiring_ocr)
        result: list[ExtractedElement] = []
        for image in images:
            if image.page_number in ocr_pages:
                # 同一页面只记录最根本的 OCR 原因；视觉 provider 已会解析整页。
                continue
            area_ratio = _area_ratio(image)
            repeated = (
                repeated_hashes[image.content_hash]
                >= self.repeated_image_page_threshold
            )
            page_metrics = metrics.get(image.page_number)
            native_text_usable = bool(
                page_metrics
                and page_metrics.native_char_count >= 40
                and page_metrics.replacement_char_ratio < 0.05
            )
            background_like = (
                area_ratio >= self.max_background_image_area_ratio
                and native_text_usable
            )
            globally_decorative = (
                global_image_layer
                and native_text_usable
                and per_page[image.page_number] == 1
            )
            if repeated or background_like or globally_decorative:
                continue
            if area_ratio >= self.min_semantic_image_area_ratio:
                result.append(image)
        return tuple(result)

    def _bounded_regions(
        self,
        candidates: list[tuple[ExtractedElement, PageRoutingReason]],
    ) -> tuple[VisualRegion, ...]:
        """区域过多或覆盖过大时返回空，调用方回退整页解析。"""

        grouped: dict[tuple[float, float, float, float], set[PageRoutingReason]] = (
            defaultdict(set)
        )
        page_area = 1.0
        for element, reason in candidates:
            grouped[element.bounding_box].add(reason)
            page_area = max(element.page_width * element.page_height, 1.0)
        if not grouped or len(grouped) > self.max_regions_per_page:
            return ()
        total_area = sum(
            (bbox[2] - bbox[0]) * (bbox[3] - bbox[1]) for bbox in grouped
        )
        if total_area / page_area > self.max_region_area_ratio:
            return ()
        return tuple(
            VisualRegion(
                bounding_box=bbox,
                reasons=tuple(sorted(reasons, key=lambda item: item.value)),
            )
            for bbox, reasons in sorted(
                grouped.items(), key=lambda item: (item[0][1], item[0][0])
            )
        )


def _area_ratio(element: ExtractedElement) -> float:
    x0, y0, x1, y1 = element.bounding_box
    return ((x1 - x0) * (y1 - y0)) / max(
        element.page_width * element.page_height, 1.0
    )


def _native_table_is_usable(data: dict) -> bool:
    markdown = str(data.get("markdown") or "").strip()
    rows = data.get("rows")
    if not markdown or not isinstance(rows, list) or not rows:
        return False
    normalized_rows = [row for row in rows if isinstance(row, (list, tuple))]
    if len(normalized_rows) != len(rows):
        return False
    widths = [len(row) for row in normalized_rows]
    if not widths or min(widths) == 0 or min(widths) != max(widths):
        return False
    cells = [cell for row in normalized_rows for cell in row]
    non_empty = sum(1 for cell in cells if str(cell or "").strip())
    # PyMuPDF 会把题目边框识别成单行多列表格；只要它已经输出稳定的
    # 矩形 rows 和非空 Markdown，就应复用原生结果，而不是因为空布局列
    # 再调用 VLM。真正缺失或不规则的表格才进入视觉增强。
    return non_empty / max(len(cells), 1) >= 0.2
