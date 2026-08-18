"""OutboxPublisher — 从 PostgreSQL Outbox 读取事件并发布到 Redis（async）。

发布流程：
1. 短事务 claim 一批 pending 事件（FOR UPDATE SKIP LOCKED）→ COMMIT
2. 事务外 async Redis publish（持 Lua 原子去重）
3. 短事务标记 delivered / failed
"""

import asyncio
import json
import logging
from typing import Optional
from uuid import UUID, uuid4

from redis.exceptions import NoScriptError

from jarvis_worker.database.engine import get_session_factory
from jarvis_worker.database.outbox.repository import PostgresOutboxRepository

logger = logging.getLogger(__name__)

# Lua 脚本：原子检查 event_id dedupe key + XADD
ATOMIC_XADD_SCRIPT = """
local dedupe_key = KEYS[1]
local stream_key = KEYS[2]
local event_id = ARGV[1]
local ttl = tonumber(ARGV[2])

-- 检查是否已发布
if redis.call("EXISTS", dedupe_key) == 1 then
    return 0  -- already published
end

-- 重构 fields: 跳过 event_id，其余成对传入
local fields = {}
for i = 3, #ARGV, 2 do
    table.insert(fields, ARGV[i])
    table.insert(fields, ARGV[i+1])
end
local stream_id = redis.call("XADD", stream_key, "MAXLEN", "~", "100000", "*", unpack(fields))
redis.call("SET", dedupe_key, "1", "EX", ttl)
return stream_id
"""

# Outbox event_type → Redis stream mapping
EVENT_TO_STREAM = {
    "task.created": "jarvis:stream:run-queue",
    "run.resume.requested": "jarvis:stream:run-queue",
    "run.retry.requested": "jarvis:stream:run-queue",
    "run.step_retry.requested": "jarvis:stream:run-queue",
    "run.queue.reconciled": "jarvis:stream:run-queue",
    "run.pause.requested": "jarvis:stream:worker-command",
    "run.cancel.requested": "jarvis:stream:worker-command",
    "permission.decision": "jarvis:stream:worker-command",
}
RUN_JOB_EVENT_TYPES = {
    "task.created", "run.resume.requested", "run.retry.requested",
    "run.step_retry.requested",
    "run.queue.reconciled",
}
WORKER_COMMAND_EVENT_TYPES = {
    "run.pause.requested", "run.cancel.requested", "permission.decision"
}
# Durable events that must go through Outbox → Redis
EVENT_TO_RUNTIME_STREAM = {
    "task.updated",
    "agent.run.started",
    "agent.run.paused",
    "agent.run.resumed",
    "agent.run.completed",
    "agent.run.failed",
    "agent.run.cancelled",
    "agent.step.started",
    "agent.step.updated",
    "agent.step.completed",
    "agent.step.failed",
    "model.call.started",
    "model.context.prepared",
    "model.call.completed",
    "model.call.failed",
    "tool.call.started",
    "tool.call.finished",
    "tool.call.failed",
    "permission.required",
    "permission.resolved",
    "permission.expired",
    "artifact.created",
}

DEDUPE_TTL_SECONDS = 86400  # 24h — 与 stream 保留策略匹配


class OutboxPublisher:
    """Outbox Publisher 后台 async task。"""

    def __init__(
        self,
        redis_client,  # redis.asyncio.Redis
        poll_interval_ms: int = 500,
        batch_size: int = 32,
        lease_seconds: int = 30,
        publisher_id: Optional[str] = None,
    ):
        self._redis = redis_client
        self._poll_interval = poll_interval_ms / 1000.0
        self._batch_size = batch_size
        self._lease_seconds = lease_seconds
        self._publisher_id = publisher_id or f"publisher-{uuid4().hex[:8]}"
        self._running = False
        self._task: Optional[asyncio.Task] = None
        self._lua_sha: Optional[str] = None

    async def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._task = asyncio.create_task(self._loop())
        logger.info("OutboxPublisher 已启动 (publisher=%s)", self._publisher_id)

    async def stop(self) -> None:
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        logger.info("OutboxPublisher 已停止")

    async def publish_once(self) -> int:
        """执行一次发布循环。"""
        await self._ensure_lua_script()
        session_factory = get_session_factory()
        published = 0

        # 1. Claim 一批 pending 事件
        async with session_factory() as session:
            repo = PostgresOutboxRepository(session)
            async with session.begin():
                events = await repo.claim_pending(
                    batch_size=self._batch_size,
                    lease_seconds=self._lease_seconds,
                    claimed_by=self._publisher_id,
                )

        if not events:
            return 0

        # 2. 事务外发布 Redis
        success_ids: list[UUID] = []
        failed_info: list[tuple[UUID, str, str]] = []

        for event in events:
            try:
                stream = EVENT_TO_STREAM.get(event.event_type)
                if stream is None and event.event_type not in EVENT_TO_RUNTIME_STREAM:
                    # 未知事件 → failed, 不静默 delivered
                    failed_info.append((
                        event.id,
                        "UNKNOWN_EVENT_TYPE",
                        f"event_type={event.event_type} 不在已知映射中",
                    ))
                    continue

                if stream is None:
                    # durable runtime event → runtime-event stream
                    stream = "jarvis:stream:runtime-event"

                fields = _build_xadd_fields(event)
                dedupe_key = f"jarvis:outbox:dedupe:{event.event_id}"

                args = [dedupe_key, stream, str(event.event_id), str(DEDUPE_TTL_SECONDS)]
                for k, v in fields.items():
                    args.append(k)
                    args.append(str(v))

                result = await self._eval_atomic_xadd(args)
                # result=0 means already published (idempotent)
                # otherwise returns Redis stream ID
                success_ids.append(event.id)
                if result != 0 and result != "0":
                    published += 1
                    log_published = (
                        logger.info
                        if event.event_type in RUN_JOB_EVENT_TYPES | WORKER_COMMAND_EVENT_TYPES
                        else logger.debug
                    )
                    log_published(
                        "Outbox 事件发布完成: event_type=%s event_id=%s stream=%s redis_id=%s",
                        event.event_type,
                        event.event_id,
                        stream,
                        result,
                        extra={
                            "trace_id": str(event.trace_id),
                            "task_id": str(event.payload.get("task_id", "")),
                            "run_id": str(event.aggregate_id),
                        },
                    )
                else:
                    logger.debug(
                        "Outbox 事件幂等跳过: event_type=%s event_id=%s stream=%s",
                        event.event_type,
                        event.event_id,
                        stream,
                        extra={
                            "trace_id": str(event.trace_id),
                            "task_id": str(event.payload.get("task_id", "")),
                            "run_id": str(event.aggregate_id),
                        },
                    )

            except Exception as e:
                error_code = _classify_error(e)
                error_msg = f"{type(e).__name__}: {e}"[:500]
                failed_info.append((event.id, error_code, error_msg))
                logger.warning(
                    "Outbox 发布失败: event_id=%s type=%s error=%s",
                    event.event_id, event.event_type, error_code,
                    extra={
                        "trace_id": str(event.trace_id),
                        "task_id": str(event.payload.get("task_id", "")),
                        "run_id": str(event.aggregate_id),
                    },
                )

        # 3. 短事务标记结果
        async with session_factory() as session:
            repo = PostgresOutboxRepository(session)
            async with session.begin():
                if success_ids:
                    await repo.mark_delivered(success_ids)
                for eid, code, msg in failed_info:
                    await repo.mark_failed([eid], code, msg)

        if published > 0:
            logger.info("Outbox 发布: published=%d failed=%d", published, len(failed_info))
        return published

    async def _loop(self) -> None:
        while self._running:
            try:
                await self.publish_once()
            except Exception as e:
                logger.error("OutboxPublisher 循环异常: %s", e, exc_info=True)
            await asyncio.sleep(self._poll_interval)

    @property
    def ready(self) -> bool:
        return self._running and self._lua_sha is not None

    async def _ensure_lua_script(self) -> None:
        if self._lua_sha is None:
            self._lua_sha = await self._redis.script_load(ATOMIC_XADD_SCRIPT)

    async def _eval_atomic_xadd(self, args: list[str]):
        await self._ensure_lua_script()
        try:
            return await self._redis.evalsha(self._lua_sha, 2, *args)
        except Exception as exc:
            if not isinstance(exc, NoScriptError) and (
                "NOSCRIPT" not in str(exc).upper()
                and "NO MATCHING SCRIPT" not in str(exc).upper()
            ):
                raise
            # Redis 重启或 SCRIPT FLUSH 后重新加载并重试一次。
            self._lua_sha = await self._redis.script_load(ATOMIC_XADD_SCRIPT)
            return await self._redis.evalsha(self._lua_sha, 2, *args)


def _build_xadd_fields(event) -> dict:
    """构造 Redis XADD fields — 与 Go redisruntime 契约对齐。"""
    payload_str = event.payload if isinstance(event.payload, str) else json.dumps(event.payload, ensure_ascii=False)
    created_at = event.created_at.isoformat() if hasattr(event.created_at, 'isoformat') else str(event.created_at)

    if event.event_type in RUN_JOB_EVENT_TYPES:
        if not isinstance(event.payload, dict):
            raise ValueError(f"{event.event_type} 的 RunJob payload 必须是 object")
        required = ("job_id", "trace_id", "task_id", "run_id", "created_at")
        missing = [key for key in required if not event.payload.get(key)]
        if missing:
            raise ValueError(
                f"{event.event_type} 的 RunJob payload 缺少字段: {', '.join(missing)}"
            )
        return {
            "schema_version": event.schema_version,
            "payload": payload_str,
            "job_id": str(event.payload["job_id"]),
            "trace_id": str(event.payload["trace_id"]),
            "task_id": str(event.payload["task_id"]),
            "run_id": str(event.payload["run_id"]),
            "type": "run.job",
            "created_at": str(event.payload["created_at"]),
        }

    if event.event_type in WORKER_COMMAND_EVENT_TYPES:
        if not isinstance(event.payload, dict):
            raise ValueError(f"{event.event_type} 的 command payload 必须是 object")
        required = ("command_id", "trace_id", "task_id", "run_id", "type")
        if event.event_type == "permission.decision":
            required += ("request_id", "decided_at")
        else:
            required += ("requested_at",)
        missing = [key for key in required if not event.payload.get(key)]
        if missing:
            raise ValueError(
                f"{event.event_type} 的 command payload 缺少字段: {', '.join(missing)}"
            )
        fields = {
            "schema_version": event.schema_version,
            "payload": payload_str,
            "command_id": str(event.payload["command_id"]),
            "trace_id": str(event.payload["trace_id"]),
            "task_id": str(event.payload["task_id"]),
            "run_id": str(event.payload["run_id"]),
            "type": str(event.payload["type"]),
        }
        if event.event_type == "permission.decision":
            fields["request_id"] = str(event.payload["request_id"])
            fields["decided_at"] = str(event.payload["decided_at"])
        else:
            fields["requested_at"] = str(event.payload["requested_at"])
        return fields

    if event.event_type in EVENT_TO_RUNTIME_STREAM:
        if not isinstance(event.payload, dict):
            raise ValueError(f"{event.event_type} 的 RuntimeEvent payload 必须是 object")
        required = (
            "event_id", "trace_id", "task_id", "run_id",
            "event_type", "produced_by",
        )
        missing = [key for key in required if not event.payload.get(key)]
        if missing:
            raise ValueError(
                f"{event.event_type} 的 RuntimeEvent payload 缺少字段: {', '.join(missing)}"
            )
        return {
            "schema_version": event.schema_version,
            "payload": payload_str,
            "event_id": str(event.payload["event_id"]),
            "trace_id": str(event.payload["trace_id"]),
            "task_id": str(event.payload["task_id"]),
            "run_id": str(event.payload["run_id"]),
            "type": str(event.payload["event_type"]),
            "produced_by": str(event.payload["produced_by"]),
        }

    transport_type = event.event_type
    if isinstance(event.payload, dict) and isinstance(event.payload.get("type"), str):
        transport_type = event.payload["type"]
    return {
        "schema_version": event.schema_version,
        "payload": payload_str,
        "event_id": str(event.event_id),
        "trace_id": str(event.trace_id),
        "type": transport_type,
        "created_at": created_at,
    }


def _classify_error(error: Exception) -> str:
    name = type(error).__name__
    if "timeout" in name.lower() or "Timeout" in name:
        return "REDIS_TIMEOUT"
    if "connection" in name.lower() or "Connection" in name:
        return "REDIS_CONNECTION_ERROR"
    if "ResponseError" in name:
        return "REDIS_RESPONSE_ERROR"
    return "REDIS_PUBLISH_ERROR"
