"""Run Queue pending/retry/DLQ 可靠性测试。"""

from __future__ import annotations

import json
import threading
import time

import fakeredis
from redis.exceptions import ResponseError

from jarvis_worker.runtime.worker import AgentWorker
from jarvis_worker.runtime_bus import ensure_consumer_group
from jarvis_worker.runtime_bus.command_consumer import WorkerCommandConsumer
from jarvis_worker.runtime_bus.consumer import RunQueueConsumer
from jarvis_worker.runtime_bus.messages import (
    FIELD_PAYLOAD,
    FIELD_SCHEMA_VERSION,
    GROUP_WORKER_POOL,
    SCHEMA_VERSION,
    STREAM_RUN_DEAD_LETTER,
    STREAM_RUN_QUEUE,
    STREAM_WORKER_COMMAND,
    RunJobMessage,
)
from jarvis_worker.runtime_bus.producer import RuntimeEventProducer
from jarvis_worker.shared.config.settings import WorkerConfig
from tests.testing_doubles import MockRunner


def _job(job_id: str = "job-reliability") -> RunJobMessage:
    return RunJobMessage(
        job_id=job_id,
        trace_id="trace-reliability",
        task_id="task-reliability",
        run_id="run-reliability",
        user_goal="reliability",
        created_at="2026-07-22T00:00:00Z",
    )


def _enqueue(client, job: RunJobMessage) -> str:
    return client.xadd(
        STREAM_RUN_QUEUE,
        {
            FIELD_SCHEMA_VERSION: SCHEMA_VERSION,
            FIELD_PAYLOAD: job.to_payload_json(),
            "type": "run.job",
            "job_id": job.job_id,
            "trace_id": job.trace_id,
            "task_id": job.task_id,
            "run_id": job.run_id,
        },
    )


def test_worker_consumer_defaults_to_unique_worker_id(monkeypatch):
    monkeypatch.setenv("JARVIS_WORKER_ID", "worker-unique")
    monkeypatch.delenv("JARVIS_WORKER_CONSUMER", raising=False)

    config = WorkerConfig.from_env()

    assert config.worker_consumer == "worker-unique"


def _raise_nogroup_once(monkeypatch, client, method_name: str) -> None:
    original = getattr(client, method_name)
    calls = 0

    def redis_call(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise ResponseError("NOGROUP No such key or consumer group")
        return original(*args, **kwargs)

    monkeypatch.setattr(client, method_name, redis_call)


def test_run_queue_consumer_recreates_group_after_redis_state_loss(monkeypatch):
    client = fakeredis.FakeRedis(decode_responses=True)
    ensure_consumer_group(client, STREAM_RUN_QUEUE, GROUP_WORKER_POOL)
    client.xgroup_destroy(STREAM_RUN_QUEUE, GROUP_WORKER_POOL)
    _raise_nogroup_once(monkeypatch, client, "xreadgroup")
    consumer = RunQueueConsumer(client, "recovered-worker")

    assert consumer.read_delivery(block_ms=1) is None
    groups = client.xinfo_groups(STREAM_RUN_QUEUE)
    assert [group["name"] for group in groups] == [GROUP_WORKER_POOL]


def test_run_queue_pending_scan_recreates_group_after_redis_state_loss(monkeypatch):
    client = fakeredis.FakeRedis(decode_responses=True)
    ensure_consumer_group(client, STREAM_RUN_QUEUE, GROUP_WORKER_POOL)
    client.xgroup_destroy(STREAM_RUN_QUEUE, GROUP_WORKER_POOL)
    _raise_nogroup_once(monkeypatch, client, "xpending_range")
    consumer = RunQueueConsumer(client, "recovered-worker")

    assert consumer.claim_stale_one() is None
    groups = client.xinfo_groups(STREAM_RUN_QUEUE)
    assert [group["name"] for group in groups] == [GROUP_WORKER_POOL]


def test_worker_command_consumer_recreates_group_after_redis_state_loss(monkeypatch):
    client = fakeredis.FakeRedis(decode_responses=True)
    ensure_consumer_group(client, STREAM_WORKER_COMMAND, GROUP_WORKER_POOL)
    client.xgroup_destroy(STREAM_WORKER_COMMAND, GROUP_WORKER_POOL)
    _raise_nogroup_once(monkeypatch, client, "xreadgroup")
    consumer = WorkerCommandConsumer(client, "recovered-worker")

    assert consumer.read_delivery(block_ms=1) is None
    client.xgroup_destroy(STREAM_WORKER_COMMAND, GROUP_WORKER_POOL)
    monkeypatch.undo()
    _raise_nogroup_once(monkeypatch, client, "xpending_range")
    assert consumer.claim_stale_delivery() is None
    groups = client.xinfo_groups(STREAM_WORKER_COMMAND)
    assert [group["name"] for group in groups] == [GROUP_WORKER_POOL]


def test_run_queue_reliability_config_is_bounded(monkeypatch):
    monkeypatch.setenv("JARVIS_RUN_QUEUE_RECLAIM_IDLE_MS", "1")
    monkeypatch.setenv("JARVIS_RUN_QUEUE_RECLAIM_INTERVAL_MS", "999999")
    monkeypatch.setenv("JARVIS_RUN_QUEUE_MAX_DELIVERIES", "99")
    monkeypatch.setenv("JARVIS_COMMAND_RECLAIM_IDLE_MS", "1")
    monkeypatch.setenv("JARVIS_COMMAND_RECLAIM_INTERVAL_MS", "999999")

    config = WorkerConfig.from_env()

    assert config.run_queue_reclaim_idle_ms == 65_000
    assert config.run_queue_reclaim_interval_ms == 60_000
    assert config.run_queue_max_deliveries == 10
    assert config.command_reclaim_idle_ms == 1_000
    assert config.command_reclaim_interval_ms == 60_000


def test_stale_pending_is_claimed_with_delivery_count():
    client = fakeredis.FakeRedis(decode_responses=True)
    ensure_consumer_group(client, STREAM_RUN_QUEUE, GROUP_WORKER_POOL)
    _enqueue(client, _job())
    crashed = RunQueueConsumer(client, "crashed")
    first = crashed.read_delivery()
    assert first is not None and first.delivery_count == 1

    recovery = RunQueueConsumer(client, "recovery", max_deliveries=3)
    # 测试中只消除真实时间等待；生产构造函数仍强制最小 65 秒。
    recovery._reclaim_idle_ms = 0
    reclaimed = recovery.claim_stale_one()

    assert reclaimed is not None
    assert reclaimed.reclaimed is True
    assert reclaimed.delivery_count == 2
    assert reclaimed.job == first.job
    assert recovery.should_dead_letter(reclaimed) is False

    reclaimed_again = recovery.claim_stale_one()
    assert reclaimed_again is not None
    assert reclaimed_again.delivery_count == 3
    assert recovery.should_dead_letter(reclaimed_again) is True


def test_pending_scan_cursor_does_not_starve_entries_after_first_page():
    client = fakeredis.FakeRedis(decode_responses=True)
    ensure_consumer_group(client, STREAM_RUN_QUEUE, GROUP_WORKER_POOL)
    crashed = RunQueueConsumer(client, "crashed")
    first_page_ids: list[str] = []
    for index in range(20):
        _enqueue(client, _job(f"job-blocked-{index}"))
        delivery = crashed.read_delivery()
        assert delivery is not None
        first_page_ids.append(delivery.message_id)
    # 把前 20 条的 delivery count 提升到 2，使其采用更长退避。
    client.xclaim(
        STREAM_RUN_QUEUE,
        GROUP_WORKER_POOL,
        "crashed",
        0,
        first_page_ids,
    )
    _enqueue(client, _job("job-after-first-page"))
    assert crashed.read_delivery() is not None

    recovery = RunQueueConsumer(client, "recovery")
    recovery.retry_idle_ms = lambda deliveries: (
        1_000_000 if deliveries >= 2 else 0
    )

    assert recovery.claim_stale_one() is None
    claimed = recovery.claim_stale_one()

    assert claimed is not None
    assert claimed.job is not None
    assert claimed.job.job_id == "job-after-first-page"


def test_worker_reclaims_stale_pending_before_fresh_traffic():
    client = fakeredis.FakeRedis(decode_responses=True)
    ensure_consumer_group(client, STREAM_RUN_QUEUE, GROUP_WORKER_POOL)
    _enqueue(client, _job("job-stale"))
    crashed = RunQueueConsumer(client, "crashed")
    assert crashed.read_delivery() is not None
    _enqueue(client, _job("job-fresh"))

    recovery = RunQueueConsumer(client, "recovery")
    recovery._reclaim_idle_ms = 0
    worker = AgentWorker(
        client,
        recovery,
        RuntimeEventProducer(client),
        MockRunner(worker_id="recovery"),
        run_queue_reclaim_interval_ms=1_000,
    )
    claimed_job_ids: list[str] = []

    def record_first_claim(job):
        claimed_job_ids.append(job.job_id)
        worker.stop()
        return "duplicate"

    worker._claim_job = record_first_claim
    thread = threading.Thread(
        target=worker.run_forever, kwargs={"poll_interval_ms": 20}, daemon=True
    )
    thread.start()
    thread.join(timeout=2)

    assert thread.is_alive() is False
    assert claimed_job_ids == ["job-stale"]
    assert worker.run_queue_metrics["reclaimed"] == 1


def test_retry_backoff_is_exponential_and_bounded():
    consumer = RunQueueConsumer(
        fakeredis.FakeRedis(decode_responses=True),
        "worker",
        reclaim_idle_ms=65_000,
    )

    assert consumer.retry_idle_ms(1) == 65_000
    assert consumer.retry_idle_ms(2) == 130_000
    assert consumer.retry_idle_ms(3) == 260_000
    assert consumer.retry_idle_ms(9) == 260_000


def test_malformed_message_moves_to_dlq_atomically_and_deduplicates():
    client = fakeredis.FakeRedis(decode_responses=True)
    ensure_consumer_group(client, STREAM_RUN_QUEUE, GROUP_WORKER_POOL)
    client.xadd(
        STREAM_RUN_QUEUE,
        {
            FIELD_SCHEMA_VERSION: SCHEMA_VERSION,
            FIELD_PAYLOAD: "{not-json",
            "type": "run.job",
        },
    )
    consumer = RunQueueConsumer(client, "worker")
    delivery = consumer.read_delivery()

    assert delivery is not None
    assert delivery.valid is False
    assert delivery.retryable is False
    assert delivery.error_code == "RUN_QUEUE_MALFORMED"

    first_dlq_id = consumer.dead_letter(
        delivery,
        error_code=delivery.error_code,
        error_message=delivery.error_message or "malformed",
    )
    second_dlq_id = consumer.dead_letter(
        delivery,
        error_code=delivery.error_code,
        error_message=delivery.error_message or "malformed",
    )

    assert first_dlq_id != "0"
    assert second_dlq_id == "0"
    assert client.xpending(STREAM_RUN_QUEUE, GROUP_WORKER_POOL)["pending"] == 0
    entries = client.xrange(STREAM_RUN_DEAD_LETTER, "-", "+")
    assert len(entries) == 1
    fields = entries[0][1]
    assert fields["type"] == "run.job.dead_letter"
    assert fields["error_code"] == "RUN_QUEUE_MALFORMED"
    assert fields["original_message_id"] == delivery.message_id
    assert FIELD_PAYLOAD not in fields
    assert len(fields["payload_sha256"]) == 64
    assert fields["payload_size_bytes"] == str(len("{not-json"))


def test_outer_schema_or_type_mismatch_is_not_retryable():
    client = fakeredis.FakeRedis(decode_responses=True)
    ensure_consumer_group(client, STREAM_RUN_QUEUE, GROUP_WORKER_POOL)
    job = _job("job-wrong-type")
    client.xadd(
        STREAM_RUN_QUEUE,
        {
            FIELD_SCHEMA_VERSION: SCHEMA_VERSION,
            FIELD_PAYLOAD: json.dumps(job.to_dict()),
            "type": "unexpected.job",
        },
    )
    consumer = RunQueueConsumer(client, "worker")

    delivery = consumer.read_delivery()

    assert delivery is not None
    assert delivery.error_code == "RUN_QUEUE_UNSUPPORTED_TYPE"
    assert delivery.retryable is False


def test_worker_moves_poison_message_to_dlq_without_crashing():
    client = fakeredis.FakeRedis(decode_responses=True)
    ensure_consumer_group(client, STREAM_RUN_QUEUE, GROUP_WORKER_POOL)
    client.xadd(
        STREAM_RUN_QUEUE,
        {
            FIELD_SCHEMA_VERSION: SCHEMA_VERSION,
            FIELD_PAYLOAD: "not-json",
            "type": "run.job",
        },
    )
    consumer = RunQueueConsumer(client, "worker")
    worker = AgentWorker(
        client,
        consumer,
        RuntimeEventProducer(client),
        MockRunner(worker_id="worker"),
        run_queue_reclaim_interval_ms=1_000,
    )
    thread = threading.Thread(
        target=worker.run_forever, kwargs={"poll_interval_ms": 20}, daemon=True
    )
    thread.start()
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        if client.xlen(STREAM_RUN_DEAD_LETTER) == 1:
            break
        time.sleep(0.01)
    worker.stop()
    thread.join(timeout=2)

    assert thread.is_alive() is False
    assert client.xlen(STREAM_RUN_DEAD_LETTER) == 1
    assert worker.run_queue_metrics == {
        "reclaimed": 0,
        "retry_deferred": 0,
        "dead_lettered": 1,
        "malformed": 1,
        "command_reclaimed": 0,
        "command_dead_lettered": 0,
        "command_malformed": 0,
    }


def test_worker_leaves_transient_claim_failure_pending_for_retry():
    client = fakeredis.FakeRedis(decode_responses=True)
    ensure_consumer_group(client, STREAM_RUN_QUEUE, GROUP_WORKER_POOL)
    _enqueue(client, _job("job-transient"))
    consumer = RunQueueConsumer(client, "worker", max_deliveries=3)
    worker = AgentWorker(
        client,
        consumer,
        RuntimeEventProducer(client),
        MockRunner(worker_id="worker"),
        run_queue_reclaim_interval_ms=1_000,
    )

    def fail_claim(_job):
        raise RuntimeError("database temporarily unavailable")

    worker._claim_job = fail_claim
    thread = threading.Thread(
        target=worker.run_forever, kwargs={"poll_interval_ms": 20}, daemon=True
    )
    thread.start()
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        if worker.run_queue_metrics["retry_deferred"] == 1:
            break
        time.sleep(0.01)
    worker.stop()
    thread.join(timeout=2)

    assert client.xpending(STREAM_RUN_QUEUE, GROUP_WORKER_POOL)["pending"] == 1
    assert client.xlen(STREAM_RUN_DEAD_LETTER) == 0
    assert worker.run_queue_metrics["retry_deferred"] == 1


def test_worker_dead_letters_claim_failure_after_delivery_budget_exhausted():
    client = fakeredis.FakeRedis(decode_responses=True)
    ensure_consumer_group(client, STREAM_RUN_QUEUE, GROUP_WORKER_POOL)
    _enqueue(client, _job("job-exhausted"))
    consumer = RunQueueConsumer(client, "worker", max_deliveries=1)
    worker = AgentWorker(
        client,
        consumer,
        RuntimeEventProducer(client),
        MockRunner(worker_id="worker"),
        run_queue_reclaim_interval_ms=1_000,
    )

    def fail_claim(_job):
        raise RuntimeError("database unavailable")

    worker._claim_job = fail_claim
    thread = threading.Thread(
        target=worker.run_forever, kwargs={"poll_interval_ms": 20}, daemon=True
    )
    thread.start()
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        if client.xlen(STREAM_RUN_DEAD_LETTER) == 1:
            break
        time.sleep(0.01)
    worker.stop()
    thread.join(timeout=2)

    assert client.xpending(STREAM_RUN_QUEUE, GROUP_WORKER_POOL)["pending"] == 0
    assert client.xlen(STREAM_RUN_DEAD_LETTER) == 1
    assert worker.run_queue_metrics["dead_lettered"] == 1
    assert worker.run_queue_metrics["retry_deferred"] == 0
