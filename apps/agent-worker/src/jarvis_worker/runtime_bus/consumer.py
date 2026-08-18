"""Run queue consumer：新消息消费、pending 接管和原子 dead-letter。"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from jarvis_worker.runtime_bus import RedisClientProtocol, ensure_consumer_group
from jarvis_worker.runtime_bus.messages import (
    FIELD_PAYLOAD,
    FIELD_SCHEMA_VERSION,
    GROUP_WORKER_POOL,
    SCHEMA_VERSION,
    STREAM_RUN_DEAD_LETTER,
    STREAM_RUN_QUEUE,
    RunJobMessage,
)


def _is_nogroup_error(exc: Exception) -> bool:
    """只识别 Redis consumer group 丢失，避免吞掉其他传输错误。"""
    return "NOGROUP" in str(exc).upper()


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
class RunQueueDelivery:
    """一条 Run Queue 投递及其 Redis delivery 元数据。"""

    message_id: str
    stream: str
    fields: dict[str, Any] = field(repr=False)
    job: RunJobMessage | None = None
    delivery_count: int = 1
    reclaimed: bool = False
    error_code: str | None = None
    error_message: str | None = None
    retryable: bool = True

    @property
    def valid(self) -> bool:
        return self.job is not None and self.error_code is None


class RunQueueConsumer:
    """消费 RunJob，并由 Worker 按周期接管崩溃进程遗留的 pending。"""

    def __init__(
        self,
        client: RedisClientProtocol,
        consumer_name: str,
        group: str = GROUP_WORKER_POOL,
        *,
        reclaim_idle_ms: int = 65_000,
        max_deliveries: int = 3,
    ):
        self._client = client
        self._consumer = consumer_name
        self._group = group
        # 必须晚于 60 秒 PostgreSQL Run lease，避免把健康 Worker 误判为失联。
        self._reclaim_idle_ms = max(reclaim_idle_ms, 65_000)
        self._max_deliveries = max(1, min(max_deliveries, 10))
        self._pending_scan_start = "-"

    def read_delivery(self, block_ms: int | None = None) -> RunQueueDelivery | None:
        """读取一条新消息；格式错误也返回带 message_id 的 delivery。"""
        try:
            result = self._client.xreadgroup(
                groupname=self._group,
                consumername=self._consumer,
                streams={STREAM_RUN_QUEUE: ">"},
                count=1,
                block=block_ms,
            )
        except Exception as exc:
            if not _is_nogroup_error(exc):
                raise RuntimeError(f"XREADGROUP 失败: {exc}") from exc
            ensure_consumer_group(
                self._client, STREAM_RUN_QUEUE, self._group, start_id="0"
            )
            try:
                result = self._client.xreadgroup(
                    groupname=self._group,
                    consumername=self._consumer,
                    streams={STREAM_RUN_QUEUE: ">"},
                    count=1,
                    block=block_ms,
                )
            except Exception as retry_exc:
                raise RuntimeError(
                    f"XREADGROUP 重建 consumer group 后仍失败: {retry_exc}"
                ) from retry_exc
        return self._decode_result(result, reclaimed=False, delivery_count=1)

    def read_one(
        self, block_ms: int | None = None
    ) -> tuple[RunJobMessage | None, str | None, str | None]:
        """兼容旧调用方；非法 delivery 保持抛出 ValueError。"""
        delivery = self.read_delivery(block_ms=block_ms)
        if delivery is None:
            return None, None, None
        if not delivery.valid:
            raise ValueError(delivery.error_message or "RunJobMessage 非法")
        return delivery.job, delivery.message_id, delivery.stream

    def claim_stale_one(self) -> RunQueueDelivery | None:
        """按指数 idle 退避接管一条 pending RunJob。"""
        try:
            pending = self._client.xpending_range(
                STREAM_RUN_QUEUE,
                self._group,
                self._pending_scan_start,
                "+",
                20,
            )
        except Exception as exc:
            if not _is_nogroup_error(exc):
                raise RuntimeError(f"XPENDING run-queue 失败: {exc}") from exc
            ensure_consumer_group(
                self._client, STREAM_RUN_QUEUE, self._group, start_id="0"
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
                    STREAM_RUN_QUEUE,
                    self._group,
                    self._consumer,
                    required_idle,
                    [message_id],
                )
            except Exception as exc:
                raise RuntimeError(
                    f"XCLAIM run-queue 失败: msg_id={message_id}: {exc}"
                ) from exc
            if not messages:
                continue
            # 下次从本条之后继续扫描，避免固定检查 PEL 前 20 条导致饥饿。
            self._pending_scan_start = (
                "-" if len(pending) < 20 else f"({message_id}"
            )
            result = [[STREAM_RUN_QUEUE, messages]]
            return self._decode_result(
                result, reclaimed=True, delivery_count=deliveries + 1
            )
        last_message_id = str(pending[-1].get("message_id", ""))
        self._pending_scan_start = (
            "-" if len(pending) < 20 or not last_message_id
            else f"({last_message_id}"
        )
        return None

    def retry_idle_ms(self, deliveries: int) -> int:
        """第 N 次已投递后，等待 base * 2^(N-1)，最多放大 4 倍。"""
        exponent = min(max(deliveries - 1, 0), 2)
        return self._reclaim_idle_ms * (2 ** exponent)

    def should_dead_letter(self, delivery: RunQueueDelivery) -> bool:
        return (
            not delivery.retryable
            or delivery.delivery_count >= self._max_deliveries
        )

    def dead_letter(
        self,
        delivery: RunQueueDelivery,
        *,
        error_code: str,
        error_message: str,
    ) -> str:
        """原子 XADD DLQ + XACK 原消息；dedupe 防止重复 DLQ 记录。"""
        safe_message = " ".join(str(error_message).split())[:300]
        raw_payload = str(delivery.fields.get(FIELD_PAYLOAD, ""))
        payload_bytes = raw_payload.encode("utf-8", errors="replace")
        fields = {
            FIELD_SCHEMA_VERSION: str(
                delivery.fields.get(FIELD_SCHEMA_VERSION) or SCHEMA_VERSION
            ),
            "type": "run.job.dead_letter",
            "original_stream": delivery.stream,
            "original_message_id": delivery.message_id,
            "consumer_group": self._group,
            "delivery_count": str(delivery.delivery_count),
            "reclaimed": "true" if delivery.reclaimed else "false",
            "error_code": error_code,
            "error_message": safe_message,
            "failed_at": datetime.now(timezone.utc).isoformat(),
            # RunJob payload 含 user_goal/workspace_path，DLQ 只留指纹和大小，
            # 避免把潜在敏感输入复制到更长生命周期的诊断 stream。
            "payload_sha256": hashlib.sha256(payload_bytes).hexdigest(),
            "payload_size_bytes": str(len(payload_bytes)),
        }
        for key in ("job_id", "trace_id", "task_id", "run_id"):
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
            f"jarvis:run-dlq:dedupe:{self._group}:{delivery.message_id}"
        )
        result = self._client.eval(
            _ATOMIC_DEAD_LETTER_SCRIPT,
            3,
            STREAM_RUN_QUEUE,
            STREAM_RUN_DEAD_LETTER,
            dedupe_key,
            *args,
        )
        return str(result)

    def ack(self, msg_id: str) -> bool:
        try:
            count = self._client.xack(STREAM_RUN_QUEUE, self._group, msg_id)
            return count > 0
        except Exception:
            return False

    def _decode_result(
        self,
        result: list,
        *,
        reclaimed: bool,
        delivery_count: int,
    ) -> RunQueueDelivery | None:
        if not result:
            return None
        for stream_name, messages in result:
            for msg_id, raw_fields in messages:
                fields = dict(raw_fields)
                return self._decode_delivery(
                    str(msg_id), str(stream_name), fields,
                    reclaimed=reclaimed, delivery_count=delivery_count,
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
    ) -> RunQueueDelivery:
        message_type = fields.get("type")
        if message_type != "run.job":
            return RunQueueDelivery(
                message_id=msg_id, stream=stream_name, fields=fields,
                delivery_count=delivery_count, reclaimed=reclaimed,
                error_code="RUN_QUEUE_UNSUPPORTED_TYPE",
                error_message=f"Run Queue type 非法: {message_type!r}",
                retryable=False,
            )
        payload = fields.get(FIELD_PAYLOAD, "")
        if not isinstance(payload, str) or not payload:
            return RunQueueDelivery(
                message_id=msg_id, stream=stream_name, fields=fields,
                delivery_count=delivery_count, reclaimed=reclaimed,
                error_code="RUN_QUEUE_MALFORMED",
                error_message="Run Queue 消息缺少 payload",
                retryable=False,
            )
        outer_version = fields.get(FIELD_SCHEMA_VERSION)
        if outer_version != SCHEMA_VERSION:
            return RunQueueDelivery(
                message_id=msg_id, stream=stream_name, fields=fields,
                delivery_count=delivery_count, reclaimed=reclaimed,
                error_code="RUN_QUEUE_SCHEMA_MISMATCH",
                error_message=(
                    f"Run Queue schema_version 不匹配: {outer_version!r}"
                ),
                retryable=False,
            )
        try:
            job = RunJobMessage.from_payload(payload)
        except ValueError as exc:
            return RunQueueDelivery(
                message_id=msg_id, stream=stream_name, fields=fields,
                delivery_count=delivery_count, reclaimed=reclaimed,
                error_code="RUN_QUEUE_MALFORMED",
                error_message=str(exc), retryable=False,
            )
        return RunQueueDelivery(
            message_id=msg_id, stream=stream_name, fields=fields, job=job,
            delivery_count=delivery_count, reclaimed=reclaimed,
        )

    @property
    def consumer_name(self) -> str:
        return self._consumer

    @property
    def group(self) -> str:
        return self._group

    @property
    def max_deliveries(self) -> int:
        return self._max_deliveries
