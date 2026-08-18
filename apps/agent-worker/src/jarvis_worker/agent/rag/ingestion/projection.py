"""将统一预处理节点映射为 RAG 持久化投影。"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from uuid import uuid5

from jarvis_worker.agent.rag.chunking.contracts import ChunkDraft
from jarvis_worker.agent.rag.contracts import (
    RagAsset,
    RagAssetFileStore,
    RagAssetKind,
    RagChunk,
    RagChunkElementLink,
    RagChunkElementRelation,
    RagDocument,
    RagElement,
    RagElementType,
    RagExtractionMethod,
    RagIngestionJob,
)
from jarvis_worker.agent.rag.identifiers import (
    build_element_locator_key,
    deterministic_asset_id,
    deterministic_chunk_element_link_id,
    deterministic_element_id,
)
from jarvis_worker.agent.rag.ingestion.sanitization import (
    remove_nul,
    remove_nul_from_json,
)
from jarvis_worker.agent.rag.preprocessing import (
    DocumentNode,
    DocumentNodeType,
    NodeExtractionMethod,
    PreprocessedDocument,
)


_ELEMENT_TYPES = {
    DocumentNodeType.IMAGE: RagElementType.IMAGE,
    DocumentNodeType.CHART: RagElementType.CHART,
    DocumentNodeType.TABLE: RagElementType.TABLE,
    DocumentNodeType.FORMULA: RagElementType.EQUATION,
}


@dataclass(frozen=True, slots=True)
class RagProjection:
    chunks: tuple[RagChunk, ...]
    elements: tuple[RagElement, ...]
    assets: tuple[RagAsset, ...]
    links: tuple[RagChunkElementLink, ...]


def build_projection(
    *,
    document: RagDocument,
    job: RagIngestionJob,
    preprocessed: PreprocessedDocument,
    drafts: tuple[ChunkDraft, ...],
    asset_file_store: RagAssetFileStore,
    existing_asset_references: frozenset[str] = frozenset(),
) -> RagProjection:
    nodes = {node.node_id: node for node in preprocessed.nodes}
    element_by_node: dict[str, RagElement] = {}
    assets: list[RagAsset] = []
    try:
        for node in preprocessed.nodes:
            element_type = _ELEMENT_TYPES.get(node.node_type)
            if element_type is None:
                continue
            element = _element(document, node, element_type)
            element_by_node[node.node_id] = element
            if node.asset_bytes is not None and node.asset_mime_type is not None:
                digest = hashlib.sha256(node.asset_bytes).hexdigest()
                asset_id = deterministic_asset_id(
                    element.id, asset_kind=RagAssetKind.CROP, content_hash=digest
                )
                reference = asset_file_store.write(
                    asset_id=asset_id,
                    content=node.asset_bytes,
                    expected_hash=digest,
                    mime_type=node.asset_mime_type,
                )
                assets.append(
                    RagAsset(
                        id=asset_id,
                        document_id=document.id,
                        element_id=element.id,
                        workspace_id=document.workspace_id,
                        asset_kind=RagAssetKind.CROP,
                        storage_reference=reference,
                        mime_type=node.asset_mime_type,
                        content_hash=digest,
                        size_bytes=len(node.asset_bytes),
                    )
                )
    except BaseException:
        for asset in assets:
            if asset.storage_reference not in existing_asset_references:
                try:
                    asset_file_store.delete(asset)
                except (OSError, ValueError):
                    pass
        raise
    chunks = tuple(_chunk(document, job, draft) for draft in drafts)
    links: list[RagChunkElementLink] = []
    for chunk, draft in zip(chunks, drafts, strict=True):
        for order, node_id in enumerate(draft.element_node_ids):
            element = element_by_node.get(node_id)
            node = nodes.get(node_id)
            if element is None or node is None:
                continue
            links.append(
                RagChunkElementLink(
                    id=deterministic_chunk_element_link_id(
                        chunk.id,
                        element_id=element.id,
                        relation_type=RagChunkElementRelation.CONTAINS,
                    ),
                    document_id=document.id,
                    workspace_id=document.workspace_id,
                    chunk_id=chunk.id,
                    element_id=element.id,
                    relation_type=RagChunkElementRelation.CONTAINS,
                    confidence=node.confidence,
                    order_index=order,
                )
            )
    return RagProjection(
        chunks=chunks,
        elements=tuple(element_by_node.values()),
        assets=tuple(assets),
        links=tuple(links),
    )


def _element(
    document: RagDocument, node: DocumentNode, element_type: RagElementType
) -> RagElement:
    locator = build_element_locator_key(
        page_number=node.page_number,
        element_type=element_type,
        bounding_box=node.bounding_box,
        extraction_version=node.extraction_version,
    )
    text = remove_nul(node.text)
    structured_data = remove_nul_from_json(node.structured_data)
    canonical = json.dumps(
        structured_data, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    asset_hash = hashlib.sha256(node.asset_bytes).hexdigest() if node.asset_bytes else ""
    content_hash = hashlib.sha256(
        f"{text}\n{canonical}\n{asset_hash}".encode("utf-8")
    ).hexdigest()
    method = {
        NodeExtractionMethod.NATIVE: RagExtractionMethod.NATIVE,
        NodeExtractionMethod.PADDLEOCR_VL: RagExtractionMethod.VISION,
        NodeExtractionMethod.HYBRID: RagExtractionMethod.HYBRID,
    }[node.extraction_method]
    return RagElement(
        id=deterministic_element_id(document.id, locator),
        document_id=document.id,
        workspace_id=document.workspace_id,
        element_type=element_type,
        page_number=node.page_number,
        bounding_box=node.bounding_box,
        page_width=node.page_width,
        page_height=node.page_height,
        locator_key=locator,
        content_hash=content_hash,
        extraction_method=method,
        extraction_version=node.extraction_version,
        confidence=node.confidence,
        ocr_text=text,
        structured_data=dict(structured_data),
    )


def _chunk(document: RagDocument, job: RagIngestionJob, draft: ChunkDraft) -> RagChunk:
    content = remove_nul(draft.content)
    content_hash = hashlib.sha256(content.encode("utf-8")).hexdigest()
    chunk_id = uuid5(job.id, f"rag-chunk:{draft.ordinal}:{content_hash}")
    return RagChunk(
        id=chunk_id,
        document_id=document.id,
        ingestion_job_id=job.id,
        workspace_id=document.workspace_id,
        ordinal=draft.ordinal,
        content=content,
        content_hash=content_hash,
        token_count=draft.token_count,
        source_locator={
            "page_start": draft.page_start,
            "page_end": draft.page_end,
            "block_start": draft.block_start,
            "block_end": draft.block_end,
            "heading_path": [remove_nul(value) for value in draft.heading_path],
            "overlap_tokens": draft.overlap_tokens,
            "modality": draft.modality.value,
            "node_ids": list(draft.node_ids),
        },
    )
