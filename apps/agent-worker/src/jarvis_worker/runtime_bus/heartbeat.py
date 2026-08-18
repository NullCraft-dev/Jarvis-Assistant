"""Worker heartbeat producer — 周期性发布 WorkerHeartbeatMessage 到 Redis heartbeat stream。

Heartbeat 是状态探针，不属于 command / event 链路，不携带 trace_id。
对齐 Go 侧 redisruntime/transport.go PublishWorkerHeartbeat。

XADD fields 使用 WorkerHeartbeatMessage.to_xadd_fields()，
格式：schema_version + payload（完整 JSON）+ 冗余标量路由字段（worker_id / type / status / reported_at）。

职责：
  - 周期性将 worker 状态写入 jarvis:stream:worker-heartbeat
  - 支持 start / stop / status 变更
  - 通过 threading.Event 支持 graceful shutdown
  - 周期性发布在独立守护线程中执行，主线程不阻塞

不负责：
  - 成为 Task / Run 业务真源
  - 携带 trace_id
  - 秒级精确心跳（间隔可配置，默认 3000ms）
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from datetime import datetime, timezone

from jarvis_worker.runtime_bus.messages import (
    STREAM_WORKER_HEARTBEAT,
    WorkerHeartbeatMessage,
)
from jarvis_worker.runtime_bus import RedisClientProtocol

log = logging.getLogger("jarvis_worker.heartbeat")


class HeartbeatProducer:
    """周期性发布 WorkerHeartbeatMessage 到 Redis heartbeat stream。

    用法：
        hb = HeartbeatProducer(client, worker_id="worker-01", interval_ms=3000)
        hb.set_status("starting")
        hb.start()         # 启动后台线程
        hb.set_status("idle")
        # ... worker 运行中 ...
        hb.set_status("draining")
        hb.stop()          # 发送 stopped + 停止后台线程
    """

    def __init__(
        self,
        client: RedisClientProtocol,
        worker_id: str,
        interval_ms: int = 3000,
        worker_kind: str = "agent",
        model_status: dict | None = None,
        runtime_bus_metrics_provider: Callable[[], dict[str, int]] | None = None,
    ):
        """创建 HeartbeatProducer。

        Args:
            client: Redis client（支持 fakeredis 注入测试）
            worker_id: 当前 worker 唯一标识
            interval_ms: 心跳发布间隔（毫秒），默认 3000
            worker_kind: worker 类型；agent 执行 AgentRun，rag 执行 RAG 作业
            model_status: 模型配置状态 dict（Phase 6B-1），
                          含 provider/model_name/api_key_configured/thinking_mode/status/last_error_code
            runtime_bus_metrics_provider: 返回 Runtime Bus 进程级累计指标的函数
        """
        self._client = client
        self._worker_id = worker_id
        if worker_kind not in {"agent", "rag"}:
            raise ValueError("worker_kind 必须是 'agent' 或 'rag'")
        self._worker_kind = worker_kind
        self._interval_s = max(interval_ms, 100) / 1000.0  # 最小 100ms
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._model_status = model_status
        self._runtime_bus_metrics_provider = runtime_bus_metrics_provider

        # 当前状态（线程安全：通过锁保护）
        self._lock = threading.Lock()
        self._status = "starting"
        self._active_run_id = ""

    # -- 状态管理 --

    @property
    def status(self) -> str:
        with self._lock:
            return self._status

    @property
    def active_run_id(self) -> str:
        with self._lock:
            return self._active_run_id

    def set_status(self, status: str) -> None:
        """设置 worker 状态，下一轮心跳立即生效。

        合法状态：starting | idle | busy | draining | stopped | failed
        """
        valid = {"starting", "idle", "busy", "draining", "stopped", "failed"}
        if status not in valid:
            raise ValueError(
                f"非法 worker status: {status!r}，合法值: {sorted(valid)}"
            )
        with self._lock:
            self._status = status
        log.debug("状态变更: %s", status)

    def set_active_run_id(self, run_id: str) -> None:
        """设置当前活跃 run_id。busy 状态时设置，idle 时清空。"""
        with self._lock:
            self._active_run_id = run_id

    def set_runtime_bus_metrics_provider(
        self, provider: Callable[[], dict[str, int]]
    ) -> None:
        """设置 Runtime Bus 指标提供器，用于解决 Worker 构造时的循环依赖。"""
        self._runtime_bus_metrics_provider = provider

    # -- 生命周期 --

    def start(self) -> None:
        """启动后台心跳线程（守护线程）。

        线程在后台以 interval_ms 间隔循环发布心跳。
        调用 stop() 后线程退出并 join。
        重复调用安全（若已启动则记录警告后忽略）。
        """
        if self._thread is not None and self._thread.is_alive():
            log.warning("HeartbeatProducer 已启动，忽略重复 start")
            return

        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._loop,
            name=f"heartbeat-{self._worker_id}",
            daemon=True,
        )
        self._thread.start()
        log.info("HeartbeatProducer 已启动: interval=%dms", self._interval_s * 1000)

    def stop(self, timeout_s: float = 5.0) -> None:
        """停止后台心跳线程。

        流程：
          1. 设置 stop event → 循环退出
          2. 等待线程 join（最多 timeout_s 秒）
          3. 已调用过或未 start 则直接返回

        注意：stop() 不发送 stopped 状态心跳。
        调用方应在 stop() 前先 set_status("draining") 并发布最后一次心跳。
        """
        if self._thread is None:
            return

        self._stop_event.set()
        if self._thread.is_alive():
            self._thread.join(timeout=timeout_s)
            if self._thread.is_alive():
                log.warning("HeartbeatProducer 线程 join 超时 (%.1fs)", timeout_s)
        self._thread = None
        log.info("HeartbeatProducer 已停止")

    def publish_now(self) -> str | None:
        """立即发布一次心跳（同步调用，不等待定时器）。

        用于关键状态转换（starting / busy / draining / stopped）时立即通知，
        不等下一个定时周期。

        Returns:
            Redis 消息 id，发布失败返回 None
        """
        return self._publish()

    # -- 内部 --

    def _loop(self) -> None:
        """后台心跳循环。

        在 stop_event 被设置前持续运行：
          - 发布一次心跳
          - 等待 interval_s（可被 stop_event 中断）
        """
        log.info("HeartbeatProducer 循环开始: worker=%s", self._worker_id)
        while not self._stop_event.is_set():
            self._publish()
            # 等待下一次发布（可被 stop 中断）
            if self._stop_event.wait(timeout=self._interval_s):
                break
        log.info("HeartbeatProducer 循环退出: worker=%s", self._worker_id)

    def _publish(self) -> str | None:
        """构造并发布一次心跳消息。"""
        with self._lock:
            status = self._status
            active_run_id = self._active_run_id

        msg = WorkerHeartbeatMessage(
            worker_id=self._worker_id,
            status=status,
            reported_at=datetime.now(timezone.utc).isoformat(),
            worker_kind=self._worker_kind,
            active_run_id=active_run_id,
            model=self._model_status,
            runtime_bus=self._read_runtime_bus_metrics(),
        )

        fields = msg.to_xadd_fields()

        try:
            msg_id = self._client.xadd(STREAM_WORKER_HEARTBEAT, fields, id="*")
            log.debug(
                "heartbeat: worker=%s status=%s active_run=%s redis_id=%s",
                self._worker_id,
                status,
                active_run_id or "-",
                msg_id,
            )
            return msg_id
        except Exception as e:
            log.error("发布 heartbeat 失败: %s", e)
            return None

    def _read_runtime_bus_metrics(self) -> dict[str, int] | None:
        """读取指标；指标异常不能阻断 worker 心跳。"""
        if self._runtime_bus_metrics_provider is None:
            return None
        try:
            metrics = self._runtime_bus_metrics_provider()
            return {str(key): int(value) for key, value in metrics.items()}
        except Exception as exc:
            log.warning("读取 Runtime Bus 指标失败，继续发送心跳: %s", exc)
            return None
