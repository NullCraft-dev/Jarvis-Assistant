"""RuntimeEvent 构造工具 — 不重新定义 RuntimeEvent shape。

所有 RuntimeEvent 的 shape 对齐：
- Go 侧 dto/types.go RuntimeEvent struct
- shared/src/types.ts RuntimeEvent type
- docs/13-interface-contract.md § Runtime Events
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any

from jarvis_worker.runtime_bus.messages import (
    SCHEMA_VERSION,
    RuntimeEventEnvelope,
)


def _iso_now() -> str:
    """ISO 8601 格式当前时间。"""
    return datetime.now(timezone.utc).isoformat()


def deterministic_event_id(run_id: str, event_type: str, seq: int) -> str:
    """基于 run_id + event_type + seq 生成确定性 event id。

    同一 run 对同一事件类型和序号重试时生成相同 id，
    使 Gateway/SSE 能按 event.id 去重，避免 partical publish 后重试产生重复事件。

    使用 UUID5，既保持重试幂等，也与 PostgreSQL UUID schema 对齐。
    """
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"jarvis:event:{run_id}:{event_type}:{seq}"))


def deterministic_step_id(run_id: str, seq: int) -> str:
    """基于 run_id + seq 生成确定性 step_id。"""
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"jarvis:step:{run_id}:{seq}"))


def build_runtime_event(
    event_type: str,
    task_id: str,
    run_id: str,
    step_id: str = "",
    payload: dict[str, Any] | None = None,
    event_id: str = "",
) -> dict[str, Any]:
    """构造一个 dto.RuntimeEvent dict。

    Args:
        event_type: RuntimeEventType 字符串
        task_id: 关联 task id
        run_id: 关联 run id
        step_id: 关联 step id（可选）
        payload: 事件 payload（可选）
        event_id: 事件 id。空字符串表示随机生成（仅在测试等非生产场景使用）

    Returns:
        RuntimeEvent dict（对齐 dto.RuntimeEvent shape）
    """
    eid = event_id if event_id else str(uuid.uuid4())
    event: dict[str, Any] = {
        "id": eid,
        "type": event_type,
        "task_id": task_id,
        "run_id": run_id,
        "timestamp": _iso_now(),
    }
    if step_id:
        event["step_id"] = step_id
    if payload:
        event["payload"] = payload
    else:
        event["payload"] = {}
    return event


def build_envelope(
    event: dict[str, Any],
    trace_id: str,
    worker_id: str,
) -> RuntimeEventEnvelope:
    """将 RuntimeEvent dict 包装为 RuntimeEventEnvelope。

    envelope 的元数据必须与内层 RuntimeEvent 一致：
      - envelope.event_id == runtime_event.id
      - envelope.task_id == runtime_event.task_id
      - envelope.run_id == runtime_event.run_id
      - envelope.event_type == runtime_event.type

    Args:
        event: 已构造的 RuntimeEvent dict
        trace_id: 链路追踪 id（复用 RunJobMessage.trace_id）
        worker_id: 产生事件的 worker id

    Returns:
        已构造但未校验的 RuntimeEventEnvelope
    """
    return RuntimeEventEnvelope(
        event_id=event["id"],
        trace_id=trace_id,
        task_id=event["task_id"],
        run_id=event["run_id"],
        event_type=event["type"],
        runtime_event=event,
        produced_by=worker_id,
        schema_version=SCHEMA_VERSION,
    )
