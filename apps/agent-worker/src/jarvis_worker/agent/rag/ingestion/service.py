"""可恢复的 RAG PDF 入库 Application Service（截至 Embedding 交接）。"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from typing import Callable
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

from jarvis_worker.agent.artifacts.file_store import LocalArtifactFileStore
from jarvis_worker.agent.rag.chunking import MultimodalChunkRouter
from jarvis_worker.agent.rag.contracts import (
    RagAsset,
    RagAssetFileStore,
    RagDocument,
    RagDocumentStatus,
    RagIngestionJob,
    RagIngestionStatus,
    RagJobProgress,
)
from jarvis_worker.agent.rag.ingestion.projection import (
    RagProjection,
    build_projection,
)
from jarvis_worker.agent.rag.ingestion.source import (
    PDF_MIME_TYPE,
    RagPdfSource,
    build_ingestion_idempotency_key,
    has_trusted_lineage,
    has_trusted_user_upload_lineage,
    is_user_upload_artifact,
    user_upload_permission_request_id,
    valid_pdf_artifact,
)
from jarvis_worker.agent.rag.preprocessing import (
    DocumentPreprocessor,
    PreprocessedDocument,
    PreprocessingProgress,
)
from jarvis_worker.database.unit_of_work import PostgresUnitOfWork
from jarvis_worker.shared.domain.models import Artifact, AuditLog, utcnow
from jarvis_worker.shared.storage_capacity import StorageCapacityExceeded

INGESTION_POLICY_VERSION = "rag-ingestion-prepared-v1"
log = logging.getLogger("jarvis_worker.rag_ingestion")


class RagIngestionError(RuntimeError):
    def __init__(self, code: str, message: str, *, recoverable: bool):
        super().__init__(message)
        self.code = code
        self.recoverable = recoverable


@dataclass(frozen=True, slots=True)
class RagIngestionEnqueueResult:
    document_id: UUID
    job_id: UUID
    status: RagIngestionStatus
    created: bool


@dataclass(frozen=True, slots=True)
class RagIngestionProcessResult:
    document_id: UUID
    job_id: UUID
    status: RagIngestionStatus
    chunk_count: int = 0
    element_count: int = 0
    asset_count: int = 0
    error_code: str | None = None


@dataclass(frozen=True, slots=True)
class RagDocumentMutationResult:
    document_id: UUID
    status: RagDocumentStatus
    version: int
    job_id: UUID | None = None
    job_status: RagIngestionStatus | None = None


class RagIngestionCommandService:
    """只负责校验受控 PDF Artifact 并创建幂等摄取作业。"""

    def __init__(
        self,
        uow_factory,
        *,
        artifact_file_store: LocalArtifactFileStore,
        now: Callable[[], datetime] = utcnow,
    ) -> None:
        self._uow_factory = uow_factory
        self._artifact_file_store = artifact_file_store
        self._now = now

    async def enqueue_pdf_for_task(
        self,
        *,
        task_id: UUID,
        source_artifact_id: UUID,
        ingestion_policy_version: str = INGESTION_POLICY_VERSION,
        max_attempts: int = 3,
    ) -> RagIngestionEnqueueResult:
        """从可信 Task 解析 Workspace，禁止模型自行提供 workspace_id。"""
        session_factory = self._uow_factory()
        async with session_factory() as session:
            task = await PostgresUnitOfWork(session).tasks.get(task_id)
        if task is None or task.workspace_id is None:
            raise RagIngestionError(
                "RAG_TASK_WORKSPACE_REQUIRED",
                "当前任务没有可用于 RAG 入库的 Workspace",
                recoverable=False,
            )
        return await self.enqueue_pdf(
            workspace_id=task.workspace_id,
            source_artifact_id=source_artifact_id,
            ingestion_policy_version=ingestion_policy_version,
            max_attempts=max_attempts,
        )

    async def enqueue_pdf(
        self,
        *,
        workspace_id: UUID,
        source_artifact_id: UUID,
        ingestion_policy_version: str = INGESTION_POLICY_VERSION,
        max_attempts: int = 3,
    ) -> RagIngestionEnqueueResult:
        if not ingestion_policy_version.strip() or max_attempts < 1:
            raise ValueError("RAG ingestion policy/max_attempts 无效")
        source = await self._load_source(workspace_id=workspace_id, artifact_id=source_artifact_id)
        await self._read_pdf(source.artifact)
        idempotency_key = build_ingestion_idempotency_key(
            workspace_id=workspace_id,
            artifact_id=source_artifact_id,
            content_hash=source.artifact.content_hash or "",
            policy_version=ingestion_policy_version,
        )
        now = self._now()
        session_factory = self._uow_factory()
        async with session_factory() as session:
            async with PostgresUnitOfWork(session).transaction() as tx:
                existing_job = await tx.rag_ingestion_jobs.get_by_idempotency_key(idempotency_key)
                if existing_job is not None:
                    if existing_job.workspace_id != workspace_id:
                        raise RagIngestionError(
                            "RAG_WORKSPACE_MISMATCH",
                            "RAG 入库作业不属于当前 Workspace",
                            recoverable=False,
                        )
                    return RagIngestionEnqueueResult(
                        document_id=existing_job.document_id,
                        job_id=existing_job.id,
                        status=existing_job.status,
                        created=False,
                    )
                document = await tx.rag_documents.get_by_source(
                    workspace_id=workspace_id,
                    source_artifact_id=source_artifact_id,
                    source_content_hash=source.artifact.content_hash or "",
                )
                if document is None:
                    document = RagDocument(
                        id=uuid5(
                            NAMESPACE_URL,
                            f"jarvis:rag-document:{workspace_id}:{source_artifact_id}:"
                            f"{source.artifact.content_hash}",
                        ),
                        workspace_id=workspace_id,
                        source_artifact_id=source_artifact_id,
                        title=source.artifact.title,
                        mime_type=PDF_MIME_TYPE,
                        source_content_hash=source.artifact.content_hash or "",
                        ingestion_policy_version=ingestion_policy_version,
                        created_at=now,
                        updated_at=now,
                    )
                    await tx.rag_documents.create(document)
                elif document.status is RagDocumentStatus.DISABLED:
                    raise RagIngestionError(
                        "RAG_DOCUMENT_DISABLED",
                        "已停用的 RAG 文档不能直接重新入库",
                        recoverable=False,
                    )
                elif document.status is RagDocumentStatus.INDEXING:
                    raise RagIngestionError(
                        "RAG_DOCUMENT_BUSY",
                        "RAG 文档已有入库流程正在进行",
                        recoverable=True,
                    )
                else:
                    document.begin_indexing(
                        ingestion_policy_version=ingestion_policy_version, now=now
                    )
                    await tx.rag_documents.update(document)
                job = RagIngestionJob(
                    id=uuid5(document.id, f"rag-ingestion:{ingestion_policy_version}"),
                    document_id=document.id,
                    workspace_id=workspace_id,
                    idempotency_key=idempotency_key,
                    ingestion_policy_version=ingestion_policy_version,
                    max_attempts=max_attempts,
                    created_at=now,
                    updated_at=now,
                )
                await tx.rag_ingestion_jobs.create(job)
                await tx.audits.create(
                    _audit(
                        source,
                        event_type="rag.ingestion.queued",
                        action_summary="RAG PDF 已进入受控入库队列",
                        details={"document_id": str(document.id), "job_id": str(job.id)},
                        now=now,
                    )
                )
                await tx.commit()
        return RagIngestionEnqueueResult(
            document_id=document.id,
            job_id=job.id,
            status=job.status,
            created=True,
        )

    async def restart_document(
        self,
        *,
        workspace_id: UUID,
        document_id: UUID,
        expected_version: int,
    ) -> RagIngestionEnqueueResult:
        """复用受控 Artifact 和幂等 Job，从 parsing 起点重新执行。"""

        session_factory = self._uow_factory()
        async with session_factory() as session:
            uow = PostgresUnitOfWork(session)
            document = await uow.rag_documents.get(document_id)
            if document is None or document.workspace_id != workspace_id:
                raise RagIngestionError(
                    "RAG_DOCUMENT_NOT_FOUND",
                    "RAG 文档不存在或不属于当前 Workspace",
                    recoverable=False,
                )
            if document.status is RagDocumentStatus.DISABLED:
                raise RagIngestionError(
                    "RAG_DOCUMENT_DISABLED",
                    "已停用的 RAG 文档不能重新执行",
                    recoverable=False,
                )
            if document.version != expected_version:
                raise RagIngestionError(
                    "RAG_DOCUMENT_VERSION_CONFLICT",
                    "RAG 文档版本已变化，请刷新后重试",
                    recoverable=True,
                )
            jobs = await uow.rag_ingestion_jobs.list_latest_by_documents(
                workspace_id=workspace_id,
                document_ids=[document_id],
            )
            if not jobs:
                raise RagIngestionError(
                    "RAG_JOB_NOT_FOUND",
                    "RAG 文档没有可重新执行的入库作业",
                    recoverable=False,
                )
            source_artifact_id = document.source_artifact_id

        source = await self._load_source(workspace_id=workspace_id, artifact_id=source_artifact_id)
        await self._read_pdf(source.artifact)
        now = self._now()
        async with session_factory() as session:
            async with PostgresUnitOfWork(session).transaction() as tx:
                document = await tx.rag_documents.get(document_id)
                jobs = await tx.rag_ingestion_jobs.list_latest_by_documents(
                    workspace_id=workspace_id,
                    document_ids=[document_id],
                )
                if document is None or document.workspace_id != workspace_id or not jobs:
                    raise RagIngestionError(
                        "RAG_RESTART_CONFLICT",
                        "RAG 文档状态已变化，请刷新后重试",
                        recoverable=True,
                    )
                if document.status is RagDocumentStatus.DISABLED:
                    raise RagIngestionError(
                        "RAG_DOCUMENT_DISABLED",
                        "已停用的 RAG 文档不能重新执行",
                        recoverable=False,
                    )
                if document.version != expected_version:
                    raise RagIngestionError(
                        "RAG_DOCUMENT_VERSION_CONFLICT",
                        "RAG 文档版本已变化，请刷新后重试",
                        recoverable=True,
                    )
                idempotency_key = build_ingestion_idempotency_key(
                    workspace_id=workspace_id,
                    artifact_id=document.source_artifact_id,
                    content_hash=document.source_content_hash,
                    policy_version=INGESTION_POLICY_VERSION,
                )
                job = await tx.rag_ingestion_jobs.get_by_idempotency_key(idempotency_key)
                created = job is None
                if job is None:
                    job = RagIngestionJob(
                        id=uuid5(
                            NAMESPACE_URL,
                            f"jarvis:rag-ingestion:{document.id}:{INGESTION_POLICY_VERSION}",
                        ),
                        document_id=document.id,
                        workspace_id=workspace_id,
                        idempotency_key=idempotency_key,
                        ingestion_policy_version=INGESTION_POLICY_VERSION,
                        created_at=now,
                        updated_at=now,
                    )
                    await tx.rag_ingestion_jobs.create(job)
                else:
                    job.restart(now=now)
                    await tx.rag_ingestion_jobs.update(job)
                if document.ingestion_policy_version == INGESTION_POLICY_VERSION:
                    document.restart_indexing(now=now)
                else:
                    document.begin_indexing(
                        ingestion_policy_version=INGESTION_POLICY_VERSION,
                        now=now,
                    )
                await tx.rag_documents.update(document)
                await tx.audits.create(
                    _audit(
                        source,
                        event_type="rag.ingestion.restarted",
                        action_summary="用户显式要求重新执行 RAG 入库作业",
                        details={
                            "document_id": str(document.id),
                            "job_id": str(job.id),
                            "ingestion_policy_version": INGESTION_POLICY_VERSION,
                        },
                        now=now,
                    )
                )
                await tx.commit()
        return RagIngestionEnqueueResult(
            document_id=document.id,
            job_id=job.id,
            status=job.status,
            created=created,
        )

    async def set_document_enabled(
        self,
        *,
        workspace_id: UUID,
        document_id: UUID,
        expected_version: int,
        enabled: bool,
    ) -> RagDocumentMutationResult:
        _document, source = await self._load_document_source(
            workspace_id=workspace_id,
            document_id=document_id,
        )
        now = self._now()
        session_factory = self._uow_factory()
        async with session_factory() as session:
            async with PostgresUnitOfWork(session).transaction() as tx:
                current = await tx.rag_documents.get(document_id)
                if current is None or current.workspace_id != workspace_id:
                    raise _document_not_found()
                if current.version != expected_version:
                    raise _version_conflict()
                jobs = await tx.rag_ingestion_jobs.list_latest_by_documents(
                    workspace_id=workspace_id,
                    document_ids=[document_id],
                )
                if not enabled and jobs and not jobs[0].is_terminal:
                    raise RagIngestionError(
                        "RAG_DOCUMENT_BUSY",
                        "运行中的 RAG 文档必须先取消作业才能停用",
                        recoverable=True,
                    )
                try:
                    current.enable(now=now) if enabled else current.disable(now=now)
                except ValueError as exc:
                    raise RagIngestionError(
                        "RAG_DOCUMENT_STATUS_INVALID",
                        str(exc),
                        recoverable=False,
                    ) from None
                await tx.rag_documents.update(current)
                await tx.audits.create(
                    _audit(
                        source,
                        event_type=("rag.document.enabled" if enabled else "rag.document.disabled"),
                        action_summary=(
                            "用户启用 RAG 文档检索" if enabled else "用户停用 RAG 文档检索"
                        ),
                        details={
                            "document_id": str(current.id),
                            "version": current.version,
                        },
                        now=now,
                        risk_level="L2",
                    )
                )
                await tx.commit()
        return RagDocumentMutationResult(
            document_id=current.id,
            status=current.status,
            version=current.version,
        )

    async def cancel_document(
        self,
        *,
        workspace_id: UUID,
        document_id: UUID,
        expected_version: int,
    ) -> RagDocumentMutationResult:
        _document, source = await self._load_document_source(
            workspace_id=workspace_id,
            document_id=document_id,
        )
        now = self._now()
        session_factory = self._uow_factory()
        async with session_factory() as session:
            async with PostgresUnitOfWork(session).transaction() as tx:
                current = await tx.rag_documents.get(document_id)
                if current is None or current.workspace_id != workspace_id:
                    raise _document_not_found()
                if current.version != expected_version:
                    raise _version_conflict()
                jobs = await tx.rag_ingestion_jobs.list_latest_by_documents(
                    workspace_id=workspace_id,
                    document_ids=[document_id],
                )
                if not jobs:
                    raise RagIngestionError(
                        "RAG_JOB_NOT_FOUND",
                        "RAG 文档没有可取消的入库作业",
                        recoverable=False,
                    )
                job = jobs[0]
                try:
                    job.cancel(now=now)
                    current.mark_failed(now=now)
                except ValueError as exc:
                    raise RagIngestionError(
                        "RAG_JOB_NOT_CANCELLABLE",
                        str(exc),
                        recoverable=False,
                    ) from None
                await tx.rag_ingestion_jobs.update(job)
                await tx.rag_documents.update(current)
                await tx.audits.create(
                    _audit(
                        source,
                        event_type="rag.ingestion.cancelled",
                        action_summary="用户取消 RAG 入库作业",
                        details={
                            "document_id": str(current.id),
                            "job_id": str(job.id),
                            "version": current.version,
                        },
                        now=now,
                        risk_level="L2",
                    )
                )
                await tx.commit()
        return RagDocumentMutationResult(
            document_id=current.id,
            status=current.status,
            version=current.version,
            job_id=job.id,
            job_status=job.status,
        )

    async def _load_document_source(
        self, *, workspace_id: UUID, document_id: UUID
    ) -> tuple[RagDocument, RagPdfSource]:
        session_factory = self._uow_factory()
        async with session_factory() as session:
            document = await PostgresUnitOfWork(session).rag_documents.get(document_id)
        if document is None or document.workspace_id != workspace_id:
            raise _document_not_found()
        source = await self._load_source(
            workspace_id=workspace_id,
            artifact_id=document.source_artifact_id,
        )
        return document, source

    async def _load_source(self, *, workspace_id: UUID, artifact_id: UUID) -> RagPdfSource:
        session_factory = self._uow_factory()
        async with session_factory() as session:
            tx = PostgresUnitOfWork(session)
            artifact = await tx.artifacts.get(artifact_id)
            task = await tx.tasks.get(artifact.task_id) if artifact is not None else None
            run = (
                await tx.runs.get(artifact.run_id)
                if artifact is not None and artifact.producer_type == "runtime"
                else None
            )
            upload_permission_id = (
                _user_upload_permission_id(artifact)
                if artifact is not None and is_user_upload_artifact(artifact)
                else None
            )
            upload_permission = (
                await tx.permissions.get_request(upload_permission_id)
                if upload_permission_id is not None
                else None
            )
            tool_call = (
                await tx.tool_calls.get(artifact.source_tool_call_id)
                if artifact is not None and artifact.source_tool_call_id is not None
                else None
            )
        common_invalid = (
            artifact is None
            or task is None
            or task.workspace_id != workspace_id
            or artifact.task_id != task.id
            or not valid_pdf_artifact(artifact)
        )
        if common_invalid:
            raise RagIngestionError(
                "RAG_SOURCE_INTEGRITY_ERROR",
                "RAG 来源 Artifact 不可读取或来源校验失败",
                recoverable=False,
            )
        tool_lineage_valid = (
            tool_call is not None
            and tool_call.task_id == task.id
            and tool_call.run_id == artifact.run_id
            and tool_call.id == artifact.source_tool_call_id
            and tool_call.status == "completed"
            and has_trusted_lineage(artifact, tool_call.result)
        )
        upload_lineage_valid = has_trusted_user_upload_lineage(
            artifact=artifact,
            task=task,
            run=run,
            permission=upload_permission,
            workspace_id=workspace_id,
        )
        if not (tool_lineage_valid or upload_lineage_valid):
            raise RagIngestionError(
                "RAG_SOURCE_INTEGRITY_ERROR",
                "RAG 来源 Artifact 不可读取或来源校验失败",
                recoverable=False,
            )
        return RagPdfSource(artifact=artifact, task=task, tool_call=tool_call)

    async def _read_pdf(self, artifact: Artifact) -> bytes:
        try:
            content = await asyncio.to_thread(
                self._artifact_file_store.read_bytes,
                artifact.file_path or "",
                expected_sha256=artifact.content_hash,
            )
        except (OSError, ValueError):
            raise RagIngestionError(
                "RAG_SOURCE_INTEGRITY_ERROR",
                "RAG 来源 PDF 完整性校验失败",
                recoverable=False,
            ) from None
        if len(content) != artifact.file_size_bytes or not content.startswith(b"%PDF-"):
            raise RagIngestionError(
                "RAG_SOURCE_INTEGRITY_ERROR",
                "RAG 来源不是有效 PDF",
                recoverable=False,
            )
        return content


class RagIngestionService(RagIngestionCommandService):
    def __init__(
        self,
        uow_factory,
        *,
        artifact_file_store: LocalArtifactFileStore,
        asset_file_store: RagAssetFileStore,
        preprocessor: DocumentPreprocessor,
        chunk_router: MultimodalChunkRouter | None = None,
        now: Callable[[], datetime] = utcnow,
        lease_duration: timedelta = timedelta(minutes=5),
        retry_base_delay: timedelta = timedelta(seconds=30),
    ) -> None:
        if lease_duration <= timedelta(0) or retry_base_delay <= timedelta(0):
            raise ValueError("RAG ingestion lease/retry delay 必须大于 0")
        super().__init__(
            uow_factory,
            artifact_file_store=artifact_file_store,
            now=now,
        )
        self._asset_file_store = asset_file_store
        self._preprocessor = preprocessor
        self._chunk_router = chunk_router or MultimodalChunkRouter()
        self._lease_duration = lease_duration
        self._retry_base_delay = retry_base_delay

    async def process_next(self, *, worker_id: str) -> RagIngestionProcessResult | None:
        if not worker_id.strip():
            raise ValueError("RAG ingestion worker_id 不能为空")
        now = self._now()
        session_factory = self._uow_factory()
        async with session_factory() as session:
            async with PostgresUnitOfWork(session).transaction() as tx:
                job = await tx.rag_ingestion_jobs.claim_next(
                    worker_id=worker_id,
                    now=now,
                    lease_until=now + self._lease_duration,
                )
                if job is None:
                    return None
                document = await tx.rag_documents.get(job.document_id)
                if job.status is RagIngestionStatus.FAILED:
                    if document is not None and document.status is RagDocumentStatus.INDEXING:
                        document.mark_failed(now=now)
                        await tx.rag_documents.update(document)
                    await tx.audits.create(_exhausted_ingestion_audit(job=job, now=now))
                    await tx.commit()
                    return RagIngestionProcessResult(
                        document_id=job.document_id,
                        job_id=job.id,
                        status=job.status,
                        error_code=job.error_code,
                    )
                if document is None or document.workspace_id != job.workspace_id:
                    raise RagIngestionError(
                        "RAG_DOCUMENT_NOT_FOUND",
                        "RAG 入库文档不存在或 Workspace 不匹配",
                        recoverable=False,
                    )
                await tx.commit()

        projection: RagProjection | None = None
        existing_asset_references: frozenset[str] = frozenset()
        try:
            source = await self._load_source(
                workspace_id=job.workspace_id, artifact_id=document.source_artifact_id
            )
            content = await self._read_pdf(source.artifact)
            preprocessed = await self._preprocess_with_heartbeat(
                job_id=job.id, worker_id=worker_id, content=content
            )
            if not await self._advance_to_chunking(job.id, worker_id=worker_id):
                return RagIngestionProcessResult(
                    document_id=document.id,
                    job_id=job.id,
                    status=RagIngestionStatus.CANCELLED,
                )
            drafts = self._chunk_router.chunk(preprocessed)
            if not drafts:
                raise RagIngestionError(
                    "RAG_NO_INDEXABLE_CONTENT",
                    "PDF 未产生可检索内容",
                    recoverable=False,
                )
            if not await self._report_progress(
                job.id,
                worker_id=worker_id,
                transform=lambda progress: replace(
                    progress,
                    active_executor="chunker",
                    chunks_total=len(drafts),
                ),
            ):
                raise RagIngestionError(
                    "RAG_JOB_LEASE_LOST",
                    "RAG 入库作业 lease 已失效",
                    recoverable=True,
                )
            existing_asset_references = await self._existing_asset_references(
                workspace_id=document.workspace_id, document_id=document.id
            )
            projection = build_projection(
                document=document,
                job=job,
                preprocessed=preprocessed,
                drafts=drafts,
                asset_file_store=self._asset_file_store,
                existing_asset_references=existing_asset_references,
            )
            result = await self._persist_projection(
                source=source,
                document=document,
                job_id=job.id,
                worker_id=worker_id,
                preprocessed=preprocessed,
                projection=projection,
            )
            if result.status is not RagIngestionStatus.EMBEDDING:
                self._cleanup_uncommitted_assets(
                    projection, existing_asset_references=existing_asset_references
                )
            return result
        except RagIngestionError as exc:
            self._cleanup_uncommitted_assets(
                projection, existing_asset_references=existing_asset_references
            )
            return await self._record_failure(
                job.id, exc.code, exc.recoverable, worker_id=worker_id
            )
        except StorageCapacityExceeded as exc:
            self._cleanup_uncommitted_assets(
                projection, existing_asset_references=existing_asset_references
            )
            return await self._record_failure(job.id, exc.code, False, worker_id=worker_id)
        except ValueError:
            self._cleanup_uncommitted_assets(
                projection, existing_asset_references=existing_asset_references
            )
            return await self._record_failure(
                job.id, "RAG_DOCUMENT_INVALID", False, worker_id=worker_id
            )
        except Exception:
            self._cleanup_uncommitted_assets(
                projection, existing_asset_references=existing_asset_references
            )
            log.exception(
                "RAG 入库发生未分类异常: job_id=%s document_id=%s",
                job.id,
                document.id,
            )
            return await self._record_failure(
                job.id, "RAG_INGESTION_FAILED", True, worker_id=worker_id
            )

    async def cancel(self, *, workspace_id: UUID, job_id: UUID) -> RagIngestionJob:
        now = self._now()
        session_factory = self._uow_factory()
        async with session_factory() as session:
            async with PostgresUnitOfWork(session).transaction() as tx:
                job = await tx.rag_ingestion_jobs.get(job_id)
                if job is None:
                    raise RagIngestionError(
                        "RAG_JOB_NOT_FOUND", "RAG 入库作业不存在", recoverable=False
                    )
                if job.workspace_id != workspace_id:
                    raise RagIngestionError(
                        "RAG_WORKSPACE_MISMATCH",
                        "RAG 入库作业不属于当前 Workspace",
                        recoverable=False,
                    )
                job.cancel(now=now)
                await tx.rag_ingestion_jobs.update(job)
                document = await tx.rag_documents.get(job.document_id)
                if document is not None and document.status is RagDocumentStatus.INDEXING:
                    document.mark_failed(now=now)
                    await tx.rag_documents.update(document)
                await tx.commit()
                return job

    async def _advance_to_chunking(self, job_id: UUID, *, worker_id: str) -> bool:
        now = self._now()
        session_factory = self._uow_factory()
        async with session_factory() as session:
            async with PostgresUnitOfWork(session).transaction() as tx:
                job = await tx.rag_ingestion_jobs.get(job_id)
                if job is None:
                    raise RagIngestionError(
                        "RAG_JOB_NOT_FOUND", "RAG 入库作业不存在", recoverable=False
                    )
                if job.status is RagIngestionStatus.CANCELLED:
                    return False
                if job.status is not RagIngestionStatus.PARSING or job.claimed_by != worker_id:
                    raise RagIngestionError(
                        "RAG_JOB_LEASE_LOST", "RAG 入库作业 lease 已失效", recoverable=True
                    )
                job.advance(RagIngestionStatus.CHUNKING, now=now)
                job.report_progress(
                    progress=replace(job.progress, active_executor="chunker"),
                    worker_id=worker_id,
                    now=now,
                )
                await tx.rag_ingestion_jobs.update(job)
                await tx.commit()
                return True

    async def _existing_asset_references(
        self, *, workspace_id: UUID, document_id: UUID
    ) -> frozenset[str]:
        session_factory = self._uow_factory()
        async with session_factory() as session:
            assets = await PostgresUnitOfWork(session).rag_assets.list_by_document(
                workspace_id=workspace_id, document_id=document_id
            )
        return frozenset(asset.storage_reference for asset in assets)

    def _cleanup_uncommitted_assets(
        self,
        projection: RagProjection | None,
        *,
        existing_asset_references: frozenset[str],
    ) -> None:
        if projection is None:
            return
        for asset in projection.assets:
            if asset.storage_reference not in existing_asset_references:
                try:
                    self._asset_file_store.delete(asset)
                except (OSError, ValueError):
                    pass

    async def _preprocess_with_heartbeat(
        self, *, job_id: UUID, worker_id: str, content: bytes
    ) -> PreprocessedDocument:
        stop = asyncio.Event()
        heartbeat = asyncio.create_task(
            self._lease_heartbeat(job_id=job_id, worker_id=worker_id, stop=stop)
        )
        try:

            async def on_progress(progress: PreprocessingProgress) -> None:
                updated = await self._report_progress(
                    job_id,
                    worker_id=worker_id,
                    transform=lambda current: replace(
                        current,
                        active_executor=progress.active_executor,
                        page_count=progress.page_count,
                        native_extraction_done=progress.native_extraction_done,
                        visual_pages_total=progress.visual_pages_total,
                        visual_pages_completed=progress.visual_pages_completed,
                        visual_route_counts=progress.visual_route_counts,
                    ),
                )
                if not updated:
                    raise RagIngestionError(
                        "RAG_JOB_LEASE_LOST",
                        "RAG 入库作业 lease 已失效",
                        recoverable=True,
                    )

            return await self._preprocessor.preprocess_pdf(content, progress_callback=on_progress)
        finally:
            stop.set()
            await heartbeat

    async def _lease_heartbeat(self, *, job_id: UUID, worker_id: str, stop: asyncio.Event) -> None:
        interval = max(1.0, min(60.0, self._lease_duration.total_seconds() / 3))
        while True:
            try:
                await asyncio.wait_for(stop.wait(), timeout=interval)
                return
            except TimeoutError:
                if not await self._renew_lease(job_id=job_id, worker_id=worker_id):
                    raise RagIngestionError(
                        "RAG_JOB_LEASE_LOST",
                        "RAG 入库作业 lease 已失效",
                        recoverable=True,
                    )

    async def _renew_lease(self, *, job_id: UUID, worker_id: str) -> bool:
        now = self._now()
        session_factory = self._uow_factory()
        async with session_factory() as session:
            async with PostgresUnitOfWork(session).transaction() as tx:
                job = await tx.rag_ingestion_jobs.get(job_id)
                if (
                    job is None
                    or job.claimed_by != worker_id
                    or job.status
                    not in {
                        RagIngestionStatus.PARSING,
                        RagIngestionStatus.CHUNKING,
                    }
                ):
                    return False
                try:
                    job.renew_lease(
                        worker_id=worker_id,
                        lease_until=now + self._lease_duration,
                        now=now,
                    )
                except ValueError:
                    return False
                await tx.rag_ingestion_jobs.update(job)
                await tx.commit()
                return True

    async def _report_progress(
        self,
        job_id: UUID,
        *,
        worker_id: str,
        transform: Callable[[RagJobProgress], RagJobProgress],
    ) -> bool:
        now = self._now()
        session_factory = self._uow_factory()
        async with session_factory() as session:
            async with PostgresUnitOfWork(session).transaction() as tx:
                job = await tx.rag_ingestion_jobs.get(job_id)
                if (
                    job is None
                    or job.claimed_by != worker_id
                    or job.status
                    not in {
                        RagIngestionStatus.PARSING,
                        RagIngestionStatus.CHUNKING,
                    }
                ):
                    return False
                job.report_progress(progress=transform(job.progress), worker_id=worker_id, now=now)
                await tx.rag_ingestion_jobs.update(job)
                await tx.commit()
                return True

    async def _persist_projection(
        self,
        *,
        source: RagPdfSource,
        document: RagDocument,
        job_id: UUID,
        worker_id: str,
        preprocessed: PreprocessedDocument,
        projection: RagProjection,
    ) -> RagIngestionProcessResult:
        now = self._now()
        old_assets: list[RagAsset] = []
        session_factory = self._uow_factory()
        async with session_factory() as session:
            async with PostgresUnitOfWork(session).transaction() as tx:
                job = await tx.rag_ingestion_jobs.get(job_id)
                current_document = await tx.rag_documents.get(document.id)
                if job is None or current_document is None:
                    raise RagIngestionError(
                        "RAG_JOB_NOT_FOUND", "RAG 入库状态不存在", recoverable=False
                    )
                if job.status is RagIngestionStatus.CANCELLED:
                    return RagIngestionProcessResult(
                        document_id=document.id,
                        job_id=job.id,
                        status=job.status,
                    )
                if job.status is not RagIngestionStatus.CHUNKING or job.claimed_by != worker_id:
                    raise RagIngestionError(
                        "RAG_JOB_LEASE_LOST", "RAG 入库作业 lease 已失效", recoverable=True
                    )
                old_assets = await tx.rag_assets.list_by_document(
                    workspace_id=document.workspace_id, document_id=document.id
                )
                await tx.rag_chunks.delete_by_document(
                    workspace_id=document.workspace_id, document_id=document.id
                )
                await tx.rag_elements.delete_by_document(
                    workspace_id=document.workspace_id, document_id=document.id
                )
                await tx.rag_elements.create_many(list(projection.elements))
                for asset in projection.assets:
                    await tx.rag_assets.create(asset)
                await tx.rag_chunks.create_many(list(projection.chunks))
                await tx.rag_chunk_element_links.create_many(list(projection.links))
                current_document.record_prepared(
                    parser_version=preprocessed.native_parser_version,
                    chunker_version=self._chunk_router.version,
                    chunk_count=len(projection.chunks),
                    now=now,
                )
                await tx.rag_documents.update(current_document)
                job.handoff_to_embedding(now=now)
                job.progress = replace(
                    job.progress,
                    active_executor=None,
                    chunks_total=len(projection.chunks),
                    embedding_total=len(projection.chunks),
                    embedding_completed=0,
                )
                await tx.rag_ingestion_jobs.update(job)
                await tx.audits.create(
                    _audit(
                        source,
                        event_type="rag.ingestion.prepared",
                        action_summary="RAG PDF 已完成解析、分片和多模态关系持久化",
                        details={
                            "document_id": str(document.id),
                            "job_id": str(job.id),
                            "chunk_count": len(projection.chunks),
                            "element_count": len(projection.elements),
                            "asset_count": len(projection.assets),
                        },
                        now=now,
                    )
                )
                await tx.commit()
        retained = {asset.storage_reference for asset in projection.assets}
        for asset in old_assets:
            if asset.storage_reference not in retained:
                try:
                    self._asset_file_store.delete(asset)
                except (OSError, ValueError):
                    pass
        return RagIngestionProcessResult(
            document_id=document.id,
            job_id=job_id,
            status=RagIngestionStatus.EMBEDDING,
            chunk_count=len(projection.chunks),
            element_count=len(projection.elements),
            asset_count=len(projection.assets),
        )

    async def _record_failure(
        self,
        job_id: UUID,
        error_code: str,
        recoverable: bool,
        *,
        worker_id: str,
    ) -> RagIngestionProcessResult:
        now = self._now()
        session_factory = self._uow_factory()
        async with session_factory() as session:
            async with PostgresUnitOfWork(session).transaction() as tx:
                job = await tx.rag_ingestion_jobs.get(job_id)
                if job is None:
                    raise RagIngestionError(
                        "RAG_JOB_NOT_FOUND", "RAG 入库作业不存在", recoverable=False
                    )
                if job.status is RagIngestionStatus.CANCELLED:
                    return RagIngestionProcessResult(
                        document_id=job.document_id,
                        job_id=job.id,
                        status=job.status,
                    )
                if job.claimed_by != worker_id:
                    return RagIngestionProcessResult(
                        document_id=job.document_id,
                        job_id=job.id,
                        status=job.status,
                        error_code="RAG_JOB_LEASE_LOST",
                    )
                attempts_exhausted = recoverable and job.attempts >= job.max_attempts
                terminal_error_code = (
                    "RAG_INGESTION_ATTEMPTS_EXHAUSTED"
                    if attempts_exhausted
                    else error_code
                )
                next_retry_at = None
                if recoverable and not attempts_exhausted:
                    multiplier = min(2 ** max(job.attempts - 1, 0), 20)
                    next_retry_at = now + self._retry_base_delay * multiplier
                job.fail(
                    error_code=terminal_error_code,
                    next_retry_at=next_retry_at,
                    now=now,
                )
                await tx.rag_ingestion_jobs.update(job)
                if job.is_terminal:
                    document = await tx.rag_documents.get(job.document_id)
                    if document is not None and document.status is RagDocumentStatus.INDEXING:
                        document.mark_failed(now=now)
                        await tx.rag_documents.update(document)
                    if attempts_exhausted:
                        await tx.audits.create(_exhausted_ingestion_audit(job=job, now=now))
                await tx.commit()
        return RagIngestionProcessResult(
            document_id=job.document_id,
            job_id=job.id,
            status=job.status,
            error_code=terminal_error_code,
        )


def _audit(
    source: RagPdfSource,
    *,
    event_type: str,
    action_summary: str,
    details: dict,
    now: datetime,
    risk_level: str = "L1",
) -> AuditLog:
    return AuditLog(
        id=uuid4(),
        task_id=source.artifact.task_id,
        run_id=source.artifact.run_id,
        tool_call_id=source.tool_call.id if source.tool_call else None,
        event_type=event_type,
        actor="rag-ingestion-service",
        risk_level=risk_level,
        action_summary=action_summary,
        details=details,
        created_at=now,
    )


def _exhausted_ingestion_audit(*, job: RagIngestionJob, now: datetime) -> AuditLog:
    """记录 crash/lease 恢复预算耗尽，不依赖仍可读取的源 Artifact。"""

    return AuditLog(
        id=uuid4(),
        event_type="rag.ingestion.failed",
        actor="rag-ingestion-service",
        risk_level="L1",
        permission_decision="not_required",
        action_summary="RAG 解析作业已耗尽恢复预算",
        details={
            "document_id": str(job.document_id),
            "job_id": str(job.id),
            "error_code": job.error_code,
            "attempts": job.attempts,
            "max_attempts": job.max_attempts,
            "retry_scheduled": False,
        },
        created_at=now,
    )


def _user_upload_permission_id(artifact: Artifact) -> UUID:
    """Resolve retry lineage while retaining compatibility with legacy uploads."""
    raw = artifact.metadata.get("permission_request_id")
    if isinstance(raw, str):
        try:
            return UUID(raw)
        except ValueError:
            pass
    return user_upload_permission_request_id(artifact.id)


def _document_not_found() -> RagIngestionError:
    return RagIngestionError(
        "RAG_DOCUMENT_NOT_FOUND",
        "RAG 文档不存在或不属于当前 Workspace",
        recoverable=False,
    )


def _version_conflict() -> RagIngestionError:
    return RagIngestionError(
        "RAG_DOCUMENT_VERSION_CONFLICT",
        "RAG 文档版本已变化，请刷新后重试",
        recoverable=True,
    )
