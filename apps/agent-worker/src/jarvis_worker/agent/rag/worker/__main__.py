"""`python -m jarvis_worker.agent.rag.worker` 入口。"""

from __future__ import annotations

import asyncio
import logging
import os
import signal

from jarvis_worker.agent.rag.worker.bootstrap import create_rag_worker_runtime
from jarvis_worker.agent.rag.worker.config import RagWorkerConfig
from jarvis_worker.shared.config.env_loader import load_default_local_env
from jarvis_worker.shared.observability import setup_logging

log = logging.getLogger("jarvis_worker.rag_worker.main")


async def _run(config: RagWorkerConfig) -> None:
    runtime = await create_rag_worker_runtime(config)
    runtime.start()
    loop = asyncio.get_running_loop()

    def request_stop() -> None:
        log.info("RAG Worker 收到停止信号")
        runtime.worker.stop()

    for signum in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(signum, request_stop)
        except NotImplementedError:
            signal.signal(signum, lambda _signum, _frame: request_stop())

    try:
        await runtime.worker.run_forever()
    finally:
        await runtime.close()


def main() -> None:
    load_default_local_env()
    config = RagWorkerConfig.from_env()
    os.environ.setdefault("JARVIS_INSTANCE_ID", config.worker_id)
    setup_logging(
        service_name="rag-worker",
        log_basename=f"rag-worker-{config.worker_id}.log",
    )
    asyncio.run(_run(config))


if __name__ == "__main__":
    main()
