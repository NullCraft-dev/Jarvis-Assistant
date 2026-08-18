"""本地多模态预处理和按模态分片测试。"""

from __future__ import annotations

import asyncio
import hashlib
import threading
import time

import pymupdf
import pytest

from jarvis_worker.agent.rag.chunking import (
    ChunkModality,
    ChunkPolicy,
    MultimodalChunkRouter,
)
from jarvis_worker.agent.rag.contracts import RagElementType
from jarvis_worker.agent.rag.ingestion.contracts import (
    ExtractedElement,
    NativePageMetrics,
    ParsedPdfDocument,
)
from jarvis_worker.agent.rag.ingestion.pdf_parser import PyMuPdfNativeParser
from jarvis_worker.agent.rag.preprocessing import (
    DocumentNode,
    DocumentNodeType,
    LocalStructureResultCache,
    MultimodalDocumentPreprocessor,
    NodeExtractionMethod,
    PageRoutingReason,
    PageRoutingPolicy,
    PreprocessedDocument,
    StructurePageResult,
)
from jarvis_worker.agent.rag.preprocessing.providers import (
    PaddleOcrVlConfig,
    PaddleOcrVlProvider,
)


class _Result:
    def __init__(self, payload: dict):
        self.json = payload


class _Pipeline:
    def __init__(self, payload: dict, *, delay: float = 0.0):
        self._payload = payload
        self._delay = delay
        self._lock = threading.Lock()
        self.active = 0
        self.max_active = 0
        self.calls = 0

    def predict(self, image: object, **kwargs: object) -> list[_Result]:
        assert kwargs["use_layout_detection"] is True
        with self._lock:
            self.active += 1
            self.max_active = max(self.max_active, self.active)
            self.calls += 1
        try:
            time.sleep(self._delay)
            return [_Result(self._payload)]
        finally:
            with self._lock:
                self.active -= 1


def _payload() -> dict:
    return {
        "width": 1000,
        "height": 2000,
        "parsing_res_list": [
            {
                "block_label": "paragraph_title",
                "block_content": "多模态预处理",
                "block_bbox": [100, 100, 900, 300],
                "block_order": 0,
            },
            {
                "block_label": "formula",
                "block_content": "E = mc^2",
                "block_bbox": [200, 400, 800, 600],
                "block_order": 1,
            },
        ],
    }


def _digital_pdf() -> bytes:
    document = pymupdf.open()
    page = document.new_page()
    page.insert_text(
        (72, 100),
        "A native digital PDF contains enough searchable text for local extraction only.",
    )
    content = document.tobytes()
    document.close()
    return content


def _scanned_pdf() -> bytes:
    image_document = pymupdf.open()
    image_page = image_document.new_page(width=500, height=700)
    image_page.draw_rect(image_page.rect, color=(0, 0, 0), fill=(0.9, 0.9, 0.9))
    image_bytes = image_page.get_pixmap(alpha=False).tobytes("png")
    image_document.close()
    document = pymupdf.open()
    page = document.new_page(width=500, height=700)
    page.insert_image(page.rect, stream=image_bytes)
    content = document.tobytes()
    document.close()
    return content


def _digital_pdf_with_semantic_image() -> bytes:
    image_document = pymupdf.open()
    image_page = image_document.new_page(width=300, height=180)
    image_page.draw_rect(image_page.rect, color=(0, 0, 0), fill=(0.8, 0.9, 1.0))
    image_bytes = image_page.get_pixmap(alpha=False).tobytes("png")
    image_document.close()
    document = pymupdf.open()
    page = document.new_page(width=500, height=700)
    page.insert_text(
        (40, 80),
        "This digital page has enough searchable native text and one semantic diagram.",
    )
    page.insert_image(pymupdf.Rect(80, 160, 420, 380), stream=image_bytes)
    content = document.tobytes()
    document.close()
    return content


class _RecordingStructureProvider:
    provider_name = "recording-structure"
    provider_version = "v1"

    def __init__(self) -> None:
        self.calls: list[tuple[float, float]] = []

    async def analyze_page(
        self,
        *,
        image: bytes,
        mime_type: str,
        page_number: int,
        page_width: float,
        page_height: float,
    ) -> StructurePageResult:
        assert image and mime_type == "image/png"
        self.calls.append((page_width, page_height))
        text = "该区域展示系统架构关系。"
        return StructurePageResult(
            page_number=page_number,
            provider=self.provider_name,
            provider_version=self.provider_version,
            nodes=(
                DocumentNode(
                    node_id=hashlib.sha256(
                        f"recording:{page_number}:{page_width}:{page_height}".encode()
                    ).hexdigest(),
                    node_type=DocumentNodeType.IMAGE,
                    page_number=page_number,
                    order_index=0,
                    bounding_box=(1, 1, page_width - 1, page_height - 1),
                    page_width=page_width,
                    page_height=page_height,
                    text=text,
                    extraction_method=NodeExtractionMethod.PADDLEOCR_VL,
                    extraction_version=self.provider_version,
                    confidence=0.9,
                ),
            ),
        )


def _node(
    label: str,
    node_type: DocumentNodeType,
    *,
    order: int,
    text: str,
) -> DocumentNode:
    return DocumentNode(
        node_id=hashlib.sha256(label.encode()).hexdigest(),
        node_type=node_type,
        page_number=1,
        order_index=order,
        bounding_box=(20.0, 20.0 + order * 50, 500.0, 60.0 + order * 50),
        page_width=595.0,
        page_height=842.0,
        text=text,
        extraction_method=NodeExtractionMethod.PADDLEOCR_VL,
        extraction_version="v1.6",
        confidence=0.9,
    )


def test_paddleocr_vl_config_rejects_non_local_service() -> None:
    with pytest.raises(ValueError, match="localhost"):
        PaddleOcrVlConfig(server_url="https://example.com/")
    with pytest.raises(ValueError, match="单并发"):
        PaddleOcrVlConfig(max_concurrency=2)


@pytest.mark.asyncio
async def test_paddleocr_vl_maps_layout_and_scales_coordinates() -> None:
    pipeline = _Pipeline(_payload())
    provider = PaddleOcrVlProvider(
        pipeline_factory=lambda config: pipeline,
        image_decoder=lambda content: object(),
    )

    result = await provider.analyze_page(
        image=b"png",
        mime_type="image/png",
        page_number=2,
        page_width=500,
        page_height=1000,
    )

    assert [node.node_type for node in result.nodes] == [
        DocumentNodeType.HEADING,
        DocumentNodeType.FORMULA,
    ]
    assert result.nodes[0].bounding_box == (50.0, 50.0, 450.0, 150.0)
    assert result.provider == "paddleocr-vl-local"


@pytest.mark.asyncio
async def test_paddleocr_vl_normalizes_html_table_for_chunking() -> None:
    payload = {
        "width": 1000,
        "height": 1000,
        "parsing_res_list": [
            {
                "block_label": "table",
                "block_content": (
                    "<table><tr><th>Quarter</th><th>Revenue</th></tr>"
                    "<tr><td>Q1</td><td>10 | USD</td></tr></table>"
                ),
                "block_bbox": [100, 100, 900, 500],
                "block_order": 0,
            }
        ],
    }
    pipeline = _Pipeline(payload)
    provider = PaddleOcrVlProvider(
        pipeline_factory=lambda config: pipeline,
        image_decoder=lambda content: object(),
    )

    result = await provider.analyze_page(
        image=b"png",
        mime_type="image/png",
        page_number=1,
        page_width=500,
        page_height=500,
    )

    assert result.nodes[0].text == (
        "| Quarter | Revenue |\n"
        "| --- | --- |\n"
        "| Q1 | 10 \\| USD |"
    )
    assert (
        result.nodes[0].structured_data["source_format"]
        == "html_table_normalized_to_markdown"
    )


@pytest.mark.asyncio
async def test_paddleocr_vl_serializes_local_vlm_requests() -> None:
    pipeline = _Pipeline(_payload(), delay=0.03)
    provider = PaddleOcrVlProvider(
        pipeline_factory=lambda config: pipeline,
        image_decoder=lambda content: object(),
    )

    await asyncio.gather(
        *(
            provider.analyze_page(
                image=b"png",
                mime_type="image/png",
                page_number=index,
                page_width=500,
                page_height=1000,
            )
            for index in (1, 2, 3)
        )
    )

    assert pipeline.calls == 3
    assert pipeline.max_active == 1
    assert provider.active_requests == 0
    assert provider.waiting_requests == 0


@pytest.mark.asyncio
async def test_preprocessor_skips_vlm_for_clean_digital_page() -> None:
    pipeline = _Pipeline(_payload())
    provider = PaddleOcrVlProvider(
        pipeline_factory=lambda config: pipeline,
        image_decoder=lambda content: object(),
    )
    preprocessor = MultimodalDocumentPreprocessor(structure_provider=provider)

    document = await preprocessor.preprocess_pdf(_digital_pdf())

    assert pipeline.calls == 0
    assert document.pages_processed_by_structure_model == ()
    assert document.page_routing_decisions == ()
    assert any(node.node_type is DocumentNodeType.PARAGRAPH for node in document.nodes)


@pytest.mark.asyncio
async def test_preprocessor_routes_scanned_page_and_retains_native_asset() -> None:
    payload = _payload()
    payload["parsing_res_list"].append(
        {
            "block_label": "image",
            "block_content": "扫描页中包含一张流程图。",
            "block_bbox": [0, 0, 1000, 2000],
            "block_order": 2,
        }
    )
    pipeline = _Pipeline(payload)
    provider = PaddleOcrVlProvider(
        pipeline_factory=lambda config: pipeline,
        image_decoder=lambda content: object(),
    )
    preprocessor = MultimodalDocumentPreprocessor(structure_provider=provider)

    document = await preprocessor.preprocess_pdf(_scanned_pdf())

    assert pipeline.calls == 1
    assert document.pages_processed_by_structure_model == (1,)
    assert document.page_routing_decisions[0].reasons == (
        PageRoutingReason.OCR_REQUIRED,
    )
    hybrid = next(node for node in document.nodes if node.asset_bytes is not None)
    assert hybrid.extraction_method is NodeExtractionMethod.HYBRID
    assert hybrid.text == "扫描页中包含一张流程图。"
    assert any(
        node.extraction_method is NodeExtractionMethod.PADDLEOCR_VL
        for node in document.nodes
    )


@pytest.mark.asyncio
async def test_preprocessor_reports_real_executor_and_page_progress() -> None:
    pipeline = _Pipeline(_payload())
    provider = PaddleOcrVlProvider(
        pipeline_factory=lambda config: pipeline,
        image_decoder=lambda content: object(),
    )
    preprocessor = MultimodalDocumentPreprocessor(structure_provider=provider)
    snapshots = []

    async def capture(progress):
        snapshots.append(progress)

    await preprocessor.preprocess_pdf(_scanned_pdf(), progress_callback=capture)

    assert snapshots[0].active_executor == "pymupdf"
    assert snapshots[0].native_extraction_done is False
    assert snapshots[1].active_executor == "paddleocr-vl"
    assert snapshots[1].page_count == 1
    assert snapshots[-1].visual_pages_completed == 1
    assert snapshots[-1].visual_pages_total == 1
    assert snapshots[-1].visual_route_counts == {"ocr_required": 1}


@pytest.mark.asyncio
async def test_preprocessor_analyzes_only_semantic_image_region() -> None:
    provider = _RecordingStructureProvider()
    preprocessor = MultimodalDocumentPreprocessor(structure_provider=provider)

    document = await preprocessor.preprocess_pdf(_digital_pdf_with_semantic_image())

    assert len(provider.calls) == 1
    analyzed_width, analyzed_height = provider.calls[0]
    assert analyzed_width < 500
    assert analyzed_height < 700
    assert document.page_routing_decisions[0].regions
    enriched = next(
        node
        for node in document.nodes
        if node.structured_data.get("analysis_scope") == "region"
    )
    assert enriched.page_width == 500
    assert enriched.page_height == 700
    assert enriched.bounding_box[0] >= 0
    assert enriched.bounding_box[2] <= 500


@pytest.mark.asyncio
async def test_structure_cache_resumes_completed_visual_regions(tmp_path) -> None:
    provider = _RecordingStructureProvider()
    cache = LocalStructureResultCache(tmp_path / "structure-cache")
    preprocessor = MultimodalDocumentPreprocessor(
        structure_provider=provider,
        structure_cache=cache,
    )
    content = _digital_pdf_with_semantic_image()

    first = await preprocessor.preprocess_pdf(content)
    second = await preprocessor.preprocess_pdf(content)

    assert len(provider.calls) == 1
    assert first.nodes == second.nodes
    assert first.pages_processed_by_structure_model == (1,)
    assert second.pages_processed_by_structure_model == (1,)


@pytest.mark.asyncio
async def test_structure_cache_invalidates_when_provider_version_changes(tmp_path) -> None:
    cache = LocalStructureResultCache(tmp_path / "structure-cache")
    content = _digital_pdf_with_semantic_image()
    first_provider = _RecordingStructureProvider()
    await MultimodalDocumentPreprocessor(
        structure_provider=first_provider,
        structure_cache=cache,
    ).preprocess_pdf(content)
    next_provider = _RecordingStructureProvider()
    next_provider.provider_version = "v2"

    await MultimodalDocumentPreprocessor(
        structure_provider=next_provider,
        structure_cache=cache,
    ).preprocess_pdf(content)

    assert len(first_provider.calls) == 1
    assert len(next_provider.calls) == 1


def _parsed_with_elements(
    elements: tuple[ExtractedElement, ...],
    *,
    page_count: int = 1,
    ocr_pages: tuple[int, ...] = (),
    native_chars: int = 200,
) -> ParsedPdfDocument:
    return ParsedPdfDocument(
        page_count=page_count,
        blocks=(),
        elements=elements,
        pages_requiring_ocr=ocr_pages,
        native_char_count=native_chars * page_count,
        parser_version="test-native-v1",
        page_metrics=tuple(
            NativePageMetrics(
                page_number=page,
                native_char_count=native_chars,
                replacement_char_ratio=0,
                image_coverage=0.2,
            )
            for page in range(1, page_count + 1)
        ),
    )


def _element(
    label: str,
    element_type: RagElementType,
    *,
    page: int = 1,
    bbox: tuple[float, float, float, float] = (50, 50, 450, 350),
    data: dict | None = None,
) -> ExtractedElement:
    return ExtractedElement(
        page_number=page,
        order_index=20_000,
        element_type=element_type,
        bounding_box=bbox,
        page_width=500,
        page_height=700,
        content_hash=hashlib.sha256(label.encode()).hexdigest(),
        structured_data=data or {},
    )


def test_page_router_keeps_usable_native_table_local() -> None:
    table = _element(
        "table",
        RagElementType.TABLE,
        data={
            "markdown": "| name | value |\n| --- | --- |\n| a | 1 |",
            "rows": [["name", "value"], ["a", "1"]],
        },
    )

    assert PageRoutingPolicy().plan(_parsed_with_elements((table,))) == ()


def test_page_router_keeps_single_row_layout_table_local() -> None:
    table = _element(
        "layout-table",
        RagElementType.TABLE,
        data={
            "markdown": "| content |  |  |\n| --- | --- | --- |",
            "rows": [["content", None, None]],
        },
    )

    assert PageRoutingPolicy().plan(_parsed_with_elements((table,))) == ()


def test_page_router_routes_unusable_native_table_with_reason() -> None:
    table = _element(
        "broken-table",
        RagElementType.TABLE,
        data={"markdown": "", "rows": [["name", None], [None]]},
    )

    decisions = PageRoutingPolicy().plan(_parsed_with_elements((table,)))

    assert decisions[0].reasons == (PageRoutingReason.COMPLEX_TABLE,)


def test_page_router_ignores_full_page_background_with_usable_text() -> None:
    background = _element(
        "background",
        RagElementType.IMAGE,
        bbox=(0, 0, 500, 700),
    )

    assert PageRoutingPolicy().plan(_parsed_with_elements((background,))) == ()


def test_page_router_routes_unique_semantic_image_with_reason() -> None:
    diagram = _element(
        "architecture-diagram",
        RagElementType.IMAGE,
        bbox=(50, 100, 450, 400),
    )

    decisions = PageRoutingPolicy().plan(_parsed_with_elements((diagram,)))

    assert decisions[0].reasons == (PageRoutingReason.COMPLEX_IMAGE,)
    assert decisions[0].regions[0].bounding_box == diagram.bounding_box


def test_page_router_uses_full_page_when_region_count_exceeds_limit() -> None:
    images = tuple(
        _element(
            f"diagram-{index}",
            RagElementType.IMAGE,
            bbox=(10 + index * 70, 100, 130 + index * 70, 260),
        )
        for index in range(5)
    )

    decisions = PageRoutingPolicy(max_regions_per_page=4).plan(
        _parsed_with_elements(images)
    )

    assert decisions[0].reasons == (PageRoutingReason.COMPLEX_IMAGE,)
    assert decisions[0].regions == ()


def test_page_router_ignores_repeated_decorative_image() -> None:
    images = tuple(
        _element("same-logo", RagElementType.IMAGE, page=page)
        for page in range(1, 4)
    )

    assert PageRoutingPolicy().plan(
        _parsed_with_elements(images, page_count=3)
    ) == ()


def test_native_ocr_metrics_do_not_depend_on_table_text_deduplication() -> None:
    class TableCoveringParser(PyMuPdfNativeParser):
        def _extract_tables(self, page, page_number, page_width, page_height):
            return [
                _element(
                    "native-table",
                    RagElementType.TABLE,
                    page=page_number,
                    bbox=(0, 0, page_width, page_height),
                    data={
                        "markdown": "| value |\n| --- |\n| native text |",
                        "rows": [["value"], ["native text"]],
                    },
                )
            ]

    parsed = TableCoveringParser().parse(_digital_pdf())

    assert parsed.blocks
    assert parsed.pages_requiring_ocr == ()
    assert parsed.page_metrics[0].native_char_count >= 40


def test_multimodal_chunk_router_uses_content_specific_strategies() -> None:
    table = "| name | value |\n| --- | --- |\n" + "\n".join(
        f"| row-{index} | {'data' * 20} |" for index in range(12)
    )
    nodes = (
        _node("heading", DocumentNodeType.HEADING, order=0, text="Architecture"),
        _node("paragraph", DocumentNodeType.PARAGRAPH, order=1, text="Native-first parsing."),
        _node("table", DocumentNodeType.TABLE, order=2, text=table),
        _node("formula", DocumentNodeType.FORMULA, order=3, text="E = mc^2"),
        _node("chart", DocumentNodeType.CHART, order=4, text="Revenue rises from 10 to 20."),
    )
    document = PreprocessedDocument(
        page_count=1,
        nodes=nodes,
        native_parser_version="pymupdf-native-v1",
        preprocessing_policy_version="test-v1",
        structure_provider="paddleocr-vl-local",
        structure_provider_version="v1.6",
        pages_processed_by_structure_model=(1,),
    )
    router = MultimodalChunkRouter(
        ChunkPolicy(target_tokens=50, max_tokens=70, min_tokens=10, semantic_overlap_tokens=8)
    )

    chunks = router.chunk(document)

    modalities = {chunk.modality for chunk in chunks}
    assert {ChunkModality.TEXT, ChunkModality.TABLE, ChunkModality.FORMULA, ChunkModality.CHART} <= modalities
    table_chunks = [chunk for chunk in chunks if chunk.modality is ChunkModality.TABLE]
    assert len(table_chunks) > 1
    assert all(chunk.content.startswith("| name | value |") for chunk in table_chunks)
    assert all(chunk.element_node_ids for chunk in table_chunks)
