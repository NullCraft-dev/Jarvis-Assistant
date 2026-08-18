"""受控的本地 Artifact 文件存储。

只接受 UUID owner 生成的文件名；调用方和 Web 均不能提交任意路径。
数据库保存相对引用、大小和 SHA-256，文件正文不进入 RuntimeEvent。
"""

from __future__ import annotations

import hashlib
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from uuid import UUID

from jarvis_worker.shared.storage_capacity import (
    StorageCapacityExceeded,
    capacity_lock,
    directory_size_bytes,
    ensure_capacity,
)

DEFAULT_ARTIFACT_MAX_RUN_BYTES = 250 * 1024 * 1024
DEFAULT_ARTIFACT_MAX_WORKSPACE_BYTES = 2 * 1024 * 1024 * 1024
DEFAULT_ARTIFACT_MAX_TOTAL_BYTES = 10 * 1024 * 1024 * 1024


@dataclass(frozen=True)
class StoredArtifactFile:
    relative_path: str
    size_bytes: int
    sha256: str
    mime_type: str


class LocalArtifactFileStore:
    def __init__(
        self,
        root: str | Path,
        *,
        max_bytes: int = 50 * 1024 * 1024,
        max_run_bytes: int = DEFAULT_ARTIFACT_MAX_RUN_BYTES,
        max_workspace_bytes: int = DEFAULT_ARTIFACT_MAX_WORKSPACE_BYTES,
        max_total_bytes: int = DEFAULT_ARTIFACT_MAX_TOTAL_BYTES,
    ):
        values = (max_bytes, max_run_bytes, max_workspace_bytes, max_total_bytes)
        if any(isinstance(value, bool) or value < 1 for value in values):
            raise ValueError("Artifact 容量配置必须是大于 0 的整数")
        if not max_bytes <= max_run_bytes <= max_workspace_bytes <= max_total_bytes:
            raise ValueError(
                "Artifact 容量必须满足 object <= run <= workspace <= total"
            )
        self._root = Path(root).expanduser().resolve()
        self._root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self._max_bytes = max_bytes
        self._max_run_bytes = max_run_bytes
        self._max_workspace_bytes = max_workspace_bytes
        self._max_total_bytes = max_total_bytes

    @property
    def root(self) -> Path:
        return self._root

    @property
    def max_bytes(self) -> int:
        return self._max_bytes

    @property
    def max_run_bytes(self) -> int:
        return self._max_run_bytes

    @property
    def max_workspace_bytes(self) -> int:
        return self._max_workspace_bytes

    @property
    def max_total_bytes(self) -> int:
        return self._max_total_bytes

    def write_text(
        self,
        artifact_id: UUID,
        content: str,
        *,
        run_id: UUID,
        workspace_id: UUID | None = None,
        workspace_path: str = "",
        suffix: str = ".txt",
        mime_type: str = "text/plain; charset=utf-8",
    ) -> StoredArtifactFile:
        if suffix not in {".txt", ".md", ".json", ".diff"}:
            raise ValueError("Artifact suffix 不受支持")
        return self._write_bytes(
            artifact_id,
            content.encode("utf-8"),
            run_id=run_id,
            workspace_id=workspace_id,
            workspace_path=workspace_path,
            suffix=suffix,
            mime_type=mime_type,
        )

    def write_bytes(
        self,
        artifact_id: UUID,
        content: bytes,
        *,
        run_id: UUID,
        workspace_id: UUID | None = None,
        workspace_path: str = "",
        suffix: str,
        mime_type: str,
    ) -> StoredArtifactFile:
        if suffix not in {".pdf"}:
            raise ValueError("二进制 Artifact suffix 不受支持")
        return self._write_bytes(
            artifact_id,
            content,
            run_id=run_id,
            workspace_id=workspace_id,
            workspace_path=workspace_path,
            suffix=suffix,
            mime_type=mime_type,
        )

    def _write_bytes(
        self,
        artifact_id: UUID,
        data: bytes,
        *,
        run_id: UUID,
        workspace_id: UUID | None,
        workspace_path: str,
        suffix: str,
        mime_type: str,
    ) -> StoredArtifactFile:
        if len(data) > self._max_bytes:
            raise StorageCapacityExceeded(
                "ARTIFACT_OBJECT_CAPACITY_EXCEEDED",
                "Artifact 单对象",
                limit=self._max_bytes,
            )

        workspace_bucket = self._workspace_bucket(
            workspace_id=workspace_id,
            workspace_path=workspace_path,
        )
        relative = (
            Path("scoped")
            / workspace_bucket
            / str(run_id)
            / str(artifact_id)[:2]
            / f"{artifact_id}{suffix}"
        )
        target = self._resolve_relative(relative.as_posix())
        target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        digest = hashlib.sha256(data).hexdigest()

        workspace_root = self._root / "scoped" / workspace_bucket
        run_root = workspace_root / str(run_id)
        with capacity_lock(self._root):
            existing_bytes = (
                target.stat().st_size
                if target.exists() and target.is_file()
                else 0
            )
            ensure_capacity(
                current_bytes=directory_size_bytes(run_root),
                existing_bytes=existing_bytes,
                requested_bytes=len(data),
                limit_bytes=self._max_run_bytes,
                code="ARTIFACT_RUN_CAPACITY_EXCEEDED",
                scope="Artifact 单 Run",
            )
            ensure_capacity(
                current_bytes=directory_size_bytes(workspace_root),
                existing_bytes=existing_bytes,
                requested_bytes=len(data),
                limit_bytes=self._max_workspace_bytes,
                code="ARTIFACT_WORKSPACE_CAPACITY_EXCEEDED",
                scope="Artifact 单 Workspace",
            )
            ensure_capacity(
                current_bytes=directory_size_bytes(self._root),
                existing_bytes=existing_bytes,
                requested_bytes=len(data),
                limit_bytes=self._max_total_bytes,
                code="ARTIFACT_TOTAL_CAPACITY_EXCEEDED",
                scope="Artifact 本地总量",
            )
            fd, temp_name = tempfile.mkstemp(
                prefix=f".{artifact_id}.", suffix=".tmp", dir=target.parent
            )
            try:
                os.fchmod(fd, 0o600)
                with os.fdopen(fd, "wb") as handle:
                    handle.write(data)
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(temp_name, target)
            except BaseException:
                try:
                    os.unlink(temp_name)
                except FileNotFoundError:
                    pass
                raise

        return StoredArtifactFile(
            relative_path=relative.as_posix(),
            size_bytes=len(data),
            sha256=digest,
            mime_type=mime_type,
        )

    def read_bytes(
        self, relative_path: str, *, expected_sha256: str | None = None
    ) -> bytes:
        target = self._resolve_relative(relative_path)
        stat = target.stat()
        if stat.st_size > self._max_bytes:
            raise ValueError("Artifact 文件超过读取上限")
        data = target.read_bytes()
        if expected_sha256:
            digest = hashlib.sha256(data).hexdigest()
            if digest != expected_sha256:
                raise ValueError("Artifact 文件哈希不一致")
        return data

    def read_text(
        self, relative_path: str, *, expected_sha256: str | None = None
    ) -> str:
        return self.read_bytes(
            relative_path, expected_sha256=expected_sha256
        ).decode("utf-8")

    def delete(self, relative_path: str) -> None:
        """删除一个已解析到受控根目录内的 Artifact；不存在时保持幂等。"""
        self._resolve_relative(relative_path).unlink(missing_ok=True)

    @staticmethod
    def _workspace_bucket(
        *,
        workspace_id: UUID | None,
        workspace_path: str,
    ) -> str:
        if workspace_id is not None:
            return f"id-{workspace_id}"
        normalized = str(Path(workspace_path).expanduser().resolve())
        if workspace_path:
            return f"path-{hashlib.sha256(normalized.encode()).hexdigest()[:32]}"
        return "unscoped"

    def _resolve_relative(self, relative_path: str) -> Path:
        relative = Path(relative_path)
        if relative.is_absolute() or ".." in relative.parts:
            raise ValueError("Artifact 文件引用非法")
        target = (self._root / relative).resolve()
        try:
            target.relative_to(self._root)
        except ValueError:
            raise ValueError("Artifact 文件引用越界") from None
        return target
