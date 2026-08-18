"""RAG 多模态对象的确定性标识。"""

from __future__ import annotations

import hashlib
import json
from uuid import UUID, uuid5

from jarvis_worker.agent.rag.contracts import (
    RagAssetKind,
    RagChunkElementRelation,
    RagElementType,
)


def build_element_locator_key(
    *,
    page_number: int,
    element_type: RagElementType,
    bounding_box: tuple[float, float, float, float],
    extraction_version: str,
) -> str:
    """为同一解析版本中的页面元素生成稳定位置指纹。"""

    payload = {
        "bounding_box": [round(value, 4) for value in bounding_box],
        "element_type": element_type.value,
        "extraction_version": extraction_version,
        "page_number": page_number,
    }
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def deterministic_element_id(document_id: UUID, locator_key: str) -> UUID:
    return uuid5(document_id, f"rag-element:{locator_key}")


def deterministic_asset_id(
    element_id: UUID, *, asset_kind: RagAssetKind, content_hash: str
) -> UUID:
    return uuid5(element_id, f"rag-asset:{asset_kind.value}:{content_hash}")


def deterministic_chunk_element_link_id(
    chunk_id: UUID,
    *,
    element_id: UUID,
    relation_type: RagChunkElementRelation,
) -> UUID:
    return uuid5(
        chunk_id,
        f"rag-chunk-element:{element_id}:{relation_type.value}",
    )
