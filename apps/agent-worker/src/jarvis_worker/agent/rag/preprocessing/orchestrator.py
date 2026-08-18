"""PyMuPDF 原生优先、PaddleOCR-VL 按页增强的预处理编排器。"""

from __future__ import annotations

import hashlib
import logging
from dataclasses import replace

import pymupdf

from jarvis_worker.agent.rag.ingestion.pdf_parser import PyMuPdfNativeParser
from jarvis_worker.agent.rag.preprocessing.contracts import (
    DocumentNode,
    DocumentNodeType,
    DocumentStructureProvider,
    NodeExtractionMethod,
    PageRoutingReason,
    PreprocessedDocument,
    PreprocessingProgress,
    PreprocessingProgressCallback,
    StructurePageResult,
    StructureResultCache,
)
from jarvis_worker.agent.rag.preprocessing.cache import build_structure_cache_key
from jarvis_worker.agent.rag.preprocessing.native import native_nodes
from jarvis_worker.agent.rag.preprocessing.identifiers import build_document_node_id
from jarvis_worker.agent.rag.preprocessing.policy import PageRoutingPolicy


log = logging.getLogger("jarvis_worker.rag_preprocessing")


class MultimodalDocumentPreprocessor:
    def __init__(
        self,
        *,
        native_parser: PyMuPdfNativeParser | None = None,
        structure_provider: DocumentStructureProvider | None = None,
        routing_policy: PageRoutingPolicy | None = None,
        structure_cache: StructureResultCache | None = None,
        render_dpi: int = 144,
    ) -> None:
        if render_dpi < 72 or render_dpi > 240:
            raise ValueError("RAG 页面渲染 DPI 必须在 72..240")
        self._native_parser = native_parser or PyMuPdfNativeParser()
        self._structure_provider = structure_provider
        self._routing_policy = routing_policy or PageRoutingPolicy()
        self._structure_cache = structure_cache
        self._render_dpi = render_dpi

    async def preprocess_pdf(
        self,
        content: bytes,
        *,
        progress_callback: PreprocessingProgressCallback | None = None,
    ) -> PreprocessedDocument:
        if progress_callback is not None:
            await progress_callback(
                PreprocessingProgress(
                    page_count=0,
                    native_extraction_done=False,
                    visual_pages_total=0,
                    visual_pages_completed=0,
                    active_executor="pymupdf",
                )
            )
        parsed = self._native_parser.parse(content)
        document_hash = hashlib.sha256(content).hexdigest()
        nodes = list(native_nodes(parsed))
        routing_decisions = self._routing_policy.plan(parsed)
        requested_pages = tuple(item.page_number for item in routing_decisions)
        route_counts: dict[str, int] = {}
        for decision in routing_decisions:
            for reason in decision.reasons:
                route_counts[reason.value] = route_counts.get(reason.value, 0) + 1
        processed_pages: list[int] = []
        visual_pages_total = len(requested_pages) if self._structure_provider is not None else 0
        if progress_callback is not None:
            await progress_callback(
                PreprocessingProgress(
                    page_count=parsed.page_count,
                    native_extraction_done=True,
                    visual_pages_total=visual_pages_total,
                    visual_pages_completed=0,
                    active_executor=("paddleocr-vl" if visual_pages_total else "pymupdf"),
                    visual_route_counts=route_counts,
                )
            )

        if requested_pages and self._structure_provider is not None:
            ocr_pages = set(parsed.pages_requiring_ocr)
            decisions_by_page = {
                decision.page_number: decision for decision in routing_decisions
            }
            for page_number in requested_pages:
                page_nodes = [node for node in nodes if node.page_number == page_number]
                page_width = page_nodes[0].page_width if page_nodes else _page_size(content, page_number)[0]
                page_height = page_nodes[0].page_height if page_nodes else _page_size(content, page_number)[1]
                decision = decisions_by_page[page_number]
                region_boxes: tuple[tuple[float, float, float, float] | None, ...]
                if (
                    PageRoutingReason.OCR_REQUIRED in decision.reasons
                    or not decision.regions
                ):
                    region_boxes = (None,)
                else:
                    region_boxes = tuple(
                        _expand_region(
                            region.bounding_box,
                            page_width=page_width,
                            page_height=page_height,
                        )
                        for region in decision.regions
                    )
                structured_nodes: list[DocumentNode] = []
                for region_box in region_boxes:
                    structured = await self._analyze_visual_region(
                        content=content,
                        document_hash=document_hash,
                        page_number=page_number,
                        page_width=page_width,
                        page_height=page_height,
                        region_box=region_box,
                    )
                    structured_nodes.extend(structured.nodes)
                nodes = _merge_page_nodes(
                    nodes,
                    tuple(structured_nodes),
                    page_number=page_number,
                    replace_native_text=page_number in ocr_pages,
                )
                processed_pages.append(page_number)
                if progress_callback is not None:
                    await progress_callback(
                        PreprocessingProgress(
                            page_count=parsed.page_count,
                            native_extraction_done=True,
                            visual_pages_total=visual_pages_total,
                            visual_pages_completed=len(processed_pages),
                            active_executor="paddleocr-vl",
                            visual_route_counts=route_counts,
                        )
                    )

        nodes.sort(
            key=lambda node: (
                node.page_number,
                node.order_index,
                node.bounding_box[1],
                node.bounding_box[0],
            )
        )
        provider = self._structure_provider
        return PreprocessedDocument(
            page_count=parsed.page_count,
            nodes=tuple(nodes),
            native_parser_version=parsed.parser_version,
            preprocessing_policy_version=self._routing_policy.policy_version,
            structure_provider=provider.provider_name if provider and processed_pages else None,
            structure_provider_version=(
                provider.provider_version if provider and processed_pages else None
            ),
            pages_processed_by_structure_model=tuple(processed_pages),
            page_routing_decisions=routing_decisions,
        )

    async def _analyze_visual_region(
        self,
        *,
        content: bytes,
        document_hash: str,
        page_number: int,
        page_width: float,
        page_height: float,
        region_box: tuple[float, float, float, float] | None,
    ) -> StructurePageResult:
        provider = self._structure_provider
        if provider is None:
            raise RuntimeError("RAG structure provider 未配置")
        cache_key = build_structure_cache_key(
            document_hash=document_hash,
            page_number=page_number,
            bounding_box=region_box,
            render_dpi=self._render_dpi,
            policy_version=self._routing_policy.policy_version,
            provider_name=provider.provider_name,
            provider_version=provider.provider_version,
        )
        if self._structure_cache is not None:
            cached = await self._structure_cache.get(cache_key)
            if cached is not None:
                log.info(
                    "RAG structure cache 命中: page=%s scope=%s key=%s",
                    page_number,
                    "region" if region_box is not None else "full_page",
                    cache_key[:12],
                )
                return cached

        image = _render_page(
            content,
            page_number=page_number,
            dpi=self._render_dpi,
            clip=region_box,
        )
        analysis_width = (
            region_box[2] - region_box[0] if region_box is not None else page_width
        )
        analysis_height = (
            region_box[3] - region_box[1] if region_box is not None else page_height
        )
        result = await provider.analyze_page(
            image=image,
            mime_type="image/png",
            page_number=page_number,
            page_width=analysis_width,
            page_height=analysis_height,
        )
        if region_box is not None:
            result = _translate_region_result(
                result,
                region_box=region_box,
                page_width=page_width,
                page_height=page_height,
            )
        if self._structure_cache is not None:
            try:
                await self._structure_cache.put(cache_key, result)
            except (OSError, ValueError):
                log.warning(
                    "RAG structure cache 写入失败: page=%s key=%s",
                    page_number,
                    cache_key[:12],
                )
        return result


def _merge_page_nodes(
    current: list[DocumentNode],
    structured: tuple[DocumentNode, ...],
    *,
    page_number: int,
    replace_native_text: bool,
) -> list[DocumentNode]:
    native_page = [node for node in current if node.page_number == page_number]
    other_pages = [node for node in current if node.page_number != page_number]
    if replace_native_text:
        retained_native = [
            node
            for node in native_page
            if node.asset_bytes is not None and node.node_type is DocumentNodeType.IMAGE
        ]
    else:
        retained_native = list(native_page)

    additions: list[DocumentNode] = []
    for candidate in structured:
        duplicate = next(
            (
                native
                for native in retained_native
                if _same_semantic_family(native.node_type, candidate.node_type)
                and _intersection_ratio(native.bounding_box, candidate.bounding_box) >= 0.7
            ),
            None,
        )
        if duplicate is None:
            additions.append(candidate)
            continue
        if duplicate.node_type is DocumentNodeType.IMAGE and candidate.text.strip():
            retained_native.remove(duplicate)
            additions.append(_hybrid_node(duplicate, candidate))
        elif duplicate.node_type in {
            DocumentNodeType.TABLE,
            DocumentNodeType.CHART,
            DocumentNodeType.FORMULA,
        } and len(candidate.text) > len(duplicate.text):
            retained_native.remove(duplicate)
            additions.append(_hybrid_node(duplicate, candidate))
    return [*other_pages, *retained_native, *additions]


def _hybrid_node(native: DocumentNode, structured: DocumentNode) -> DocumentNode:
    text = structured.text.strip() or native.text.strip()
    combined_data = {
        **native.structured_data,
        **structured.structured_data,
        "native_node_id": native.node_id,
        "structure_node_id": structured.node_id,
    }
    content_material = (
        text
        + str(combined_data.get("source_content_hash") or "")
        + native.node_id
        + structured.node_id
    )
    content_hash = hashlib.sha256(content_material.encode("utf-8")).hexdigest()
    version = f"{native.extraction_version}+{structured.extraction_version}"
    node_id = build_document_node_id(
        page_number=native.page_number,
        order_index=min(native.order_index, structured.order_index),
        node_type=native.node_type,
        bounding_box=native.bounding_box,
        extraction_method=NodeExtractionMethod.HYBRID,
        extraction_version=version,
        content_hash=content_hash,
    )
    return replace(
        native,
        node_id=node_id,
        order_index=min(native.order_index, structured.order_index),
        text=text,
        structured_data=combined_data,
        extraction_method=NodeExtractionMethod.HYBRID,
        extraction_version=version,
        confidence=min(native.confidence, structured.confidence),
    )


def _same_semantic_family(first: DocumentNodeType, second: DocumentNodeType) -> bool:
    textual = {
        DocumentNodeType.HEADING,
        DocumentNodeType.PARAGRAPH,
        DocumentNodeType.LIST,
        DocumentNodeType.CODE,
        DocumentNodeType.CAPTION,
    }
    if first in textual and second in textual:
        return True
    return first is second


def _intersection_ratio(
    first: tuple[float, float, float, float],
    second: tuple[float, float, float, float],
) -> float:
    x0, y0 = max(first[0], second[0]), max(first[1], second[1])
    x1, y1 = min(first[2], second[2]), min(first[3], second[3])
    if x1 <= x0 or y1 <= y0:
        return 0.0
    intersection = (x1 - x0) * (y1 - y0)
    smaller = min(
        (first[2] - first[0]) * (first[3] - first[1]),
        (second[2] - second[0]) * (second[3] - second[1]),
    )
    return intersection / max(smaller, 1.0)


def _translate_region_result(
    result: StructurePageResult,
    *,
    region_box: tuple[float, float, float, float],
    page_width: float,
    page_height: float,
) -> StructurePageResult:
    offset_x, offset_y = region_box[0], region_box[1]
    translated: list[DocumentNode] = []
    for node in result.nodes:
        bbox = (
            max(0.0, min(page_width, node.bounding_box[0] + offset_x)),
            max(0.0, min(page_height, node.bounding_box[1] + offset_y)),
            max(0.0, min(page_width, node.bounding_box[2] + offset_x)),
            max(0.0, min(page_height, node.bounding_box[3] + offset_y)),
        )
        if bbox[0] >= bbox[2] or bbox[1] >= bbox[3]:
            continue
        structured_data = {
            **node.structured_data,
            "analysis_scope": "region",
            "source_region_bbox": list(region_box),
        }
        material = (
            node.text
            + node.node_id
            + ":".join(f"{value:.4f}" for value in bbox)
        )
        node_id = build_document_node_id(
            page_number=node.page_number,
            order_index=node.order_index,
            node_type=node.node_type,
            bounding_box=bbox,
            extraction_method=node.extraction_method,
            extraction_version=node.extraction_version,
            content_hash=hashlib.sha256(material.encode("utf-8")).hexdigest(),
        )
        translated.append(
            replace(
                node,
                node_id=node_id,
                bounding_box=bbox,
                page_width=page_width,
                page_height=page_height,
                structured_data=structured_data,
            )
        )
    return StructurePageResult(
        page_number=result.page_number,
        nodes=tuple(translated),
        provider=result.provider,
        provider_version=result.provider_version,
    )


def _expand_region(
    bbox: tuple[float, float, float, float],
    *,
    page_width: float,
    page_height: float,
    padding: float = 8.0,
) -> tuple[float, float, float, float]:
    return (
        max(0.0, bbox[0] - padding),
        max(0.0, bbox[1] - padding),
        min(page_width, bbox[2] + padding),
        min(page_height, bbox[3] + padding),
    )


def _render_page(
    content: bytes,
    *,
    page_number: int,
    dpi: int,
    clip: tuple[float, float, float, float] | None = None,
) -> bytes:
    document = pymupdf.open(stream=content, filetype="pdf")
    try:
        page = document[page_number - 1]
        rect = pymupdf.Rect(clip) if clip is not None else None
        return page.get_pixmap(dpi=dpi, alpha=False, clip=rect).tobytes("png")
    finally:
        document.close()


def _page_size(content: bytes, page_number: int) -> tuple[float, float]:
    document = pymupdf.open(stream=content, filetype="pdf")
    try:
        page = document[page_number - 1]
        return float(page.rect.width), float(page.rect.height)
    finally:
        document.close()
