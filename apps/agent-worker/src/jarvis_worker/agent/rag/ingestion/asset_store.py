"""RAG 多模态元素的本地受控二进制存储。"""

from __future__ import annotations

import hashlib
import os
import tempfile
from pathlib import Path
from uuid import UUID

from jarvis_worker.agent.rag.contracts import RagAsset
from jarvis_worker.shared.storage_capacity import (
    StorageCapacityExceeded,
    capacity_lock,
    directory_size_bytes,
    ensure_capacity,
)

_SUFFIXES = {
    "image/jpeg": ".jpg",
    "image/png": ".png",
}


class LocalRagAssetFileStore:
    def __init__(
        self,
        root: str | Path,
        *,
        max_bytes: int = 16 * 1024 * 1024,
        max_total_bytes: int = 20 * 1024 * 1024 * 1024,
    ):
        if (
            isinstance(max_bytes, bool)
            or isinstance(max_total_bytes, bool)
            or max_bytes < 1
            or max_total_bytes < max_bytes
        ):
            raise ValueError(
                "RAG asset 容量必须满足 0 < object <= total"
            )
        self._root = Path(root).expanduser().resolve()
        self._root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self._max_bytes = max_bytes
        self._max_total_bytes = max_total_bytes

    def write(
        self,
        *,
        asset_id: UUID,
        content: bytes,
        expected_hash: str,
        mime_type: str | None = None,
    ) -> str:
        if not content or len(content) > self._max_bytes:
            if not content:
                raise ValueError("RAG asset 内容为空")
            raise StorageCapacityExceeded(
                "RAG_ASSET_OBJECT_CAPACITY_EXCEEDED",
                "RAG Asset 单对象",
                limit=self._max_bytes,
            )
        digest = hashlib.sha256(content).hexdigest()
        if digest != expected_hash:
            raise ValueError("RAG asset 内容哈希不一致")
        suffix = _SUFFIXES.get((mime_type or "").split(";", 1)[0].lower())
        if suffix is None:
            raise ValueError("RAG asset MIME 不受支持")
        relative = Path(str(asset_id)[:2]) / f"{asset_id}{suffix}"
        target = self._resolve(relative.as_posix())
        target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with capacity_lock(self._root):
            existing_bytes = (
                target.stat().st_size
                if target.exists() and target.is_file()
                else 0
            )
            ensure_capacity(
                current_bytes=directory_size_bytes(self._root),
                existing_bytes=existing_bytes,
                requested_bytes=len(content),
                limit_bytes=self._max_total_bytes,
                code="RAG_ASSET_TOTAL_CAPACITY_EXCEEDED",
                scope="RAG Asset 本地总量",
            )
            fd, temporary = tempfile.mkstemp(
                prefix=f".{asset_id}.", suffix=".tmp", dir=target.parent
            )
            try:
                os.fchmod(fd, 0o600)
                with os.fdopen(fd, "wb") as handle:
                    handle.write(content)
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(temporary, target)
            except BaseException:
                try:
                    os.unlink(temporary)
                except FileNotFoundError:
                    pass
                raise
        return relative.as_posix()

    def read(self, asset: RagAsset) -> bytes:
        target = self._resolve(asset.storage_reference)
        data = target.read_bytes()
        if len(data) != asset.size_bytes or len(data) > self._max_bytes:
            raise ValueError("RAG asset 文件大小不一致")
        if hashlib.sha256(data).hexdigest() != asset.content_hash:
            raise ValueError("RAG asset 文件哈希不一致")
        return data

    def delete(self, asset: RagAsset) -> None:
        self.delete_reference(asset.storage_reference)

    def delete_reference(self, storage_reference: str) -> None:
        try:
            self._resolve(storage_reference).unlink()
        except FileNotFoundError:
            return

    def _resolve(self, reference: str) -> Path:
        relative = Path(reference)
        if relative.is_absolute() or ".." in relative.parts or "\\" in reference:
            raise ValueError("RAG asset 文件引用非法")
        target = (self._root / relative).resolve()
        try:
            target.relative_to(self._root)
        except ValueError:
            raise ValueError("RAG asset 文件引用越界") from None
        return target
