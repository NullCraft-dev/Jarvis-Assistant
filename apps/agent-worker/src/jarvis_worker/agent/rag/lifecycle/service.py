"""RAG 文档高风险生命周期操作。"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime
from uuid import NAMESPACE_URL, UUID, uuid5

from jarvis_worker.agent.rag.ingestion.asset_store import LocalRagAssetFileStore
from jarvis_worker.database.unit_of_work import PostgresUnitOfWork
from jarvis_worker.runtime.permissions.policy import (
    permission_request_deadline,
    permission_request_is_expired,
)
from jarvis_worker.shared.domain.models import (
    AuditLog,
    PermissionRequest,
    PermissionStatus,
    WorkspaceStatus,
    new_id,
    utcnow,
)
from jarvis_worker.shared.errors import application as errors

RAG_DELETE_TOOL_NAME = "rag.delete_document"


@dataclass(frozen=True, slots=True)
class RagDeleteResolution:
    request: PermissionRequest
    document_id: UUID
    deleted: bool
    cleanup_pending_count: int = 0
    source_artifact_retained: bool = True


class RagDocumentLifecycleService:
    def __init__(
        self,
        uow_factory,
        *,
        asset_file_store: LocalRagAssetFileStore,
        now=utcnow,
    ) -> None:
        self._uow_factory = uow_factory
        self._asset_file_store = asset_file_store
        self._now = now

    async def create_delete_request(
        self,
        *,
        workspace_id: UUID,
        document_id: UUID,
        expected_version: int,
    ) -> PermissionRequest:
        session_factory = self._uow_factory()
        async with session_factory() as session:
            async with PostgresUnitOfWork(session).transaction() as tx:
                document = await tx.rag_documents.get(document_id)
                if document is None or document.workspace_id != workspace_id:
                    raise errors.not_found("RagDocument", str(document_id))
                workspace = await tx.workspaces.get(workspace_id)
                if workspace is None or workspace.status is not WorkspaceStatus.ACTIVE:
                    raise errors.not_found("Workspace", str(workspace_id))
                if document.version != expected_version:
                    raise errors.AppError(
                        code="RAG_DOCUMENT_VERSION_CONFLICT",
                        message="RAG 文档版本已变化，请刷新后重试",
                        category="conflict",
                        recoverable=True,
                    )
                jobs = await tx.rag_ingestion_jobs.list_latest_by_documents(
                    workspace_id=workspace_id,
                    document_ids=[document_id],
                )
                if jobs and not jobs[0].is_terminal:
                    raise errors.AppError(
                        code="RAG_DOCUMENT_BUSY",
                        message="运行中的 RAG 文档必须先取消作业才能删除",
                        category="conflict",
                        recoverable=True,
                    )
                artifact = await tx.artifacts.get(document.source_artifact_id)
                if artifact is None:
                    raise errors.not_found("Artifact", str(document.source_artifact_id))
                request_id = uuid5(
                    NAMESPACE_URL,
                    f"jarvis:rag-delete:{workspace_id}:{document_id}:{expected_version}",
                )
                existing = await tx.permissions.get_request(request_id)
                if existing is not None:
                    await tx.commit()
                    return existing
                now = self._now()
                request = PermissionRequest(
                    id=request_id,
                    task_id=artifact.task_id,
                    run_id=artifact.run_id,
                    tool_name=RAG_DELETE_TOOL_NAME,
                    action_summary=f"永久删除 RAG 文档及派生索引：{document.title}",
                    reason="该操作会删除文档、作业、分块、向量和 RAG 派生图片，且不可撤销",
                    risk_level="L4",
                    scope={
                        "type": "once",
                        "workspace_id": str(workspace_id),
                        "document_id": str(document_id),
                    },
                    arguments_summary={
                        "document_id": str(document_id),
                        "title": document.title,
                        "chunk_count": document.chunk_count,
                        "source_artifact_retained": True,
                    },
                    allowed_decisions=["allow_once", "deny"],
                    checkpoint={
                        "version": 1,
                        "action": "rag_document_delete",
                        "workspace_id": str(workspace_id),
                        "document_id": str(document_id),
                        "expected_version": expected_version,
                        "cleanup_pending_references": [],
                    },
                    created_at=now,
                    expires_at=permission_request_deadline(now),
                )
                await tx.permissions.create_request(request)
                await tx.audits.create(
                    self._audit(
                        request,
                        event_type="rag.document.delete.permission_requested",
                        result_summary="pending",
                        now=now,
                    )
                )
                await tx.commit()
                return request

    async def resolve_delete_request(
        self, request_id: UUID, decision: str, note: str = ""
    ) -> RagDeleteResolution:
        if decision not in {"allow_once", "deny"}:
            raise errors.validation_error("RAG 文档删除只允许 allow_once 或 deny")
        request, document_id, deleted, pending = await self._resolve_database(
            request_id=request_id,
            decision=decision,
            note=note,
        )
        if decision == "deny":
            return RagDeleteResolution(request, document_id, deleted=False)
        failed = await self._cleanup_files(pending)
        request = await self._record_cleanup(request_id, failed)
        return RagDeleteResolution(
            request=request,
            document_id=document_id,
            deleted=deleted,
            cleanup_pending_count=len(failed),
        )

    async def _resolve_database(
        self, *, request_id: UUID, decision: str, note: str
    ) -> tuple[PermissionRequest, UUID, bool, tuple[str, ...]]:
        session_factory = self._uow_factory()
        async with session_factory() as session:
            async with PostgresUnitOfWork(session).transaction() as tx:
                request = await tx.permissions.get_request_for_update(request_id)
                if request is None:
                    raise errors.not_found("PermissionRequest", str(request_id))
                if request.tool_name != RAG_DELETE_TOOL_NAME:
                    raise errors.validation_error("权限请求不属于 RAG 文档删除")
                checkpoint = request.checkpoint or {}
                try:
                    document_id = UUID(str(checkpoint["document_id"]))
                    workspace_id = UUID(str(checkpoint["workspace_id"]))
                except (KeyError, ValueError):
                    raise errors.validation_error("RAG 删除权限检查点无效") from None
                if request.status in {PermissionStatus.CONSUMED, PermissionStatus.DENIED}:
                    if request.decision != decision:
                        raise errors.permission_conflict(
                            str(request_id), request.decision or "unknown", decision
                        )
                    pending = tuple(checkpoint.get("cleanup_pending_references") or ())
                    await tx.commit()
                    return request, document_id, request.status is PermissionStatus.CONSUMED, pending
                if request.status is not PermissionStatus.PENDING:
                    raise errors.permission_not_pending(str(request_id), request.status.value)

                now = self._now()
                if permission_request_is_expired(
                    expires_at=request.expires_at, now=now
                ):
                    raise errors.permission_not_pending(
                        str(request_id), PermissionStatus.EXPIRED.value
                    )
                if decision == "deny":
                    request.status = PermissionStatus.DENIED
                    request.decision = "deny"
                    request.decided_at = now
                    request.note = note[:500]
                    await tx.permissions.update_request(request)
                    await tx.audits.create(
                        self._audit(
                            request,
                            event_type="rag.document.delete.permission_decision",
                            result_summary="denied",
                            decision="deny",
                            note=note,
                            now=now,
                        )
                    )
                    await tx.commit()
                    return request, document_id, False, ()

                document = await tx.rag_documents.get(document_id)
                expected_version = int(checkpoint.get("expected_version", 0))
                if document is None or document.workspace_id != workspace_id:
                    raise errors.not_found("RagDocument", str(document_id))
                if document.version != expected_version:
                    raise errors.AppError(
                        code="RAG_DOCUMENT_VERSION_CONFLICT",
                        message="RAG 文档版本已变化，原删除确认已失效",
                        category="conflict",
                        recoverable=True,
                    )
                jobs = await tx.rag_ingestion_jobs.list_latest_by_documents(
                    workspace_id=workspace_id,
                    document_ids=[document_id],
                )
                if jobs and not jobs[0].is_terminal:
                    raise errors.AppError(
                        code="RAG_DOCUMENT_BUSY",
                        message="运行中的 RAG 文档不能永久删除",
                        category="conflict",
                        recoverable=True,
                    )
                assets = await tx.rag_assets.list_by_document(
                    workspace_id=workspace_id,
                    document_id=document_id,
                    limit=10_000,
                )
                references = tuple(dict.fromkeys(asset.storage_reference for asset in assets))
                if not await tx.rag_documents.delete(
                    workspace_id=workspace_id,
                    document_id=document_id,
                ):
                    raise errors.not_found("RagDocument", str(document_id))
                request.status = PermissionStatus.CONSUMED
                request.decision = "allow_once"
                request.decided_at = now
                request.note = note[:500]
                request.checkpoint = {
                    **checkpoint,
                    "cleanup_pending_references": list(references),
                    "deleted_at": now.isoformat(),
                }
                await tx.permissions.update_request(request)
                await tx.audits.create(
                    self._audit(
                        request,
                        event_type="rag.document.deleted",
                        result_summary="deleted",
                        decision="allow_once",
                        note=note,
                        details={"derived_asset_count": len(references)},
                        now=now,
                    )
                )
                await tx.commit()
                return request, document_id, True, references

    async def _cleanup_files(self, references: tuple[str, ...]) -> tuple[str, ...]:
        failed: list[str] = []
        for reference in references:
            try:
                await asyncio.to_thread(self._asset_file_store.delete_reference, reference)
            except (OSError, ValueError):
                failed.append(reference)
        return tuple(failed)

    async def _record_cleanup(
        self, request_id: UUID, failed: tuple[str, ...]
    ) -> PermissionRequest:
        session_factory = self._uow_factory()
        async with session_factory() as session:
            async with PostgresUnitOfWork(session).transaction() as tx:
                request = await tx.permissions.get_request_for_update(request_id)
                if request is None:
                    raise errors.not_found("PermissionRequest", str(request_id))
                request.checkpoint = {
                    **(request.checkpoint or {}),
                    "cleanup_pending_references": list(failed),
                }
                await tx.permissions.update_request(request)
                await tx.audits.create(
                    self._audit(
                        request,
                        event_type=(
                            "rag.document.delete.cleanup_pending"
                            if failed
                            else "rag.document.delete.cleanup_completed"
                        ),
                        result_summary="cleanup_pending" if failed else "completed",
                        details={"pending_count": len(failed)},
                        now=self._now(),
                    )
                )
                await tx.commit()
                return request

    @staticmethod
    def _audit(
        request: PermissionRequest,
        *,
        event_type: str,
        result_summary: str,
        now: datetime,
        decision: str | None = None,
        note: str = "",
        details: dict | None = None,
    ) -> AuditLog:
        return AuditLog(
            id=new_id(),
            task_id=request.task_id,
            run_id=request.run_id,
            event_type=event_type,
            actor="user",
            risk_level="L4",
            permission_decision=decision,
            action_summary=request.action_summary,
            details={
                "request_id": str(request.id),
                "document_id": str((request.checkpoint or {}).get("document_id", "")),
                "note": note[:500],
                **(details or {}),
            },
            result_summary=result_summary,
            created_at=now,
        )
