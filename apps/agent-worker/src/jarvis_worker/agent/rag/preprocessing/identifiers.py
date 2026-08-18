"""预处理节点的确定性标识。"""

from __future__ import annotations

import hashlib
import json

from jarvis_worker.agent.rag.preprocessing.contracts import (
    DocumentNodeType,
    NodeExtractionMethod,
)


def build_document_node_id(
    *,
    page_number: int,
    order_index: int,
    node_type: DocumentNodeType,
    bounding_box: tuple[float, float, float, float],
    extraction_method: NodeExtractionMethod,
    extraction_version: str,
    content_hash: str,
) -> str:
    payload = {
        "bbox": [round(value, 4) for value in bounding_box],
        "content_hash": content_hash,
        "extraction_method": extraction_method.value,
        "extraction_version": extraction_version,
        "node_type": node_type.value,
        "order_index": order_index,
        "page_number": page_number,
    }
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()
