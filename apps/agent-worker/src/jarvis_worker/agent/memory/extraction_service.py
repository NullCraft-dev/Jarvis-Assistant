"""可恢复的异步 MemoryExtractor 作业执行器。"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
from datetime import timedelta

from jarvis_worker.agent.memory.candidate_service import (
    ExtractedMemoryCandidateInput,
    MemoryCandidateApplicationService,
)
from jarvis_worker.agent.memory.deepseek_extractor import MemoryExtractionError
from jarvis_worker.agent.memory.extractor import (
    ExistingMemoryReference,
    ExtractedMemoryCandidateSpec,
    MEMORY_EXTRACTION_POLICY_VERSION,
    MemoryExtractionInput,
    MemoryExtractor,
)
from jarvis_worker.database.unit_of_work import PostgresUnitOfWork
from jarvis_worker.shared.domain.models import (
    AuditLog,
    MemoryExtractionJob,
    RunStatus,
    TaskStatus,
    new_id,
    utcnow,
)
from jarvis_worker.shared.errors.application import AppError


log = logging.getLogger("jarvis_worker.memory_extraction")


class MemoryExtractionApplicationService:
    """领取一个持久化作业并把模型结果转换为 pending candidates。"""

    def __init__(
        self,
        uow_factory,
        extractor: MemoryExtractor,
        *,
        policy_version: str = MEMORY_EXTRACTION_POLICY_VERSION,
        max_attempts: int = 3,
        stale_after_seconds: int = 300,
    ) -> None:
        self._uow_factory = uow_factory
        self._extractor = extractor
        self._policy_version = policy_version
        self._max_attempts = max_attempts
        self._stale_after = timedelta(seconds=stale_after_seconds)
        self._candidate_service = MemoryCandidateApplicationService(uow_factory)

    async def process_next(self) -> bool:
        job = await self._claim_next()
        if job is None:
            return False
        try:
            extraction_input = await self._load_input(job)
            specs = await self._extractor.extract(extraction_input)
            created = 0
            skipped = 0
            for spec in specs:
                if (
                    spec.confidence < 0.75
                    or spec.importance < 40
                    or not _has_valid_evidence(spec, extraction_input)
                ):
                    skipped += 1
                    continue
                workspace_id = (
                    extraction_input.workspace_id
                    if spec.scope_type == "workspace"
                    else None
                )
                if spec.scope_type == "workspace" and workspace_id is None:
                    skipped += 1
                    continue
                try:
                    candidate = await self._candidate_service.create_candidate(
                        ExtractedMemoryCandidateInput(
                            scope_type=spec.scope_type,
                            workspace_id=workspace_id,
                            category=spec.category,
                            suggested_key=spec.suggested_key,
                            content=spec.content,
                            source_task_id=job.source_task_id,
                            source_run_id=job.source_run_id,
                            source_message_ids=extraction_input.source_message_ids,
                            extraction_input_fingerprint=extraction_input.input_fingerprint,
                            confidence=spec.confidence,
                            importance=spec.importance,
                            sensitivity=spec.sensitivity,
                            extraction_policy_version=self._policy_version,
                            extractor_provider=self._extractor.provider_name,
                            extractor_model=self._extractor.model_name,
                            expires_at=utcnow() + timedelta(days=30),
                        )
                    )
                    created += int(candidate is not None)
                    skipped += int(candidate is None)
                except AppError as exc:
                    if exc.code in {
                        "MEMORY_CANDIDATE_SENSITIVE",
                        "VALIDATION_ERROR",
                        "WORKSPACE_ACCESS_DENIED",
                    }:
                        skipped += 1
                        continue
                    raise
            await self._mark_completed(job, created=created, skipped=skipped)
            log.info(
                "MemoryExtractionJob 完成: job_id=%s run_id=%s created=%d skipped=%d",
                job.id,
                job.source_run_id,
                created,
                skipped,
            )
        except Exception as exc:
            await self._mark_failed(job, exc)
            log.warning(
                "MemoryExtractionJob 失败: job_id=%s run_id=%s code=%s attempts=%d",
                job.id,
                job.source_run_id,
                _error_code(exc),
                job.attempts,
            )
        return True

    async def _claim_next(self) -> MemoryExtractionJob | None:
        now = utcnow()
        async with self._uow_factory()() as session:
            async with PostgresUnitOfWork(session).transaction() as tx:
                job = await tx.memory_extraction_jobs.claim_next(
                    now=now, stale_before=now - self._stale_after
                )
                await tx.commit()
                return job

    async def _load_input(self, job: MemoryExtractionJob) -> MemoryExtractionInput:
        async with self._uow_factory()() as session:
            tx = PostgresUnitOfWork(session)
            task = await tx.tasks.get(job.source_task_id)
            run = await tx.runs.get(job.source_run_id)
            if (
                task is None
                or run is None
                or run.task_id != task.id
                or task.status is not TaskStatus.COMPLETED
                or run.status is not RunStatus.COMPLETED
            ):
                raise MemoryExtractionError(
                    "MEMORY_EXTRACTION_SOURCE_INVALID",
                    "记忆提取来源任务状态无效",
                    recoverable=False,
                )
            messages = await tx.messages.list_by_task(task.id)
            existing_memories = await tx.memories.list_active_for_context(
                task.workspace_id, limit=20
            )
        source_messages = [
            item
            for item in messages
            if item.role in {"user", "assistant"} and item.content.strip()
        ]
        final_messages = [
            item
            for item in source_messages
            if item.role == "assistant" and item.run_id == run.id
        ]
        if not final_messages:
            raise MemoryExtractionError(
                "MEMORY_EXTRACTION_SOURCE_MISSING",
                "记忆提取缺少最终回复",
                recoverable=False,
            )
        user_goal = task.user_goal.strip()[:12_000]
        final_response = final_messages[-1].content.strip()[:16_000]
        source_ids = tuple(item.id for item in source_messages)
        fingerprint_payload = json.dumps(
            {
                "policy": self._policy_version,
                "task_id": str(task.id),
                "run_id": str(run.id),
                "user_goal": user_goal,
                "final_response": final_response,
                "source_message_ids": [str(value) for value in source_ids],
                "existing_memories": [
                    {"key": item.key, "content": item.content}
                    for item in existing_memories
                ],
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        return MemoryExtractionInput(
            source_task_id=task.id,
            source_run_id=run.id,
            workspace_id=task.workspace_id,
            user_goal=user_goal,
            final_response=final_response,
            source_message_ids=source_ids,
            input_fingerprint=hashlib.sha256(
                fingerprint_payload.encode("utf-8")
            ).hexdigest(),
            existing_memories=tuple(
                ExistingMemoryReference(key=item.key, content=item.content)
                for item in existing_memories
            ),
        )

    async def _mark_completed(
        self, job: MemoryExtractionJob, *, created: int, skipped: int
    ) -> None:
        now = utcnow()
        async with self._uow_factory()() as session:
            async with PostgresUnitOfWork(session).transaction() as tx:
                await tx.memory_extraction_jobs.mark_completed(job.id, now=now)
                await tx.audits.create(
                    _job_audit(
                        job,
                        "memory.extraction.completed",
                        {"candidates_created": created, "candidates_skipped": skipped},
                    )
                )
                await tx.commit()

    async def _mark_failed(self, job: MemoryExtractionJob, exc: Exception) -> None:
        now = utcnow()
        recoverable = _recoverable(exc)
        retry = recoverable and job.attempts < self._max_attempts
        next_retry_at = (
            now + timedelta(seconds=min(2 ** max(job.attempts - 1, 0) * 5, 60))
            if retry
            else None
        )
        code = _error_code(exc)
        async with self._uow_factory()() as session:
            async with PostgresUnitOfWork(session).transaction() as tx:
                await tx.memory_extraction_jobs.mark_failed(
                    job.id,
                    error_code=code,
                    next_retry_at=next_retry_at,
                    now=now,
                )
                await tx.audits.create(
                    _job_audit(
                        job,
                        "memory.extraction.failed",
                        {
                            "error_code": code,
                            "attempts": job.attempts,
                            "retry_scheduled": next_retry_at is not None,
                        },
                    )
                )
                await tx.commit()


class MemoryExtractionBackgroundWorker:
    """运行在 Worker 固定 asyncio loop 中的轻量后台轮询器。"""

    def __init__(
        self, service: MemoryExtractionApplicationService, *, poll_interval: float = 1.0
    ) -> None:
        self._service = service
        self._poll_interval = poll_interval
        self._stop: asyncio.Event | None = None
        self._task: asyncio.Task[None] | None = None

    async def start(self) -> None:
        if self._task is not None and not self._task.done():
            return
        self._stop = asyncio.Event()
        self._task = asyncio.create_task(self._loop())
        log.info("MemoryExtractionBackgroundWorker 已启动")

    async def stop(self) -> None:
        if self._task is None:
            return
        assert self._stop is not None
        self._stop.set()
        self._task.cancel()
        await asyncio.gather(self._task, return_exceptions=True)
        self._task = None
        log.info("MemoryExtractionBackgroundWorker 已停止")

    async def _loop(self) -> None:
        assert self._stop is not None
        while not self._stop.is_set():
            try:
                processed = await self._service.process_next()
            except Exception:
                log.exception("MemoryExtractionBackgroundWorker 循环异常")
                processed = False
            if processed:
                continue
            try:
                await asyncio.wait_for(
                    self._stop.wait(), timeout=self._poll_interval
                )
            except TimeoutError:
                pass


def _job_audit(
    job: MemoryExtractionJob, event_type: str, details: dict[str, object]
) -> AuditLog:
    return AuditLog(
        id=new_id(),
        event_type=event_type,
        actor="system",
        action_summary=f"{event_type}: {job.id}",
        task_id=job.source_task_id,
        run_id=job.source_run_id,
        details={
            "job_id": str(job.id),
            "extraction_policy_version": job.extraction_policy_version,
            **details,
        },
    )


def _error_code(exc: Exception) -> str:
    code = getattr(exc, "code", None)
    if isinstance(code, str) and code:
        return code[:80]
    return "MEMORY_EXTRACTION_INTERNAL"


def _recoverable(exc: Exception) -> bool:
    value = getattr(exc, "recoverable", None)
    return value if isinstance(value, bool) else True


_USER_ASSERTION_CUE = re.compile(
    r"记住|以后|默认|偏好|规则|必须|始终|不要|我(?:是|叫|喜欢|习惯)|"
    r"remember|always|default|prefer|my rule",
    re.I,
)
_USER_QUESTION_CUE = re.compile(
    r"告诉我|是什么|什么|如何|怎样|是否|为什么|吗(?:[，。！？?]|$)|呢(?:[，。！？?]|$)|"
    r"tell me|what|how|whether|why|\?",
    re.I,
)


def _has_valid_evidence(
    spec: ExtractedMemoryCandidateSpec,
    extraction_input: MemoryExtractionInput,
) -> bool:
    """候选必须由允许的原文片段支持，禁止从助手复述生成用户事实。"""
    if spec.category != "project_fact" and spec.evidence_source != "user_goal":
        return False
    source = (
        extraction_input.user_goal
        if spec.evidence_source == "user_goal"
        else extraction_input.final_response
    )
    quote = " ".join(spec.evidence_quote.split()).casefold()
    normalized_source = " ".join(source.split()).casefold()
    if quote not in normalized_source:
        return False
    if spec.category in {"preference", "user_fact", "rule"}:
        if _USER_ASSERTION_CUE.search(spec.evidence_quote) is None:
            return False
        if _USER_QUESTION_CUE.search(spec.evidence_quote) is not None:
            return False
    content_numbers = set(re.findall(r"\d+", spec.content))
    quote_numbers = set(re.findall(r"\d+", spec.evidence_quote))
    if not content_numbers.issubset(quote_numbers):
        return False
    existing_keys = {item.key for item in extraction_input.existing_memories}
    if spec.suggested_key in existing_keys:
        return False
    return True
