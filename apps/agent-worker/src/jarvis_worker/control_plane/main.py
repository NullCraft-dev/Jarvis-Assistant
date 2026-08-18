"""Control Plane 标准入口：先初始化日志，再启动 Uvicorn。"""

from __future__ import annotations

import argparse

import uvicorn

from jarvis_worker.shared.observability import setup_logging


def main() -> None:
    parser = argparse.ArgumentParser(description="Jarvis Python Control Plane")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8100)
    args = parser.parse_args()

    setup_logging(service_name="control-plane", log_basename="control-plane.log")
    uvicorn.run(
        "jarvis_worker.control_plane.app:app",
        host=args.host,
        port=args.port,
        log_config=None,
        access_log=False,
    )


if __name__ == "__main__":
    main()
