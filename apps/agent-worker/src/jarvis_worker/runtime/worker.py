"""Agent Worker — 主循环：消费 run queue → AgentRunner → 发布 event stream。

3B 集成 heartbeat producer：
  - 启动时发布 starting 并启动后台心跳
  - 等待 job 期间周期性发布 idle
  - 获取 job 后发布 busy（带 active_run_id）
  - job 完成并 ack 后发布 idle
  - stop() 时发布 draining，退出前发布 stopped
  - 处理 job 出错：不 ack，发布 failed 后恢复 idle（可继续处理后续 job）

3C 集成 cancel 支持：
  - active run 期间启动 command polling daemon thread
  - 后台线程持续消费 worker-command stream
  - 收到 run.cancel 且 run_id == 当前 active_run_id 时设置 _cancel_requested 并 ack
  - 不匹配当前 active_run_id 或无 active run 的 run.cancel：不 ack
    （避免多 worker 同 consumer group 场景下非 owner worker 吞掉 cancel command；
    完整 multi-worker cancel routing 仍需后续 worker_id 定向或 reclaim 机制）
  - permission.decision 按 active run + request id 精确匹配并唤醒等待链路
  - run executor 在步骤间通过 cancel_check lambda 检查 flag
  - 收到 cancel 后发出 agent.run.cancelled，停止后续事件
  - cancel 后 heartbeat 回到 idle

4A/6C：生产任务通过 AgentRunner 和 ToolGateway 执行真实模型决策与工具调用。
"""

from __future__ import annotations

import contextvars
import logging
import threading
import time
from uuid import NAMESPACE_URL, UUID, uuid5

from jarvis_worker.agent.core.checkpoint import (
    RUN_CHECKPOINT_VERSION,
    is_resumable_run_checkpoint,
    validate_permission_checkpoint,
    validate_run_checkpoint,
)
from jarvis_worker.runtime.async_bridge import AsyncServiceBridge
from jarvis_worker.runtime.events import (
    build_envelope,
    build_runtime_event,
    deterministic_event_id,
)
from jarvis_worker.runtime.run_executor import RunExecutor
from jarvis_worker.runtime_bus import RedisClientProtocol
from jarvis_worker.runtime_bus.command_consumer import (
    CMD_MALFORMED,
    CMD_UNSUPPORTED,
    WorkerCommandConsumer,
    WorkerCommandDelivery,
)
from jarvis_worker.runtime_bus.consumer import RunQueueConsumer, RunQueueDelivery
from jarvis_worker.runtime_bus.heartbeat import HeartbeatProducer
from jarvis_worker.runtime_bus.messages import (
    McpDiscoveryRefreshCommand,
    PermissionDecisionCommand,
    RunJobMessage,
    RunPauseCommand,
)
from jarvis_worker.runtime_bus.producer import RuntimeEventProducer
from jarvis_worker.shared.domain.models import PermissionStatus, RunStatus
from jarvis_worker.shared.observability import clear_log_context, set_log_context

log = logging.getLogger("jarvis_worker.worker")


class AgentWorker:
    """Agent Worker 主循环。

    职责：
      - 循环消费 Redis run queue 中的 RunJobMessage
      - 对每条 job 执行生产 Agent run executor
      - 关键事件通过 Application Service 写入 PostgreSQL + Outbox
      - 临时事件直接写入 Redis runtime event stream
      - 通过 HeartbeatProducer 维护 worker 状态可见性
      - 通过 WorkerCommandConsumer 接收 cancel 命令（3C）
      - active run 期间启动后台 command poll thread，确保 cancel 实时生效
      - 记录结构化日志
      - 通过 _stop_event (threading.Event) 支持 graceful shutdown

    不负责：
      - 直接调用模型或执行工具
      - 作为 Task / Run 业务真源
      - resume / retry 的业务决策（resume 通过标准 RunJob 重新入队）
    """

    def __init__(
        self,
        client: RedisClientProtocol,
        consumer: RunQueueConsumer,
        producer: RuntimeEventProducer,
        runner: RunExecutor,
        heartbeat: HeartbeatProducer | None = None,
        cmd_consumer: WorkerCommandConsumer | None = None,
        task_service: object | None = None,
        run_service: object | None = None,
        event_service: object | None = None,
        perm_service: object | None = None,
        context_builder: object | None = None,
        memory_service: object | None = None,
        memory_extraction_worker: object | None = None,
        memory_candidate_maintenance_worker: object | None = None,
        mcp_service: object | None = None,
        mcp_client: object | None = None,
        service_bridge: AsyncServiceBridge | None = None,
        async_closeables: tuple[object, ...] = (),
        run_queue_reclaim_interval_ms: int = 5_000,
        command_reclaim_interval_ms: int = 1_000,
        durable_control_poll_interval_ms: int = 100,
    ):
        self._client = client
        self._task_svc = task_service
        self._run_svc = run_service
        self._event_svc = event_service
        self._perm_svc = perm_service
        self._context_builder = context_builder
        self._memory_service = memory_service
        self._memory_extraction_worker = memory_extraction_worker
        self._memory_candidate_maintenance_worker = memory_candidate_maintenance_worker
        self._mcp_service = mcp_service
        self._mcp_client = mcp_client
        self._service_bridge = service_bridge or (
            AsyncServiceBridge() if run_service is not None and event_service is not None else None
        )
        self._async_closeables = async_closeables
        self._consumer = consumer
        self._producer = producer
        self._runner = runner
        self._heartbeat = heartbeat
        self._cmd_consumer = cmd_consumer
        self._stop_event = threading.Event()
        self._run_queue_reclaim_interval_s = max(run_queue_reclaim_interval_ms, 1_000) / 1000.0
        self._last_run_queue_reclaim = 0.0
        self._command_reclaim_interval_s = max(command_reclaim_interval_ms, 500) / 1000.0
        self._last_command_reclaim = 0.0
        # Redis command 是正常投递路径；这个短轮询只在 active run 期间读取
        # PostgreSQL 权威 Run 状态，用于覆盖 Outbox/consumer 的末端竞态。
        self._durable_control_poll_interval_s = max(durable_control_poll_interval_ms, 50) / 1000.0
        self._last_durable_control_poll = 0.0
        self._run_queue_metrics = {
            "reclaimed": 0,
            "retry_deferred": 0,
            "dead_lettered": 0,
            "malformed": 0,
            "command_reclaimed": 0,
            "command_dead_lettered": 0,
            "command_malformed": 0,
        }

        # 3C: cancel flag（线程安全）
        self._cancel_lock = threading.Lock()
        self._cancel_requested = False
        self._pause_requested = False
        self._pause_command_id = ""
        self._active_run_id = ""
        self._last_lease_renewal = 0.0

        # 3C: command poll thread 控制
        self._poll_stop = threading.Event()

        # Permission MVP: permission decision wait
        self._perm_lock = threading.Lock()
        self._perm_request_id = ""  # 当前等待的 permission request_id
        self._perm_decision: str | None = None  # 收到的 decision（None=等待中）
        self._perm_received = threading.Event()

    @property
    def cancel_requested(self) -> bool:
        with self._cancel_lock:
            return self._cancel_requested

    def _set_cancel_requested(self, v: bool) -> None:
        with self._cancel_lock:
            self._cancel_requested = v

    @property
    def pause_requested(self) -> bool:
        with self._cancel_lock:
            return self._pause_requested

    @property
    def pause_command_id(self) -> str | None:
        with self._cancel_lock:
            return self._pause_command_id or None

    def _set_pause_requested(self, value: bool, command_id: str = "") -> None:
        with self._cancel_lock:
            self._pause_requested = value
            self._pause_command_id = command_id if value else ""

    def _set_active_run_id(self, run_id: str) -> None:
        with self._cancel_lock:
            self._active_run_id = run_id

    def _get_active_run_id(self) -> str:
        with self._cancel_lock:
            return self._active_run_id

    def stop(self) -> None:
        """设置 stop signal，使 run_forever() 在下一次迭代时退出。"""
        log.info("Worker 收到停止信号")
        self._stop_event.set()
        self._poll_stop.set()  # 也停止 command poll thread

    def verify_persistence(self) -> bool:
        """在 Worker 的固定 async loop 上验证 PostgreSQL 连接。"""
        if self._service_bridge is None:
            return False
        from jarvis_worker.database.engine import check_connection

        return self._service_bridge.run(check_connection(), timeout=15)

    def run_forever(self, poll_interval_ms: int = 500) -> None:
        """阻塞运行 worker 主循环，直到 stop() 被调用。"""
        log.info(
            "Worker 启动: consumer=%s group=%s worker=%s",
            self._consumer.consumer_name,
            self._consumer.group,
            self._runner.worker_id,
        )

        # 3B: 启动 heartbeat
        if self._heartbeat:
            self._heartbeat.set_status("starting")
            self._heartbeat.publish_now()
            self._heartbeat.start()
            self._heartbeat.set_status("idle")

        if self._memory_extraction_worker is not None and self._service_bridge is not None:
            self._service_bridge.run(self._memory_extraction_worker.start(), timeout=15)
        if (
            self._memory_candidate_maintenance_worker is not None
            and self._service_bridge is not None
        ):
            self._service_bridge.run(self._memory_candidate_maintenance_worker.start(), timeout=15)

        while not self._stop_event.is_set():
            # 每个扫描周期优先接管至多一条 stale PEL，避免持续新流量让
            # 崩溃 Worker 遗留消息永久饥饿；没有可接管消息时再读取新消息。
            delivery = self._claim_stale_run_delivery_if_due()
            if delivery is None:
                try:
                    delivery = self._consumer.read_delivery(block_ms=1000)
                except Exception as e:
                    log.error("读取 run queue 失败: %s", e)
                    if self._stop_event.wait(timeout=1):
                        break
                    continue

            if self._stop_event.is_set():
                break

            if delivery is None:
                # 空闲时 poll cancel commands
                self._poll_cancel_commands()
                if self._stop_event.wait(timeout=poll_interval_ms / 1000):
                    break
                continue

            if delivery.reclaimed:
                self._run_queue_metrics["reclaimed"] += 1

            if not delivery.valid:
                self._run_queue_metrics["malformed"] += 1
                self._dead_letter_run_delivery(
                    delivery,
                    delivery.error_code or "RUN_QUEUE_MALFORMED",
                    delivery.error_message or "Run Queue 消息非法",
                )
                continue

            job = delivery.job
            if job is None:  # 仅为类型收窄；invalid 已在上方收口。
                continue
            msg_id = delivery.message_id
            set_log_context(
                trace_id=job.trace_id,
                task_id=job.task_id,
                run_id=job.run_id,
            )

            log.info(
                "收到 run job: job_id=%s reclaimed=%s delivery_count=%d",
                job.job_id,
                delivery.reclaimed,
                delivery.delivery_count,
            )

            # 3B + 3C: 处理前准备
            self._set_active_run_id(job.run_id)
            self._set_cancel_requested(False)
            self._set_pause_requested(False)
            self._poll_stop.clear()
            if self._heartbeat:
                self._heartbeat.set_active_run_id(job.run_id)
                self._heartbeat.set_status("busy")
                self._heartbeat.publish_now()

            try:
                disposition = self._claim_job(job)
                log.info(
                    "RunJob claim 完成: job_id=%s disposition=%s resume=%s",
                    job.job_id,
                    disposition,
                    job.resume_from_checkpoint,
                )
                if disposition == "duplicate":
                    log.info("重复 RunJob 已幂等跳过: job_id=%s", job.job_id)
                    self._ack_job(msg_id)
                    self._finish_job()
                    continue
                if disposition == "cancel":
                    log.info("Run 在执行前已请求取消: run_id=%s", job.run_id)
                    self._publish_cancelled_event(job)
                    self._ack_job(msg_id)
                    self._finish_job()
                    continue
            except Exception as e:
                if self._consumer.should_dead_letter(delivery):
                    self._dead_letter_run_delivery(
                        delivery,
                        "RUN_QUEUE_RETRY_EXHAUSTED",
                        f"claim RunJob 失败: {type(e).__name__}",
                    )
                else:
                    self._run_queue_metrics["retry_deferred"] += 1
                    log.warning(
                        "claim RunJob 失败，保留 pending 等待退避重试: "
                        "job_id=%s delivery=%d/%d error=%s",
                        job.job_id,
                        delivery.delivery_count,
                        self._consumer.max_deliveries,
                        type(e).__name__,
                    )
                self._finish_job(failed=True)
                continue

            try:
                started_at = time.monotonic()
                log.info("AgentRun 执行开始: job_id=%s", job.job_id)
                self._process_job_with_cancel_check(job)
            except Exception as e:
                log.error(
                    "处理 run job 失败: job_id=%s duration_ms=%d error_type=%s",
                    job.job_id,
                    int((time.monotonic() - started_at) * 1000),
                    type(e).__name__,
                )
                try:
                    self._publish_failed_event(job)
                except Exception as persist_error:
                    log.error("记录 run failure 失败，不 ACK: %s", persist_error)
                    self._finish_job(failed=True)
                    continue
                self._ack_job(msg_id)
                self._finish_job(failed=True)
                continue

            # 处理成功 → ack
            self._ack_job(msg_id)
            log.info(
                "AgentRun 执行收口: job_id=%s duration_ms=%d",
                job.job_id,
                int((time.monotonic() - started_at) * 1000),
            )

            # 清理
            self._finish_job()

        # 3B: graceful shutdown
        self._poll_stop.set()
        if self._heartbeat:
            self._heartbeat.set_status("draining")
            self._heartbeat.publish_now()

        if self._service_bridge is not None:
            if self._memory_extraction_worker is not None:
                self._service_bridge.run(self._memory_extraction_worker.stop(), timeout=15)
            if self._memory_candidate_maintenance_worker is not None:
                self._service_bridge.run(
                    self._memory_candidate_maintenance_worker.stop(), timeout=15
                )
            for resource in self._async_closeables:
                close = getattr(resource, "aclose", None)
                if close is not None:
                    self._service_bridge.run(close(), timeout=15)
            from jarvis_worker.database.engine import dispose_engine

            self._service_bridge.run(dispose_engine(), timeout=15)
            self._service_bridge.close()
            self._heartbeat.stop()
            self._heartbeat.set_status("stopped")
            self._heartbeat.publish_now()

        log.info("Worker 主循环退出")

    def _claim_stale_run_delivery_if_due(self) -> RunQueueDelivery | None:
        """按周期有界扫描 PEL；claim 自带基于 delivery count 的退避。"""
        now = time.monotonic()
        if now - self._last_run_queue_reclaim < self._run_queue_reclaim_interval_s:
            return None
        self._last_run_queue_reclaim = now
        try:
            return self._consumer.claim_stale_one()
        except Exception:
            log.warning("接管 stale RunJob 失败", exc_info=True)
            return None

    def _dead_letter_run_delivery(
        self,
        delivery: RunQueueDelivery,
        error_code: str,
        error_message: str,
    ) -> bool:
        """先以 PostgreSQL 收口可信 Run，再原子写 DLQ 并 ACK 原消息。"""
        try:
            job = delivery.job
            if job is not None and self._run_svc is not None and self._service_bridge is not None:
                fail_delivery = getattr(self._run_svc, "fail_run_queue_delivery", None)
                if callable(fail_delivery):
                    self._service_bridge.run(
                        fail_delivery(
                            UUID(job.run_id),
                            delivery.message_id,
                            error_code,
                            delivery.delivery_count,
                        ),
                        timeout=15,
                    )
            dlq_id = self._consumer.dead_letter(
                delivery,
                error_code=error_code,
                error_message=error_message,
            )
            self._run_queue_metrics["dead_lettered"] += 1
            log.error(
                "RunJob 已进入 DLQ: msg_id=%s dlq_id=%s code=%s delivery=%d",
                delivery.message_id,
                dlq_id,
                error_code,
                delivery.delivery_count,
            )
            return True
        except Exception:
            log.error(
                "RunJob DLQ 收口失败，保留 pending: msg_id=%s code=%s",
                delivery.message_id,
                error_code,
                exc_info=True,
            )
            return False

    @property
    def run_queue_metrics(self) -> dict[str, int]:
        """进程内只读计数器；业务状态仍以 PostgreSQL 为真源。"""
        return dict(self._run_queue_metrics)

    # -- active run 期间的后台 command poll --

    def _start_command_poll_thread(self) -> threading.Thread | None:
        """启动后台 daemon 线程，在 active run 期间持续 poll cancel commands（3C）。

        线程在 _poll_stop 被设置或 _active_run_id 被清空时退出。
        """
        if self._cmd_consumer is None:
            return None

        log_context = contextvars.copy_context()
        t = threading.Thread(
            target=log_context.run,
            args=(self._command_poll_loop,),
            name=f"cmd-poll-{self._runner.worker_id}",
            daemon=True,
        )
        t.start()
        return t

    def _command_poll_loop(self) -> None:
        """后台循环：持续 poll worker-command stream 直到 stop 或 run 完成。"""
        log.debug("command poll thread 启动")
        while not self._poll_stop.is_set() and self._get_active_run_id() != "":
            self._renew_active_run_lease_if_due()
            self._sync_durable_pause_request_if_due()
            self._poll_cancel_commands()
            # read_delivery 最多阻塞 50ms；再等待 50ms，使控制观察窗口不超过约 100ms。
            if self._poll_stop.wait(timeout=0.05):
                break
        log.debug("command poll thread 退出")

    def _sync_durable_pause_request_if_due(self) -> None:
        """用 PostgreSQL 的 pause_requested 兜底 Redis 命令的投递延迟。

        pause API 的事务先把状态写为 ``pause_requested``，再依赖 Outbox 投递
        ``run.pause``。若模型调用恰好结束，单靠异步命令可能错过最后一个安全边界。
        active Worker 因而以短、限频的只读查询观察该权威状态；Redis 仍是正常命令
        投递与 ACK 通道，数据库不承担命令队列职责。
        """
        if (
            self._run_svc is None
            or self._service_bridge is None
            or self.pause_requested
        ):
            return
        now = time.monotonic()
        if now - self._last_durable_control_poll < self._durable_control_poll_interval_s:
            return
        self._last_durable_control_poll = now
        run_id = self._get_active_run_id()
        if not run_id:
            return
        try:
            run = self._service_bridge.run(
                self._run_svc.get_run(UUID(run_id)), timeout=2
            )
        except Exception:
            # 控制面短暂不可读时继续走 Redis 正常通道，不能让只读兜底中断 Run。
            log.debug("读取 active Run 控制状态失败: run_id=%s", run_id, exc_info=True)
            return
        status = getattr(run, "status", None)
        status_value = getattr(status, "value", status)
        if status_value != RunStatus.PAUSE_REQUESTED.value:
            return
        command_id = str(uuid5(NAMESPACE_URL, f"jarvis:durable-pause:{run_id}"))
        self._set_pause_requested(True, command_id)
        log.info(
            "从 PostgreSQL 观察到 pause_requested，提前设置 pause token: run_id=%s",
            run_id,
        )

    def _renew_active_run_lease_if_due(self) -> None:
        """后台续租，防止长模型调用被 reconciliation 误判为孤儿 Run。"""
        if self._run_svc is None or self._service_bridge is None:
            return
        now = time.monotonic()
        if now - self._last_lease_renewal < 20:
            return
        run_id = self._get_active_run_id()
        if not run_id:
            return
        try:
            renewed = self._service_bridge.run(
                self._run_svc.renew_run_lease(UUID(run_id), self._runner.worker_id),
                timeout=10,
            )
            if renewed:
                self._last_lease_renewal = now
        except Exception:
            log.warning("续租 active Run 失败: run_id=%s", run_id, exc_info=True)

    # -- job 处理 --

    def _process_job_with_cancel_check(self, job: RunJobMessage) -> None:
        """处理单条 run job，支持 cancel（3C）+ 多轮对话历史。

        流程：
          1. 启动后台 command poll thread
          2. 预取会话历史（局部变量，不跨 job 残留）
          3. 执行 run executor（每步之间通过 cancel_check 检查 flag）
          4. 发布事件
          5. 如果被 cancel，发出 agent.run.cancelled 而非 completed
          6. 停止 command poll thread
        """
        poll_thread = self._start_command_poll_thread()

        # 多轮对话 MVP：预取会话历史为局部变量
        # 不使用跨调用可变暂存，确保取消/失败/异常后不残留到下一 job
        history_messages = None
        trusted_history_provenance = None
        memory_items = None
        try:
            conversation_context = self._fetch_conversation_context(job)
            if conversation_context is not None:
                history_messages = conversation_context.history_messages
                trusted_history_provenance = (
                    conversation_context.trusted_provenance_links
                )
        except Exception:
            log.warning(
                "获取会话历史失败，按无历史继续: conv_id=%s task_id=%s",
                job.conversation_id,
                job.task_id,
                exc_info=True,
            )
        try:
            memory_items = self._fetch_long_term_memory(job)
        except Exception:
            log.warning("获取长期记忆失败，按无记忆继续: task_id=%s", job.task_id, exc_info=True)

        try:
            # 先检查是否在开始前已被 cancel
            if self.cancel_requested:
                log.info("run 已被 cancel，跳过执行: run_id=%s", job.run_id)
                self._publish_cancelled_event(job)
                return

            published_ids: set[str] = set()

            def publish_cb(env):
                self._publish_and_track(env, published_ids)

            if job.resume_from_checkpoint or job.retry_from_checkpoint:
                checkpoint = self._load_run_checkpoint(job)
                try:
                    validate_run_checkpoint(checkpoint)
                except (TypeError, ValueError):
                    log.error(
                        "Run checkpoint 与当前 Step 身份语义不兼容，安全终止: "
                        "run_id=%s version=%s",
                        job.run_id,
                        checkpoint.get("version"),
                    )
                    self._publish_failed_event(
                        job,
                        code="RUN_CHECKPOINT_INCOMPATIBLE",
                        message="运行检查点版本不兼容，请重新发起任务",
                        recoverable=False,
                    )
                    return
                lifecycle = (
                    self._build_retry_started_event(job, checkpoint)
                    if job.retry_from_checkpoint
                    else self._build_resumed_event(job, checkpoint)
                )
                publish_cb(lifecycle)
                envelopes = self._runner.resume_from_checkpoint(
                    checkpoint,
                    cancel_check=lambda: self.cancel_requested,
                    pause_check=lambda: self.pause_command_id,
                    publish_cb=publish_cb,
                )
            else:
                # 执行 run executor（传入 cancel checker + permission wait + history）
                run_kwargs = {
                    "cancel_check": lambda: self.cancel_requested,
                    "pause_check": lambda: self.pause_command_id,
                    "prepare_wait": lambda req_id: self._prepare_permission_wait(req_id),
                    "wait_decision": lambda req_id: self._wait_permission_decision(req_id),
                    "publish_cb": publish_cb,
                    "history_messages": history_messages,
                }
                # 保持旧的 RunExecutor 测试替身/外部适配器兼容；只有实际读取到
                # Memory 时才启用新增参数。
                if memory_items is not None:
                    run_kwargs["memory_items"] = memory_items
                if trusted_history_provenance:
                    run_kwargs["trusted_history_provenance"] = (
                        trusted_history_provenance
                    )
                envelopes = self._runner.run_with_cancel_check(job, **run_kwargs)

            if not envelopes:
                self._publish_cancelled_event(job)
                return

            # 发布事件（跳过 publish_cb 已发布的）
            for i, env in enumerate(envelopes):
                if env.event_id in published_ids:
                    continue
                try:
                    msg_id = self._publish_or_persist(env)
                    log.debug(
                        "[%d/%d] 发布 %s: event_id=%s redis_id=%s",
                        i + 1,
                        len(envelopes),
                        env.event_type,
                        env.event_id,
                        msg_id,
                    )
                except Exception as e:
                    raise RuntimeError(
                        f"发布事件 [{i}/{len(envelopes)}] {env.event_type} 失败: {e}"
                    ) from e

            log.info(
                "run 完成: task_id=%s run_id=%s events=%d",
                job.task_id,
                job.run_id,
                len(envelopes),
            )
        finally:
            # 确保 poll thread 退出
            self._poll_stop.set()
            if poll_thread is not None:
                poll_thread.join(timeout=3.0)

    def _publish_cancelled_event(self, job: RunJobMessage) -> None:
        """发布 agent.run.cancelled terminal event（3C）。"""
        event_id = deterministic_event_id(job.run_id, "agent.run.cancelled", 99)
        event = build_runtime_event(
            event_type="agent.run.cancelled",
            task_id=job.task_id,
            run_id=job.run_id,
            event_id=event_id,
            payload={
                "run_id": job.run_id,
                "reason": "cancelled_by_user",
            },
        )
        env = build_envelope(event, job.trace_id, self._runner.worker_id)
        msg_id = self._publish_or_persist(env)
        log.info(
            "发布 agent.run.cancelled: run_id=%s redis_id=%s",
            job.run_id,
            msg_id,
        )

    def _build_resumed_event(self, job: RunJobMessage, checkpoint: dict):
        event = build_runtime_event(
            event_type="agent.run.resumed",
            task_id=job.task_id,
            run_id=job.run_id,
            event_id=str(uuid5(NAMESPACE_URL, f"jarvis:{job.job_id}:agent.run.resumed")),
            payload={
                "run_id": job.run_id,
                "resume_node": str(checkpoint.get("resume_node", "")),
            },
        )
        return build_envelope(event, job.trace_id, self._runner.worker_id)

    def _build_retry_started_event(self, job: RunJobMessage, checkpoint: dict):
        event = build_runtime_event(
            event_type="agent.run.started",
            task_id=job.task_id,
            run_id=job.run_id,
            event_id=str(uuid5(NAMESPACE_URL, f"jarvis:{job.job_id}:agent.run.retry.started")),
            payload={
                "agent_id": self._runner.worker_id,
                "mode": "single_agent",
                "retry_from_checkpoint": True,
                "resume_node": str(checkpoint.get("resume_node", "")),
            },
        )
        return build_envelope(event, job.trace_id, self._runner.worker_id)

    def _publish_and_track(self, env, published_ids: set[str]) -> None:
        """发布一个 envelope 并追踪其 event_id（避免后续重复发布）。"""
        self._publish_or_persist(env)
        published_ids.add(env.event_id)

    def _publish_or_persist(self, env) -> str:
        """关键事件 PostgreSQL+Outbox；临时事件直接 Redis。"""
        if self._event_svc is not None and self._service_bridge is not None:
            if self._event_svc.is_durable(env.event_type):
                self._service_bridge.run(self._event_svc.record_envelope(env))
                log.debug(
                    "RuntimeEvent 已持久化至 Outbox: event_type=%s event_id=%s",
                    env.event_type,
                    env.event_id,
                )
                return "outbox"
        message_id = self._producer.publish(env)
        log.debug(
            "RuntimeEvent 已发布至 Redis: event_type=%s event_id=%s message_id=%s",
            env.event_type,
            env.event_id,
            message_id,
        )
        return message_id

    def _claim_job(self, job: RunJobMessage) -> str:
        if self._run_svc is None or self._service_bridge is None:
            return "execute"
        if job.resume_from_checkpoint:
            _, disposition = self._service_bridge.run(
                self._run_svc.claim_recovery(UUID(job.run_id), self._runner.worker_id, job.job_id)
            )
            return disposition
        _, disposition = self._service_bridge.run(
            self._run_svc.claim_job(UUID(job.run_id), self._runner.worker_id, job.job_id)
        )
        return disposition

    def _load_run_checkpoint(self, job: RunJobMessage) -> dict:
        if self._run_svc is None or self._service_bridge is None:
            raise RuntimeError("Run recovery 需要 PostgreSQL RunService")
        run = self._service_bridge.run(self._run_svc.get_run(UUID(job.run_id)), timeout=10)
        if run is None or not run.checkpoint:
            raise RuntimeError("Run recovery checkpoint 不存在")
        return dict(run.checkpoint)

    def _publish_failed_event(
        self,
        job: RunJobMessage,
        *,
        code: str = "WORKER_EXECUTION_FAILED",
        message: str = "Worker 执行失败",
        recoverable: bool = True,
    ) -> None:
        event = build_runtime_event(
            event_type="agent.run.failed",
            task_id=job.task_id,
            run_id=job.run_id,
            event_id=deterministic_event_id(job.run_id, "agent.run.failed", 1000),
            payload={
                "error": {
                    "code": code,
                    "message": message,
                    "category": "runtime",
                    "recoverable": recoverable,
                }
            },
        )
        self._publish_or_persist(build_envelope(event, job.trace_id, self._runner.worker_id))

    def _fetch_conversation_history(self, job: RunJobMessage) -> list[dict[str, str]] | None:
        """多轮对话 MVP：从 PostgreSQL 获取会话历史（返回局部值，不暂存）。

        只在以下条件同时满足时获取：
        - job 携带 conversation_id
        - context_builder 已初始化
        - service_bridge 可用（持久化链路就绪）

        Returns:
            list[dict] | None: 会话历史消息列表
        """
        conv_id = job.conversation_id
        if not conv_id:
            return None
        if self._context_builder is None or self._service_bridge is None:
            return None

        from uuid import UUID

        cid = UUID(conv_id)
        tid = UUID(job.task_id)
        history = self._service_bridge.run(
            self._context_builder.build_history(cid, exclude_task_id=tid),
            timeout=10,
        )
        log.info(
            "Context 历史加载完成: conversation_id=%s messages=%d",
            conv_id,
            len(history) if history else 0,
        )
        return history

    def _fetch_conversation_context(self, job: RunJobMessage):
        """Load model history and its Runtime-only trusted provenance sidecar."""
        conv_id = job.conversation_id
        if not conv_id:
            return None
        if self._context_builder is None or self._service_bridge is None:
            return None

        from uuid import UUID

        context = self._service_bridge.run(
            self._context_builder.build_run_context(
                UUID(conv_id), exclude_task_id=UUID(job.task_id)
            ),
            timeout=10,
        )
        log.info(
            "Context 加载完成: conversation_id=%s messages=%d provenance=%d source_run=%s",
            conv_id,
            len(context.history_messages),
            len(context.trusted_provenance_links),
            context.provenance_run_id or "none",
        )
        return context

    def _fetch_long_term_memory(self, job: RunJobMessage) -> list[dict] | None:
        """读取当前 Task 可见的 global + workspace 有效长期记忆。"""
        if self._memory_service is None or self._service_bridge is None:
            return None
        memories = self._service_bridge.run(
            self._memory_service.build_context_for_task(UUID(job.task_id), limit=20),
            timeout=10,
        )
        log.info(
            "Memory 上下文加载完成: memories=%d limit=%d",
            len(memories),
            20,
        )
        return [
            {
                "id": str(item.id),
                "scope_type": item.scope_type.value,
                "category": item.category.value,
                "key": item.key,
                "content": item.content,
                "importance": item.importance,
            }
            for item in memories
        ]

    def _ack_job(self, msg_id: str | None) -> None:
        if not msg_id:
            return
        if self._consumer.ack(msg_id):
            log.debug("ack 成功: msg_id=%s", msg_id)
        else:
            log.warning("ack 失败: msg_id=%s", msg_id)

    def _finish_job(self, failed: bool = False) -> None:
        self._poll_stop.set()
        if self._heartbeat:
            if failed:
                self._heartbeat.set_status("failed")
                self._heartbeat.publish_now()
            self._heartbeat.set_active_run_id("")
            self._heartbeat.set_status("idle")
        self._set_active_run_id("")
        self._set_cancel_requested(False)
        self._set_pause_requested(False)
        clear_log_context()

    def _prepare_permission_wait(self, request_id: str) -> None:
        """在发布 permission.required 前登记 pending request_id（Permission MVP）。

        必须在 publish_cb 之前调用，避免 permission.decision 早于 pending request 登记
        的竞态：decision 到达时 _perm_request_id 尚未设置 → 被判定为不匹配 → 丢失。
        """
        with self._perm_lock:
            self._perm_request_id = request_id
            self._perm_decision = None
            self._perm_received.clear()
        log.debug("permission wait prepared: request_id=%s", request_id)

    def _wait_permission_decision(self, request_id: str, timeout_s: float = 30.0) -> str | None:
        """等待 permission.decision（Permission MVP）。

        不覆盖 _prepare_permission_wait 已登记的 request_id 或已到达的 decision。
        如果 _perm_request_id 尚不匹配（如未 prepare），则 fallback 初始化。
        如果 decision 已在 prepare → publish → wait 窗口内到达，直接返回。

        Args:
            request_id: 当前等待的 permission request_id
            timeout_s: 超时秒数（默认 30s，测试中可设更短）

        Returns:
            decision string（如 "allow_once", "deny"），超时返回 None
        """
        log.info("等待 permission decision: request_id=%s timeout=%.0fs", request_id, timeout_s)

        # 检查是否已有 matching decision（prepare → publish → wait 窗口内到达）
        with self._perm_lock:
            cur_req = self._perm_request_id
            if cur_req == request_id and self._perm_decision is not None:
                decision = self._perm_decision
                self._perm_request_id = ""
                self._perm_decision = None
                self._perm_received.clear()
                log.info("permission decision 已到达（窗口内命中）: %s", decision)
                return decision
            # 如果尚未 prepare 或 request_id 不同，fallback 初始化
            if cur_req != request_id:
                self._perm_request_id = request_id
                self._perm_decision = None
                self._perm_received.clear()

        # 等待 decision（可被 stop 中断）
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            if self._stop_event.is_set():
                log.info("permission wait 被 stop 中断")
                self._clear_permission_wait()
                return None

            if self._perm_received.wait(timeout=0.2):
                with self._perm_lock:
                    decision = self._perm_decision
                    self._perm_request_id = ""
                    self._perm_decision = None
                log.info("收到 permission decision: %s", decision)
                return decision

            if self.cancel_requested:
                log.info("permission wait 被 cancel 中断")
                self._clear_permission_wait()
                return None

        log.warning("permission decision 超时: request_id=%s", request_id)
        with self._perm_lock:
            self._perm_request_id = ""
            self._perm_decision = None
        return None

    def _clear_permission_wait(self) -> None:
        with self._perm_lock:
            self._perm_request_id = ""
            self._perm_decision = None
            self._perm_received.clear()

    def _poll_cancel_commands(self) -> None:
        """轮询 worker-command stream，处理命令（3C + Permission MVP）。

        支持的命令类型：
          - run.cancel → 设置 cancel flag
          - permission.decision → 设置 _perm_decision 并 signal _perm_received
          - 其他已知 type → ack + 警告
          - malformed → 不 ack
        """
        if self._cmd_consumer is None:
            return

        delivery: WorkerCommandDelivery | None = None
        try:
            delivery = self._claim_stale_command_if_due()
            if delivery is None:
                read_delivery = getattr(self._cmd_consumer, "read_delivery", None)
                if callable(read_delivery):
                    delivery = read_delivery(block_ms=50)
                else:
                    cmd, msg_id, _stream = self._cmd_consumer.read_one(block_ms=50)
                    if cmd is None:
                        return
                    if cmd in (CMD_UNSUPPORTED, CMD_MALFORMED):
                        log.warning(
                            "legacy worker command 非法，保留 pending: msg_id=%s",
                            msg_id,
                        )
                        return
                    self._handle_claimed_command(cmd, msg_id)
                    return
        except Exception as exc:
            log.error("读取或接管 worker command 失败: %s", exc)
            return

        if delivery is None:
            return
        if delivery.reclaimed:
            self._run_queue_metrics["command_reclaimed"] += 1
        if not delivery.valid:
            self._run_queue_metrics["command_malformed"] += 1
            self._dead_letter_worker_command(delivery)
            return
        self._handle_claimed_command(delivery.command, delivery.message_id)

    def _claim_stale_command_if_due(
        self,
    ) -> WorkerCommandDelivery | None:
        """active/idle Worker 均可周期接管 command，避免 owner 路由饥饿。"""
        if self._cmd_consumer is None:
            return None
        now = time.monotonic()
        if now - self._last_command_reclaim < self._command_reclaim_interval_s:
            return None
        self._last_command_reclaim = now
        claim = getattr(self._cmd_consumer, "claim_stale_delivery", None)
        if not callable(claim):
            return None
        return claim()

    def _dead_letter_worker_command(self, delivery: WorkerCommandDelivery) -> bool:
        """非法 command 无可信业务语义，只写脱敏 Redis 诊断副本。"""
        if self._cmd_consumer is None:
            return False
        try:
            dlq_id = self._cmd_consumer.dead_letter(delivery)
            self._run_queue_metrics["command_dead_lettered"] += 1
            log.error(
                "非法 worker command 已进入 DLQ: msg_id=%s dlq_id=%s code=%s",
                delivery.message_id,
                dlq_id,
                delivery.error_code,
            )
            return True
        except Exception:
            log.error(
                "worker command DLQ 失败，保留 pending: msg_id=%s",
                delivery.message_id,
                exc_info=True,
            )
            return False

    def _handle_claimed_command(self, cmd, msg_id: str) -> None:
        """处理已读取或已接管的合法 command。"""

        if isinstance(cmd, McpDiscoveryRefreshCommand):
            if self._get_active_run_id():
                log.info(
                    "MCP discovery 等待 Worker 空闲（不 ack）: command_id=%s",
                    cmd.command_id,
                )
                return
            if (
                self._mcp_service is None
                or self._mcp_client is None
                or self._service_bridge is None
            ):
                log.error(
                    "MCP discovery service 未配置（不 ack）: command_id=%s",
                    cmd.command_id,
                )
                return
            try:
                discoveries = self._service_bridge.run(
                    self._mcp_service.refresh_enabled(self._mcp_client),
                    timeout=60,
                )
            except Exception:
                log.error(
                    "MCP discovery 命令执行失败（不 ack）: command_id=%s",
                    cmd.command_id,
                    exc_info=True,
                )
                return
            if self._cmd_consumer.ack(msg_id):
                log.info(
                    "MCP discovery 命令完成: command_id=%s server_count=%d",
                    cmd.command_id,
                    len(discoveries),
                )
            return

        # Permission MVP: handle permission.decision
        if isinstance(cmd, PermissionDecisionCommand):
            active_run = self._get_active_run_id()
            pending_req: str = ""
            with self._perm_lock:
                pending_req = self._perm_request_id

            if cmd.run_id == active_run and active_run != "":
                if cmd.request_id == pending_req and pending_req != "":
                    # 匹配 run_id + request_id → 接受 decision
                    log.info(
                        "收到匹配的 permission.decision: decision=%s request_id=%s run_id=%s",
                        cmd.decision,
                        cmd.request_id,
                        cmd.run_id,
                    )
                    with self._perm_lock:
                        self._perm_decision = cmd.decision
                        self._perm_received.set()
                    self._cmd_consumer.ack(msg_id)
                else:
                    # run_id 匹配但 request_id 不匹配 → 不 ack
                    # （可能是 stale/旧 decision，或其他 worker 的）
                    log.info(
                        "permission.decision request_id 不匹配（不 ack）: "
                        "cmd_req=%s pending_req=%s",
                        cmd.request_id,
                        pending_req,
                    )
            else:
                if active_run == "":
                    self._resume_permission_command(cmd, msg_id)
                else:
                    # 单 Worker MVP 中不会出现；多 Worker 阶段需按 run 定向 command。
                    log.info(
                        "permission.decision 不匹配当前 active run（不 ack）: "
                        "cmd_run=%s active_run=%s",
                        cmd.run_id,
                        active_run,
                    )
            return

        if isinstance(cmd, RunPauseCommand):
            active_run = self._get_active_run_id()
            if cmd.run_id == active_run and active_run:
                log.info("pause 匹配当前 active run: run_id=%s", cmd.run_id)
                self._set_pause_requested(True, cmd.command_id)
                self._cmd_consumer.ack(msg_id)
            elif active_run == "":
                self._ack_completed_pause_command(cmd, msg_id)
            else:
                log.info(
                    "pause 不匹配当前 active run（不 ack）: cmd_run=%s active_run=%s",
                    cmd.run_id,
                    active_run,
                )
            return

        # cmd is RunCancelCommand
        log.info(
            "收到 worker command: type=%s run_id=%s cmd_id=%s",
            cmd.type,
            cmd.run_id,
            cmd.command_id,
        )

        active_run = self._get_active_run_id()

        if cmd.run_id == active_run and active_run != "":
            log.info("cancel 匹配当前 active run: run_id=%s", cmd.run_id)
            self._set_cancel_requested(True)
            self._cmd_consumer.ack(msg_id)
        elif active_run == "":
            self._cancel_idle_run(cmd, msg_id)
        else:
            # 不匹配当前 active run → 不 ack
            # 注：Redis consumer group 语义下，不 ack 的消息会进入当前 consumer 的
            # pending，owner worker 用 XREADGROUP ">" 不会自动读到。完整 multi-worker
            # cancel routing 仍需后续实现 worker_id 定向或 XPENDING/XAUTOCLAIM reclaim。
            log.info(
                "cancel 不匹配当前 active run（不 ack）: cmd_run=%s active_run=%s",
                cmd.run_id,
                active_run,
            )

    def _ack_completed_pause_command(self, cmd: RunPauseCommand, msg_id: str) -> None:
        """空闲 Worker 仅清理已被 PostgreSQL 权威状态收口的 pause command。"""
        if self._run_svc is None or self._service_bridge is None:
            return
        try:
            run = self._service_bridge.run(self._run_svc.get_run(UUID(cmd.run_id)), timeout=10)
            if run is None or run.status in (
                RunStatus.PAUSED,
                RunStatus.RESUME_REQUESTED,
                RunStatus.COMPLETED,
                RunStatus.FAILED,
                RunStatus.CANCELLED,
            ):
                self._cmd_consumer.ack(msg_id)
        except Exception:
            log.warning("核对 idle pause command 失败，不 ack", exc_info=True)

    def _resume_permission_command(self, cmd: PermissionDecisionCommand, msg_id: str) -> None:
        """由任意空闲 Worker 从 PostgreSQL 检查点恢复等待授权的 run。"""
        if self._perm_svc is None or self._service_bridge is None:
            log.warning("permission resume service 未配置，不 ack: request_id=%s", cmd.request_id)
            return
        resume = getattr(self._runner, "resume_permission", None)
        if not callable(resume):
            log.warning("run executor 不支持 permission resume，不 ack")
            return

        try:
            req = self._service_bridge.run(
                self._perm_svc.get_request(UUID(cmd.request_id)), timeout=10
            )
            if req is None:
                log.warning("permission request 不存在，不 ack: %s", cmd.request_id)
                return
            if req.status == PermissionStatus.EXPIRED:
                if str(req.run_id) == cmd.run_id and str(req.task_id) == cmd.task_id:
                    log.info("permission command 对应请求已过期，幂等 ack: %s", cmd.request_id)
                    self._cmd_consumer.ack(msg_id)
                else:
                    log.warning("已过期 permission command 身份不匹配，不 ack: %s", cmd.request_id)
                return
            if req.status == PermissionStatus.CONSUMED and req.decision == cmd.decision:
                if self._run_svc is not None:
                    consumed_run = self._service_bridge.run(
                        self._run_svc.get_run(UUID(cmd.run_id)), timeout=10
                    )
                    if consumed_run is not None and consumed_run.status not in (
                        RunStatus.COMPLETED,
                        RunStatus.FAILED,
                        RunStatus.CANCELLED,
                    ):
                        interrupted_job = RunJobMessage(
                            job_id=cmd.command_id,
                            trace_id=cmd.trace_id,
                            task_id=cmd.task_id,
                            run_id=cmd.run_id,
                            user_goal="permission continuation interrupted",
                            created_at=cmd.decided_at,
                        )
                        recoverable = (
                            is_resumable_run_checkpoint(consumed_run.checkpoint)
                            and consumed_run.checkpoint.get("resume_node") == "call_model"
                        )
                        self._publish_failed_event(
                            interrupted_job,
                            code="PERMISSION_CONTINUATION_INTERRUPTED",
                            message=(
                                "工具结果已持久化，但后续推理中断"
                                if recoverable
                                else "权限恢复在非安全检查点中断，任务已安全终止"
                            ),
                            recoverable=recoverable,
                        )
                self._cmd_consumer.ack(msg_id)
                return
            expected_status = (
                PermissionStatus.DENIED if cmd.decision == "deny" else PermissionStatus.APPROVED
            )
            if (
                str(req.run_id) != cmd.run_id
                or str(req.task_id) != cmd.task_id
                or req.decision != cmd.decision
                or req.status != expected_status
                or not req.checkpoint
            ):
                log.warning(
                    "permission resume 校验失败，不 ack: request_id=%s status=%s decision=%s",
                    cmd.request_id,
                    req.status.value,
                    req.decision,
                )
                return

            try:
                validate_permission_checkpoint(
                    req.checkpoint,
                    expected_request_id=cmd.request_id,
                    expected_task_id=cmd.task_id,
                    expected_run_id=cmd.run_id,
                    expected_step_id=str(req.step_id) if req.step_id is not None else "",
                    expected_tool_call_id=(
                        str(req.tool_call_id) if req.tool_call_id is not None else ""
                    ),
                    expected_tool_name=req.tool_name,
                )
            except (TypeError, ValueError) as exc:
                is_legacy = req.checkpoint.get("version") != RUN_CHECKPOINT_VERSION
                log.error(
                    "Permission checkpoint 校验失败，拒绝执行工具: "
                    "request_id=%s run_id=%s version=%s reason=%s",
                    cmd.request_id,
                    cmd.run_id,
                    req.checkpoint.get("version"),
                    type(exc).__name__,
                )
                incompatible_job = RunJobMessage(
                    job_id=cmd.command_id,
                    trace_id=cmd.trace_id,
                    task_id=cmd.task_id,
                    run_id=cmd.run_id,
                    user_goal="incompatible permission checkpoint",
                    created_at=cmd.decided_at,
                )
                self._publish_failed_event(
                    incompatible_job,
                    code=(
                        "PERMISSION_CHECKPOINT_INCOMPATIBLE"
                        if is_legacy
                        else "PERMISSION_CHECKPOINT_INVALID"
                    ),
                    message=(
                        "权限恢复检查点版本不兼容，工具未执行，请重新发起任务"
                        if is_legacy
                        else "权限恢复检查点身份校验失败，工具未执行，请重新发起任务"
                    ),
                    recoverable=False,
                )
                self._cmd_consumer.ack(msg_id)
                return

            if self._run_svc is not None:
                _run, disposition = self._service_bridge.run(
                    self._run_svc.claim_permission_resume(UUID(cmd.run_id), self._runner.worker_id),
                    timeout=10,
                )
                if disposition == "skip":
                    self._cmd_consumer.ack(msg_id)
                    return
                if disposition == "busy":
                    log.info("permission resume 已由其他 Worker 占用，不 ack: %s", cmd.run_id)
                    return
                if disposition == "stale":
                    stale_job = RunJobMessage(
                        job_id=cmd.command_id,
                        trace_id=cmd.trace_id,
                        task_id=cmd.task_id,
                        run_id=cmd.run_id,
                        user_goal="permission resume interrupted",
                        created_at=cmd.decided_at,
                    )
                    effect_unknown = cmd.decision != "deny"
                    self._publish_failed_event(
                        stale_job,
                        code=(
                            "PERMISSION_RESUME_EFFECT_UNKNOWN"
                            if effect_unknown
                            else "PERMISSION_DENIAL_INTERRUPTED"
                        ),
                        message=(
                            "获批工具的执行结果未知，为避免重复副作用已安全终止"
                            if effect_unknown
                            else "拒绝权限后的收口处理中断，工具未执行"
                        ),
                        recoverable=False,
                    )
                    self._cmd_consumer.ack(msg_id)
                    return

            self._set_active_run_id(cmd.run_id)
            self._set_cancel_requested(False)
            self._poll_stop.clear()
            if self._heartbeat:
                self._heartbeat.set_active_run_id(cmd.run_id)
                self._heartbeat.set_status("busy")
                self._heartbeat.publish_now()

            poll_thread = self._start_command_poll_thread()
            try:
                published_ids: set[str] = set()

                def publish_cb(env):
                    self._publish_and_track(env, published_ids)

                envelopes = resume(
                    req.checkpoint,
                    cmd.decision,
                    cancel_check=lambda: self.cancel_requested,
                    publish_cb=publish_cb,
                )
                for env in envelopes:
                    if env.event_id in published_ids:
                        continue
                    self._publish_or_persist(env)
                if self._cmd_consumer.ack(msg_id):
                    log.info(
                        "permission checkpoint 恢复完成并 ack: request_id=%s events=%d",
                        cmd.request_id,
                        len(envelopes),
                    )
            finally:
                self._poll_stop.set()
                if poll_thread is not None:
                    poll_thread.join(timeout=3.0)
                self._finish_job()
        except Exception:
            log.exception("permission checkpoint 恢复失败，不 ack: request_id=%s", cmd.request_id)
            self._finish_job(failed=True)

    def _cancel_idle_run(self, cmd, msg_id: str) -> None:
        """收口等待权限期间的取消命令。"""
        if self._run_svc is None or self._service_bridge is None:
            log.info("cancel 无 active run 且持久化服务不可用，不 ack: %s", cmd.run_id)
            return
        try:
            run = self._service_bridge.run(self._run_svc.get_run(UUID(cmd.run_id)), timeout=10)
            if run is None:
                log.info("idle cancel Run 不存在，不 ack: run_id=%s", cmd.run_id)
                return
            if str(run.task_id) != cmd.task_id:
                log.warning("idle cancel command task 身份不匹配，不 ack: run_id=%s", cmd.run_id)
                return
            if run.status in (RunStatus.COMPLETED, RunStatus.FAILED, RunStatus.CANCELLED):
                log.info(
                    "idle cancel Run 已终态，幂等 ack: run_id=%s status=%s",
                    cmd.run_id,
                    run.status.value,
                )
                self._cmd_consumer.ack(msg_id)
                return
            if run.status != RunStatus.CANCEL_REQUESTED:
                log.info("idle cancel 状态不匹配，不 ack: run_id=%s", cmd.run_id)
                return
            job = RunJobMessage(
                job_id=cmd.command_id,
                trace_id=cmd.trace_id,
                task_id=cmd.task_id,
                run_id=cmd.run_id,
                user_goal="cancel",
                created_at=cmd.requested_at,
            )
            self._publish_cancelled_event(job)
            self._cmd_consumer.ack(msg_id)
        except Exception:
            log.exception("idle cancel 收口失败，不 ack: run_id=%s", cmd.run_id)
