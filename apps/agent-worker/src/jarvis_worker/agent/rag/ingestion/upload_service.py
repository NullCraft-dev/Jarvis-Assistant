"""用户显式上传 PDF 到 Artifact Store 并创建 RAG 入库作业。"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from pathlib import PurePath
from uuid import NAMESPACE_URL, UUID, uuid5

from jarvis_worker.agent.artifacts.file_store import LocalArtifactFileStore
from jarvis_worker.agent.rag.ingestion.service import (
    RagIngestionCommandService,
    RagIngestionEnqueueResult,
)
from jarvis_worker.agent.rag.ingestion.source import (
    RAG_UPLOAD_OPERATION_TYPE,
    RAG_UPLOAD_TOOL_NAME,
    user_upload_permission_request_id,
)
from jarvis_worker.database.unit_of_work import PostgresUnitOfWork
from jarvis_worker.runtime.permissions.policy import (
    permission_request_deadline,
    permission_request_is_expired,
)
from jarvis_worker.shared.domain.models import (
    AgentRun,
    Artifact,
    AuditLog,
    Conversation,
    Message,
    PermissionRequest,
    PermissionStatus,
    RunStatus,
    RuntimeEvent,
    Task,
    TaskStatus,
    WorkspaceStatus,
    new_id,
    utcnow,
)
from jarvis_worker.shared.errors.application import AppError
from jarvis_worker.shared.storage_capacity import StorageCapacityExceeded

PDF_MIME_TYPE = "application/pdf"
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_MAX_UPLOAD_ATTEMPTS = 32


@dataclass(frozen=True, slots=True)
class RagUploadResult:
    artifact_id: UUID
    enqueue: RagIngestionEnqueueResult
    uploaded: bool


class RagUploadApplicationService:
    """显式用户写入 owner；不接受路径，只接受有界 PDF bytes。"""

    def __init__(
        self,
        uow_factory,
        *,
        artifact_file_store: LocalArtifactFileStore,
        ingestion_service: RagIngestionCommandService,
    ) -> None:
        self._uow_factory = uow_factory
        self._artifact_file_store = artifact_file_store
        self._ingestion_service = ingestion_service

    async def create_upload_request(
        self,
        *,
        workspace_id: UUID,
        filename: str,
        size_bytes: int,
        content_sha256: str,
    ) -> PermissionRequest:
        title = _safe_title(filename)
        if size_bytes < 1 or size_bytes > self._artifact_file_store.max_bytes:
            raise AppError(
                code="RAG_UPLOAD_SIZE_INVALID",
                message=(
                    "PDF 必须大于 0 且不超过 "
                    f"{self._artifact_file_store.max_bytes // (1024 * 1024)} MiB"
                ),
                category="validation",
            )
        digest = content_sha256.strip().lower()
        if _SHA256_RE.fullmatch(digest) is None:
            raise AppError(
                code="RAG_UPLOAD_HASH_INVALID",
                message="PDF 内容哈希无效",
                category="validation",
            )
        artifact_id = uuid5(NAMESPACE_URL, f"jarvis:rag-user-upload:{workspace_id}:{digest}")
        conversation_id = uuid5(workspace_id, "jarvis:rag-user-uploads")
        now = utcnow()
        session_factory = self._uow_factory()
        async with session_factory() as session:
            async with PostgresUnitOfWork(session).transaction() as tx:
                workspace = await tx.workspaces.get(workspace_id)
                if workspace is None or workspace.status is not WorkspaceStatus.ACTIVE:
                    raise AppError(
                        code="WORKSPACE_NOT_FOUND",
                        message="工作区不存在或已撤销",
                        category="validation",
                    )
                root_request_id = user_upload_permission_request_id(artifact_id)
                existing = await tx.permissions.get_request_for_update(root_request_id)
                attempt = 1
                while existing is not None:
                    if _upload_permission_is_reusable(existing):
                        await tx.commit()
                        return existing
                    attempt += 1
                    if attempt > _MAX_UPLOAD_ATTEMPTS:
                        raise AppError(
                            code="RAG_UPLOAD_ATTEMPTS_EXHAUSTED",
                            message="该文件的上传权限尝试次数已达上限",
                            category="permission",
                            recoverable=False,
                        )
                    existing = await tx.permissions.get_request(
                        user_upload_permission_request_id(artifact_id, attempt)
                    )
                task_id, run_id, request_id = _upload_attempt_ids(artifact_id, attempt)
                conversation = await tx.conversations.get(conversation_id)
                if conversation is None:
                    conversation = Conversation(
                        id=conversation_id,
                        title="RAG 文档上传",
                        created_at=now,
                        updated_at=now,
                    )
                    await tx.conversations.create(conversation)
                    await tx.flush()
                task = await tx.tasks.get(task_id)
                if task is None:
                    task = Task(
                        id=task_id,
                        conversation_id=conversation_id,
                        title=f"上传到 RAG：{title}",
                        user_goal=f"用户请求上传 PDF 到 RAG 文档库：{title}",
                        status=TaskStatus.WAITING_FOR_USER,
                        workspace_path=workspace.canonical_path,
                        workspace_id=workspace_id,
                        active_run_id=run_id,
                        created_at=now,
                        updated_at=now,
                        metadata={
                            "operation_type": RAG_UPLOAD_OPERATION_TYPE,
                            "artifact_id": str(artifact_id),
                        },
                    )
                    await tx.tasks.create(task)
                    await tx.flush()
                    await tx.runs.create(
                        AgentRun(
                            id=run_id,
                            task_id=task_id,
                            status=RunStatus.WAITING_PERMISSION,
                            version=1,
                            created_at=now,
                            updated_at=now,
                            metadata={
                                "operation_type": RAG_UPLOAD_OPERATION_TYPE,
                                "artifact_id": str(artifact_id),
                            },
                        )
                    )
                    await tx.flush()
                    await tx.messages.create(
                        Message(
                            id=uuid5(request_id, "jarvis:rag-upload-message"),
                            conversation_id=conversation_id,
                            task_id=task_id,
                            run_id=run_id,
                            role="user",
                            content=f"上传 PDF 到 RAG 文档库：{title}",
                            created_at=now,
                            metadata={"operation_type": RAG_UPLOAD_OPERATION_TYPE},
                        )
                    )
                    await tx.events.append(
                        [
                            RuntimeEvent(
                                id=new_id(),
                                event_id=uuid5(run_id, "task.created"),
                                type="task.created",
                                task_id=task_id,
                                run_id=run_id,
                                event_sequence=1,
                                payload={
                                    "task_id": str(task_id),
                                    "run_id": str(run_id),
                                    "operation_type": RAG_UPLOAD_OPERATION_TYPE,
                                },
                                created_at=now,
                            )
                        ]
                    )
                request = PermissionRequest(
                    id=request_id,
                    task_id=task_id,
                    run_id=run_id,
                    tool_name=RAG_UPLOAD_TOOL_NAME,
                    action_summary=f"上传并加入 RAG：{title}",
                    reason="PDF 将写入本地 Artifact Store，并创建解析、分块和向量化作业",
                    risk_level="L2",
                    scope={
                        "type": "once",
                        "workspace_id": str(workspace_id),
                        "artifact_id": str(artifact_id),
                    },
                    arguments_summary={
                        "filename": title,
                        "size_bytes": size_bytes,
                        "content_sha256": digest,
                    },
                    allowed_decisions=["allow_once", "deny"],
                    checkpoint={
                        "version": 1,
                        "action": "rag_upload_pdf",
                        "workspace_id": str(workspace_id),
                        "artifact_id": str(artifact_id),
                        "filename": title,
                        "size_bytes": size_bytes,
                        "sha256": digest,
                        "attempt": attempt,
                        "root_request_id": str(root_request_id),
                    },
                    created_at=now,
                    expires_at=permission_request_deadline(now),
                )
                await tx.permissions.create_request(request)
                sequence = await tx.events.get_next_sequence(run_id)
                await tx.events.append(
                    [
                        RuntimeEvent(
                            id=new_id(),
                            event_id=uuid5(run_id, "permission.required"),
                            type="permission.required",
                            task_id=task_id,
                            run_id=run_id,
                            event_sequence=sequence,
                            payload={"request": _public_permission(request)},
                            created_at=now,
                        )
                    ]
                )
                await tx.audits.create(
                    AuditLog(
                        id=new_id(),
                        task_id=task_id,
                        run_id=run_id,
                        event_type="rag.upload.permission_requested",
                        actor="user",
                        risk_level="L2",
                        action_summary=request.action_summary,
                        details={
                            "request_id": str(request.id),
                            "workspace_id": str(workspace_id),
                            "artifact_id": str(artifact_id),
                            "size_bytes": size_bytes,
                            "attempt": attempt,
                        },
                        result_summary="pending",
                        created_at=now,
                    )
                )
                await tx.commit()
                return request

    async def resolve_upload_request(
        self, request_id: UUID, decision: str, note: str = ""
    ) -> PermissionRequest:
        if decision not in {"allow_once", "deny"}:
            raise AppError(
                code="VALIDATION_ERROR",
                message="RAG 上传只允许 allow_once 或 deny",
                category="validation",
            )
        session_factory = self._uow_factory()
        async with session_factory() as session:
            async with PostgresUnitOfWork(session).transaction() as tx:
                request = await tx.permissions.get_request_for_update(request_id)
                if request is None:
                    raise AppError("NOT_FOUND", "权限请求不存在", "validation")
                if request.tool_name != RAG_UPLOAD_TOOL_NAME:
                    raise AppError("VALIDATION_ERROR", "权限请求不属于 RAG 上传", "validation")
                if request.status in {
                    PermissionStatus.APPROVED,
                    PermissionStatus.DENIED,
                    PermissionStatus.CONSUMED,
                }:
                    if request.decision != decision:
                        raise AppError(
                            "PERMISSION_DECISION_CONFLICT",
                            "权限请求已使用不同决定处理",
                            "conflict",
                        )
                    await tx.commit()
                    return request
                if request.status is not PermissionStatus.PENDING:
                    raise AppError(
                        "PERMISSION_NOT_PENDING",
                        "权限请求已不处于待处理状态",
                        "validation",
                    )
                now = utcnow()
                if permission_request_is_expired(expires_at=request.expires_at, now=now):
                    raise AppError(
                        "PERMISSION_NOT_PENDING",
                        "权限请求已过期",
                        "permission",
                    )
                request.status = (
                    PermissionStatus.APPROVED
                    if decision == "allow_once"
                    else PermissionStatus.DENIED
                )
                request.decision = decision
                request.decided_at = now
                request.note = note[:500]
                await tx.permissions.update_request(request)
                if decision == "deny":
                    task = await tx.tasks.get(request.task_id)
                    run = await tx.runs.get(request.run_id)
                    if task is not None:
                        task.status = TaskStatus.CANCELLED
                        task.updated_at = now
                        task.completed_at = now
                        await tx.tasks.update(task)
                    if run is not None:
                        run.status = RunStatus.CANCELLED
                        run.updated_at = now
                        run.completed_at = now
                        await tx.runs.update(run)
                    sequence = await tx.events.get_next_sequence(request.run_id)
                    await tx.events.append(
                        [
                            RuntimeEvent(
                                id=new_id(),
                                event_id=uuid5(request.run_id, "agent.run.cancelled"),
                                type="agent.run.cancelled",
                                task_id=request.task_id,
                                run_id=request.run_id,
                                event_sequence=sequence,
                                payload={"reason": "rag_upload_permission_denied"},
                                created_at=now,
                            )
                        ]
                    )
                await tx.audits.create(
                    AuditLog(
                        id=new_id(),
                        task_id=request.task_id,
                        run_id=request.run_id,
                        event_type="rag.upload.permission_decision",
                        actor="user",
                        risk_level="L2",
                        permission_decision=decision,
                        action_summary=request.action_summary,
                        details={"request_id": str(request.id), "note": note[:500]},
                        result_summary="approved" if decision == "allow_once" else "denied",
                        created_at=now,
                    )
                )
                await tx.commit()
                return request

    async def upload_pdf(
        self,
        *,
        workspace_id: UUID,
        filename: str,
        content: bytes,
        permission_request_id: UUID,
    ) -> RagUploadResult:
        title = _safe_title(filename)
        if not content or len(content) > self._artifact_file_store.max_bytes:
            raise AppError(
                code="RAG_UPLOAD_SIZE_INVALID",
                message=f"PDF 必须大于 0 且不超过 {self._artifact_file_store.max_bytes // (1024 * 1024)} MiB",
                category="validation",
            )
        digest = hashlib.sha256(content).hexdigest()
        artifact_id = uuid5(NAMESPACE_URL, f"jarvis:rag-user-upload:{workspace_id}:{digest}")
        try:
            permission = await self._validate_upload_permission(
                permission_request_id=permission_request_id,
                workspace_id=workspace_id,
                filename=title,
                size_bytes=len(content),
                content_sha256=digest,
            )
        except AppError as exc:
            if exc.code != "RAG_UPLOAD_PERMISSION_MISMATCH":
                raise
            permission = await self._validate_upload_permission(
                permission_request_id=permission_request_id,
                workspace_id=workspace_id,
                filename=title,
                size_bytes=len(content),
                content_sha256=digest,
                allow_consumed_filename_alias=True,
            )
            existing = await self._get_artifact(artifact_id)
        else:
            existing = await self._get_artifact(artifact_id)
        if not content.startswith(b"%PDF-"):
            await self._fail_upload_permission(
                permission.id,
                code="RAG_UPLOAD_PDF_INVALID",
                message="上传内容不是有效 PDF",
            )
            raise AppError(
                code="RAG_UPLOAD_PDF_INVALID",
                message="上传内容不是有效 PDF",
                category="validation",
            )
        if permission.status is PermissionStatus.CONSUMED and existing is None:
            raise AppError(
                code="RAG_UPLOAD_INTEGRITY_ERROR",
                message="已消费的上传权限缺少对应 Artifact",
                category="storage",
                recoverable=False,
            )
        uploaded = existing is None
        if existing is None:
            run_id = permission.run_id
            try:
                stored = self._artifact_file_store.write_bytes(
                    artifact_id,
                    content,
                    run_id=run_id,
                    workspace_id=workspace_id,
                    suffix=".pdf",
                    mime_type=PDF_MIME_TYPE,
                )
            except StorageCapacityExceeded as exc:
                raise AppError(
                    code=exc.code,
                    message="Artifact 存储容量不足，无法上传 PDF",
                    category="storage",
                    recoverable=True,
                ) from None
            try:
                await self._persist_upload_operation(
                    workspace_id=workspace_id,
                    artifact_id=artifact_id,
                    title=title,
                    relative_path=stored.relative_path,
                    size_bytes=stored.size_bytes,
                    content_hash=stored.sha256,
                    task_id=permission.task_id,
                    run_id=permission.run_id,
                    permission_request_id=permission.id,
                )
            except BaseException:
                self._artifact_file_store.delete(stored.relative_path)
                raise
        else:
            await self._validate_existing(existing, workspace_id, digest, len(content))

        enqueue = await self._ingestion_service.enqueue_pdf(
            workspace_id=workspace_id,
            source_artifact_id=artifact_id,
        )
        await self._consume_upload_permission(permission.id, artifact_id)
        return RagUploadResult(artifact_id=artifact_id, enqueue=enqueue, uploaded=uploaded)

    async def _validate_upload_permission(
        self,
        *,
        permission_request_id: UUID,
        workspace_id: UUID,
        filename: str,
        size_bytes: int,
        content_sha256: str,
        allow_consumed_filename_alias: bool = False,
    ) -> PermissionRequest:
        session_factory = self._uow_factory()
        async with session_factory() as session:
            request = await PostgresUnitOfWork(session).permissions.get_request(
                permission_request_id
            )
        if request is None or request.tool_name != RAG_UPLOAD_TOOL_NAME:
            raise AppError("PERMISSION_REQUIRED", "RAG 上传缺少有效权限请求", "permission")
        if request.status is PermissionStatus.DENIED:
            raise AppError("PERMISSION_DENIED", "用户已拒绝 RAG 上传", "permission")
        if request.status not in {PermissionStatus.APPROVED, PermissionStatus.CONSUMED}:
            raise AppError("PERMISSION_REQUIRED", "RAG 上传尚未获得用户确认", "permission")
        checkpoint = request.checkpoint or {}
        expected_artifact_id = uuid5(
            NAMESPACE_URL,
            f"jarvis:rag-user-upload:{workspace_id}:{content_sha256}",
        )
        consumed_content_alias = bool(
            allow_consumed_filename_alias
            and request.status is PermissionStatus.CONSUMED
            and checkpoint.get("artifact_id") == str(expected_artifact_id)
            and checkpoint.get("workspace_id") == str(workspace_id)
            and checkpoint.get("size_bytes") == size_bytes
            and checkpoint.get("sha256") == content_sha256
        )
        if (
            checkpoint.get("action") != "rag_upload_pdf"
            or checkpoint.get("workspace_id") != str(workspace_id)
            or (checkpoint.get("filename") != filename and not consumed_content_alias)
            or checkpoint.get("size_bytes") != size_bytes
            or checkpoint.get("sha256") != content_sha256
        ):
            raise AppError(
                "RAG_UPLOAD_PERMISSION_MISMATCH",
                "上传文件与已批准的文件摘要不一致",
                "permission",
            )
        return request

    async def _get_artifact(self, artifact_id: UUID) -> Artifact | None:
        session_factory = self._uow_factory()
        async with session_factory() as session:
            return await PostgresUnitOfWork(session).artifacts.get(artifact_id)

    async def _validate_existing(
        self, artifact: Artifact, workspace_id: UUID, digest: str, size_bytes: int
    ) -> None:
        session_factory = self._uow_factory()
        async with session_factory() as session:
            uow = PostgresUnitOfWork(session)
            task = await uow.tasks.get(artifact.task_id)
        if (
            task is None
            or task.workspace_id != workspace_id
            or artifact.content_hash != digest
            or artifact.file_size_bytes != size_bytes
            or artifact.metadata.get("source") != "user_upload"
        ):
            raise AppError(
                code="RAG_UPLOAD_INTEGRITY_ERROR",
                message="已存在上传记录的来源校验失败",
                category="storage",
                recoverable=False,
            )
        try:
            stored = self._artifact_file_store.read_bytes(
                artifact.file_path or "", expected_sha256=digest
            )
        except (OSError, ValueError):
            raise AppError(
                code="RAG_UPLOAD_INTEGRITY_ERROR",
                message="已存在上传文件不可读取或完整性校验失败",
                category="storage",
                recoverable=False,
            ) from None
        if len(stored) != size_bytes:
            raise AppError(
                code="RAG_UPLOAD_INTEGRITY_ERROR",
                message="已存在上传文件大小不一致",
                category="storage",
                recoverable=False,
            )

    async def _persist_upload_operation(
        self,
        *,
        workspace_id: UUID,
        artifact_id: UUID,
        title: str,
        relative_path: str,
        size_bytes: int,
        content_hash: str,
        task_id: UUID,
        run_id: UUID,
        permission_request_id: UUID,
    ) -> None:
        now = utcnow()
        session_factory = self._uow_factory()
        async with session_factory() as session:
            async with PostgresUnitOfWork(session).transaction() as tx:
                workspace = await tx.workspaces.get(workspace_id)
                if workspace is None or workspace.status is not WorkspaceStatus.ACTIVE:
                    raise AppError(
                        code="WORKSPACE_NOT_FOUND",
                        message="工作区不存在或已撤销",
                        category="validation",
                    )
                task = await tx.tasks.get(task_id)
                run = await tx.runs.get(run_id)
                if task is None or run is None:
                    raise AppError(
                        code="RAG_UPLOAD_PERMISSION_STATE_INVALID",
                        message="RAG 上传权限未绑定有效任务",
                        category="permission",
                    )
                artifact = Artifact(
                    id=artifact_id,
                    task_id=task_id,
                    run_id=run_id,
                    kind="file",
                    title=title,
                    purpose="deliverable",
                    producer_type="runtime",
                    file_path=relative_path,
                    file_size_bytes=size_bytes,
                    mime_type=PDF_MIME_TYPE,
                    content_hash=content_hash,
                    metadata={
                        "storage": "local_file",
                        "source": "user_upload",
                        "explicit_user_action": True,
                        "permission_request_id": str(permission_request_id),
                    },
                    created_at=now,
                )
                await tx.artifacts.create(artifact)
                await tx.audits.create(
                    AuditLog(
                        id=new_id(),
                        task_id=task_id,
                        run_id=run_id,
                        event_type="rag.upload.artifact_persisted",
                        actor="system",
                        risk_level="L2",
                        permission_decision="allow_once",
                        action_summary="将获批 PDF 写入 RAG Artifact Store",
                        details={
                            "artifact_id": str(artifact_id),
                            "workspace_id": str(workspace_id),
                            "size_bytes": size_bytes,
                        },
                        created_at=now,
                    )
                )
                await tx.commit()

    async def _consume_upload_permission(self, request_id: UUID, artifact_id: UUID) -> None:
        now = utcnow()
        session_factory = self._uow_factory()
        async with session_factory() as session:
            async with PostgresUnitOfWork(session).transaction() as tx:
                request = await tx.permissions.get_request_for_update(request_id)
                if request is None or request.tool_name != RAG_UPLOAD_TOOL_NAME:
                    raise AppError("PERMISSION_REQUIRED", "RAG 上传权限请求不存在", "permission")
                if request.status is PermissionStatus.CONSUMED:
                    await tx.commit()
                    return
                if request.status is not PermissionStatus.APPROVED:
                    raise AppError("PERMISSION_NOT_APPROVED", "RAG 上传权限尚未批准", "permission")
                task = await tx.tasks.get(request.task_id)
                run = await tx.runs.get(request.run_id)
                if task is None or run is None:
                    raise AppError(
                        "RAG_UPLOAD_PERMISSION_STATE_INVALID",
                        "RAG 上传权限未绑定有效任务",
                        "permission",
                    )
                request.status = PermissionStatus.CONSUMED
                request.decision = "allow_once"
                request.checkpoint = {
                    **(request.checkpoint or {}),
                    "artifact_id": str(artifact_id),
                    "consumed_at": now.isoformat(),
                }
                await tx.permissions.update_request(request)
                task.status = TaskStatus.COMPLETED
                task.updated_at = now
                task.completed_at = now
                await tx.tasks.update(task)
                run.status = RunStatus.COMPLETED
                run.updated_at = now
                run.completed_at = now
                await tx.runs.update(run)
                sequence = await tx.events.get_next_sequence(request.run_id)
                await tx.events.append(
                    [
                        RuntimeEvent(
                            id=new_id(),
                            event_id=uuid5(request.run_id, "agent.run.completed"),
                            type="agent.run.completed",
                            task_id=request.task_id,
                            run_id=request.run_id,
                            event_sequence=sequence,
                            payload={
                                "task_id": str(request.task_id),
                                "run_id": str(request.run_id),
                                "artifact_id": str(artifact_id),
                            },
                            created_at=now,
                        )
                    ]
                )
                await tx.audits.create(
                    AuditLog(
                        id=new_id(),
                        task_id=request.task_id,
                        run_id=request.run_id,
                        event_type="rag.upload.completed",
                        actor="user",
                        risk_level="L2",
                        permission_decision="allow_once",
                        action_summary=request.action_summary,
                        details={
                            "request_id": str(request.id),
                            "artifact_id": str(artifact_id),
                        },
                        result_summary="completed",
                        created_at=now,
                    )
                )
                await tx.commit()

    async def _fail_upload_permission(
        self,
        request_id: UUID,
        *,
        code: str,
        message: str,
    ) -> None:
        """Consume one-shot approval and terminalize its synthetic Run on invalid bytes."""

        now = utcnow()
        error = {
            "code": code,
            "message": message,
            "category": "validation",
            "recoverable": False,
        }
        session_factory = self._uow_factory()
        async with session_factory() as session:
            async with PostgresUnitOfWork(session).transaction() as tx:
                request = await tx.permissions.get_request_for_update(request_id)
                if request is None or request.tool_name != RAG_UPLOAD_TOOL_NAME:
                    raise AppError("PERMISSION_REQUIRED", "RAG 上传权限请求不存在", "permission")
                if request.status is PermissionStatus.CONSUMED:
                    await tx.commit()
                    return
                if request.status is not PermissionStatus.APPROVED:
                    raise AppError(
                        "PERMISSION_NOT_APPROVED",
                        "RAG 上传权限尚未批准",
                        "permission",
                    )
                task = await tx.tasks.get(request.task_id)
                run = await tx.runs.get(request.run_id)
                if task is None or run is None:
                    raise AppError(
                        "RAG_UPLOAD_PERMISSION_STATE_INVALID",
                        "RAG 上传权限未绑定有效任务",
                        "permission",
                    )
                request.status = PermissionStatus.CONSUMED
                request.checkpoint = {
                    **(request.checkpoint or {}),
                    "failed_at": now.isoformat(),
                    "error_code": code,
                }
                await tx.permissions.update_request(request)
                task.status = TaskStatus.FAILED
                task.last_step_summary = message
                task.updated_at = now
                await tx.tasks.update(task)
                run.status = RunStatus.FAILED
                run.failed_at = now
                run.updated_at = now
                run.error = error
                run.checkpoint = {}
                await tx.runs.update(run)
                sequence = await tx.events.get_next_sequence(request.run_id)
                await tx.events.append(
                    [
                        RuntimeEvent(
                            id=new_id(),
                            event_id=uuid5(request.run_id, f"agent.run.failed:{code}"),
                            type="agent.run.failed",
                            task_id=request.task_id,
                            run_id=request.run_id,
                            event_sequence=sequence,
                            payload={"error": error},
                            created_at=now,
                        )
                    ]
                )
                await tx.audits.create(
                    AuditLog(
                        id=new_id(),
                        task_id=request.task_id,
                        run_id=request.run_id,
                        event_type="rag.upload.failed",
                        actor="system",
                        risk_level="L2",
                        permission_decision="allow_once",
                        action_summary=request.action_summary,
                        details={"request_id": str(request.id), "error_code": code},
                        error=error,
                        result_summary="failed",
                        created_at=now,
                    )
                )
                await tx.commit()


def _safe_title(filename: str) -> str:
    title = PurePath(filename.strip()).name
    if not title or len(title) > 500 or not title.lower().endswith(".pdf"):
        raise AppError(
            code="RAG_UPLOAD_FILENAME_INVALID",
            message="文件名必须是长度不超过 500 的 PDF 文件名",
            category="validation",
        )
    return title


def _upload_attempt_ids(artifact_id: UUID, attempt: int) -> tuple[UUID, UUID, UUID]:
    request_id = user_upload_permission_request_id(artifact_id, attempt)
    if attempt == 1:
        return (
            uuid5(artifact_id, "jarvis:rag-upload-task"),
            uuid5(artifact_id, "jarvis:rag-upload-run"),
            request_id,
        )
    return (
        uuid5(request_id, "jarvis:rag-upload-task"),
        uuid5(request_id, "jarvis:rag-upload-run"),
        request_id,
    )


def _upload_permission_is_reusable(request: PermissionRequest) -> bool:
    """Reuse active/successful attempts, but never reopen a terminal failure."""
    if request.status in {PermissionStatus.PENDING, PermissionStatus.APPROVED}:
        return True
    return bool(
        request.status is PermissionStatus.CONSUMED
        and not (request.checkpoint or {}).get("error_code")
    )


def _public_permission(request: PermissionRequest) -> dict:
    return {
        "id": str(request.id),
        "task_id": str(request.task_id),
        "run_id": str(request.run_id),
        "step_id": None,
        "tool_name": request.tool_name,
        "action_summary": request.action_summary,
        "reason": request.reason,
        "risk_level": request.risk_level,
        "scope": request.scope,
        "arguments_summary": request.arguments_summary,
        "allowed_decisions": request.allowed_decisions,
        "created_at": request.created_at.isoformat(),
        "expires_at": request.expires_at.isoformat(),
        "status": request.status.value,
        "decision": request.decision,
    }
