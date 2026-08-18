"""Outbox Reconciliation — 定期检测和处理异常状态。"""

import asyncio
import logging
from typing import Any, Optional

from jarvis_worker.database.engine import get_session_factory
from jarvis_worker.database.outbox.repository import PostgresOutboxRepository

logger = logging.getLogger(__name__)


class ReconciliationJob:
    """周期性 reconciliation：按 lease_until 回收过期 dispatching。"""

    def __init__(
        self,
        redis_client: Any = None,
        run_service: Any = None,
        permission_service: Any = None,
        interval_seconds: int = 30,
        stale_dispatching_seconds: int = 60,
    ):
        self._redis = redis_client
        self._interval = interval_seconds
        self._stale_seconds = stale_dispatching_seconds
        self._run_service = run_service
        self._permission_service = permission_service
        self._running = False
        self._task: Optional[asyncio.Task] = None

    async def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._task = asyncio.create_task(self._loop())
        logger.info("ReconciliationJob 已启动 (interval=%ds)", self._interval)

    async def stop(self) -> None:
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        logger.info("ReconciliationJob 已停止")

    async def run_once(self) -> dict:
        result = {
            "stale_dispatching_reset": 0,
            "queued_runs_requeued": 0,
            "queued_runs_failed_closed": 0,
            "runs_rescheduled": 0,
            "runs_failed_closed": 0,
            "permissions_expired": 0,
        }
        session_factory = get_session_factory()
        async with session_factory() as session:
            repo = PostgresOutboxRepository(session)
            async with session.begin():
                count = await repo.reset_stale_dispatching(stale_seconds=self._stale_seconds)
                result["stale_dispatching_reset"] = count
                if count > 0:
                    logger.warning("重置了 %d 个 lease 过期的 OutboxEvent", count)
        if self._run_service is not None:
            queued_recovery = await self._run_service.reconcile_stale_queued_runs(
                queue_event_exists=self._queue_event_exists,
                stale_seconds=self._stale_seconds
            )
            result.update(queued_recovery)
            recovery = await self._run_service.reconcile_expired_runs()
            result.update(recovery)
            if (
                queued_recovery["queued_runs_requeued"]
                or queued_recovery["queued_runs_failed_closed"]
                or recovery["runs_rescheduled"]
                or recovery["runs_failed_closed"]
            ):
                logger.warning(
                    "处理待恢复 AgentRun: queued_requeued=%d queued_failed_closed=%d "
                    "rescheduled=%d failed_closed=%d",
                    queued_recovery["queued_runs_requeued"],
                    queued_recovery["queued_runs_failed_closed"],
                    recovery["runs_rescheduled"],
                    recovery["runs_failed_closed"],
                )
        if self._permission_service is not None:
            expired = await self._permission_service.expire_pending_requests()
            result["permissions_expired"] = expired
            if expired:
                logger.warning("安全收口了 %d 个过期权限请求", expired)
        return result

    async def _queue_event_exists(self, event_id) -> bool:
        """Redis 不可确认时按“仍存在”处理，禁止误重投。"""
        if self._redis is None:
            return True
        try:
            return bool(await self._redis.exists(f"jarvis:outbox:dedupe:{event_id}"))
        except Exception:
            logger.warning("无法核对 Redis queue event，已保守跳过 queued Run 重投")
            return True

    async def _loop(self) -> None:
        while self._running:
            try:
                await self.run_once()
            except Exception as e:
                logger.error("ReconciliationJob 异常: %s", e, exc_info=True)
            await asyncio.sleep(self._interval)
