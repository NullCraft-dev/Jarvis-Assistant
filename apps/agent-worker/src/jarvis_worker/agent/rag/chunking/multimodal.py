"""按预处理节点类型路由的多模态分片。"""

from __future__ import annotations

import hashlib
from dataclasses import replace

from jarvis_worker.agent.rag.chunking.contracts import (
    ChunkDraft,
    ChunkModality,
    ChunkPolicy,
)
from jarvis_worker.agent.rag.chunking.deterministic import (
    DeterministicBlockChunker,
    estimate_tokens,
)
from jarvis_worker.agent.rag.ingestion.contracts import DocumentBlock, PdfBlockType
from jarvis_worker.agent.rag.ingestion.sanitization import remove_nul
from jarvis_worker.agent.rag.preprocessing.contracts import (
    DocumentNode,
    DocumentNodeType,
    PreprocessedDocument,
)


_TEXT_TYPES = {
    DocumentNodeType.HEADING,
    DocumentNodeType.PARAGRAPH,
    DocumentNodeType.LIST,
    DocumentNodeType.CAPTION,
}
_ELEMENT_TYPES = {
    DocumentNodeType.TABLE,
    DocumentNodeType.IMAGE,
    DocumentNodeType.CHART,
    DocumentNodeType.FORMULA,
}


class MultimodalChunkRouter:
    def __init__(self, policy: ChunkPolicy | None = None) -> None:
        self._policy = policy or ChunkPolicy()
        self._text_chunker = DeterministicBlockChunker(self._policy)

    @property
    def version(self) -> str:
        return f"multimodal-router-v1+{self._text_chunker.version}"

    def chunk(self, document: PreprocessedDocument) -> tuple[ChunkDraft, ...]:
        ordered = sorted(
            document.nodes,
            key=lambda node: (node.page_number, node.order_index, node.bounding_box[1]),
        )
        text_nodes = [
            node
            for node in ordered
            if node.node_type in _TEXT_TYPES and remove_nul(node.text).strip()
        ]
        drafts = list(self._chunk_text_nodes(text_nodes))
        for node in ordered:
            if node.node_type is DocumentNodeType.CODE and remove_nul(node.text).strip():
                drafts.extend(self._chunk_special(node, ChunkModality.CODE))
            elif node.node_type is DocumentNodeType.TABLE and remove_nul(node.text).strip():
                drafts.extend(self._chunk_table(node))
            elif node.node_type is DocumentNodeType.FORMULA and remove_nul(node.text).strip():
                drafts.extend(self._chunk_special(node, ChunkModality.FORMULA))
            elif node.node_type is DocumentNodeType.CHART and remove_nul(node.text).strip():
                drafts.extend(self._chunk_special(node, ChunkModality.CHART))
            elif node.node_type is DocumentNodeType.IMAGE and remove_nul(node.text).strip():
                drafts.extend(self._chunk_special(node, ChunkModality.IMAGE))

        drafts.sort(key=lambda draft: (draft.page_start, draft.block_start, draft.modality.value))
        return tuple(replace(draft, ordinal=index) for index, draft in enumerate(drafts))

    def _chunk_text_nodes(self, nodes: list[DocumentNode]) -> tuple[ChunkDraft, ...]:
        blocks = tuple(_document_block(node, index) for index, node in enumerate(nodes))
        base = self._text_chunker.chunk(blocks)
        return tuple(
            replace(
                draft,
                block_start=min(
                    node.order_index
                    for node in nodes[draft.block_start : draft.block_end + 1]
                ),
                block_end=max(
                    node.order_index
                    for node in nodes[draft.block_start : draft.block_end + 1]
                ),
                node_ids=tuple(
                    node.node_id for node in nodes[draft.block_start : draft.block_end + 1]
                ),
                element_node_ids=tuple(
                    node.node_id
                    for node in nodes[draft.block_start : draft.block_end + 1]
                    if node.node_type in _ELEMENT_TYPES
                ),
            )
            for draft in base
        )

    def _chunk_table(self, node: DocumentNode) -> list[ChunkDraft]:
        text = remove_nul(node.text)
        lines = [line.strip() for line in text.splitlines() if line.strip()]
        if estimate_tokens(text) <= self._policy.max_tokens or len(lines) < 3:
            return self._chunk_special(node, ChunkModality.TABLE)
        header = lines[:2] if "|" in lines[0] else lines[:1]
        rows = lines[len(header) :]
        parts: list[str] = []
        current = list(header)
        for row in rows:
            candidate = "\n".join([*current, row])
            if len(current) > len(header) and estimate_tokens(candidate) > self._policy.max_tokens:
                parts.append("\n".join(current))
                current = [*header, row]
            else:
                current.append(row)
        if len(current) > len(header):
            parts.append("\n".join(current))
        return [self._draft_for_node(node, part, ChunkModality.TABLE) for part in parts]

    def _chunk_special(
        self, node: DocumentNode, modality: ChunkModality
    ) -> list[ChunkDraft]:
        block = _document_block(node, 0)
        return [
            replace(
                draft,
                block_start=node.order_index,
                block_end=node.order_index,
                modality=modality,
                node_ids=(node.node_id,),
                element_node_ids=(node.node_id,) if node.node_type in _ELEMENT_TYPES else (),
            )
            for draft in self._text_chunker.chunk((block,))
        ]

    @staticmethod
    def _draft_for_node(
        node: DocumentNode, content: str, modality: ChunkModality
    ) -> ChunkDraft:
        content = remove_nul(content)
        return ChunkDraft(
            ordinal=0,
            content=content,
            token_count=estimate_tokens(content),
            content_hash=hashlib.sha256(content.encode("utf-8")).hexdigest(),
            page_start=node.page_number,
            page_end=node.page_number,
            block_start=node.order_index,
            block_end=node.order_index,
            heading_path=(),
            modality=modality,
            node_ids=(node.node_id,),
            element_node_ids=(node.node_id,),
        )


def _document_block(node: DocumentNode, order_index: int) -> DocumentBlock:
    block_type = {
        DocumentNodeType.HEADING: PdfBlockType.HEADING,
        DocumentNodeType.PARAGRAPH: PdfBlockType.PARAGRAPH,
        DocumentNodeType.LIST: PdfBlockType.LIST,
        DocumentNodeType.CAPTION: PdfBlockType.PARAGRAPH,
        DocumentNodeType.CODE: PdfBlockType.CODE,
        DocumentNodeType.TABLE: PdfBlockType.TABLE_PROXY,
        DocumentNodeType.IMAGE: PdfBlockType.PARAGRAPH,
        DocumentNodeType.CHART: PdfBlockType.PARAGRAPH,
        DocumentNodeType.FORMULA: PdfBlockType.PARAGRAPH,
    }[node.node_type]
    heading_level = node.structured_data.get("heading_level")
    return DocumentBlock(
        page_number=node.page_number,
        order_index=order_index,
        block_type=block_type,
        text=remove_nul(node.text),
        bounding_box=node.bounding_box,
        page_width=node.page_width,
        page_height=node.page_height,
        heading_level=heading_level if isinstance(heading_level, int) else None,
    )
