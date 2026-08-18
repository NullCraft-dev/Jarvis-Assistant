"""将 PyMuPDF 的格式特定结果转换为统一多模态中间结构。"""

from __future__ import annotations

import hashlib

from jarvis_worker.agent.rag.contracts import RagElementType
from jarvis_worker.agent.rag.ingestion.contracts import (
    DocumentBlock,
    ExtractedElement,
    ParsedPdfDocument,
    PdfBlockType,
)
from jarvis_worker.agent.rag.preprocessing.contracts import (
    DocumentNode,
    DocumentNodeType,
    NodeExtractionMethod,
)
from jarvis_worker.agent.rag.preprocessing.identifiers import build_document_node_id


def native_nodes(parsed: ParsedPdfDocument) -> tuple[DocumentNode, ...]:
    nodes = [
        node
        for block in parsed.blocks
        if block.block_type is not PdfBlockType.TABLE_PROXY
        for node in [_block_node(block, parsed.parser_version)]
    ]
    nodes.extend(_element_node(element, parsed.parser_version) for element in parsed.elements)
    return tuple(
        sorted(
            nodes,
            key=lambda node: (
                node.page_number,
                node.bounding_box[1],
                node.bounding_box[0],
                node.order_index,
            ),
        )
    )


def _block_node(block: DocumentBlock, version: str) -> DocumentNode:
    node_type = {
        PdfBlockType.HEADING: DocumentNodeType.HEADING,
        PdfBlockType.PARAGRAPH: DocumentNodeType.PARAGRAPH,
        PdfBlockType.LIST: DocumentNodeType.LIST,
        PdfBlockType.CODE: DocumentNodeType.CODE,
        PdfBlockType.TABLE_PROXY: DocumentNodeType.TABLE,
    }[block.block_type]
    content_hash = hashlib.sha256(block.text.encode("utf-8")).hexdigest()
    return DocumentNode(
        node_id=build_document_node_id(
            page_number=block.page_number,
            order_index=block.order_index,
            node_type=node_type,
            bounding_box=block.bounding_box,
            extraction_method=NodeExtractionMethod.NATIVE,
            extraction_version=version,
            content_hash=content_hash,
        ),
        node_type=node_type,
        page_number=block.page_number,
        order_index=block.order_index,
        bounding_box=block.bounding_box,
        page_width=block.page_width,
        page_height=block.page_height,
        text=block.text,
        structured_data={"heading_level": block.heading_level},
        extraction_method=NodeExtractionMethod.NATIVE,
        extraction_version=version,
        confidence=1.0,
    )


def _element_node(element: ExtractedElement, version: str) -> DocumentNode:
    node_type = {
        RagElementType.TABLE: DocumentNodeType.TABLE,
        RagElementType.CHART: DocumentNodeType.CHART,
        RagElementType.EQUATION: DocumentNodeType.FORMULA,
        RagElementType.IMAGE: DocumentNodeType.IMAGE,
        RagElementType.FIGURE: DocumentNodeType.IMAGE,
        RagElementType.DIAGRAM: DocumentNodeType.IMAGE,
    }[element.element_type]
    text = element.caption_text.strip()
    if node_type is DocumentNodeType.TABLE:
        text = str(element.structured_data.get("markdown") or text).strip()
    return DocumentNode(
        node_id=build_document_node_id(
            page_number=element.page_number,
            order_index=element.order_index,
            node_type=node_type,
            bounding_box=element.bounding_box,
            extraction_method=NodeExtractionMethod.NATIVE,
            extraction_version=version,
            content_hash=element.content_hash,
        ),
        node_type=node_type,
        page_number=element.page_number,
        order_index=element.order_index,
        bounding_box=element.bounding_box,
        page_width=element.page_width,
        page_height=element.page_height,
        text=text,
        structured_data={
            **element.structured_data,
            "source_content_hash": element.content_hash,
        },
        asset_bytes=element.asset_bytes,
        asset_mime_type=element.asset_mime_type,
        extraction_method=NodeExtractionMethod.NATIVE,
        extraction_version=version,
        confidence=element.confidence,
    )
