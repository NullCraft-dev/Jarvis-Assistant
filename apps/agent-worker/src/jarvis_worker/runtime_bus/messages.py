"""Redis message types — 与 Go 侧 redisruntime/messages.go 对齐。

真源：docs/13-interface-contract.md § Redis Message Contract (2B-1a)
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

# -- Stream keys（对齐 Go 侧 redisruntime/keys.go） --

STREAM_RUN_QUEUE = "jarvis:stream:run-queue"
STREAM_RUN_DEAD_LETTER = "jarvis:stream:run-dead-letter"
STREAM_WORKER_COMMAND = "jarvis:stream:worker-command"
STREAM_WORKER_COMMAND_DEAD_LETTER = "jarvis:stream:worker-command-dead-letter"
STREAM_RUNTIME_EVENT = "jarvis:stream:runtime-event"
STREAM_WORKER_HEARTBEAT = "jarvis:stream:worker-heartbeat"
STREAM_PENDING_PERMISSION = "jarvis:stream:pending-permission"

# -- Consumer groups（对齐 Go 侧 redisruntime/keys.go） --

GROUP_WORKER_POOL = "jarvis:group:worker-pool"
GROUP_GATEWAY_EVENTS = "jarvis:group:gateway-events"

# -- Schema version（对齐 Go 侧 redisruntime/keys.go） --

SCHEMA_VERSION = "2B-1a.1"

# -- XADD field names（对齐 Go 侧 redisruntime/keys.go） --

FIELD_SCHEMA_VERSION = "schema_version"
FIELD_PAYLOAD = "payload"
FIELD_JOB_ID = "job_id"
FIELD_TRACE_ID = "trace_id"
FIELD_TASK_ID = "task_id"
FIELD_RUN_ID = "run_id"
FIELD_EVENT_ID = "event_id"
FIELD_COMMAND_ID = "command_id"
FIELD_REQUEST_ID = "request_id"
FIELD_WORKER_ID = "worker_id"


@dataclass
class RunJobMessage:
    """Go Orchestrator 入队到 Redis run queue 的 job 消息。

    Python worker 消费此消息后启动 AgentRun loop。
    """

    job_id: str
    trace_id: str
    task_id: str
    run_id: str
    user_goal: str
    created_at: str
    schema_version: str = SCHEMA_VERSION
    workspace_path: str = ""
    conversation_id: str = ""
    resume_from_checkpoint: bool = False
    retry_from_checkpoint: bool = False
    scheduled_task_id: str = ""
    authorized_tools: list[str] = field(default_factory=list)
    source_policy: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_payload(cls, payload_str: str) -> "RunJobMessage":
        """从 Redis stream message 的 payload JSON string 解码并校验。"""
        try:
            data = json.loads(payload_str)
        except json.JSONDecodeError as e:
            raise ValueError(f"RunJobMessage payload 无效 JSON: {e}") from e

        return cls.from_dict(data)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "RunJobMessage":
        """从 dict 解码并校验必要字段。"""
        # 校验 schema_version
        ver = data.get("schema_version")
        if ver != SCHEMA_VERSION:
            raise ValueError(
                f"schema_version 不匹配: got {ver!r}, want {SCHEMA_VERSION!r}"
            )

        # 校验必要字段
        required = ["job_id", "trace_id", "task_id", "run_id", "user_goal", "created_at"]
        for f in required:
            v = data.get(f)
            if not v or not isinstance(v, str):
                raise ValueError(f"RunJobMessage 缺少必要字段或非 string: {f}")

        return cls(
            job_id=data["job_id"],
            trace_id=data["trace_id"],
            task_id=data["task_id"],
            run_id=data["run_id"],
            user_goal=data["user_goal"],
            created_at=data["created_at"],
            schema_version=ver,
            workspace_path=data.get("workspace_path", ""),
            conversation_id=data.get("conversation_id", ""),
            resume_from_checkpoint=data.get("resume_from_checkpoint") is True,
            retry_from_checkpoint=data.get("retry_from_checkpoint") is True,
            scheduled_task_id=data.get("scheduled_task_id", ""),
            authorized_tools=[str(item) for item in data.get("authorized_tools", []) if isinstance(item, str)],
            source_policy=dict(data.get("source_policy", {})) if isinstance(data.get("source_policy", {}), dict) else {},
        )

    def to_dict(self) -> dict[str, Any]:
        """序列化为 dict（用于 JSON payload）。"""
        d: dict[str, Any] = {
            "job_id": self.job_id,
            "trace_id": self.trace_id,
            "task_id": self.task_id,
            "run_id": self.run_id,
            "user_goal": self.user_goal,
            "created_at": self.created_at,
            "schema_version": self.schema_version,
        }
        if self.workspace_path:
            d["workspace_path"] = self.workspace_path
        if self.conversation_id:
            d["conversation_id"] = self.conversation_id
        if self.resume_from_checkpoint:
            d["resume_from_checkpoint"] = True
        if self.retry_from_checkpoint:
            d["retry_from_checkpoint"] = True
        if self.scheduled_task_id:
            d["scheduled_task_id"] = self.scheduled_task_id
        if self.authorized_tools:
            d["authorized_tools"] = list(self.authorized_tools)
        if self.source_policy:
            d["source_policy"] = dict(self.source_policy)
        return d

    def to_payload_json(self) -> str:
        """序列化为 payload JSON string（写入 Redis XADD）。"""
        return json.dumps(self.to_dict(), ensure_ascii=False)


@dataclass
class RuntimeEventEnvelope:
    """Python worker 通过 Redis 上报 RuntimeEvent 的传输信封。

    在 dto.RuntimeEvent 之上附加传输层元数据，不重新定义 RuntimeEvent shape。
    """

    event_id: str
    trace_id: str
    task_id: str
    run_id: str
    event_type: str
    runtime_event: dict[str, Any]
    produced_by: str
    schema_version: str = SCHEMA_VERSION
    internal: dict[str, Any] = field(default_factory=dict, repr=False)

    def validate(self) -> None:
        """校验 envelope 与内层 runtime_event 的一致性。

        对齐 Go 侧 DecodeRuntimeEventEnvelope 的校验逻辑。
        """
        re = self.runtime_event

        # 内层核心字段
        for f in ("id", "type", "task_id", "run_id", "timestamp"):
            if not re.get(f):
                raise ValueError(f"runtime_event.{f} 为空")

        # envelope 与内层一致性
        if self.event_id != re["id"]:
            raise ValueError(
                f"event_id {self.event_id!r} != runtime_event.id {re['id']!r}"
            )
        if self.event_type != re["type"]:
            raise ValueError(
                f"event_type {self.event_type!r} != runtime_event.type {re['type']!r}"
            )
        if self.task_id != re["task_id"]:
            raise ValueError(
                f"task_id {self.task_id!r} != runtime_event.task_id {re['task_id']!r}"
            )
        if self.run_id != re["run_id"]:
            raise ValueError(
                f"run_id {self.run_id!r} != runtime_event.run_id {re['run_id']!r}"
            )

    def to_payload_json(self) -> str:
        """序列化为 payload JSON string。"""
        d = {
            "event_id": self.event_id,
            "trace_id": self.trace_id,
            "task_id": self.task_id,
            "run_id": self.run_id,
            "event_type": self.event_type,
            "runtime_event": self.runtime_event,
            "produced_by": self.produced_by,
            "schema_version": self.schema_version,
        }
        return json.dumps(d, ensure_ascii=False)

    def to_xadd_fields(self) -> dict[str, str]:
        """转换为 Redis XADD field-value 对。

        对齐 Go 侧 RuntimeEventToStreamFields：
          schema_version + payload（完整 JSON 字符串）+ 冗余标量路由字段
        """
        return {
            FIELD_SCHEMA_VERSION: self.schema_version,
            FIELD_PAYLOAD: self.to_payload_json(),
            FIELD_EVENT_ID: self.event_id,
            FIELD_TRACE_ID: self.trace_id,
            FIELD_TASK_ID: self.task_id,
            FIELD_RUN_ID: self.run_id,
            "type": self.event_type,
            "produced_by": self.produced_by,
        }


@dataclass
class PermissionDecisionCommand:
    """Go Orchestrator 通过 worker command stream 发送的权限决策命令。

    对齐 Go 侧 redisruntime/messages.go PermissionDecisionCommand。
    作为 command 消息，必须携带 trace_id。
    type 字段固定为 "permission.decision"。
    """

    command_id: str
    trace_id: str
    request_id: str
    task_id: str
    run_id: str
    decision: str
    decided_at: str
    schema_version: str = SCHEMA_VERSION
    note: str = ""

    @classmethod
    def from_payload(cls, payload_str: str) -> "PermissionDecisionCommand":
        """从 Redis stream message 的 payload JSON string 解码并校验。"""
        try:
            data = json.loads(payload_str)
        except json.JSONDecodeError as e:
            raise ValueError(f"PermissionDecisionCommand payload 无效 JSON: {e}") from e
        return cls.from_dict(data)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "PermissionDecisionCommand":
        """从 dict 解码并校验必要字段。"""
        ver = data.get("schema_version")
        if ver != SCHEMA_VERSION:
            raise ValueError(
                f"schema_version 不匹配: got {ver!r}, want {SCHEMA_VERSION!r}"
            )

        required = [
            "command_id", "trace_id", "request_id", "task_id",
            "run_id", "decision", "decided_at", "schema_version",
        ]
        for f in required:
            v = data.get(f)
            if not v or not isinstance(v, str):
                raise ValueError(
                    f"PermissionDecisionCommand 缺少必要字段或非 string: {f}"
                )

        # type 字段可选（3C 阶段已存在），若存在必须为 permission.decision
        cmd_type = data.get("type", "")
        if cmd_type and cmd_type != "permission.decision":
            raise ValueError(
                f"PermissionDecisionCommand type 不匹配: got {cmd_type!r}"
            )

        return cls(
            command_id=data["command_id"],
            trace_id=data["trace_id"],
            request_id=data["request_id"],
            task_id=data["task_id"],
            run_id=data["run_id"],
            decision=data["decision"],
            decided_at=data["decided_at"],
            schema_version=ver,
            note=data.get("note", ""),
        )


@dataclass
class RunCancelCommand:
    """Go Orchestrator 通过 worker command stream 发送的取消运行命令（3C cancel）。

    作为 command 消息，必须携带 trace_id。
    """

    command_id: str
    trace_id: str
    task_id: str
    run_id: str
    type: str = "run.cancel"
    requested_at: str = ""
    schema_version: str = SCHEMA_VERSION
    reason: str = ""

    @classmethod
    def from_payload(cls, payload_str: str) -> "RunCancelCommand":
        """从 Redis stream message 的 payload JSON string 解码并校验。"""
        try:
            data = json.loads(payload_str)
        except json.JSONDecodeError as e:
            raise ValueError(f"RunCancelCommand payload 无效 JSON: {e}") from e

        return cls.from_dict(data)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "RunCancelCommand":
        """从 dict 解码并校验必要字段。"""
        ver = data.get("schema_version")
        if ver != SCHEMA_VERSION:
            raise ValueError(
                f"schema_version 不匹配: got {ver!r}, want {SCHEMA_VERSION!r}"
            )

        required = [
            "command_id", "trace_id", "task_id", "run_id",
            "type", "requested_at", "schema_version",
        ]
        for f in required:
            v = data.get(f)
            if not v or not isinstance(v, str):
                raise ValueError(f"RunCancelCommand 缺少必要字段或非 string: {f}")

        cmd_type = data["type"]
        if cmd_type != "run.cancel":
            raise ValueError(
                f"不支持的 command type: {cmd_type!r}，当前只支持 run.cancel"
            )

        return cls(
            command_id=data["command_id"],
            trace_id=data["trace_id"],
            task_id=data["task_id"],
            run_id=data["run_id"],
            type=cmd_type,
            requested_at=data["requested_at"],
            schema_version=ver,
            reason=data.get("reason", ""),
        )


@dataclass
class RunPauseCommand:
    """请求 active Worker 在下一个可恢复 checkpoint 暂停 Run。"""

    command_id: str
    trace_id: str
    task_id: str
    run_id: str
    type: str = "run.pause"
    requested_at: str = ""
    schema_version: str = SCHEMA_VERSION
    reason: str = ""

    @classmethod
    def from_payload(cls, payload_str: str) -> "RunPauseCommand":
        try:
            data = json.loads(payload_str)
        except json.JSONDecodeError as exc:
            raise ValueError(f"RunPauseCommand payload 无效 JSON: {exc}") from exc
        return cls.from_dict(data)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "RunPauseCommand":
        if data.get("schema_version") != SCHEMA_VERSION:
            raise ValueError(
                "schema_version 不匹配: "
                f"got {data.get('schema_version')!r}, want {SCHEMA_VERSION!r}"
            )
        required = (
            "command_id", "trace_id", "task_id", "run_id",
            "type", "requested_at", "schema_version",
        )
        for name in required:
            if not isinstance(data.get(name), str) or not data[name]:
                raise ValueError(
                    f"RunPauseCommand 缺少必要字段或非 string: {name}"
                )
        if data["type"] != "run.pause":
            raise ValueError(f"RunPauseCommand type 不匹配: {data['type']!r}")
        return cls(
            command_id=data["command_id"],
            trace_id=data["trace_id"],
            task_id=data["task_id"],
            run_id=data["run_id"],
            type=data["type"],
            requested_at=data["requested_at"],
            schema_version=data["schema_version"],
            reason=data.get("reason", ""),
        )


@dataclass
class McpDiscoveryRefreshCommand:
    """请求一个空闲 Worker 重新发现所有 enabled MCP server。"""

    command_id: str
    trace_id: str
    type: str = "mcp.discovery.refresh"
    requested_at: str = ""
    schema_version: str = SCHEMA_VERSION

    @classmethod
    def from_payload(cls, payload_str: str) -> "McpDiscoveryRefreshCommand":
        try:
            data = json.loads(payload_str)
        except json.JSONDecodeError as exc:
            raise ValueError(
                f"McpDiscoveryRefreshCommand payload 无效 JSON: {exc}"
            ) from exc
        if data.get("schema_version") != SCHEMA_VERSION:
            raise ValueError(
                "schema_version 不匹配: "
                f"got {data.get('schema_version')!r}, want {SCHEMA_VERSION!r}"
            )
        for name in (
            "command_id", "trace_id", "type", "requested_at", "schema_version",
        ):
            if not isinstance(data.get(name), str) or not data[name]:
                raise ValueError(
                    f"McpDiscoveryRefreshCommand 缺少必要字段或非 string: {name}"
                )
        if data["type"] != "mcp.discovery.refresh":
            raise ValueError(
                f"McpDiscoveryRefreshCommand type 不匹配: {data['type']!r}"
            )
        return cls(
            command_id=data["command_id"], trace_id=data["trace_id"],
            type=data["type"], requested_at=data["requested_at"],
            schema_version=data["schema_version"],
        )


@dataclass
class WorkerHeartbeatMessage:
    """Python worker 通过 Redis 上报的心跳和状态消息。

    心跳是状态探针，不属于 command / event 链路，不携带 trace_id。
    Phase 6B-1: 增加 model 字段，暴露模型配置状态。
    """

    worker_id: str
    status: str  # starting / idle / busy / draining / stopped / failed
    reported_at: str
    worker_kind: str = "agent"  # agent / rag；旧消息缺省为 agent
    schema_version: str = SCHEMA_VERSION
    active_run_id: str = ""
    model: dict | None = None  # Phase 6B-1: 模型配置状态
    runtime_bus: dict | None = None  # Redis Runtime Bus 进程级累计指标

    def to_payload_json(self) -> str:
        """序列化为 payload JSON string。"""
        d = {
            "worker_id": self.worker_id,
            "status": self.status,
            "reported_at": self.reported_at,
            "worker_kind": self.worker_kind,
            "schema_version": self.schema_version,
        }
        if self.active_run_id:
            d["active_run_id"] = self.active_run_id
        if self.model is not None:
            d["model"] = self.model
        if self.runtime_bus is not None:
            d["runtime_bus"] = self.runtime_bus
        return json.dumps(d, ensure_ascii=False)

    def to_xadd_fields(self) -> dict[str, str]:
        """转换为 Redis XADD field-value 对。

        对齐 Go 侧 WorkerHeartbeatToStreamFields：
          schema_version + payload（完整 JSON 字符串）+ 冗余标量路由字段
        心跳不携带 trace_id。
        """
        return {
            FIELD_SCHEMA_VERSION: self.schema_version,
            FIELD_PAYLOAD: self.to_payload_json(),
            FIELD_WORKER_ID: self.worker_id,
            "type": "worker.heartbeat",
            "status": self.status,
            "reported_at": self.reported_at,
        }
