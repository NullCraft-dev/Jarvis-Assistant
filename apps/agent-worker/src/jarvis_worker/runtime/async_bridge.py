"""为同步 Worker 提供固定 asyncio event loop。"""

from __future__ import annotations

import asyncio
import threading
from typing import Coroutine, TypeVar


T = TypeVar("T")


class AsyncServiceBridge:
    """在线程内持有固定 event loop，避免 asyncpg 连接跨 loop 复用。"""

    def __init__(self) -> None:
        self._ready = threading.Event()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread = threading.Thread(
            target=self._run_loop,
            name="worker-application-services",
            daemon=True,
        )
        self._thread.start()
        if not self._ready.wait(timeout=5):
            raise RuntimeError("Application Service event loop 启动超时")

    def _run_loop(self) -> None:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        self._loop = loop
        self._ready.set()
        loop.run_forever()
        pending = asyncio.all_tasks(loop)
        for task in pending:
            task.cancel()
        if pending:
            loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
        loop.close()

    def run(self, coroutine: Coroutine[object, object, T], timeout: float = 30) -> T:
        if self._loop is None or not self._loop.is_running():
            coroutine.close()
            raise RuntimeError("Application Service event loop 未运行")
        future = asyncio.run_coroutine_threadsafe(coroutine, self._loop)
        return future.result(timeout=timeout)

    def close(self) -> None:
        if self._loop is not None and self._loop.is_running():
            self._loop.call_soon_threadsafe(self._loop.stop)
        if self._thread.is_alive():
            self._thread.join(timeout=5)
