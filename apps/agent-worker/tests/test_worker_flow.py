"""Worker 端到端流程测试 — 使用 fakeredis 模拟 Redis。

验证从 run job 消费到 RuntimeEvent 生产的完整链路。
"""

from __future__ import annotations

import json
import time

import fakeredis
import pytest

from jarvis_worker.runtime_bus.messages import (
    SCHEMA_VERSION,
    FIELD_PAYLOAD,
    FIELD_SCHEMA_VERSION,
    GROUP_WORKER_POOL,
    STREAM_RUN_QUEUE,
    STREAM_RUNTIME_EVENT,
    RunJobMessage,
)
from jarvis_worker.runtime_bus import ensure_consumer_group
from jarvis_worker.runtime_bus.consumer import RunQueueConsumer
from jarvis_worker.runtime_bus.producer import RuntimeEventProducer
from tests.testing_doubles import MockRunner


# -- helpers --


def _enqueue_run_job(client: fakeredis.FakeRedis, job: RunJobMessage) -> str:
    """模拟 Go 侧 EnqueueRunJob：XADD RunJobMessage 到 run queue。

    对齐 Go 侧 RunJobToStreamFields 输出：
      schema_version + payload（完整 JSON）+ 冗余标量路由字段
    """
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


def _read_events_from_stream(
    client: fakeredis.FakeRedis, count: int = 20
) -> list[dict]:
    """从 runtime event stream 读取事件（使用 XREAD，非 consumer group）。

    返回 payload JSON 解码后的 dict 列表。
    """
    result = client.xread({STREAM_RUNTIME_EVENT: "0"}, count=count)
    events: list[dict] = []
    for _stream_name, messages in result:
        for _msg_id, fields in messages:
            payload = fields.get(FIELD_PAYLOAD, "")
            if payload:
                events.append(json.loads(payload))
    return events


# -- fixtures --


@pytest.fixture
def redis_client() -> fakeredis.FakeRedis:
    """创建 fakeredis 实例。"""
    return fakeredis.FakeRedis(decode_responses=True)


@pytest.fixture
def worker_components(redis_client: fakeredis.FakeRedis):
    """创建完整的 worker 组件栈（consumer + producer + runner）。"""
    consumer = RunQueueConsumer(redis_client, "test-consumer")
    producer = RuntimeEventProducer(redis_client)
    runner = MockRunner(worker_id="test-worker")
    return redis_client, consumer, producer, runner


# -- 测试 --


class TestConsumerDecode:
    """Consumer 解码测试。"""

    def test_consume_valid_run_job(self, redis_client, worker_components):
        """入队合法 RunJobMessage → consumer 正确解码。"""
        client, consumer, _, _ = worker_components

        # 确保 consumer group 存在
        ensure_consumer_group(client, STREAM_RUN_QUEUE, GROUP_WORKER_POOL)

        # 入队
        job = RunJobMessage(
            job_id="job-001",
            trace_id="trace-001",
            task_id="task-001",
            run_id="run-001",
            user_goal="测试任务",
            created_at="2026-07-06T10:00:00Z",
        )
        _enqueue_run_job(client, job)

        # 消费
        consumed, msg_id, stream = consumer.read_one()
        assert consumed is not None
        assert consumed.job_id == "job-001"
        assert consumed.trace_id == "trace-001"
        assert consumed.user_goal == "测试任务"
        assert msg_id is not None

        # ack
        assert consumer.ack(msg_id)

    def test_resume_job_round_trip(self, redis_client, worker_components):
        client, consumer, _, _ = worker_components
        ensure_consumer_group(client, STREAM_RUN_QUEUE, GROUP_WORKER_POOL)
        job = RunJobMessage(
            job_id="job-resume", trace_id="trace-resume",
            task_id="task-resume", run_id="run-resume",
            user_goal="resume", created_at="2026-07-21T00:00:00Z",
            resume_from_checkpoint=True,
        )
        _enqueue_run_job(client, job)

        consumed, msg_id, _stream = consumer.read_one()

        assert consumed is not None
        assert consumed.resume_from_checkpoint is True
        assert consumed.to_dict()["resume_from_checkpoint"] is True
        assert consumer.ack(msg_id)

    def test_consume_empty_queue(self, worker_components):
        """空队列 → 返回 (None, None, None)。"""
        client, consumer, _, _ = worker_components
        ensure_consumer_group(client, STREAM_RUN_QUEUE, GROUP_WORKER_POOL)
        consumed, msg_id, stream = consumer.read_one()
        assert consumed is None
        assert msg_id is None


class TestProducerPublish:
    """Producer 发布测试。"""

    def test_publish_event_appears_in_stream(self, worker_components):
        """发布 RuntimeEventEnvelope → runtime event stream 中有对应事件。"""
        client, _, producer, runner = worker_components

        job = RunJobMessage(
            job_id="job-001",
            trace_id="trace-001",
            task_id="task-001",
            run_id="run-001",
            user_goal="测试",
            created_at="2026-07-06T10:00:00Z",
        )

        envelopes = runner.run(job)
        assert len(envelopes) == 5

        # 发布所有事件
        for env in envelopes:
            msg_id = producer.publish(env)
            assert msg_id is not None

        # 读取 stream 验证
        events = _read_events_from_stream(client)
        assert len(events) == 5

        # 验证事件类型顺序
        types = [e["event_type"] for e in events]
        assert types == [
            "agent.run.started",
            "agent.step.started",
            "model.delta",
            "agent.step.completed",
            "agent.run.completed",
        ]

    def test_envelope_consistency_in_stream(self, worker_components):
        """Stream 中事件的 envelope 与内层 runtime_event 一致性。"""
        client, _, producer, runner = worker_components

        job = RunJobMessage(
            job_id="job-consistency",
            trace_id="trace-consistency",
            task_id="task-consistency",
            run_id="run-consistency",
            user_goal="一致性测试",
            created_at="2026-07-06T10:00:00Z",
        )

        envelopes = runner.run(job)
        for env in envelopes:
            producer.publish(env)

        events = _read_events_from_stream(client)
        for e in events:
            re = e["runtime_event"]
            assert e["event_id"] == re["id"], f"event_id 不一致: {e['event_id']} vs {re['id']}"
            assert e["event_type"] == re["type"], f"event_type 不一致"
            assert e["task_id"] == re["task_id"], f"task_id 不一致"
            assert e["run_id"] == re["run_id"], f"run_id 不一致"
            assert e["trace_id"] == "trace-consistency"


class TestMockRunner:
    """Mock runner 事件序列测试。"""

    def test_event_count(self, worker_components):
        """Mock runner 产生恰好 5 个事件。"""
        _, _, _, runner = worker_components

        job = RunJobMessage(
            job_id="job-001",
            trace_id="trace-001",
            task_id="task-001",
            run_id="run-001",
            user_goal="测试",
            created_at="2026-07-06T10:00:00Z",
        )

        envelopes = runner.run(job)
        assert len(envelopes) == 5

    def test_event_order(self, worker_components):
        """事件顺序固定：started → step.started → delta → step.completed → run.completed。"""
        _, _, _, runner = worker_components

        job = RunJobMessage(
            job_id="job-001",
            trace_id="trace-001",
            task_id="task-001",
            run_id="run-001",
            user_goal="测试",
            created_at="2026-07-06T10:00:00Z",
        )

        envelopes = runner.run(job)
        types = [e.event_type for e in envelopes]
        assert types == [
            "agent.run.started",
            "agent.step.started",
            "model.delta",
            "agent.step.completed",
            "agent.run.completed",
        ]

    def test_last_event_is_terminal(self, worker_components):
        """最后一个事件是 terminal event。"""
        _, _, _, runner = worker_components

        job = RunJobMessage(
            job_id="job-001",
            trace_id="trace-001",
            task_id="task-001",
            run_id="run-001",
            user_goal="测试",
            created_at="2026-07-06T10:00:00Z",
        )

        envelopes = runner.run(job)
        assert envelopes[-1].event_type == "agent.run.completed"

    def test_all_envelopes_have_trace_id(self, worker_components):
        """所有 envelope 携带正确的 trace_id。"""
        _, _, _, runner = worker_components

        job = RunJobMessage(
            job_id="job-001",
            trace_id="trace-my-trace",
            task_id="task-001",
            run_id="run-001",
            user_goal="测试",
            created_at="2026-07-06T10:00:00Z",
        )

        envelopes = runner.run(job)
        for env in envelopes:
            assert env.trace_id == "trace-my-trace"

    def test_all_envelopes_pass_validation(self, worker_components):
        """所有 envelope 通过 validate()。"""
        _, _, _, runner = worker_components

        job = RunJobMessage(
            job_id="job-001",
            trace_id="trace-001",
            task_id="task-001",
            run_id="run-001",
            user_goal="测试",
            created_at="2026-07-06T10:00:00Z",
        )

        envelopes = runner.run(job)
        for env in envelopes:
            env.validate()  # 不应抛异常


class TestConsumerGroupIdempotent:
    """Consumer group 幂等性测试。"""

    def test_create_group_idempotent(self, redis_client):
        """重复创建同一 consumer group 不报错。"""
        # 第一次
        ensure_consumer_group(redis_client, STREAM_RUN_QUEUE, GROUP_WORKER_POOL, "0")
        # 第二次 → 不抛异常
        ensure_consumer_group(redis_client, STREAM_RUN_QUEUE, GROUP_WORKER_POOL, "0")


class TestEndToEndFlow:
    """端到端流程：入队 → 消费 → mock runner → 发布 → stream 验证。"""

    def test_full_flow(self, worker_components):
        """完整的 worker flow 端到端测试。"""
        client, consumer, producer, runner = worker_components

        # 1. 确保 consumer group
        ensure_consumer_group(client, STREAM_RUN_QUEUE, GROUP_WORKER_POOL)

        # 2. 入队 run job（模拟 Go Gateway）
        job = RunJobMessage(
            job_id="job-e2e",
            trace_id="trace-e2e",
            task_id="task-e2e",
            run_id="run-e2e",
            user_goal="端到端测试任务",
            created_at="2026-07-06T10:00:00Z",
        )
        _enqueue_run_job(client, job)

        # 3. 消费
        consumed, msg_id, _stream = consumer.read_one()
        assert consumed is not None
        assert consumed.job_id == "job-e2e"

        # 4. 执行 mock runner
        envelopes = runner.run(consumed)
        assert len(envelopes) == 5

        # 5. 发布所有事件
        for env in envelopes:
            producer.publish(env)

        # 6. ack
        assert consumer.ack(msg_id)

        # 7. 验证 runtime event stream 中有 5 个事件
        events = _read_events_from_stream(client)
        assert len(events) == 5

        # 8. 验证事件类型顺序
        types = [e["event_type"] for e in events]
        assert types[-1] == "agent.run.completed"

        # 9. 验证所有事件携带 schema_version
        for e in events:
            assert e["schema_version"] == SCHEMA_VERSION

        # 10. 验证所有事件的 trace_id 与 run job 一致
        for e in events:
            assert e["trace_id"] == "trace-e2e"


class TestDeterministicEventIds:
    """确定性 event id 测试 — 验证重试幂等性。"""

    def test_same_job_produces_same_event_ids(self, worker_components):
        """同一 RunJobMessage 多次 run() 产生相同 event id。"""
        _, _, _, runner = worker_components

        job = RunJobMessage(
            job_id="job-d-1",
            trace_id="trace-d-1",
            task_id="task-d-1",
            run_id="run-d-1",
            user_goal="幂等测试",
            created_at="2026-07-06T10:00:00Z",
        )

        envelopes_1 = runner.run(job)
        envelopes_2 = runner.run(job)

        assert len(envelopes_1) == len(envelopes_2) == 5

        ids_1 = [e.event_id for e in envelopes_1]
        ids_2 = [e.event_id for e in envelopes_2]

        assert ids_1 == ids_2, f"两次 run() 的 event id 应相同: {ids_1} vs {ids_2}"

    def test_different_runs_produce_different_ids(self, worker_components):
        """不同 run_id 产生不同 event id。"""
        _, _, _, runner = worker_components

        job_1 = RunJobMessage(
            job_id="job-1", trace_id="t-1", task_id="t-1", run_id="run-aaa",
            user_goal="a", created_at="2026-07-06T10:00:00Z",
        )
        job_2 = RunJobMessage(
            job_id="job-2", trace_id="t-2", task_id="t-2", run_id="run-bbb",
            user_goal="b", created_at="2026-07-06T10:00:00Z",
        )

        ids_1 = {e.event_id for e in runner.run(job_1)}
        ids_2 = {e.event_id for e in runner.run(job_2)}

        assert ids_1.isdisjoint(ids_2), (
            f"不同 run 的 event id 不应重叠: {ids_1} vs {ids_2}"
        )

    def test_different_jobs_same_run_produce_different_ids(self, worker_components):
        """不同 job 但相同 run_id 产生相同 event id（因为基于 run_id）。"""
        _, _, _, runner = worker_components

        job_a = RunJobMessage(
            job_id="job-a", trace_id="t-a", task_id="t-a", run_id="run-same",
            user_goal="a", created_at="2026-07-06T10:00:00Z",
        )
        job_b = RunJobMessage(
            job_id="job-b", trace_id="t-b", task_id="t-b", run_id="run-same",
            user_goal="b", created_at="2026-07-06T10:00:00Z",
        )

        ids_a = [e.event_id for e in runner.run(job_a)]
        ids_b = [e.event_id for e in runner.run(job_b)]

        # 同一个 run_id → 相同 event id（重试幂等路径）
        assert ids_a == ids_b, (
            f"同一 run_id 应产生相同 event id: {ids_a} vs {ids_b}"
        )


class TestWorkerGracefulShutdown:
    """Graceful shutdown 测试 — 验证 stop() 能使 run_forever 退出。"""

    def test_stop_exits_run_forever(self, worker_components):
        """调用 stop() 后 run_forever 应在一个迭代内退出。"""
        client, consumer, producer, runner = worker_components

        # 确保 consumer group 存在（否则 read_one 可能异常）
        ensure_consumer_group(client, STREAM_RUN_QUEUE, GROUP_WORKER_POOL)

        from jarvis_worker.runtime.worker import AgentWorker

        worker = AgentWorker(client, consumer, producer, runner)

        import threading
        import time as _time

        # 在另一个线程中延迟调用 stop()
        def _delayed_stop() -> None:
            _time.sleep(0.1)
            worker.stop()

        stopper = threading.Thread(target=_delayed_stop, daemon=True)
        stopper.start()

        start = _time.monotonic()
        worker.run_forever(poll_interval_ms=100)
        elapsed = _time.monotonic() - start

        stopper.join(timeout=1)

        # run_forever 应在 stop 后快速退出（< 2s）
        assert elapsed < 2.0, f"run_forever 退出太慢: {elapsed:.1f}s"

    def test_run_forever_exits_without_redis(self, worker_components):
        """即使 read_one 持续抛异常，stop() 后 run_forever 也应退出。"""
        client, consumer, producer, runner = worker_components

        from jarvis_worker.runtime.worker import AgentWorker

        worker = AgentWorker(client, consumer, producer, runner)

        import threading
        import time as _time

        def _delayed_stop() -> None:
            _time.sleep(0.15)
            worker.stop()

        stopper = threading.Thread(target=_delayed_stop, daemon=True)
        stopper.start()

        start = _time.monotonic()
        worker.run_forever(poll_interval_ms=100)
        elapsed = _time.monotonic() - start

        stopper.join(timeout=1)

        assert elapsed < 5.0, f"run_forever 在异常循环中退出太慢: {elapsed:.1f}s"


# ============================================================
# Phase 6B-1: Provider 异常 → agent.run.failed + ack
# ============================================================

class TestProviderErrorWorkerAck:
    """Provider 异常 → AgentWorker 发布 agent.run.failed 并 ack。"""

    def test_failed_provider_publishes_failed_event_and_acks_job(self):
        """完整链路：Provider 异常 → _process_job_with_cancel_check 发布终态 → ack 后不 pending。"""
        import fakeredis as _fr
        from jarvis_worker.agent.models.errors import model_timeout_error
        from jarvis_worker.agent.models.provider import ModelProvider
        from jarvis_worker.agent.core.runner import AgentRunner
        from jarvis_worker.agent.tool_gateway.gateway import ToolGateway
        from jarvis_worker.agent.permissions.manager import PermissionManager
        from jarvis_worker.bootstrap.tool_registry import create_tool_registry

        class _Failing(ModelProvider):
            def decide_next_action(self, state):
                raise model_timeout_error("timeout")

        client = _fr.FakeRedis(decode_responses=True)
        registry = create_tool_registry()
        gateway = ToolGateway(registry, PermissionManager())
        ar = AgentRunner(model_provider=_Failing(), tool_gateway=gateway, worker_id="test")
        runner = MockRunner(
            worker_id="test", tool_gateway=gateway,
            agent_runner=ar, dev_mock_scenarios_enabled=False,
        )

        job = RunJobMessage(
            job_id="job-1", trace_id="trace-1", task_id="task-1", run_id="run-1",
            user_goal="test", created_at="2026-01-01T00:00:00Z", workspace_path="",
        )
        msg_id = _enqueue_run_job(client, job)
        ensure_consumer_group(client, STREAM_RUN_QUEUE, GROUP_WORKER_POOL)

        consumer = RunQueueConsumer(client, "c1", group=GROUP_WORKER_POOL)
        producer = RuntimeEventProducer(client)
        from jarvis_worker.runtime.worker import AgentWorker
        worker = AgentWorker(client, consumer, producer, runner)

        # run_forever 中的消费 → 处理路径
        consumed_job, msg_id, _stream = consumer.read_one(block_ms=100)
        assert consumed_job is not None, "应能消费到 job"
        assert msg_id is not None

        worker._process_job_with_cancel_check(consumed_job)

        # 验证 event stream 中有 agent.run.failed
        raw = client.xrange(STREAM_RUNTIME_EVENT, "-", "+", count=10)
        payloads = []
        for _mid, fields in raw:
            p = fields.get("payload", "")
            if p:
                payloads.append(json.loads(p))
        event_types = [e["event_type"] for e in payloads]
        assert "agent.run.failed" in event_types, f"事件: {event_types}"
        assert "agent.run.completed" not in event_types

        # 验证 error.code == MODEL_TIMEOUT
        failed = next(e for e in payloads if e["event_type"] == "agent.run.failed")
        assert (
            failed.get("runtime_event", {}).get("payload", {}).get("error", {}).get("code")
            == "MODEL_TIMEOUT"
        )

        # ack 后 run queue 不应有 pending
        assert consumer.ack(msg_id) is True
        pending = client.xpending(STREAM_RUN_QUEUE, GROUP_WORKER_POOL)
        assert pending["pending"] == 0, f"存在 pending: {pending}"
