"""RAG 分层组件：PDF 原生解析、结构分片与百度 OCR adapter。"""

from __future__ import annotations

import httpx
import pymupdf
import pytest

from jarvis_worker.agent.rag.chunking import (
    ChunkPolicy,
    DeterministicBlockChunker,
)
from jarvis_worker.agent.rag.ingestion import (
    DocumentBlock,
    PdfBlockType,
    PyMuPdfNativeParser,
)
from jarvis_worker.agent.rag.ocr import BaiduOcrError, BaiduOcrProvider


def _block(
    text: str,
    *,
    index: int,
    block_type: PdfBlockType = PdfBlockType.PARAGRAPH,
    heading_level: int | None = None,
) -> DocumentBlock:
    return DocumentBlock(
        page_number=1,
        order_index=index,
        block_type=block_type,
        text=text,
        bounding_box=(20.0, 20.0 + index * 30, 500.0, 45.0 + index * 30),
        page_width=595.0,
        page_height=842.0,
        heading_level=heading_level,
    )


def _digital_pdf() -> bytes:
    document = pymupdf.open()
    page = document.new_page()
    page.insert_text((72, 90), "RAG Architecture", fontsize=20)
    page.insert_text(
        (72, 140),
        "Native extraction keeps searchable text local and preserves page coordinates.",
        fontsize=11,
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


def test_native_pdf_parser_extracts_text_without_requesting_ocr() -> None:
    parsed = PyMuPdfNativeParser().parse(_digital_pdf())

    assert parsed.page_count == 1
    assert parsed.native_char_count > 40
    assert "RAG Architecture" in " ".join(block.text for block in parsed.blocks)
    assert parsed.pages_requiring_ocr == ()


def test_native_pdf_parser_marks_scanned_page_and_keeps_image_separate() -> None:
    parsed = PyMuPdfNativeParser().parse(_scanned_pdf())

    assert parsed.pages_requiring_ocr == (1,)
    assert parsed.blocks == ()
    assert len(parsed.elements) == 1
    assert parsed.elements[0].asset_bytes is not None


def test_chunker_preserves_structure_without_overlap_for_normal_blocks() -> None:
    blocks = (
        _block("Memory Design", index=0, block_type=PdfBlockType.HEADING, heading_level=1),
        _block("A" * 180, index=1),
        _block("Context Management", index=2, block_type=PdfBlockType.HEADING, heading_level=1),
        _block("B" * 180, index=3),
    )
    chunker = DeterministicBlockChunker(
        ChunkPolicy(target_tokens=50, max_tokens=70, min_tokens=20, semantic_overlap_tokens=8)
    )

    first = chunker.chunk(blocks)
    second = chunker.chunk(blocks)

    assert first == second
    assert len(first) >= 2
    assert all(chunk.overlap_tokens == 0 for chunk in first)
    assert first[0].heading_path == ("Memory Design",)
    assert first[-1].heading_path == ("Context Management",)


def test_chunker_uses_bounded_overlap_only_for_oversized_semantic_block() -> None:
    block = _block("这是一个需要保持切分边界上下文的超长段落。" * 80, index=0)
    chunker = DeterministicBlockChunker(
        ChunkPolicy(target_tokens=60, max_tokens=80, min_tokens=20, semantic_overlap_tokens=10)
    )

    chunks = chunker.chunk((block,))

    assert len(chunks) > 1
    assert chunks[0].overlap_tokens == 0
    assert all(0 < chunk.overlap_tokens <= 10 for chunk in chunks[1:])
    assert all(chunk.token_count <= 80 for chunk in chunks)


def test_chunker_removes_postgres_incompatible_nul_bytes() -> None:
    blocks = (
        _block("Batch\x00 Normalization", index=0, block_type=PdfBlockType.HEADING),
        _block("A searchable\x00 paragraph.", index=1),
    )

    chunks = DeterministicBlockChunker().chunk(blocks)

    assert chunks
    assert all("\x00" not in chunk.content for chunk in chunks)
    assert "Batch Normalization" in chunks[0].content


@pytest.mark.asyncio
async def test_baidu_ocr_adapter_parses_position_and_reuses_token() -> None:
    calls = {"token": 0, "ocr": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/oauth/2.0/token":
            calls["token"] += 1
            return httpx.Response(200, json={"access_token": "token-value", "expires_in": 3600})
        calls["ocr"] += 1
        return httpx.Response(
            200,
            json={
                "words_result": [
                    {
                        "words": "向量检索",
                        "location": {"left": 10, "top": 20, "width": 80, "height": 24},
                        "probability": {"average": 0.97},
                    }
                ]
            },
        )

    transport = httpx.MockTransport(handler)
    provider = BaiduOcrProvider(
        api_key="api-key",
        secret_key="secret-key",
        client_factory=lambda: httpx.AsyncClient(transport=transport),
    )

    first = await provider.recognize(image=b"png", mime_type="image/png", languages=("zh",))
    second = await provider.recognize(image=b"png", mime_type="image/png", languages=("zh",))

    assert first == second
    assert first.text == "向量检索"
    assert first.spans[0].bounding_box == (10.0, 20.0, 90.0, 44.0)
    assert calls == {"token": 1, "ocr": 2}


@pytest.mark.asyncio
async def test_baidu_ocr_adapter_does_not_expose_provider_error_body() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/oauth/2.0/token":
            return httpx.Response(200, json={"access_token": "token-value"})
        return httpx.Response(400, json={"error_code": 17, "error_msg": "secret-detail"})

    provider = BaiduOcrProvider(
        api_key="api-key",
        secret_key="secret-key",
        client_factory=lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )

    with pytest.raises(BaiduOcrError, match="百度 OCR 识别失败") as exc_info:
        await provider.recognize(image=b"png", mime_type="image/png", languages=("zh",))
    assert "secret-detail" not in str(exc_info.value)
