"""契约编解码测试 — 验证 Python 侧与 Go 侧消息格式对齐。"""

from __future__ import annotations

import json

import pytest

from jarvis_worker.runtime_bus.messages import (
    SCHEMA_VERSION,
    FIELD_PAYLOAD,
    RunJobMessage,
    RuntimeEventEnvelope,
)


class TestRunJobMessage:
    """RunJobMessage 编解码测试。"""

    def test_decode_from_valid_payload(self) -> None:
        """合法 payload JSON → 正确解码 RunJobMessage。"""
        payload = json.dumps({
            "job_id": "job-001",
            "trace_id": "trace-001",
            "task_id": "task-001",
            "run_id": "run-001",
            "user_goal": "测试任务",
            "created_at": "2026-07-06T10:00:00Z",
            "schema_version": SCHEMA_VERSION,
        })

        job = RunJobMessage.from_payload(payload)
        assert job.job_id == "job-001"
        assert job.trace_id == "trace-001"
        assert job.task_id == "task-001"
        assert job.run_id == "run-001"
        assert job.user_goal == "测试任务"
        assert job.created_at == "2026-07-06T10:00:00Z"
        assert job.schema_version == SCHEMA_VERSION

    def test_decode_bad_schema_version_raises(self) -> None:
        """错误的 schema_version → ValueError。"""
        payload = json.dumps({
            "job_id": "job-001",
            "trace_id": "trace-001",
            "task_id": "task-001",
            "run_id": "run-001",
            "user_goal": "测试",
            "created_at": "2026-07-06T10:00:00Z",
            "schema_version": "9.9.9-wrong",
        })
        with pytest.raises(ValueError, match="schema_version"):
            RunJobMessage.from_payload(payload)

    def test_decode_missing_required_field_raises(self) -> None:
        """缺少必要字段 → ValueError。"""
        payload = json.dumps({
            "job_id": "job-001",
            "trace_id": "trace-001",
            "task_id": "task-001",
            # 缺少 run_id
            "user_goal": "测试",
            "created_at": "2026-07-06T10:00:00Z",
            "schema_version": SCHEMA_VERSION,
        })
        with pytest.raises(ValueError, match="run_id"):
            RunJobMessage.from_payload(payload)

    def test_decode_invalid_json_raises(self) -> None:
        """非法 JSON → ValueError。"""
        with pytest.raises(ValueError, match="无效 JSON"):
            RunJobMessage.from_payload("{not valid")

    def test_roundtrip(self) -> None:
        """编码 → 解码往返一致。"""
        job = RunJobMessage(
            job_id="job-001",
            trace_id="trace-001",
            task_id="task-001",
            run_id="run-001",
            user_goal="测试",
            created_at="2026-07-06T10:00:00Z",
            scheduled_task_id="schedule-001",
            authorized_tools=["literature.search_arxiv", "knowledge.create_document"],
            source_policy={"provider": "arxiv", "query": "AI agents", "max_results": 5},
        )
        payload = job.to_payload_json()
        decoded = RunJobMessage.from_payload(payload)
        assert decoded.job_id == job.job_id
        assert decoded.trace_id == job.trace_id
        assert decoded.source_policy == job.source_policy


class TestRuntimeEventEnvelope:
    """RuntimeEventEnvelope 一致性和 XADD fields 测试。"""

    def test_validate_envelope_consistency(self) -> None:
        """envelope 与内层 runtime_event 字段一致 → 校验通过。"""
        env = RuntimeEventEnvelope(
            event_id="evt-001",
            trace_id="trace-001",
            task_id="task-001",
            run_id="run-001",
            event_type="agent.run.completed",
            runtime_event={
                "id": "evt-001",
                "type": "agent.run.completed",
                "task_id": "task-001",
                "run_id": "run-001",
                "timestamp": "2026-07-06T10:00:00Z",
                "payload": {},
            },
            produced_by="worker-01",
            schema_version=SCHEMA_VERSION,
        )
        env.validate()  # 不应抛异常

    def test_validate_event_id_mismatch_raises(self) -> None:
        """envelope.event_id != runtime_event.id → ValueError。"""
        env = RuntimeEventEnvelope(
            event_id="evt-001",
            trace_id="trace-001",
            task_id="task-001",
            run_id="run-001",
            event_type="agent.run.completed",
            runtime_event={
                "id": "evt-002",  # 不一致！
                "type": "agent.run.completed",
                "task_id": "task-001",
                "run_id": "run-001",
                "timestamp": "2026-07-06T10:00:00Z",
                "payload": {},
            },
            produced_by="worker-01",
        )
        with pytest.raises(ValueError, match="event_id"):
            env.validate()

    def test_validate_event_type_mismatch_raises(self) -> None:
        """envelope.event_type != runtime_event.type → ValueError。"""
        env = RuntimeEventEnvelope(
            event_id="evt-001",
            trace_id="trace-001",
            task_id="task-001",
            run_id="run-001",
            event_type="agent.run.completed",
            runtime_event={
                "id": "evt-001",
                "type": "tool.call.started",  # 不一致！
                "task_id": "task-001",
                "run_id": "run-001",
                "timestamp": "2026-07-06T10:00:00Z",
                "payload": {},
            },
            produced_by="worker-01",
        )
        with pytest.raises(ValueError, match="event_type"):
            env.validate()

    def test_to_xadd_fields_format(self) -> None:
        """XADD fields 格式对齐 Go 侧 RuntimeEventToStreamFields。"""
        env = RuntimeEventEnvelope(
            event_id="evt-001",
            trace_id="trace-001",
            task_id="task-001",
            run_id="run-001",
            event_type="agent.run.completed",
            runtime_event={
                "id": "evt-001",
                "type": "agent.run.completed",
                "task_id": "task-001",
                "run_id": "run-001",
                "timestamp": "2026-07-06T10:00:00Z",
                "payload": {"output": "done"},
            },
            produced_by="worker-01",
        )

        fields = env.to_xadd_fields()

        # 验证必要字段存在
        assert fields["schema_version"] == SCHEMA_VERSION
        assert fields["event_id"] == "evt-001"
        assert fields["trace_id"] == "trace-001"
        assert fields["task_id"] == "task-001"
        assert fields["run_id"] == "run-001"
        assert fields["type"] == "agent.run.completed"
        assert fields["produced_by"] == "worker-01"

        # payload 是完整 JSON string
        payload = fields["payload"]
        assert isinstance(payload, str)
        decoded = json.loads(payload)
        assert decoded["event_id"] == "evt-001"
        # 内层 runtime_event 在 payload JSON 中
        assert decoded["runtime_event"]["payload"]["output"] == "done"

    def test_payload_roundtrip(self) -> None:
        """payload JSON 可被 Go 侧 DecodeRuntimeEventEnvelope 正确解析。

        验证 payload 中包含完整 envelope 结构：
        event_id / trace_id / task_id / run_id / event_type / runtime_event / produced_by / schema_version
        """
        env = RuntimeEventEnvelope(
            event_id="evt-roundtrip",
            trace_id="trace-roundtrip",
            task_id="task-roundtrip",
            run_id="run-roundtrip",
            event_type="agent.run.started",
            runtime_event={
                "id": "evt-roundtrip",
                "type": "agent.run.started",
                "task_id": "task-roundtrip",
                "run_id": "run-roundtrip",
                "timestamp": "2026-07-06T10:00:00Z",
                "payload": {"agent_id": "agent-default"},
            },
            produced_by="worker-01",
        )

        payload_str = env.to_payload_json()
        decoded = json.loads(payload_str)

        assert decoded["event_id"] == "evt-roundtrip"
        assert decoded["trace_id"] == "trace-roundtrip"
        assert decoded["runtime_event"]["id"] == "evt-roundtrip"
        assert decoded["runtime_event"]["type"] == "agent.run.started"
        assert decoded["schema_version"] == SCHEMA_VERSION
