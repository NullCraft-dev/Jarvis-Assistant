"""版本化、本地且不承载业务真相的视觉解析缓存。"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import os
import re
import tempfile
from pathlib import Path

from jarvis_worker.agent.rag.preprocessing.contracts import (
    DocumentNode,
    DocumentNodeType,
    NodeExtractionMethod,
    StructurePageResult,
)


_KEY = re.compile(r"^[0-9a-f]{64}$")
_CACHE_SCHEMA_VERSION = "structure-result-v1"


class LocalStructureResultCache:
    """按内容和处理版本寻址；损坏或旧缓存一律按 miss 处理。"""

    def __init__(self, root: str | Path, *, max_entry_bytes: int = 16 * 1024 * 1024):
        if max_entry_bytes < 1:
            raise ValueError("RAG structure cache entry 上限必须大于 0")
        self._root = Path(root).expanduser().resolve()
        self._root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self._max_entry_bytes = max_entry_bytes

    async def get(self, cache_key: str) -> StructurePageResult | None:
        return await asyncio.to_thread(self._get_sync, cache_key)

    async def put(self, cache_key: str, result: StructurePageResult) -> None:
        await asyncio.to_thread(self._put_sync, cache_key, result)

    def _get_sync(self, cache_key: str) -> StructurePageResult | None:
        target = self._target(cache_key)
        try:
            if target.stat().st_size > self._max_entry_bytes:
                return None
            payload = json.loads(target.read_text(encoding="utf-8"))
            return _decode_result(payload)
        except (FileNotFoundError, OSError, UnicodeError, ValueError, TypeError, json.JSONDecodeError):
            return None

    def _put_sync(self, cache_key: str, result: StructurePageResult) -> None:
        target = self._target(cache_key)
        encoded = json.dumps(
            _encode_result(result), ensure_ascii=False, separators=(",", ":")
        ).encode("utf-8")
        if len(encoded) > self._max_entry_bytes:
            return
        target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd, temporary = tempfile.mkstemp(prefix=f".{cache_key}.", dir=target.parent)
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "wb") as handle:
                handle.write(encoded)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, target)
        except BaseException:
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass
            raise

    def _target(self, cache_key: str) -> Path:
        if not _KEY.fullmatch(cache_key):
            raise ValueError("RAG structure cache key 必须是 SHA-256")
        return self._root / cache_key[:2] / f"{cache_key}.json"


def build_structure_cache_key(
    *,
    document_hash: str,
    page_number: int,
    bounding_box: tuple[float, float, float, float] | None,
    render_dpi: int,
    policy_version: str,
    provider_name: str,
    provider_version: str,
) -> str:
    material = json.dumps(
        {
            "schema": _CACHE_SCHEMA_VERSION,
            "document_hash": document_hash,
            "page_number": page_number,
            "bounding_box": bounding_box,
            "render_dpi": render_dpi,
            "policy_version": policy_version,
            "provider_name": provider_name,
            "provider_version": provider_version,
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def _encode_result(result: StructurePageResult) -> dict:
    return {
        "schema_version": _CACHE_SCHEMA_VERSION,
        "page_number": result.page_number,
        "provider": result.provider,
        "provider_version": result.provider_version,
        "nodes": [
            {
                "node_id": node.node_id,
                "node_type": node.node_type.value,
                "page_number": node.page_number,
                "order_index": node.order_index,
                "bounding_box": list(node.bounding_box),
                "page_width": node.page_width,
                "page_height": node.page_height,
                "extraction_method": node.extraction_method.value,
                "extraction_version": node.extraction_version,
                "confidence": node.confidence,
                "text": node.text,
                "structured_data": node.structured_data,
                "asset_bytes": (
                    base64.b64encode(node.asset_bytes).decode("ascii")
                    if node.asset_bytes is not None
                    else None
                ),
                "asset_mime_type": node.asset_mime_type,
                "parent_node_id": node.parent_node_id,
                "related_node_ids": list(node.related_node_ids),
            }
            for node in result.nodes
        ],
    }


def _decode_result(payload: object) -> StructurePageResult:
    if not isinstance(payload, dict) or payload.get("schema_version") != _CACHE_SCHEMA_VERSION:
        raise ValueError("RAG structure cache schema 不兼容")
    raw_nodes = payload.get("nodes")
    if not isinstance(raw_nodes, list):
        raise ValueError("RAG structure cache nodes 无效")
    nodes: list[DocumentNode] = []
    for raw in raw_nodes:
        if not isinstance(raw, dict):
            raise ValueError("RAG structure cache node 无效")
        encoded_asset = raw.get("asset_bytes")
        asset_bytes = (
            base64.b64decode(encoded_asset, validate=True)
            if isinstance(encoded_asset, str)
            else None
        )
        nodes.append(
            DocumentNode(
                node_id=str(raw["node_id"]),
                node_type=DocumentNodeType(str(raw["node_type"])),
                page_number=int(raw["page_number"]),
                order_index=int(raw["order_index"]),
                bounding_box=tuple(float(value) for value in raw["bounding_box"]),
                page_width=float(raw["page_width"]),
                page_height=float(raw["page_height"]),
                extraction_method=NodeExtractionMethod(str(raw["extraction_method"])),
                extraction_version=str(raw["extraction_version"]),
                confidence=float(raw["confidence"]),
                text=str(raw.get("text") or ""),
                structured_data=dict(raw.get("structured_data") or {}),
                asset_bytes=asset_bytes,
                asset_mime_type=(
                    str(raw["asset_mime_type"])
                    if raw.get("asset_mime_type") is not None
                    else None
                ),
                parent_node_id=(
                    str(raw["parent_node_id"])
                    if raw.get("parent_node_id") is not None
                    else None
                ),
                related_node_ids=tuple(str(value) for value in raw.get("related_node_ids") or []),
            )
        )
    return StructurePageResult(
        page_number=int(payload["page_number"]),
        nodes=tuple(nodes),
        provider=str(payload["provider"]),
        provider_version=str(payload["provider_version"]),
    )
