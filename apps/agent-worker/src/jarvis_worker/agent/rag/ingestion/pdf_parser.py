"""PyMuPDF 原生 PDF 解析 adapter。

只接收已经由 Artifact 边界校验过的 bytes；不接受任意文件路径，也不执行 OCR。
"""

from __future__ import annotations

import hashlib
import re
from collections import Counter
from statistics import median

import pymupdf

from jarvis_worker.agent.rag.contracts import RagElementType
from jarvis_worker.agent.rag.ingestion.contracts import (
    DocumentBlock,
    ExtractedElement,
    NativePageMetrics,
    ParsedPdfDocument,
    PdfBlockType,
    PdfExtractionPolicy,
)
from jarvis_worker.agent.rag.ingestion.sanitization import (
    remove_nul,
    remove_nul_from_json,
)


_LIST_PREFIX = re.compile(r"^(?:[-*•‣▪◦]|\d+[.)]|[A-Za-z][.)])\s+")
_SPACE = re.compile(r"[ \t\u00a0]+")


class PdfParseError(ValueError):
    pass


class PyMuPdfNativeParser:
    def __init__(self, policy: PdfExtractionPolicy | None = None):
        self._policy = policy or PdfExtractionPolicy()

    @property
    def version(self) -> str:
        return self._policy.parser_version

    def parse(self, content: bytes) -> ParsedPdfDocument:
        if not content.startswith(b"%PDF-"):
            raise PdfParseError("RAG 来源不是有效 PDF")
        try:
            document = pymupdf.open(stream=content, filetype="pdf")
        except (RuntimeError, ValueError, TypeError) as exc:
            raise PdfParseError("RAG PDF 无法打开") from exc
        try:
            if document.needs_pass:
                raise PdfParseError("RAG PDF 已加密，当前不支持解析")
            if document.page_count < 1:
                raise PdfParseError("RAG PDF 不包含页面")
            if document.page_count > self._policy.max_pages:
                raise PdfParseError("RAG PDF 页数超过解析上限")
            return self._parse_document(document)
        finally:
            document.close()

    def _parse_document(self, document: pymupdf.Document) -> ParsedPdfDocument:
        blocks: list[DocumentBlock] = []
        elements: list[ExtractedElement] = []
        page_metrics: list[NativePageMetrics] = []
        total_chars = 0

        for page_index in range(document.page_count):
            page = document[page_index]
            page_number = page_index + 1
            page_width = float(page.rect.width)
            page_height = float(page.rect.height)
            tables = self._extract_tables(page, page_number, page_width, page_height)
            table_boxes = [item.bounding_box for item in tables]
            elements.extend(tables)

            text_blocks = self._extract_text_blocks(
                page,
                page_number=page_number,
                page_width=page_width,
                page_height=page_height,
                table_boxes=table_boxes,
            )
            blocks.extend(text_blocks)
            for table in tables:
                markdown = str(table.structured_data.get("markdown") or "").strip()
                if markdown:
                    blocks.append(DocumentBlock(
                        page_number=page_number,
                        order_index=table.order_index,
                        block_type=PdfBlockType.TABLE_PROXY,
                        text=markdown,
                        bounding_box=table.bounding_box,
                        page_width=page_width,
                        page_height=page_height,
                    ))

            image_elements, image_coverage = self._extract_images(
                page, page_number, page_width, page_height
            )
            elements.extend(image_elements)
            # OCR 质量判断必须基于 PyMuPDF 的原始文字层，而不是已经为避免
            # table proxy 重复而过滤过的 text_blocks。否则一页只要被
            # find_tables() 覆盖，就会被误判为“无原生文字”。
            native_text = page.get_text("text", flags=pymupdf.TEXTFLAGS_TEXT, sort=True)
            native_chars = len(native_text)
            total_chars += native_chars
            if total_chars > self._policy.max_extracted_chars:
                raise PdfParseError("RAG PDF 提取文字超过上限")
            replacement_ratio = native_text.count("�") / max(native_chars, 1)
            page_metrics.append(NativePageMetrics(
                page_number=page_number,
                native_char_count=native_chars,
                image_coverage=image_coverage,
                replacement_char_ratio=replacement_ratio,
            ))

        blocks = self._remove_repeated_headers_and_footers(blocks, document.page_count)
        blocks.sort(key=lambda item: (item.page_number, item.bounding_box[1], item.bounding_box[0], item.order_index))
        elements.sort(key=lambda item: (item.page_number, item.bounding_box[1], item.bounding_box[0], item.order_index))
        pages_requiring_ocr = tuple(
            metrics.page_number
            for metrics in page_metrics
            if self._requires_ocr(
                metrics.native_char_count,
                metrics.image_coverage,
                metrics.replacement_char_ratio,
            )
        )
        return ParsedPdfDocument(
            page_count=document.page_count,
            blocks=tuple(blocks),
            elements=tuple(elements),
            pages_requiring_ocr=pages_requiring_ocr,
            native_char_count=sum(len(block.text) for block in blocks),
            parser_version=self.version,
            page_metrics=tuple(page_metrics),
        )

    def _requires_ocr(
        self, native_chars: int, image_coverage: float, replacement_ratio: float
    ) -> bool:
        if native_chars == 0:
            return True
        if replacement_ratio >= self._policy.max_replacement_char_ratio:
            return True
        return (
            native_chars < self._policy.min_native_chars_per_page
            and image_coverage >= self._policy.scanned_image_coverage_threshold
        )

    def _extract_text_blocks(
        self,
        page: pymupdf.Page,
        *,
        page_number: int,
        page_width: float,
        page_height: float,
        table_boxes: list[tuple[float, float, float, float]],
    ) -> list[DocumentBlock]:
        raw = page.get_text("dict", flags=pymupdf.TEXTFLAGS_TEXT, sort=True)
        raw_blocks = [item for item in raw.get("blocks", []) if item.get("type") == 0]
        font_sizes = [
            float(span.get("size") or 0)
            for block in raw_blocks
            for line in block.get("lines", [])
            for span in line.get("spans", [])
            if str(span.get("text") or "").strip()
        ]
        body_size = median(font_sizes) if font_sizes else 10.0
        result: list[DocumentBlock] = []
        for order_index, block in enumerate(raw_blocks):
            bbox = _bbox(block.get("bbox"), page_width, page_height)
            if bbox is None or any(_intersection_ratio(bbox, table) >= 0.5 for table in table_boxes):
                continue
            lines = block.get("lines", [])
            text = _join_lines([
                "".join(str(span.get("text") or "") for span in line.get("spans", []))
                for line in lines
            ])
            if not text:
                continue
            spans = [span for line in lines for span in line.get("spans", [])]
            max_size = max((float(span.get("size") or 0) for span in spans), default=body_size)
            bold = any(int(span.get("flags") or 0) & 16 for span in spans)
            monospaced = bool(spans) and sum(
                1 for span in spans if int(span.get("flags") or 0) & 8
            ) >= max(1, len(spans) // 2)
            block_type, heading_level = _classify_block(
                text, max_size=max_size, body_size=body_size, bold=bold,
                monospaced=monospaced,
            )
            result.append(DocumentBlock(
                page_number=page_number,
                order_index=order_index,
                block_type=block_type,
                text=text,
                bounding_box=bbox,
                page_width=page_width,
                page_height=page_height,
                heading_level=heading_level,
                font_size=max_size,
                bold=bold,
            ))
        return result

    def _extract_tables(
        self, page: pymupdf.Page, page_number: int, page_width: float, page_height: float
    ) -> list[ExtractedElement]:
        result: list[ExtractedElement] = []
        finder = page.find_tables()
        for index, table in enumerate(finder.tables):
            bbox = _bbox(table.bbox, page_width, page_height)
            if bbox is None:
                continue
            rows = remove_nul_from_json(table.extract())
            markdown = remove_nul(table.to_markdown()).strip()
            canonical = markdown or repr(rows)
            result.append(ExtractedElement(
                page_number=page_number,
                order_index=10_000 + index,
                element_type=RagElementType.TABLE,
                bounding_box=bbox,
                page_width=page_width,
                page_height=page_height,
                content_hash=hashlib.sha256(canonical.encode("utf-8")).hexdigest(),
                structured_data={"rows": rows, "markdown": markdown},
                confidence=1.0,
            ))
        return result

    def _extract_images(
        self, page: pymupdf.Page, page_number: int, page_width: float, page_height: float
    ) -> tuple[list[ExtractedElement], float]:
        result: list[ExtractedElement] = []
        total_area = 0.0
        page_area = max(page_width * page_height, 1.0)
        for index, info in enumerate(page.get_image_info(hashes=True, xrefs=False)):
            bbox = _bbox(info.get("bbox"), page_width, page_height)
            if bbox is None:
                continue
            area = (bbox[2] - bbox[0]) * (bbox[3] - bbox[1])
            total_area += area
            if area / page_area < 0.01 or int(info.get("width") or 0) < 32 or int(info.get("height") or 0) < 32:
                continue
            try:
                pixmap = page.get_pixmap(clip=pymupdf.Rect(bbox), dpi=144, alpha=False)
                asset_bytes = pixmap.tobytes("png")
            except (RuntimeError, ValueError):
                asset_bytes = None
                pixmap = None
            if asset_bytes is not None:
                content_hash = hashlib.sha256(asset_bytes).hexdigest()
                bounded_asset = (
                    asset_bytes
                    if len(asset_bytes) <= self._policy.max_native_asset_bytes
                    else None
                )
                asset_width = pixmap.width if pixmap is not None else None
                asset_height = pixmap.height if pixmap is not None else None
            else:
                digest = info.get("digest")
                digest_bytes = digest if isinstance(digest, bytes) else repr(info).encode("utf-8")
                content_hash = hashlib.sha256(digest_bytes).hexdigest()
                bounded_asset = None
                asset_width = None
                asset_height = None
            result.append(ExtractedElement(
                page_number=page_number,
                order_index=20_000 + index,
                element_type=RagElementType.IMAGE,
                bounding_box=bbox,
                page_width=page_width,
                page_height=page_height,
                content_hash=content_hash,
                asset_bytes=bounded_asset,
                asset_mime_type="image/png" if bounded_asset is not None else None,
                asset_width=asset_width,
                asset_height=asset_height,
                confidence=1.0,
            ))
        return result, min(total_area / page_area, 1.0)

    @staticmethod
    def _remove_repeated_headers_and_footers(
        blocks: list[DocumentBlock], page_count: int
    ) -> list[DocumentBlock]:
        if page_count < 3:
            return blocks
        marginal = [
            block for block in blocks
            if block.bounding_box[1] <= block.page_height * 0.08
            or block.bounding_box[3] >= block.page_height * 0.92
        ]
        counts = Counter(_margin_key(block.text) for block in marginal)
        threshold = max(3, (page_count + 1) // 2)
        repeated = {key for key, count in counts.items() if key and count >= threshold}
        return [
            block for block in blocks
            if not (
                _margin_key(block.text) in repeated
                and (
                    block.bounding_box[1] <= block.page_height * 0.08
                    or block.bounding_box[3] >= block.page_height * 0.92
                )
            )
        ]


def _bbox(
    value: object, page_width: float, page_height: float
) -> tuple[float, float, float, float] | None:
    if not isinstance(value, (list, tuple)) or len(value) != 4:
        return None
    try:
        x0, y0, x1, y1 = (float(item) for item in value)
    except (TypeError, ValueError):
        return None
    x0, y0 = max(0.0, x0), max(0.0, y0)
    x1, y1 = min(page_width, x1), min(page_height, y1)
    if x1 <= x0 or y1 <= y0:
        return None
    return (x0, y0, x1, y1)


def _intersection_ratio(
    first: tuple[float, float, float, float],
    second: tuple[float, float, float, float],
) -> float:
    x0, y0 = max(first[0], second[0]), max(first[1], second[1])
    x1, y1 = min(first[2], second[2]), min(first[3], second[3])
    if x1 <= x0 or y1 <= y0:
        return 0.0
    intersection = (x1 - x0) * (y1 - y0)
    area = max((first[2] - first[0]) * (first[3] - first[1]), 1.0)
    return intersection / area


def _join_lines(lines: list[str]) -> str:
    normalized = [_SPACE.sub(" ", remove_nul(line)).strip() for line in lines]
    normalized = [line for line in normalized if line]
    if not normalized:
        return ""
    result = normalized[0]
    for line in normalized[1:]:
        if result.endswith("-") and line[:1].islower():
            result = result[:-1] + line
        elif _is_cjk(result[-1:]) and _is_cjk(line[:1]):
            result += line
        else:
            result += " " + line
    return result.strip()


def _is_cjk(value: str) -> bool:
    return bool(value) and "\u3400" <= value <= "\u9fff"


def _classify_block(
    text: str, *, max_size: float, body_size: float, bold: bool, monospaced: bool
) -> tuple[PdfBlockType, int | None]:
    if monospaced and len(text) > 20:
        return PdfBlockType.CODE, None
    if _LIST_PREFIX.match(text):
        return PdfBlockType.LIST, None
    short = len(text) <= 160 and text.count(".") <= 2
    ratio = max_size / max(body_size, 1.0)
    if short and (ratio >= 1.15 or (bold and ratio >= 1.0)):
        level = 1 if ratio >= 1.7 else 2 if ratio >= 1.35 else 3
        return PdfBlockType.HEADING, level
    return PdfBlockType.PARAGRAPH, None


def _margin_key(text: str) -> str:
    return re.sub(r"\d+", "#", _SPACE.sub(" ", text).strip().casefold())
