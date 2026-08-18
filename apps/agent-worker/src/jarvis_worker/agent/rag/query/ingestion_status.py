"""RAG 入库作业的 Workspace 受限完成状态查询。"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from uuid import UUID

from jarvis_worker.agent.rag.contracts import (
    RagDocumentStatus,
    RagIngestionStatus,
)
from jarvis_worker.database.unit_of_work import PostgresUnitOfWork


class RagIngestionMonitorError(RuntimeError):
    def __init__(self, code: str, message: str, *, recoverable: bool) -> None:
        super().__init__(message)
        self.code = code
        self.recoverable = recoverable


@dataclass(frozen=True, slots=True)
class RagIngestionCompletion:
    job_id: UUID
    document_id: UUID
    status: RagIngestionStatus
    document_status: RagDocumentStatus
    chunk_count: int
    embedding_completed: int

    @property
    def ready(self) -> bool:
        return (
            self.status is RagIngestionStatus.COMPLETED
            and self.document_status is RagDocumentStatus.READY
        )


class RagIngestionMonitorService:
    """等待独立 RAG Worker 写入真实终态，不自行推进 ingestion。"""

    def __init__(
        self,
        uow_factory,
        *,
        max_wait_seconds: float = 900,
        poll_interval_seconds: float = 1,
    ) -> None:
        if max_wait_seconds <= 0 or poll_interval_seconds <= 0:
            raise ValueError("RAG ingestion wait 配置必须为正数")
        self._uow_factory = uow_factory
        self._max_wait_seconds = max_wait_seconds
        self._poll_interval_seconds = poll_interval_seconds

    async def wait_for_task_job(
        self,
        *,
        task_id: UUID,
        job_id: UUID,
    ) -> RagIngestionCompletion:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self._max_wait_seconds
        while True:
            completion = await self._read_completion(task_id=task_id, job_id=job_id)
            if completion.ready:
                return completion
            if completion.status in {
                RagIngestionStatus.FAILED,
                RagIngestionStatus.CANCELLED,
            }:
                return completion
            if loop.time() >= deadline:
                raise RagIngestionMonitorError(
                    "RAG_INGESTION_WAIT_TIMEOUT",
                    "RAG 入库仍在后台处理中，可稍后继续等待",
                    recoverable=True,
                )
            await asyncio.sleep(self._poll_interval_seconds)

    async def _read_completion(
        self,
        *,
        task_id: UUID,
        job_id: UUID,
    ) -> RagIngestionCompletion:
        session_factory = self._uow_factory()
        async with session_factory() as session:
            uow = PostgresUnitOfWork(session)
            task = await uow.tasks.get(task_id)
            job = await uow.rag_ingestion_jobs.get(job_id)
            document = await uow.rag_documents.get(job.document_id) if job is not None else None
        if (
            task is None
            or task.workspace_id is None
            or job is None
            or document is None
            or job.workspace_id != task.workspace_id
            or document.workspace_id != task.workspace_id
        ):
            raise RagIngestionMonitorError(
                "RAG_JOB_NOT_FOUND",
                "RAG 入库作业不存在或不属于当前任务 Workspace",
                recoverable=False,
            )
        return RagIngestionCompletion(
            job_id=job.id,
            document_id=document.id,
            status=job.status,
            document_status=document.status,
            chunk_count=document.chunk_count,
            embedding_completed=job.progress.embedding_completed,
        )
