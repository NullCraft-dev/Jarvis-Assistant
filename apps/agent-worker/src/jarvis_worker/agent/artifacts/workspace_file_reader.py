"""受控读取由 workspace 工具创建并登记为 Artifact 的 UTF-8 文件。"""

from __future__ import annotations

import hashlib
import os
import stat
from dataclasses import dataclass
from pathlib import PurePosixPath


@dataclass(frozen=True)
class WorkspaceArtifactContent:
    content: str
    size_bytes: int
    sha256: str


class WorkspaceArtifactFileReader:
    """只接受 Storage 中的可信 workspace root 与规范相对路径。

    读取全程使用 dir-fd 和 ``O_NOFOLLOW``，不允许任意绝对路径、父级穿越或
    symlink 替换；大小和 SHA-256 必须与 Artifact 元数据一致。
    """

    def __init__(self, *, max_bytes: int = 1024 * 1024):
        if max_bytes < 1:
            raise ValueError("Workspace Artifact max_bytes 必须大于 0")
        self._max_bytes = max_bytes

    def read_text(
        self,
        workspace_root: str,
        relative_path: str,
        *,
        expected_size_bytes: int,
        expected_sha256: str,
    ) -> WorkspaceArtifactContent:
        if (
            not isinstance(workspace_root, str)
            or not workspace_root
            or not os.path.isabs(workspace_root)
        ):
            raise ValueError("Workspace root 无效")
        canonical_root = os.path.abspath(workspace_root)
        if os.path.realpath(workspace_root) != canonical_root:
            raise ValueError("Workspace root 不再是可信规范目录")
        if (
            not isinstance(expected_size_bytes, int)
            or isinstance(expected_size_bytes, bool)
            or expected_size_bytes < 0
            or expected_size_bytes > self._max_bytes
        ):
            raise ValueError("Artifact 文件大小元数据无效")
        if (
            not isinstance(expected_sha256, str)
            or len(expected_sha256) != 64
            or any(char not in "0123456789abcdef" for char in expected_sha256)
        ):
            raise ValueError("Artifact 文件哈希元数据无效")

        components = self._parse_relative_path(relative_path)
        required_flags = ("O_DIRECTORY", "O_NOFOLLOW", "O_CLOEXEC")
        if not all(hasattr(os, flag) for flag in required_flags):
            raise OSError("当前平台不支持安全 Workspace Artifact 读取")

        root_fd = os.open(
            canonical_root,
            os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
        )
        parent_fd = os.dup(root_fd)
        file_fd: int | None = None
        try:
            for component in components[:-1]:
                next_fd = os.open(
                    component,
                    os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
                    dir_fd=parent_fd,
                )
                os.close(parent_fd)
                parent_fd = next_fd
            file_fd = os.open(
                components[-1],
                os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC,
                dir_fd=parent_fd,
            )
            before = os.fstat(file_fd)
            if not stat.S_ISREG(before.st_mode):
                raise ValueError("Artifact 引用不是普通文件")
            if (
                before.st_size != expected_size_bytes
                or before.st_size > self._max_bytes
            ):
                raise ValueError("Artifact 文件大小与元数据不一致")

            chunks: list[bytes] = []
            remaining = self._max_bytes + 1
            while remaining > 0:
                chunk = os.read(file_fd, min(64 * 1024, remaining))
                if not chunk:
                    break
                chunks.append(chunk)
                remaining -= len(chunk)
            data = b"".join(chunks)
            after = os.fstat(file_fd)
            if (
                len(data) != expected_size_bytes
                or before.st_dev != after.st_dev
                or before.st_ino != after.st_ino
                or before.st_size != after.st_size
                or before.st_mtime_ns != after.st_mtime_ns
            ):
                raise ValueError("Artifact 文件在读取期间发生变化")
            digest = hashlib.sha256(data).hexdigest()
            if digest != expected_sha256:
                raise ValueError("Artifact 文件哈希不一致")
            return WorkspaceArtifactContent(
                content=data.decode("utf-8"),
                size_bytes=len(data),
                sha256=digest,
            )
        finally:
            if file_fd is not None:
                os.close(file_fd)
            os.close(parent_fd)
            os.close(root_fd)

    @staticmethod
    def _parse_relative_path(relative_path: str) -> tuple[str, ...]:
        if (
            not isinstance(relative_path, str)
            or not relative_path
            or "\x00" in relative_path
        ):
            raise ValueError("Workspace Artifact 相对路径无效")
        parsed = PurePosixPath(relative_path)
        if (
            parsed.is_absolute()
            or str(parsed) != relative_path
            or not parsed.parts
            or any(part in ("", ".", "..") for part in parsed.parts)
        ):
            raise ValueError("Workspace Artifact 相对路径必须规范且不可越界")
        return tuple(parsed.parts)
