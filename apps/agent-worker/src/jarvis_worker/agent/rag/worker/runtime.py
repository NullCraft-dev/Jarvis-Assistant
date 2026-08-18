"""独立 RAG Worker 的单并发、公平轮转生命周期。"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol

log = logging.getLogger("jarvis_worker.rag_worker")


class RagStageService(Protocol):
    async def process_next(self, *, worker_id: str) -> object | None: ...


@dataclass(frozen=True, slots=True)
class RagWorkerStats:
    ingestion_processed: int
    embedding_processed: int
    cycle_errors: int


class RagWorker:
    """顺序执行两个重型阶段，避免本地视觉模型与 Embedding 无界并发。"""

    def __init__(
        self,
        *,
        worker_id: str,
        ingestion_service: RagStageService,
        embedding_service: RagStageService,
        poll_interval: float = 1.0,
        error_backoff: float = 5.0,
        status_callback: Callable[[str], None] | None = None,
    ) -> None:
        if not worker_id.strip():
            raise ValueError("RAG worker_id 不能为空")
        if poll_interval <= 0 or error_backoff <= 0:
            raise ValueError("RAG Worker 轮询间隔必须大于 0")
        self._worker_id = worker_id
        self._ingestion_service = ingestion_service
        self._embedding_service = embedding_service
        self._poll_interval = poll_interval
        self._error_backoff = error_backoff
        self._stop_event = asyncio.Event()
        self._running = False
        self._ingestion_processed = 0
        self._embedding_processed = 0
        self._cycle_errors = 0
        self._status_callback = status_callback

    @property
    def stats(self) -> RagWorkerStats:
        return RagWorkerStats(
            ingestion_processed=self._ingestion_processed,
            embedding_processed=self._embedding_processed,
            cycle_errors=self._cycle_errors,
        )

    def stop(self) -> None:
        self._stop_event.set()

    async def run_forever(self) -> None:
        if self._running:
            raise RuntimeError("RAG Worker 已在运行")
        self._running = True
        self._set_status("idle")
        log.info("RAG Worker 启动: worker_id=%s concurrency=1", self._worker_id)
        try:
            while not self._stop_event.is_set():
                try:
                    did_work, had_error = await self._run_cycle()
                except asyncio.CancelledError:
                    raise
                except Exception:
                    self._cycle_errors += 1
                    self._set_status("failed")
                    log.exception("RAG Worker cycle 执行失败；将在退避后重试")
                    await self._wait(self._error_backoff)
                    continue
                if had_error:
                    await self._wait(self._error_backoff)
                elif not did_work:
                    await self._wait(self._poll_interval)
        finally:
            self._set_status("draining")
            self._running = False
            log.info("RAG Worker 已停止: worker_id=%s stats=%s", self._worker_id, self.stats)

    async def _run_cycle(self) -> tuple[bool, bool]:
        ingestion, ingestion_error = await self._process_stage("ingestion", self._ingestion_service)
        embedding, embedding_error = await self._process_stage("embedding", self._embedding_service)
        if ingestion:
            self._ingestion_processed += 1
        if embedding:
            self._embedding_processed += 1
        return ingestion or embedding, ingestion_error or embedding_error

    async def _process_stage(self, name: str, service: RagStageService) -> tuple[bool, bool]:
        # RAG Job 不是 AgentRun，因此不填写 active_run_id；但 busy 仍准确表达
        # 当前单并发 Worker 正在占用本地视觉模型或 Embedding 资源。
        try:
            self._set_status("busy")
            result = await service.process_next(worker_id=self._worker_id)
            self._set_status("idle")
            return result is not None, False
        except asyncio.CancelledError:
            raise
        except Exception:
            self._cycle_errors += 1
            self._set_status("failed")
            log.exception("RAG Worker %s 阶段执行失败", name)
            return False, True

    def _set_status(self, status: str) -> None:
        if self._status_callback is None:
            return
        try:
            self._status_callback(status)
        except Exception:
            log.exception("RAG Worker 状态回调失败: status=%s", status)

    async def _wait(self, seconds: float) -> None:
        try:
            await asyncio.wait_for(self._stop_event.wait(), timeout=seconds)
        except TimeoutError:
            pass
