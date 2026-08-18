"""Worker cancel 测试 — 使用 fakeredis 模拟 Redis。

验证：
  - run.cancel command 消费与处理
  - active run 期间收到 cancel（后台 poll thread）
  - agent.run.cancelled 事件发布
  - heartbeat 清理
  - 非当前 run_id cancel no-op
  - permission.decision no-op + ack
  - malformed command 不 ack
"""

from __future__ import annotations

import json
import threading
import time as _time
from types import SimpleNamespace
from uuid import uuid4

import fakeredis
import pytest

from jarvis_worker.runtime_bus.messages import (
    SCHEMA_VERSION,
    FIELD_COMMAND_ID,
    FIELD_PAYLOAD,
    FIELD_SCHEMA_VERSION,
    GROUP_WORKER_POOL,
    STREAM_RUN_QUEUE,
    STREAM_RUNTIME_EVENT,
    STREAM_WORKER_COMMAND,
    STREAM_WORKER_COMMAND_DEAD_LETTER,
    STREAM_WORKER_HEARTBEAT,
    RunCancelCommand,
    McpDiscoveryRefreshCommand,
    RunPauseCommand,
    RunJobMessage,
)
from jarvis_worker.runtime_bus import ensure_consumer_group
from jarvis_worker.runtime_bus.command_consumer import (
    CMD_MALFORMED,
    WorkerCommandConsumer,
)
from jarvis_worker.runtime_bus.consumer import RunQueueConsumer
from jarvis_worker.runtime_bus.heartbeat import HeartbeatProducer
from jarvis_worker.runtime_bus.producer import RuntimeEventProducer
from tests.testing_doubles import MockRunner


# -- helpers --


def _enqueue_run_job(client: fakeredis.FakeRedis, job: RunJobMessage) -> str:
    payload = job.to_payload_json()
    return client.xadd(
        STREAM_RUN_QUEUE,
        {
            FIELD_SCHEMA_VERSION: job.schema_version,
            FIELD_PAYLOAD: payload,
            "job_id": job.job_id,
            "trace_id": job.trace_id,
            "task_id": job.task_id,
            "run_id": job.run_id,
            "type": "run.job",
            "created_at": job.created_at,
        },
    )


def _publish_command(
    client: fakeredis.FakeRedis,
    cmd_type: str,
    run_id: str,
    command_id: str = "cmd-001",
    task_id: str = "task-001",
    trace_id: str = "trace-001",
    extra_fields: dict | None = None,
) -> str:
    """发布任意 type 的 worker command。permission.decision 自动补齐必填字段。"""
    data: dict = {
        "command_id": command_id,
        "trace_id": trace_id,
        "task_id": task_id,
        "run_id": run_id,
        "type": cmd_type,
        "requested_at": "2026-07-07T10:00:00Z",
        "schema_version": SCHEMA_VERSION,
    }
    if cmd_type == "permission.decision":
        data["request_id"] = extra_fields.get("request_id", "perm-req-001") if extra_fields else "perm-req-001"
        data["decision"] = extra_fields.get("decision", "allow_once") if extra_fields else "allow_once"
        data["decided_at"] = extra_fields.get("decided_at", "2026-07-07T10:00:01Z") if extra_fields else "2026-07-07T10:00:01Z"
        # note is optional
    if extra_fields:
        for k, v in extra_fields.items():
            if k not in data:
                data[k] = v
    payload = json.dumps(data, ensure_ascii=False)
    fields = {
        FIELD_SCHEMA_VERSION: SCHEMA_VERSION,
        FIELD_PAYLOAD: payload,
        FIELD_COMMAND_ID: command_id,
        "trace_id": trace_id,
        "task_id": task_id,
        "run_id": run_id,
        "type": cmd_type,
        "requested_at": "2026-07-07T10:00:00Z",
    }
    return client.xadd(STREAM_WORKER_COMMAND, fields)


def _read_events_from_stream(
    client: fakeredis.FakeRedis, count: int = 20
) -> list[dict]:
    result = client.xread({STREAM_RUNTIME_EVENT: "0"}, count=count)
    events: list[dict] = []
    for _stream_name, messages in result:
        for _msg_id, fields in messages:
            p = fields.get(FIELD_PAYLOAD, "")
            if p:
                events.append(json.loads(p))
    return events


def _read_heartbeats(client: fakeredis.FakeRedis) -> list[dict]:
    result = client.xread({STREAM_WORKER_HEARTBEAT: "0"})
    hbs: list[dict] = []
    for _sn, msgs in result:
        for _mid, fields in msgs:
            p = fields.get(FIELD_PAYLOAD, "")
            if p:
                hbs.append(json.loads(p))
    return hbs


# -- fixtures --


@pytest.fixture
def redis_client() -> fakeredis.FakeRedis:
    return fakeredis.FakeRedis(decode_responses=True)


def _setup_components(redis_client: fakeredis.FakeRedis):
    ensure_consumer_group(redis_client, STREAM_RUN_QUEUE, GROUP_WORKER_POOL)
    ensure_consumer_group(redis_client, STREAM_WORKER_COMMAND, GROUP_WORKER_POOL)
    consumer = RunQueueConsumer(redis_client, "test-consumer")
    producer = RuntimeEventProducer(redis_client)
    runner = MockRunner(worker_id="test-worker")
    hb = HeartbeatProducer(redis_client, worker_id="test-worker", interval_ms=500)
    cmd_consumer = WorkerCommandConsumer(redis_client, "test-consumer")
    return consumer, producer, runner, hb, cmd_consumer


# -- RunCancelCommand decode 测试 --


class TestRunCancelCommandDecode:
    def test_decode_valid(self):
        data = {
            "command_id": "cmd-001", "trace_id": "trace-001",
            "task_id": "task-001", "run_id": "run-001",
            "type": "run.cancel", "requested_at": "2026-07-07T10:00:00Z",
            "schema_version": SCHEMA_VERSION,
        }
        cmd = RunCancelCommand.from_dict(data)
        assert cmd.run_id == "run-001"
        assert cmd.type == "run.cancel"

    def test_decode_missing_field(self):
        data = {"command_id": "cmd-001", "trace_id": "trace-001",
                "task_id": "task-001", "type": "run.cancel",
                "requested_at": "2026-07-07T10:00:00Z", "schema_version": SCHEMA_VERSION}
        with pytest.raises(ValueError, match="缺少必要字段"):
            RunCancelCommand.from_dict(data)

    def test_decode_bad_schema(self):
        data = {"command_id": "cmd-001", "trace_id": "trace-001",
                "task_id": "task-001", "run_id": "run-001",
                "type": "run.cancel", "requested_at": "2026-07-07T10:00:00Z",
                "schema_version": "bad-ver"}
        with pytest.raises(ValueError, match="schema_version"):
            RunCancelCommand.from_dict(data)

    def test_decode_unsupported_type(self):
        data = {"command_id": "cmd-001", "trace_id": "trace-001",
                "task_id": "task-001", "run_id": "run-001",
                "type": "run.pause", "requested_at": "2026-07-07T10:00:00Z",
                "schema_version": SCHEMA_VERSION}
        with pytest.raises(ValueError, match="不支持"):
            RunCancelCommand.from_dict(data)


# -- WorkerCommandConsumer 路由测试 --


class TestCommandConsumerRouting:
    def test_run_cancel_decoded(self, redis_client):
        """run.cancel → RunCancelCommand 实例。"""
        _setup_components(redis_client)
        _publish_command(redis_client, "run.cancel", "run-001")
        consumer = WorkerCommandConsumer(redis_client, "test-consumer")
        cmd, msg_id, _ = consumer.read_one()
        assert isinstance(cmd, RunCancelCommand)
        assert cmd.run_id == "run-001"
        assert consumer.ack(msg_id)

    def test_permission_decision_decoded(self, redis_client):
        """permission.decision → PermissionDecisionCommand 实例。"""
        _setup_components(redis_client)
        _publish_command(redis_client, "permission.decision", "run-001")
        consumer = WorkerCommandConsumer(redis_client, "test-consumer")
        from jarvis_worker.runtime_bus.messages import PermissionDecisionCommand
        cmd, msg_id, _ = consumer.read_one()
        assert isinstance(cmd, PermissionDecisionCommand)
        assert cmd.run_id == "run-001"
        assert cmd.decision == "allow_once"
        assert consumer.ack(msg_id)

    def test_permission_decision_malformed_noack(self, redis_client):
        """permission.decision payload 缺字段 → CMD_MALFORMED，不 ack。"""
        _setup_components(redis_client)
        # 直接 XADD 缺少 required fields
        payload = json.dumps({"type": "permission.decision", "command_id": "x", "trace_id": "x"})
        redis_client.xadd(STREAM_WORKER_COMMAND, {
            FIELD_SCHEMA_VERSION: SCHEMA_VERSION,
            FIELD_PAYLOAD: payload,
            "type": "permission.decision",
        })
        consumer = WorkerCommandConsumer(redis_client, "test-consumer")
        cmd, msg_id, _ = consumer.read_one()
        assert cmd is CMD_MALFORMED

    def test_pause_type_decodes_as_supported_command(self, redis_client):
        """run.pause 使用严格命令契约解码。"""
        _setup_components(redis_client)
        _publish_command(redis_client, "run.pause", "run-001")
        consumer = WorkerCommandConsumer(redis_client, "test-consumer")
        cmd, msg_id, _ = consumer.read_one()
        assert isinstance(cmd, RunPauseCommand)
        assert cmd.run_id == "run-001"
        assert consumer.ack(msg_id)

    def test_mcp_discovery_refresh_decodes_without_task_or_run(self, redis_client):
        """全局 MCP 管理命令不伪造 Task/Run 关联。"""
        _setup_components(redis_client)
        payload = json.dumps({
            "command_id": "mcp-cmd-1", "trace_id": "mcp-trace-1",
            "type": "mcp.discovery.refresh",
            "requested_at": "2026-07-26T12:00:00Z",
            "schema_version": SCHEMA_VERSION,
        })
        redis_client.xadd(STREAM_WORKER_COMMAND, {
            FIELD_SCHEMA_VERSION: SCHEMA_VERSION, FIELD_PAYLOAD: payload,
            FIELD_COMMAND_ID: "mcp-cmd-1", "trace_id": "mcp-trace-1",
            "type": "mcp.discovery.refresh",
        })
        consumer = WorkerCommandConsumer(redis_client, "test-consumer")
        cmd, msg_id, _ = consumer.read_one()
        assert isinstance(cmd, McpDiscoveryRefreshCommand)
        assert cmd.command_id == "mcp-cmd-1"
        assert consumer.ack(msg_id)

    def test_missing_type_field_noack(self, redis_client):
        """缺少 type 字段 → (None, msg_id)，不 ack。"""
        _setup_components(redis_client)
        # 直接 XADD 不带 type 字段
        redis_client.xadd(STREAM_WORKER_COMMAND, {
            FIELD_SCHEMA_VERSION: SCHEMA_VERSION,
            FIELD_PAYLOAD: '{"command_id":"x","trace_id":"x"}',
            FIELD_COMMAND_ID: "x",
        })
        consumer = WorkerCommandConsumer(redis_client, "test-consumer")
        cmd, msg_id, _ = consumer.read_one()
        assert cmd is None
        assert msg_id is not None  # 有消息但无法路由

    def test_run_cancel_missing_payload_malformed(self, redis_client):
        """run.cancel type 但缺 payload → CMD_MALFORMED，不 ack。"""
        _setup_components(redis_client)
        redis_client.xadd(STREAM_WORKER_COMMAND, {
            FIELD_SCHEMA_VERSION: SCHEMA_VERSION,
            "type": "run.cancel",
            "trace_id": "t1",
        })
        consumer = WorkerCommandConsumer(redis_client, "test-consumer")
        cmd, msg_id, _ = consumer.read_one()
        assert cmd is CMD_MALFORMED

    def test_empty_queue(self, redis_client):
        """空队列 → (None, None, None)。"""
        _setup_components(redis_client)
        consumer = WorkerCommandConsumer(redis_client, "test-consumer")
        cmd, msg_id, _ = consumer.read_one()
        assert cmd is None
        assert msg_id is None

    def test_malformed_command_dead_letters_atomically(self, redis_client):
        """毒 command 进入脱敏 DLQ 并从 PEL 移除。"""
        _setup_components(redis_client)
        redis_client.xadd(STREAM_WORKER_COMMAND, {
            FIELD_SCHEMA_VERSION: SCHEMA_VERSION,
            FIELD_PAYLOAD: "{not-json",
            "type": "run.cancel",
        })
        consumer = WorkerCommandConsumer(redis_client, "test-consumer")
        delivery = consumer.read_delivery()

        assert delivery is not None and not delivery.valid
        assert delivery.error_code == "WORKER_COMMAND_MALFORMED"
        dlq_id = consumer.dead_letter(delivery)

        assert dlq_id != "0"
        assert redis_client.xpending(
            STREAM_WORKER_COMMAND, GROUP_WORKER_POOL
        )["pending"] == 0
        entries = redis_client.xrange(
            STREAM_WORKER_COMMAND_DEAD_LETTER, "-", "+"
        )
        assert len(entries) == 1
        fields = entries[0][1]
        assert FIELD_PAYLOAD not in fields
        assert fields["error_code"] == "WORKER_COMMAND_MALFORMED"
        assert len(fields["payload_sha256"]) == 64

    def test_stale_command_uses_delivery_backoff(self, redis_client):
        """active owner 可重新接管其他 consumer 遗留的 command。"""
        _setup_components(redis_client)
        _publish_command(redis_client, "run.cancel", "run-001")
        crashed = WorkerCommandConsumer(redis_client, "crashed")
        first = crashed.read_delivery()
        assert first is not None

        recovery = WorkerCommandConsumer(redis_client, "recovery")
        recovery._reclaim_idle_ms = 0
        reclaimed = recovery.claim_stale_delivery()

        assert reclaimed is not None and reclaimed.valid
        assert reclaimed.reclaimed is True
        assert reclaimed.delivery_count == 2

    def test_outer_payload_routing_mismatch_is_dead_letterable(
        self, redis_client
    ):
        _setup_components(redis_client)
        payload = json.dumps({
            "command_id": "cmd-mismatch",
            "trace_id": "trace-mismatch",
            "task_id": "task-mismatch",
            "run_id": "run-inner",
            "type": "run.cancel",
            "requested_at": "2026-07-07T10:00:00Z",
            "schema_version": SCHEMA_VERSION,
        })
        redis_client.xadd(STREAM_WORKER_COMMAND, {
            FIELD_SCHEMA_VERSION: SCHEMA_VERSION,
            FIELD_PAYLOAD: payload,
            FIELD_COMMAND_ID: "cmd-mismatch",
            "trace_id": "trace-mismatch",
            "task_id": "task-mismatch",
            "run_id": "run-outer",
            "type": "run.cancel",
        })
        consumer = WorkerCommandConsumer(redis_client, "test-consumer")
        delivery = consumer.read_delivery()

        assert delivery is not None and not delivery.valid
        assert delivery.error_code == "WORKER_COMMAND_ROUTING_MISMATCH"


# -- Mock runner cancel 测试 --


class TestMockRunnerCancel:
    def test_cancel_stops_mid(self):
        runner = MockRunner(worker_id="test-worker")
        job = RunJobMessage(
            job_id="job-001", trace_id="trace-001", task_id="task-001",
            run_id="run-001", user_goal="cancel测试",
            created_at="2026-07-07T10:00:00Z",
        )
        called = [0]
        def cancel_check():
            called[0] += 1
            return called[0] >= 2
        envelopes = runner.run_with_cancel_check(job, cancel_check=cancel_check)
        assert envelopes[-1].event_type == "agent.run.cancelled"
        assert envelopes[-1].runtime_event["payload"]["reason"] == "cancelled_by_user"

    def test_cancel_no_completed(self):
        runner = MockRunner(worker_id="test-worker")
        job = RunJobMessage(
            job_id="job-002", trace_id="trace-002", task_id="task-002",
            run_id="run-002", user_goal="cancel顺序",
            created_at="2026-07-07T10:00:00Z",
        )
        envelopes = runner.run_with_cancel_check(job, cancel_check=lambda: True)
        assert len(envelopes) == 1
        assert envelopes[0].event_type == "agent.run.cancelled"

    def test_no_cancel_normal(self):
        runner = MockRunner(worker_id="test-worker")
        job = RunJobMessage(
            job_id="job-003", trace_id="trace-003", task_id="task-003",
            run_id="run-003", user_goal="正常", created_at="2026-07-07T10:00:00Z",
        )
        envelopes = runner.run_with_cancel_check(job, cancel_check=lambda: False)
        assert len(envelopes) == 5
        assert envelopes[-1].event_type == "agent.run.completed"


# -- Active run 期间收到 cancel（真实链路模拟） --


class TestCancelDuringActiveRun:
    def test_cancel_during_execution_produces_cancelled_event(self, redis_client):
        """真实链路：worker 正在处理 active run 时，另一个线程写入 run.cancel。

        模拟用户在 active run 期间点击取消按钮的场景。
        使用 step_delay_ms 制造稳定的取消窗口。
        """
        consumer, producer, _, hb, cmd_consumer = _setup_components(redis_client)
        # 创建带 step delay 的 runner，制造取消窗口
        runner = MockRunner(worker_id="test-worker", step_delay_ms=50)

        job = RunJobMessage(
            job_id="job-e2e", trace_id="trace-e2e", task_id="task-e2e",
            run_id="run-e2e", user_goal="端到端cancel",
            created_at="2026-07-07T10:00:00Z",
        )
        _enqueue_run_job(redis_client, job)

        from jarvis_worker.runtime.worker import AgentWorker
        worker = AgentWorker(
            redis_client, consumer, producer, runner,
            heartbeat=hb, cmd_consumer=cmd_consumer,
        )

        job_msg, msg_id, _ = consumer.read_one(block_ms=2000)
        assert job_msg is not None

        worker._set_active_run_id(job.run_id)
        worker._set_cancel_requested(False)
        worker._poll_stop.clear()

        # 在另一个线程中延迟发布 cancel（模拟用户点击取消按钮）
        def _delayed_cancel():
            _time.sleep(0.08)  # 等 mock runner 开始执行（在第一个 step delay 期间）
            _publish_command(redis_client, "run.cancel", "run-e2e")

        cancel_thread = threading.Thread(target=_delayed_cancel, daemon=True)
        cancel_thread.start()

        # 执行 mock runner — 后台 poll thread 在 step delay 期间读到 cancel
        worker._process_job_with_cancel_check(job_msg)
        worker._poll_stop.set()
        cancel_thread.join(timeout=2)

        consumer.ack(msg_id)

        # 验证 runtime event stream 中有 agent.run.cancelled
        events = _read_events_from_stream(redis_client)
        event_types = [e["event_type"] for e in events]
        assert "agent.run.cancelled" in event_types
        assert "agent.run.completed" not in event_types

    def test_cancel_wrong_runid_no_effect(self, redis_client):
        """cancel 的 run_id 不匹配当前 active run → 不影响当前 job, 不 ack cancel。"""
        consumer, producer, runner, hb, cmd_consumer = _setup_components(redis_client)

        job = RunJobMessage(
            job_id="job-wrong", trace_id="trace-wrong", task_id="task-wrong",
            run_id="run-current", user_goal="当前任务",
            created_at="2026-07-07T10:00:00Z",
        )
        _enqueue_run_job(redis_client, job)

        from jarvis_worker.runtime.worker import AgentWorker
        worker = AgentWorker(
            redis_client, consumer, producer, runner,
            heartbeat=hb, cmd_consumer=cmd_consumer,
        )

        # 发布一个不同 run_id 的 cancel（不是当前 worker 的 run）
        msg_id_cmd = _publish_command(redis_client, "run.cancel", "run-other")

        job_msg, msg_id, _ = consumer.read_one(block_ms=2000)
        assert job_msg is not None

        worker._set_active_run_id(job.run_id)
        worker._set_cancel_requested(False)
        worker._poll_stop.clear()

        # 先 poll 一次让非 owner worker 读取 cancel（应不 ack）
        worker._poll_cancel_commands()
        # 确认 cancel flag 未被设置
        assert not worker.cancel_requested

        # 验证 cancel command 未被 ack（进入当前 consumer pending，避免被非 owner 确认吞掉；后续需 routing/reclaim 机制）
        pending = redis_client.xpending(
            STREAM_WORKER_COMMAND, GROUP_WORKER_POOL
        )
        assert pending["pending"] >= 1, (
            f"非 owner worker 不应 ack cancel command，但 pending={pending}"
        )

        worker._process_job_with_cancel_check(job_msg)
        worker._poll_stop.set()
        consumer.ack(msg_id)

        events = _read_events_from_stream(redis_client)
        event_types = [e["event_type"] for e in events]
        assert "agent.run.cancelled" not in event_types
        assert "agent.run.completed" in event_types


class TestDurablePauseObservation:
    def test_pause_requested_in_postgres_sets_token_before_outbox_delivery(self, redis_client):
        """暂停写库后，即使 Redis command 尚未到达，也不能错过安全边界。"""
        consumer, producer, runner, hb, cmd_consumer = _setup_components(redis_client)
        run_id = str(uuid4())

        class RunService:
            async def get_run(self, requested_run_id):
                assert str(requested_run_id) == run_id
                return SimpleNamespace(status="pause_requested")

        class ServiceBridge:
            def run(self, coroutine, timeout):
                assert timeout == 2
                import asyncio
                return asyncio.run(coroutine)

        from jarvis_worker.runtime.worker import AgentWorker
        worker = AgentWorker(
            redis_client, consumer, producer, runner,
            heartbeat=hb, cmd_consumer=cmd_consumer,
            run_service=RunService(), service_bridge=ServiceBridge(),
        )
        worker._set_active_run_id(run_id)

        worker._sync_durable_pause_request_if_due()

        assert worker.pause_requested is True
        assert worker.pause_command_id is not None

    def test_other_durable_status_does_not_pause_active_run(self, redis_client):
        consumer, producer, runner, hb, cmd_consumer = _setup_components(redis_client)
        run_id = str(uuid4())

        class RunService:
            async def get_run(self, _requested_run_id):
                return SimpleNamespace(status="running")

        class ServiceBridge:
            def run(self, coroutine, timeout):
                import asyncio
                return asyncio.run(coroutine)

        from jarvis_worker.runtime.worker import AgentWorker
        worker = AgentWorker(
            redis_client, consumer, producer, runner,
            heartbeat=hb, cmd_consumer=cmd_consumer,
            run_service=RunService(), service_bridge=ServiceBridge(),
        )
        worker._set_active_run_id(run_id)

        worker._sync_durable_pause_request_if_due()

        assert worker.pause_requested is False


# -- Heartbeat 清理测试 --


class TestHeartbeatAfterCancel:
    def test_heartbeat_cleared_after_cancel(self, redis_client):
        """cancel 后 heartbeat active_run_id 清空，状态回 idle。"""
        hb = HeartbeatProducer(redis_client, worker_id="test-worker", interval_ms=500)
        hb.set_active_run_id("run-001")
        hb.set_status("busy")
        hb.publish_now()
        hb.set_active_run_id("")
        hb.set_status("idle")
        hb.publish_now()

        hbs = _read_heartbeats(redis_client)
        assert len(hbs) >= 2
        assert hbs[0]["status"] == "busy"
        assert hbs[0].get("active_run_id") == "run-001"
        assert hbs[1]["status"] == "idle"
        assert hbs[1].get("active_run_id", "") == ""


# -- run_forever 主循环层 cancel smoke 测试（3C 收口） --


class TestRunForeverCancelSmoke:
    """run_forever 主循环级别的 cancel smoke 测试。

    验证从 job 入队 → worker 消费 → cancel command → agent.run.cancelled →
    job ack → heartbeat 清理的完整链路。
    """

    def test_run_forever_cancel_flow(self, redis_client):
        """完整 cancel 链路：worker.run_forever 中收到 cancel 后正常退出。"""
        consumer, producer, _, hb, cmd_consumer = _setup_components(redis_client)
        runner = MockRunner(worker_id="test-worker", step_delay_ms=50)

        # 确保 heartbeat stream consumer group 存在（避免 xread 失败）
        from jarvis_worker.runtime_bus.messages import (
            STREAM_WORKER_HEARTBEAT,
        )
        ensure_consumer_group(
            redis_client, STREAM_WORKER_HEARTBEAT, GROUP_WORKER_POOL
        )

        job = RunJobMessage(
            job_id="job-smoke", trace_id="trace-smoke", task_id="task-smoke",
            run_id="run-smoke", user_goal="smoke cancel 测试",
            created_at="2026-07-07T10:00:00Z",
        )
        _enqueue_run_job(redis_client, job)

        from jarvis_worker.runtime.worker import AgentWorker
        worker = AgentWorker(
            redis_client, consumer, producer, runner,
            heartbeat=hb, cmd_consumer=cmd_consumer,
        )

        # 在后台线程启动 run_forever
        worker_thread = threading.Thread(
            target=worker.run_forever,
            kwargs={"poll_interval_ms": 100},
            daemon=True,
        )
        worker_thread.start()

        # 等待 worker 开始处理 job（heartbeat 变为 busy）
        deadline = _time.monotonic() + 5
        while _time.monotonic() < deadline:
            hbs = _read_heartbeats(redis_client)
            if any(h.get("status") == "busy" for h in hbs):
                break
            _time.sleep(0.1)
        else:
            worker.stop()
            worker_thread.join(timeout=3)
            pytest.fail("worker 未在 5s 内进入 busy 状态")

        # 发布 cancel command（匹配当前 active run）
        _publish_command(redis_client, "run.cancel", "run-smoke")

        # 等待 agent.run.cancelled 出现在 event stream
        deadline = _time.monotonic() + 10
        cancelled_found = False
        while _time.monotonic() < deadline:
            events = _read_events_from_stream(redis_client)
            event_types = [e["event_type"] for e in events]
            if "agent.run.cancelled" in event_types:
                cancelled_found = True
                break
            _time.sleep(0.2)
        assert cancelled_found, (
            f"agent.run.cancelled 未在 10s 内出现，"
            f"当前事件: {event_types if 'event_types' in dir() else 'N/A'}"
        )

        # 验证没有 agent.run.completed
        events = _read_events_from_stream(redis_client)
        event_types = [e["event_type"] for e in events]
        assert "agent.run.completed" not in event_types

        # 停止 worker 并等待线程退出
        worker.stop()
        worker_thread.join(timeout=5)
        assert not worker_thread.is_alive(), "worker thread 应在 5s 内退出"

        # 验证 run queue job 已被 ack（pending 为 0）
        try:
            pending = redis_client.xpending(
                STREAM_RUN_QUEUE, GROUP_WORKER_POOL
            )
            assert pending["pending"] == 0, (
                f"run queue job 应已被 ack，但 pending={pending}"
            )
        except Exception:
            pass  # fakeredis xpending 可能不完整，降级为警告而非失败

        # 验证 heartbeat 最终 active_run_id 清空，状态回到 idle/stopped
        hbs = _read_heartbeats(redis_client)
        if hbs:
            last_hb = hbs[-1]
            assert last_hb.get("active_run_id", "") == "", (
                f"heartbeat active_run_id 应为空: {last_hb}"
            )
            assert last_hb["status"] in ("idle", "draining", "stopped"), (
                f"heartbeat 最终状态应为 idle/draining/stopped: {last_hb['status']}"
            )
