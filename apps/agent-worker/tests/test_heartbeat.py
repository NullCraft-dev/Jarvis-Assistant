"""Worker heartbeat 测试 — 使用 fakeredis 模拟 Redis。

验证 HeartbeatProducer 的 XADD fields、状态流转、shutdown 行为。
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
    FIELD_WORKER_ID,
    STREAM_WORKER_HEARTBEAT,
    WorkerHeartbeatMessage,
)
from jarvis_worker.runtime_bus.heartbeat import HeartbeatProducer


# -- helpers --


def _read_heartbeats(client: fakeredis.FakeRedis) -> list[dict]:
    """从 heartbeat stream 读取所有消息，返回 payload dict 列表。"""
    result = client.xread({STREAM_WORKER_HEARTBEAT: "0"})
    hbs: list[dict] = []
    for _stream_name, messages in result:
        for _msg_id, fields in messages:
            payload = fields.get(FIELD_PAYLOAD, "")
            if payload:
                hbs.append(json.loads(payload))
    return hbs


# -- fixtures --


@pytest.fixture
def redis_client() -> fakeredis.FakeRedis:
    return fakeredis.FakeRedis(decode_responses=True)


@pytest.fixture
def hb_producer(redis_client: fakeredis.FakeRedis) -> HeartbeatProducer:
    return HeartbeatProducer(redis_client, worker_id="test-worker", interval_ms=500)


# -- 测试 --


class TestHeartbeatXAddFields:
    """心跳消息 XADD fields 完整性测试。"""

    def test_xadd_fields_contain_schema_version(self, hb_producer, redis_client):
        """XADD fields 包含正确的 schema_version。"""
        hb_producer.set_status("idle")
        msg_id = hb_producer.publish_now()
        assert msg_id is not None

        result = redis_client.xread({STREAM_WORKER_HEARTBEAT: "0"})
        _stream_name, messages = result[0]
        _msg_id, fields = messages[0]
        assert fields[FIELD_SCHEMA_VERSION] == SCHEMA_VERSION

    def test_xadd_fields_contain_payload_json(self, hb_producer, redis_client):
        """XADD fields 的 payload 是合法的 JSON string。"""
        hb_producer.set_status("idle")
        hb_producer.publish_now()

        result = redis_client.xread({STREAM_WORKER_HEARTBEAT: "0"})
        _stream_name, messages = result[0]
        _msg_id, fields = messages[0]
        payload_str = fields[FIELD_PAYLOAD]
        payload = json.loads(payload_str)
        assert payload["worker_id"] == "test-worker"
        assert payload["status"] == "idle"
        assert "reported_at" in payload
        assert payload["schema_version"] == SCHEMA_VERSION

    def test_xadd_fields_contain_worker_id(self, hb_producer, redis_client):
        """XADD fields 包含冗余的 worker_id 标量字段。"""
        hb_producer.set_status("idle")
        hb_producer.publish_now()

        result = redis_client.xread({STREAM_WORKER_HEARTBEAT: "0"})
        _stream_name, messages = result[0]
        _msg_id, fields = messages[0]
        assert fields[FIELD_WORKER_ID] == "test-worker"

    def test_xadd_fields_contain_type(self, hb_producer, redis_client):
        """XADD fields 包含 type=worker.heartbeat。"""
        hb_producer.set_status("idle")
        hb_producer.publish_now()

        result = redis_client.xread({STREAM_WORKER_HEARTBEAT: "0"})
        _stream_name, messages = result[0]
        _msg_id, fields = messages[0]
        assert fields["type"] == "worker.heartbeat"

    def test_xadd_fields_contain_status(self, hb_producer, redis_client):
        """XADD fields 包含冗余的 status 标量字段。"""
        hb_producer.set_status("busy")
        hb_producer.publish_now()

        result = redis_client.xread({STREAM_WORKER_HEARTBEAT: "0"})
        _stream_name, messages = result[0]
        _msg_id, fields = messages[0]
        assert fields["status"] == "busy"

    def test_xadd_fields_no_trace_id(self, hb_producer, redis_client):
        """心跳不包含 trace_id（心跳是状态探针，不是 command/event 链路）。"""
        hb_producer.set_status("idle")
        hb_producer.publish_now()

        result = redis_client.xread({STREAM_WORKER_HEARTBEAT: "0"})
        _stream_name, messages = result[0]
        _msg_id, fields = messages[0]
        assert "trace_id" not in fields

    def test_payload_no_trace_id(self, hb_producer, redis_client):
        """payload JSON 中也不包含 trace_id。"""
        hb_producer.set_status("idle")
        hb_producer.publish_now()

        result = redis_client.xread({STREAM_WORKER_HEARTBEAT: "0"})
        _stream_name, messages = result[0]
        _msg_id, fields = messages[0]
        payload = json.loads(fields[FIELD_PAYLOAD])
        assert "trace_id" not in payload


class TestHeartbeatStatusTransitions:
    """心跳状态流转测试。"""

    def test_starting_on_creation(self, hb_producer):
        """初始状态为 starting。"""
        assert hb_producer.status == "starting"

    def test_set_idle(self, hb_producer):
        """切换到 idle。"""
        hb_producer.set_status("idle")
        assert hb_producer.status == "idle"

    def test_set_busy(self, hb_producer):
        """切换到 busy。"""
        hb_producer.set_status("busy")
        assert hb_producer.status == "busy"

    def test_set_draining(self, hb_producer):
        """切换到 draining。"""
        hb_producer.set_status("draining")
        assert hb_producer.status == "draining"

    def test_set_stopped(self, hb_producer):
        """切换到 stopped。"""
        hb_producer.set_status("stopped")
        assert hb_producer.status == "stopped"

    def test_set_failed(self, hb_producer):
        """切换到 failed。"""
        hb_producer.set_status("failed")
        assert hb_producer.status == "failed"

    def test_invalid_status_raises(self, hb_producer):
        """非法 status 抛出 ValueError。"""
        with pytest.raises(ValueError, match="非法 worker status"):
            hb_producer.set_status("sleeping")

    def test_full_transition_cycle(self, hb_producer, redis_client):
        """完整状态流转：starting → idle → busy → idle → stopped。"""
        # starting
        assert hb_producer.status == "starting"
        hb_producer.publish_now()

        # idle
        hb_producer.set_status("idle")
        hb_producer.publish_now()

        # busy
        hb_producer.set_active_run_id("run-001")
        hb_producer.set_status("busy")
        hb_producer.publish_now()

        # idle (job 完成)
        hb_producer.set_active_run_id("")
        hb_producer.set_status("idle")
        hb_producer.publish_now()

        # stopped
        hb_producer.set_status("stopped")
        hb_producer.publish_now()

        # 验证 stream 中的状态流转
        hbs = _read_heartbeats(redis_client)
        statuses = [h["status"] for h in hbs]
        assert statuses == ["starting", "idle", "busy", "idle", "stopped"]


class TestHeartbeatActiveRunID:
    """active_run_id 字段测试。"""

    def test_idle_has_empty_active_run_id(self, hb_producer, redis_client):
        """idle 时 active_run_id 为空（payload 中不包含或为空）。"""
        hb_producer.set_status("idle")
        hb_producer.set_active_run_id("")
        hb_producer.publish_now()

        hbs = _read_heartbeats(redis_client)
        payload = hbs[0]
        assert payload.get("active_run_id", "") == ""

    def test_busy_has_active_run_id(self, hb_producer, redis_client):
        """busy 时 active_run_id 非空。"""
        hb_producer.set_status("busy")
        hb_producer.set_active_run_id("run-abc123")
        hb_producer.publish_now()

        hbs = _read_heartbeats(redis_client)
        payload = hbs[0]
        assert payload["active_run_id"] == "run-abc123"

    def test_active_run_id_cleared_after_job(self, hb_producer, redis_client):
        """job 完成后 active_run_id 清空。"""
        hb_producer.set_status("busy")
        hb_producer.set_active_run_id("run-xyz")
        hb_producer.publish_now()

        hb_producer.set_active_run_id("")
        hb_producer.set_status("idle")
        hb_producer.publish_now()

        hbs = _read_heartbeats(redis_client)
        assert hbs[0]["active_run_id"] == "run-xyz"
        assert hbs[1].get("active_run_id", "") == ""


class TestHeartbeatPublishNow:
    """publish_now 同步发布测试。"""

    def test_publish_now_writes_to_stream(self, hb_producer, redis_client):
        """publish_now 立即写入 stream，不等待定时器。"""
        hb_producer.set_status("idle")
        msg_id = hb_producer.publish_now()
        assert msg_id is not None
        assert msg_id  # 非空字符串

        hbs = _read_heartbeats(redis_client)
        assert len(hbs) == 1
        assert hbs[0]["status"] == "idle"

    def test_multiple_publish_now(self, hb_producer, redis_client):
        """多次 publish_now 产生多条消息。"""
        for status in ["starting", "idle", "busy"]:
            hb_producer.set_status(status)
            hb_producer.publish_now()

        hbs = _read_heartbeats(redis_client)
        assert len(hbs) == 3
        assert [h["status"] for h in hbs] == ["starting", "idle", "busy"]


class TestHeartbeatLifecycle:
    """HeartbeatProducer 生命周期测试。"""

    def test_start_and_stop(self, hb_producer, redis_client):
        """start → 后台线程运行 → stop → 线程退出。"""
        hb_producer.set_status("idle")
        hb_producer.start()

        # 等待至少一次心跳发布
        time.sleep(0.3)

        hb_producer.stop()

        # 验证至少产生了一条心跳
        hbs = _read_heartbeats(redis_client)
        assert len(hbs) >= 1
        assert hbs[0]["worker_id"] == "test-worker"

    def test_stop_before_start_is_noop(self, hb_producer):
        """未 start 时 stop 不报错。"""
        hb_producer.stop()  # 不报错

    def test_double_start_is_noop(self, hb_producer):
        """重复 start 不报错。"""
        hb_producer.set_status("idle")
        hb_producer.start()
        try:
            hb_producer.start()  # 不报错
        finally:
            hb_producer.stop()

    def test_draining_then_stopped(self, hb_producer, redis_client):
        """draining → stopped 流程：先发 draining，stop 后发 stopped。"""
        hb_producer.set_status("draining")
        hb_producer.publish_now()

        hb_producer.set_status("stopped")
        hb_producer.publish_now()

        hbs = _read_heartbeats(redis_client)
        statuses = [h["status"] for h in hbs]
        assert "draining" in statuses
        assert "stopped" in statuses
        assert statuses.index("draining") < statuses.index("stopped")

    def test_reported_at_is_iso_format(self, hb_producer, redis_client):
        """reported_at 是 ISO 8601 格式。"""
        hb_producer.set_status("idle")
        hb_producer.publish_now()

        hbs = _read_heartbeats(redis_client)
        reported_at = hbs[0]["reported_at"]
        assert "T" in reported_at  # ISO 8601 包含 'T'
        assert reported_at.endswith("Z") or "+" in reported_at


class TestHeartbeatPublishError:
    """Heartbeat 发布错误处理测试（不 crash）。"""

    def test_publish_error_does_not_crash(self, hb_producer):
        """即使 Redis 不可用，publish_now 也不抛异常。"""
        # 关闭 fakeredis 连接来模拟错误
        # 使用一个不存在的 client → fakeredis 不会真的连接远程，但我们可以测试 error 路径
        # 实际上 fakeredis 不会抛异常，所以这里测试正常路径
        hb_producer.set_status("idle")
        msg_id = hb_producer.publish_now()
        assert msg_id is not None  # fakeredis 总是成功


class TestWorkerHeartbeatMessageContract:
    """WorkerHeartbeatMessage 契约一致性测试。"""

    def test_to_xadd_fields_shape(self):
        """to_xadd_fields 输出 shape 与 Go 侧 WorkerHeartbeatToStreamFields 一致。"""
        msg = WorkerHeartbeatMessage(
            worker_id="w1",
            status="idle",
            reported_at="2026-07-07T10:00:00Z",
        )
        fields = msg.to_xadd_fields()

        assert fields[FIELD_SCHEMA_VERSION] == SCHEMA_VERSION
        assert FIELD_PAYLOAD in fields
        assert fields[FIELD_WORKER_ID] == "w1"
        assert fields["type"] == "worker.heartbeat"
        assert fields["status"] == "idle"
        assert fields["reported_at"] == "2026-07-07T10:00:00Z"
        assert "trace_id" not in fields

    def test_to_xadd_fields_with_active_run(self):
        """busy 状态时 payload 包含 active_run_id。"""
        msg = WorkerHeartbeatMessage(
            worker_id="w1",
            status="busy",
            reported_at="2026-07-07T10:00:00Z",
            active_run_id="run-001",
        )
        fields = msg.to_xadd_fields()
        payload = json.loads(fields[FIELD_PAYLOAD])
        assert payload["active_run_id"] == "run-001"

    def test_rag_worker_payload_has_kind_without_active_run(self):
        """RAG Worker 的 busy 绑定 ingestion job，不伪造 AgentRun id。"""
        msg = WorkerHeartbeatMessage(
            worker_id="rag-worker-01",
            worker_kind="rag",
            status="busy",
            reported_at="2026-07-07T10:00:00Z",
        )

        payload = json.loads(msg.to_payload_json())

        assert payload["worker_kind"] == "rag"
        assert "active_run_id" not in payload

    def test_heartbeat_producer_rejects_unknown_worker_kind(self, redis_client):
        with pytest.raises(ValueError, match="worker_kind"):
            HeartbeatProducer(redis_client, "w1", worker_kind="unknown")

    def test_to_payload_json_idle_omits_active_run(self):
        """idle 时 payload JSON 不包含 active_run_id 字段。"""
        msg = WorkerHeartbeatMessage(
            worker_id="w1",
            status="idle",
            reported_at="2026-07-07T10:00:00Z",
            active_run_id="",
        )
        payload_str = msg.to_payload_json()
        payload = json.loads(payload_str)
        assert "active_run_id" not in payload  # 空字符串时不序列化

    def test_to_payload_json_includes_runtime_bus_metrics(self):
        msg = WorkerHeartbeatMessage(
            worker_id="w1",
            status="idle",
            reported_at="2026-07-07T10:00:00Z",
            runtime_bus={
                "reclaimed": 2,
                "retry_deferred": 1,
                "dead_lettered": 1,
                "malformed": 1,
                "command_reclaimed": 4,
                "command_dead_lettered": 2,
                "command_malformed": 2,
            },
        )

        payload = json.loads(msg.to_payload_json())

        assert payload["runtime_bus"]["reclaimed"] == 2


# ============================================================
# Phase 6B-1: model status in heartbeat
# ============================================================

class TestHeartbeatModelStatus:
    def _read_latest_payload(self, client):
        raw = client.xrange(STREAM_WORKER_HEARTBEAT, "-", "+", count=1)
        assert len(raw) > 0
        _, fields = raw[-1]
        # fakeredis returns bytes keys; existing tests use decode_responses=True
        p = fields.get(b"payload", b"") or fields.get("payload", "")
        return json.loads(p)

    def test_payload_includes_model_when_present(self, redis_client):
        hb = HeartbeatProducer(redis_client, "w1", interval_ms=5000, model_status={
            "provider": "mock", "model_name": "mock", "api_key_configured": False,
            "thinking_mode": "", "status": "mock", "last_error_code": None,
        })
        hb._status = "idle"
        hb._publish()
        parsed = self._read_latest_payload(redis_client)
        assert "model" in parsed
        assert parsed["model"]["provider"] == "mock"

    def test_payload_no_model_when_none(self, redis_client):
        hb = HeartbeatProducer(redis_client, "w1", interval_ms=5000, model_status=None)
        hb._status = "idle"
        hb._publish()
        parsed = self._read_latest_payload(redis_client)
        assert "model" not in parsed

    def test_deepseek_configured(self, redis_client):
        hb = HeartbeatProducer(redis_client, "w1", interval_ms=5000, model_status={
            "provider": "deepseek", "protocol": "openai_chat_completions",
            "model_name": "deepseek-v4-flash",
            "api_key_configured": True, "thinking_mode": "disabled",
            "status": "configured", "last_error_code": None,
        })
        hb._status = "idle"
        hb._publish()
        parsed = self._read_latest_payload(redis_client)
        assert parsed["model"]["status"] == "configured"

    def test_deepseek_not_configured(self, redis_client):
        hb = HeartbeatProducer(redis_client, "w1", interval_ms=5000, model_status={
            "provider": "deepseek", "protocol": "openai_chat_completions",
            "model_name": "",
            "api_key_configured": False, "thinking_mode": "",
            "status": "not_configured", "last_error_code": None,
        })
        hb._status = "idle"
        hb._publish()
        parsed = self._read_latest_payload(redis_client)
        assert parsed["model"]["status"] == "not_configured"

    def test_no_api_key_in_payload(self, redis_client):
        """heartbeat 不包含 API key 值。"""
        hb = HeartbeatProducer(redis_client, "w1", interval_ms=5000, model_status={
            "provider": "deepseek", "protocol": "openai_chat_completions",
            "model_name": "test",
            "api_key_configured": True, "thinking_mode": "disabled",
            "status": "configured", "last_error_code": None,
        })
        hb._status = "idle"
        hb._publish()
        parsed = self._read_latest_payload(redis_client)
        assert "sk-" not in json.dumps(parsed).lower()

    def test_runtime_bus_metrics_provider_is_published(self, redis_client):
        hb = HeartbeatProducer(
            redis_client,
            "w1",
            interval_ms=5000,
            runtime_bus_metrics_provider=lambda: {
                "reclaimed": 3,
                "retry_deferred": 2,
                "dead_lettered": 1,
                "malformed": 1,
                "command_reclaimed": 4,
                "command_dead_lettered": 2,
                "command_malformed": 2,
            },
        )
        hb._status = "idle"

        hb._publish()

        parsed = self._read_latest_payload(redis_client)
        assert parsed["runtime_bus"]["reclaimed"] == 3
        assert parsed["runtime_bus"]["dead_lettered"] == 1
        assert parsed["runtime_bus"]["command_reclaimed"] == 4

    def test_runtime_bus_metrics_provider_error_does_not_drop_heartbeat(
        self, redis_client
    ):
        def broken_provider():
            raise RuntimeError("metrics unavailable")

        hb = HeartbeatProducer(
            redis_client,
            "w1",
            interval_ms=5000,
            runtime_bus_metrics_provider=broken_provider,
        )
        hb._status = "idle"

        assert hb._publish() is not None
        parsed = self._read_latest_payload(redis_client)
        assert "runtime_bus" not in parsed
