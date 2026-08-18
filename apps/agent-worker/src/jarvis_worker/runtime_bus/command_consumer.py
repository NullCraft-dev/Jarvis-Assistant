"""Worker command consumer：严格解码、pending 接管和原子 dead-letter。"""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from jarvis_worker.runtime_bus import RedisClientProtocol, ensure_consumer_group
from jarvis_worker.runtime_bus.messages import (
    FIELD_PAYLOAD,
    FIELD_SCHEMA_VERSION,
    GROUP_WORKER_POOL,
    SCHEMA_VERSION,
    STREAM_WORKER_COMMAND,
    STREAM_WORKER_COMMAND_DEAD_LETTER,
    McpDiscoveryRefreshCommand,
    PermissionDecisionCommand,
    RunCancelCommand,
    RunPauseCommand,
)


def _is_nogroup_error(exc: Exception) -> bool:
    """只识别 Redis consumer group 丢失，避免吞掉其他传输错误。"""
    return "NOGROUP" in str(exc).upper()

log = logging.getLogger("jarvis_worker.command_consumer")

CMD_RUN_CANCEL = "run.cancel"
CMD_RUN_PAUSE = "run.pause"
CMD_MCP_DISCOVERY_REFRESH = "mcp.discovery.refresh"
CMD_UNSUPPORTED = object()
CMD_MALFORMED = object()

DLQ_DEDUPE_TTL_SECONDS = 7 * 24 * 60 * 60
DLQ_MAXLEN = 10_000

_ATOMIC_DEAD_LETTER_SCRIPT = """
local source_stream = KEYS[1]
local dead_letter_stream = KEYS[2]
local dedupe_key = KEYS[3]
local group = ARGV[1]
local message_id = ARGV[2]
local ttl = tonumber(ARGV[3])
local maxlen = tonumber(ARGV[4])

if redis.call("EXISTS", dedupe_key) == 1 then
    redis.call("XACK", source_stream, group, message_id)
    return "0"
end

local fields = {}
for i = 5, #ARGV, 2 do
    table.insert(fields, ARGV[i])
    table.insert(fields, ARGV[i + 1])
end
local dead_letter_id = redis.call(
    "XADD", dead_letter_stream, "MAXLEN", "~", maxlen, "*", unpack(fields)
)
redis.call("SET", dedupe_key, "1", "EX", ttl)
redis.call("XACK", source_stream, group, message_id)
return dead_letter_id
"""


@dataclass(frozen=True)
class WorkerCommandDelivery:
    """一条 worker command 及其 Redis delivery 元数据。"""

    message_id: str
    stream: str
    fields: dict[str, Any] = field(repr=False)
    command: Any = None
    delivery_count: int = 1
    reclaimed: bool = False
    error_code: str | None = None
    error_message: str | None = None

    @property
    def valid(self) -> bool:
        return self.command is not None and self.error_code is None


class WorkerCommandConsumer:
    """消费 command；业务状态判断仍由 Worker + PostgreSQL services 完成。"""

    def __init__(
        self,
        client: RedisClientProtocol,
        consumer_name: str,
        group: str = GROUP_WORKER_POOL,
        *,
        reclaim_idle_ms: int = 5_000,
    ):
        self._client = client
        self._consumer = consumer_name
        self._group = group
        self._reclaim_idle_ms = max(1_000, min(reclaim_idle_ms, 300_000))
        self._pending_scan_start = "-"

    def read_delivery(
        self, block_ms: int | None = 200
    ) -> WorkerCommandDelivery | None:
        try:
            result = self._client.xreadgroup(
                groupname=self._group,
                consumername=self._consumer,
                streams={STREAM_WORKER_COMMAND: ">"},
                count=1,
                block=block_ms,
            )
        except Exception as exc:
            if not _is_nogroup_error(exc):
                raise RuntimeError(
                    f"XREADGROUP worker-command 失败: {exc}"
                ) from exc
            ensure_consumer_group(
                self._client,
                STREAM_WORKER_COMMAND,
                self._group,
                start_id="0",
            )
            try:
                result = self._client.xreadgroup(
                    groupname=self._group,
                    consumername=self._consumer,
                    streams={STREAM_WORKER_COMMAND: ">"},
                    count=1,
                    block=block_ms,
                )
            except Exception as retry_exc:
                raise RuntimeError(
                    "XREADGROUP worker-command 重建 consumer group 后仍失败: "
                    f"{retry_exc}"
                ) from retry_exc
        return self._decode_result(result, reclaimed=False, delivery_count=1)

    def read_one(
        self, block_ms: int | None = 200
    ) -> tuple[Any, str | None, str | None]:
        """兼容旧调用方；错误仍映射为原 sentinel。"""
        delivery = self.read_delivery(block_ms=block_ms)
        if delivery is None:
            return None, None, None
        command = delivery.command
        if not delivery.fields.get("type"):
            command = None
        return command, delivery.message_id, delivery.stream

    def claim_stale_delivery(self) -> WorkerCommandDelivery | None:
        """有界扫描 PEL，并按 delivery count 指数退避接管一条 command。"""
        try:
            pending = self._client.xpending_range(
                STREAM_WORKER_COMMAND,
                self._group,
                self._pending_scan_start,
                "+",
                20,
            )
        except Exception as exc:
            if not _is_nogroup_error(exc):
                raise RuntimeError(
                    f"XPENDING worker-command 失败: {exc}"
                ) from exc
            ensure_consumer_group(
                self._client,
                STREAM_WORKER_COMMAND,
                self._group,
                start_id="0",
            )
            self._pending_scan_start = "-"
            return None

        if not pending:
            self._pending_scan_start = "-"
            return None

        for entry in pending:
            message_id = str(entry.get("message_id", ""))
            deliveries = max(1, int(entry.get("times_delivered", 1)))
            idle_ms = max(0, int(entry.get("time_since_delivered", 0)))
            required_idle = self.retry_idle_ms(deliveries)
            if not message_id or idle_ms < required_idle:
                continue
            try:
                messages = self._client.xclaim(
                    STREAM_WORKER_COMMAND,
                    self._group,
                    self._consumer,
                    required_idle,
                    [message_id],
                )
            except Exception as exc:
                raise RuntimeError(
                    f"XCLAIM worker-command 失败: msg_id={message_id}: {exc}"
                ) from exc
            if not messages:
                continue
            self._pending_scan_start = (
                "-" if len(pending) < 20 else f"({message_id}"
            )
            return self._decode_result(
                [[STREAM_WORKER_COMMAND, messages]],
                reclaimed=True,
                delivery_count=deliveries + 1,
            )

        last_message_id = str(pending[-1].get("message_id", ""))
        self._pending_scan_start = (
            "-" if len(pending) < 20 or not last_message_id
            else f"({last_message_id}"
        )
        return None

    def claim_stale_one(
        self, min_idle_ms: int | None = None
    ) -> tuple[Any, str | None, str | None]:
        """兼容旧调用方；min_idle_ms 仅用于旧测试覆盖。"""
        original = self._reclaim_idle_ms
        if min_idle_ms is not None:
            self._reclaim_idle_ms = max(0, min_idle_ms)
        try:
            delivery = self.claim_stale_delivery()
        finally:
            self._reclaim_idle_ms = original
        if delivery is None:
            return None, None, None
        return delivery.command, delivery.message_id, delivery.stream

    def retry_idle_ms(self, deliveries: int) -> int:
        exponent = min(max(deliveries - 1, 0), 4)
        return self._reclaim_idle_ms * (2 ** exponent)

    def dead_letter(self, delivery: WorkerCommandDelivery) -> str:
        """原子写脱敏 command DLQ 并 ACK，避免 poison message 永久占据 PEL。"""
        raw_payload = str(delivery.fields.get(FIELD_PAYLOAD, ""))
        payload_bytes = raw_payload.encode("utf-8", errors="replace")
        fields = {
            FIELD_SCHEMA_VERSION: str(
                delivery.fields.get(FIELD_SCHEMA_VERSION) or SCHEMA_VERSION
            ),
            "type": "worker.command.dead_letter",
            "original_stream": delivery.stream,
            "original_message_id": delivery.message_id,
            "consumer_group": self._group,
            "delivery_count": str(delivery.delivery_count),
            "reclaimed": "true" if delivery.reclaimed else "false",
            "error_code": delivery.error_code or "WORKER_COMMAND_MALFORMED",
            "error_message": " ".join(
                str(delivery.error_message or "worker command 非法").split()
            )[:300],
            "failed_at": datetime.now(timezone.utc).isoformat(),
            "payload_sha256": hashlib.sha256(payload_bytes).hexdigest(),
            "payload_size_bytes": str(len(payload_bytes)),
        }
        for key in ("command_id", "trace_id", "task_id", "run_id", "request_id"):
            value = delivery.fields.get(key)
            if value:
                fields[key] = str(value)

        args: list[str] = [
            self._group,
            delivery.message_id,
            str(DLQ_DEDUPE_TTL_SECONDS),
            str(DLQ_MAXLEN),
        ]
        for key, value in fields.items():
            args.extend([key, value])
        dedupe_key = (
            f"jarvis:worker-command-dlq:dedupe:{self._group}:"
            f"{delivery.message_id}"
        )
        result = self._client.eval(
            _ATOMIC_DEAD_LETTER_SCRIPT,
            3,
            STREAM_WORKER_COMMAND,
            STREAM_WORKER_COMMAND_DEAD_LETTER,
            dedupe_key,
            *args,
        )
        return str(result)

    def ack(self, msg_id: str) -> bool:
        try:
            return self._client.xack(
                STREAM_WORKER_COMMAND, self._group, msg_id
            ) > 0
        except Exception:
            return False

    def _decode_result(
        self, result: list, *, reclaimed: bool, delivery_count: int
    ) -> WorkerCommandDelivery | None:
        if not result:
            return None
        for stream_name, messages in result:
            for msg_id, fields in messages:
                return self._decode_delivery(
                    str(msg_id),
                    str(stream_name),
                    dict(fields),
                    reclaimed=reclaimed,
                    delivery_count=delivery_count,
                )
        return None

    @staticmethod
    def _decode_delivery(
        msg_id: str,
        stream_name: str,
        fields: dict[str, Any],
        *,
        reclaimed: bool,
        delivery_count: int,
    ) -> WorkerCommandDelivery:
        base = dict(
            message_id=msg_id,
            stream=stream_name,
            fields=fields,
            delivery_count=delivery_count,
            reclaimed=reclaimed,
        )
        outer_version = fields.get(FIELD_SCHEMA_VERSION)
        if outer_version != SCHEMA_VERSION:
            return WorkerCommandDelivery(
                **base,
                command=CMD_MALFORMED,
                error_code="WORKER_COMMAND_SCHEMA_MISMATCH",
                error_message=(
                    f"worker-command schema_version 不匹配: {outer_version!r}"
                ),
            )

        cmd_type = fields.get("type")
        if not isinstance(cmd_type, str) or not cmd_type:
            return WorkerCommandDelivery(
                **base,
                command=CMD_MALFORMED,
                error_code="WORKER_COMMAND_MALFORMED",
                error_message="worker-command 缺少 type 字段",
            )
        if cmd_type not in (
            CMD_RUN_CANCEL, CMD_RUN_PAUSE, CMD_MCP_DISCOVERY_REFRESH,
            "permission.decision",
        ):
            return WorkerCommandDelivery(
                **base,
                command=CMD_UNSUPPORTED,
                error_code="WORKER_COMMAND_UNSUPPORTED_TYPE",
                error_message=f"worker-command type 不支持: {cmd_type!r}",
            )

        payload = fields.get(FIELD_PAYLOAD)
        if not isinstance(payload, str) or not payload:
            return WorkerCommandDelivery(
                **base,
                command=CMD_MALFORMED,
                error_code="WORKER_COMMAND_MALFORMED",
                error_message=f"worker-command {cmd_type} 缺少 payload",
            )
        try:
            if cmd_type == CMD_RUN_CANCEL:
                command = RunCancelCommand.from_payload(payload)
            elif cmd_type == CMD_RUN_PAUSE:
                command = RunPauseCommand.from_payload(payload)
            elif cmd_type == CMD_MCP_DISCOVERY_REFRESH:
                command = McpDiscoveryRefreshCommand.from_payload(payload)
            else:
                command = PermissionDecisionCommand.from_payload(payload)
        except ValueError as exc:
            return WorkerCommandDelivery(
                **base,
                command=CMD_MALFORMED,
                error_code="WORKER_COMMAND_MALFORMED",
                error_message=str(exc),
            )

        routing_keys = ["command_id", "trace_id"]
        if cmd_type != CMD_MCP_DISCOVERY_REFRESH:
            routing_keys.extend(["task_id", "run_id"])
        for key in routing_keys:
            outer = fields.get(key)
            inner = getattr(command, key)
            if outer != inner:
                return WorkerCommandDelivery(
                    **base,
                    command=CMD_MALFORMED,
                    error_code="WORKER_COMMAND_ROUTING_MISMATCH",
                    error_message=f"worker-command {key} outer/payload 不一致",
                )
        return WorkerCommandDelivery(**base, command=command)

    @property
    def consumer_name(self) -> str:
        return self._consumer

    @property
    def group(self) -> str:
        return self._group
