#!/usr/bin/env python3
"""运行 Jarvis PyMuPDF → PaddleOCR-VL → 多模态分片真实闭环。"""

from __future__ import annotations

import argparse
import asyncio
import json
import resource
import sys
import time
from pathlib import Path

import pymupdf


WORKER_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(WORKER_ROOT / "src"))

from jarvis_worker.agent.rag.chunking import MultimodalChunkRouter  # noqa: E402
from jarvis_worker.agent.rag.preprocessing import (  # noqa: E402
    MultimodalDocumentPreprocessor,
)
from jarvis_worker.agent.rag.preprocessing.providers import (  # noqa: E402
    PaddleOcrVlConfig,
    PaddleOcrVlProvider,
)


def _input_pdf(path: Path) -> bytes:
    if path.suffix.casefold() == ".pdf":
        return path.read_bytes()
    pixmap = pymupdf.Pixmap(str(path))
    document = pymupdf.open()
    try:
        page = document.new_page(width=pixmap.width, height=pixmap.height)
        page.insert_image(page.rect, filename=str(path))
        return document.tobytes()
    finally:
        document.close()


async def _run(input_path: Path) -> dict:
    provider = PaddleOcrVlProvider(
        PaddleOcrVlConfig(max_pixels=2_000_000, max_new_tokens=2048)
    )
    preprocessor = MultimodalDocumentPreprocessor(
        structure_provider=provider,
        render_dpi=108,
    )
    started = time.monotonic()
    document = await preprocessor.preprocess_pdf(_input_pdf(input_path))
    elapsed = time.monotonic() - started
    chunks = MultimodalChunkRouter().chunk(document)
    return {
        "input": input_path.name,
        "elapsed_seconds": round(elapsed, 3),
        "page_count": document.page_count,
        "structure_pages": list(document.pages_processed_by_structure_model),
        "structure_provider": document.structure_provider,
        "node_count": len(document.nodes),
        "chunk_count": len(chunks),
        "max_rss_mb": round(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024 / 1024, 1),
        "nodes": [
            {
                "id": node.node_id,
                "type": node.node_type.value,
                "method": node.extraction_method.value,
                "page": node.page_number,
                "order": node.order_index,
                "bbox": list(node.bounding_box),
                "has_asset": node.asset_bytes is not None,
                "text": node.text[:500],
            }
            for node in document.nodes
        ],
        "chunks": [
            {
                "ordinal": chunk.ordinal,
                "modality": chunk.modality.value,
                "tokens": chunk.token_count,
                "node_ids": list(chunk.node_ids),
                "element_node_ids": list(chunk.element_node_ids),
                "content": chunk.content[:1000],
            }
            for chunk in chunks
        ],
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("input", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if not args.input.is_file():
        raise SystemExit("input file does not exist")
    summary = asyncio.run(_run(args.input.resolve()))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
