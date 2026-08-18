"""本地受控文件存储的跨进程容量原语。"""

from __future__ import annotations

import fcntl
import os
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

_LOCK_FILE_NAME = ".capacity.lock"
DEFAULT_CAPACITY_SCAN_MAX_ENTRIES = 100_000


class StorageCapacityExceeded(ValueError):
    """稳定、无敏感路径的容量错误。"""

    def __init__(
        self,
        code: str,
        scope: str,
        *,
        limit: int,
        unit: str = "bytes",
    ):
        self.code = code
        self.scope = scope
        self.limit = limit
        self.unit = unit
        super().__init__(f"{scope} 容量已达到上限")


@contextmanager
def capacity_lock(root: Path) -> Iterator[None]:
    """对同一存储根目录串行执行 usage 检查和原子替换。"""
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    lock_path = root / _LOCK_FILE_NAME
    fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        os.fchmod(fd, 0o600)
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def directory_size_bytes(
    root: Path,
    *,
    max_entries: int = DEFAULT_CAPACITY_SCAN_MAX_ENTRIES,
) -> int:
    """有界统计文件大小；不跟随 symlink，忽略锁和未完成临时文件。"""
    if isinstance(max_entries, bool) or max_entries < 1:
        raise ValueError("容量扫描条目上限必须是大于 0 的整数")
    if not root.exists():
        return 0
    total = 0
    scanned_entries = 0
    stack = [root]
    while stack:
        current = stack.pop()
        try:
            entries = os.scandir(current)
        except FileNotFoundError:
            continue
        with entries:
            for entry in entries:
                scanned_entries += 1
                if scanned_entries > max_entries:
                    raise StorageCapacityExceeded(
                        "STORAGE_CAPACITY_SCAN_LIMIT_EXCEEDED",
                        "本地存储容量扫描",
                        limit=max_entries,
                        unit="entries",
                    )
                if entry.name == _LOCK_FILE_NAME or entry.name.endswith(".tmp"):
                    continue
                try:
                    if entry.is_dir(follow_symlinks=False):
                        stack.append(Path(entry.path))
                    elif entry.is_file(follow_symlinks=False):
                        total += entry.stat(follow_symlinks=False).st_size
                except FileNotFoundError:
                    continue
    return total


def ensure_capacity(
    *,
    current_bytes: int,
    existing_bytes: int,
    requested_bytes: int,
    limit_bytes: int,
    code: str,
    scope: str,
) -> None:
    projected = current_bytes - existing_bytes + requested_bytes
    if projected > limit_bytes:
        raise StorageCapacityExceeded(code, scope, limit=limit_bytes)
