"""ToolGateway 副作用边界与测试专用故障注入屏障。

生产 Runtime 默认不装配任何屏障。隔离故障验收显式启用后，ToolGateway 会在
一次性权限已经验证、但 capability executor 尚未开始前写入 reached 标记，并等待
外部测试驱动创建 release 文件。测试驱动可在这个窗口强杀 Worker，以验证
``tool_in_flight`` 的 fail-closed 恢复语义。
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Protocol

from jarvis_worker.agent.tool_gateway.contracts import (
    PermissionApproval,
    ToolManifest,
    ToolRequest,
)

log = logging.getLogger("jarvis_worker.agent.tool_gateway.effect_boundary")


class ToolEffectBoundary(Protocol):
    """获批工具进入 capability executor 前的 Harness 生命周期端口。"""

    def before_effect(
        self,
        *,
        request: ToolRequest,
        manifest: ToolManifest,
        approval: PermissionApproval,
    ) -> None: ...


class ToolEffectBoundaryError(RuntimeError):
    """测试屏障无法安全完成时，阻止真实工具 effect。"""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


class FileToolEffectBarrier:
    """通过 reached/release 文件提供可观察、可控制的 effect 前窗口。"""

    def __init__(
        self,
        root: Path,
        *,
        timeout_seconds: float = 120,
        poll_interval_seconds: float = 0.05,
    ) -> None:
        if not root.is_absolute():
            raise ValueError("Tool effect barrier root 必须是绝对路径")
        if timeout_seconds <= 0 or poll_interval_seconds <= 0:
            raise ValueError("Tool effect barrier timeout/poll interval 必须大于 0")
        self._root = root
        self._timeout_seconds = timeout_seconds
        self._poll_interval_seconds = poll_interval_seconds
        self._root.mkdir(parents=True, exist_ok=True)

    @property
    def root(self) -> Path:
        return self._root

    def before_effect(
        self,
        *,
        request: ToolRequest,
        manifest: ToolManifest,
        approval: PermissionApproval,
    ) -> None:
        identity = {
            "task_id": request.task_id,
            "run_id": request.run_id,
            "step_id": request.step_id,
            "tool_name": request.tool_name,
            "approval_request_id": approval.request_id,
        }
        barrier_key = hashlib.sha256(
            json.dumps(identity, ensure_ascii=True, sort_keys=True).encode("utf-8")
        ).hexdigest()
        reached_path = self._root / f"{barrier_key}.reached.json"
        release_path = self._root / f"{barrier_key}.release"
        marker = {
            "schema_version": 1,
            "boundary": "tool.before_effect",
            **identity,
            "risk_level": manifest.risk_level_default,
            "release_file": release_path.name,
            "reached_at": datetime.now(timezone.utc).isoformat(),
            "worker_pid": os.getpid(),
        }
        try:
            self._write_marker(reached_path, marker)
        except OSError as exc:
            raise ToolEffectBoundaryError(
                "TOOL_EFFECT_BARRIER_UNAVAILABLE",
                "测试副作用屏障无法建立",
            ) from exc

        log.warning(
            "测试副作用屏障已到达: barrier_key=%s tool=%s",
            barrier_key,
            request.tool_name,
            extra={
                "task_id": request.task_id,
                "run_id": request.run_id,
                "step_id": request.step_id,
            },
        )
        deadline = time.monotonic() + self._timeout_seconds
        while True:
            if release_path.is_file():
                log.warning(
                    "测试副作用屏障已释放: barrier_key=%s tool=%s",
                    barrier_key,
                    request.tool_name,
                    extra={
                        "task_id": request.task_id,
                        "run_id": request.run_id,
                        "step_id": request.step_id,
                    },
                )
                return
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise ToolEffectBoundaryError(
                    "TOOL_EFFECT_BARRIER_TIMEOUT",
                    "测试副作用屏障等待释放超时",
                )
            time.sleep(min(self._poll_interval_seconds, remaining))

    def _write_marker(self, target: Path, marker: dict[str, object]) -> None:
        temporary_path: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                dir=self._root,
                prefix=f".{target.stem}.",
                suffix=".tmp",
                delete=False,
            ) as handle:
                temporary_path = Path(handle.name)
                json.dump(marker, handle, ensure_ascii=False, sort_keys=True)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary_path, target)
            temporary_path = None
            try:
                directory_fd = os.open(self._root, os.O_RDONLY)
                try:
                    os.fsync(directory_fd)
                finally:
                    os.close(directory_fd)
            except OSError:
                # 标记文件本身已经原子落盘；部分平台不支持目录 fsync。
                pass
        finally:
            if temporary_path is not None:
                try:
                    temporary_path.unlink(missing_ok=True)
                except OSError:
                    pass
