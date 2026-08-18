"""Event builder 测试 — 验证 RuntimeEvent 构造和 envelope 包装。"""

from __future__ import annotations

from jarvis_worker.runtime_bus.messages import SCHEMA_VERSION
from jarvis_worker.runtime.events import build_envelope, build_runtime_event


class TestBuildRuntimeEvent:
    """RuntimeEvent dict 构造测试。"""

    def test_basic_event(self) -> None:
        """构造基本 RuntimeEvent。"""
        event = build_runtime_event(
            "agent.run.started", "task-001", "run-001"
        )
        assert event["id"] != ""
        assert event["type"] == "agent.run.started"
        assert event["task_id"] == "task-001"
        assert event["run_id"] == "run-001"
        assert "timestamp" in event
        assert "payload" in event
        # 没有 step_id 时不包含 step_id key
        assert "step_id" not in event

    def test_event_with_step(self) -> None:
        """构造带 step_id 的事件。"""
        event = build_runtime_event(
            "agent.step.started",
            "task-001",
            "run-001",
            step_id="step-abc",
        )
        assert event["step_id"] == "step-abc"

    def test_event_with_payload(self) -> None:
        """构造带 payload 的事件。"""
        event = build_runtime_event(
            "model.delta",
            "task-001",
            "run-001",
            payload={"delta": "hello", "accumulated": "hello"},
        )
        assert event["payload"]["delta"] == "hello"

    def test_unique_ids(self) -> None:
        """每次调用生成不同 event id。"""
        e1 = build_runtime_event("task.created", "t1", "r1")
        e2 = build_runtime_event("task.created", "t1", "r1")
        assert e1["id"] != e2["id"]


class TestBuildEnvelope:
    """Envelope 包装测试。"""

    def test_envelope_consistency(self) -> None:
        """build_envelope 产生的 envelope 通过 validate。"""
        event = build_runtime_event(
            "agent.run.completed",
            "task-001",
            "run-001",
            payload={"output": "done"},
        )
        envelope = build_envelope(event, trace_id="trace-001", worker_id="worker-01")

        # 不抛异常即通过
        envelope.validate()

        assert envelope.event_id == event["id"]
        assert envelope.trace_id == "trace-001"
        assert envelope.produced_by == "worker-01"
        assert envelope.schema_version == SCHEMA_VERSION

    def test_envelope_xadd_fields_include_all_routing(self) -> None:
        """XADD fields 包含所有路由字段。"""
        event = build_runtime_event("agent.run.completed", "task-001", "run-001")
        envelope = build_envelope(event, trace_id="trace-001", worker_id="w1")

        fields = envelope.to_xadd_fields()
        for key in (
            "schema_version",
            "payload",
            "event_id",
            "trace_id",
            "task_id",
            "run_id",
            "type",
            "produced_by",
        ):
            assert key in fields, f"缺少字段: {key}"
