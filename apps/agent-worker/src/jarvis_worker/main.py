"""Jarvis Python Agent Worker — 入口。

启动流程：
  0. 加载本地 .env（仅补充缺失的环境变量，外部已注入的优先）
  1. 加载配置（环境变量）
  2. 组装运行时组件（bootstrap/container）
  3. 注册 graceful shutdown
  4. 阻塞运行主循环

用法：
  python -m jarvis_worker.main
  JARVIS_REDIS_ADDR=10.0.0.1:6379 python -m jarvis_worker.main
"""

from __future__ import annotations

import logging
import signal

from jarvis_worker.bootstrap.container import create_worker_runtime
from jarvis_worker.shared.config.env_loader import load_default_local_env
from jarvis_worker.shared.config.settings import WorkerConfig
from jarvis_worker.shared.observability import setup_logging

log = logging.getLogger("jarvis_worker.main")

def main() -> None:
    """Worker 主入口。"""
    setup_logging()

    # 0. 加载本地 .env（补充缺失的环境变量；外部已注入的优先）
    load_default_local_env()

    cfg = WorkerConfig.from_env()
    log.info(
        "Jarvis Agent Worker 启动中: worker_id=%s redis=%s",
        cfg.worker_id,
        cfg.redis_addr,
    )

    # 1. 组装运行时组件（Redis / ToolGateway / AgentRunner / Worker）
    runtime = create_worker_runtime(cfg)

    # 2. 注册 graceful shutdown
    def _shutdown(signum: int, _frame: object) -> None:
        log.info("收到信号 %s，准备关闭...", signum)
        runtime.worker.stop()

    signal.signal(signal.SIGINT, _shutdown)
    signal.signal(signal.SIGTERM, _shutdown)

    # 3. 运行主循环
    log.info("Worker 进入主循环")
    try:
        runtime.worker.run_forever()
    except KeyboardInterrupt:
        log.info("Worker 被用户中断")
        runtime.worker.stop()
    finally:
        runtime.client.close()
        log.info("Worker 已停止")


if __name__ == "__main__":
    main()
