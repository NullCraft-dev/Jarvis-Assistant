"""受控 PDF Artifact 的来源契约与纯校验。"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from uuid import UUID, uuid5

from jarvis_worker.shared.domain.models import (
    AgentRun,
    Artifact,
    PermissionRequest,
    PermissionStatus,
    RunStatus,
    Task,
    TaskStatus,
    ToolCall,
)

PDF_MIME_TYPE = "application/pdf"
RAG_UPLOAD_TOOL_NAME = "rag.upload_pdf"
RAG_UPLOAD_OPERATION_TYPE = "rag_user_upload"


@dataclass(frozen=True, slots=True)
class RagPdfSource:
    artifact: Artifact
    task: Task
    tool_call: ToolCall | None


def valid_pdf_artifact(artifact: Artifact) -> bool:
    return (
        artifact.kind == "file"
        and artifact.purpose == "deliverable"
        and artifact.producer_type in {"tool", "runtime"}
        and artifact.metadata.get("storage") == "local_file"
        and artifact.mime_type == PDF_MIME_TYPE
        and isinstance(artifact.file_path, str)
        and bool(artifact.file_path)
        and isinstance(artifact.file_size_bytes, int)
        and artifact.file_size_bytes > 0
        and isinstance(artifact.content_hash, str)
        and len(artifact.content_hash) == 64
        and all(character in "0123456789abcdef" for character in artifact.content_hash)
    )


def is_user_upload_artifact(artifact: Artifact) -> bool:
    """只接受由受控上传 Application Service 写入的 runtime Artifact 标记。"""
    return (
        valid_pdf_artifact(artifact)
        and artifact.producer_type == "runtime"
        and artifact.source_tool_call_id is None
        and artifact.metadata.get("source") == "user_upload"
        and artifact.metadata.get("explicit_user_action") is True
    )


def user_upload_permission_request_id(artifact_id: UUID, attempt: int = 1) -> UUID:
    """Return the deterministic permission ID for one upload attempt."""
    if attempt < 1:
        raise ValueError("RAG upload attempt 必须大于 0")
    root = uuid5(artifact_id, "jarvis:rag-upload-permission")
    return root if attempt == 1 else uuid5(root, f"jarvis:rag-upload-attempt:{attempt}")


def has_trusted_user_upload_lineage(
    *,
    artifact: Artifact,
    task: Task,
    run: AgentRun | None,
    permission: PermissionRequest | None,
    workspace_id: UUID,
) -> bool:
    """校验已完成或正在执行的用户显式上传来源。

    enqueue 发生在一次性权限消费和 Task/Run 完成之前，因此受控上传需要一个
    很窄的 approved staging 状态。该状态必须把权限、Artifact、Task、Run、
    Workspace 和文件摘要全部绑定；不能只凭 ``source=user_upload`` 放行。
    """
    if (
        not is_user_upload_artifact(artifact)
        or task.workspace_id != workspace_id
        or artifact.task_id != task.id
        or run is None
        or run.task_id != task.id
        or run.id != artifact.run_id
        or task.active_run_id != run.id
        or task.metadata.get("operation_type") != RAG_UPLOAD_OPERATION_TYPE
        or run.metadata.get("operation_type") != RAG_UPLOAD_OPERATION_TYPE
        or task.metadata.get("artifact_id") != str(artifact.id)
        or run.metadata.get("artifact_id") != str(artifact.id)
    ):
        return False

    if task.status is TaskStatus.COMPLETED and run.status is RunStatus.COMPLETED:
        return True

    checkpoint = permission.checkpoint if permission is not None else {}
    scope = permission.scope if permission is not None else {}
    attempt = checkpoint.get("attempt", 1)
    root_request_id = checkpoint.get(
        "root_request_id",
        str(user_upload_permission_request_id(artifact.id)),
    )
    valid_permission_identity = bool(
        isinstance(attempt, int)
        and 1 <= attempt <= 32
        and permission is not None
        and permission.id == user_upload_permission_request_id(artifact.id, attempt)
        and root_request_id == str(user_upload_permission_request_id(artifact.id))
    )
    return bool(
        task.status is TaskStatus.WAITING_FOR_USER
        and run.status is RunStatus.WAITING_PERMISSION
        and permission is not None
        and valid_permission_identity
        and permission.task_id == task.id
        and permission.run_id == run.id
        and permission.tool_name == RAG_UPLOAD_TOOL_NAME
        and permission.risk_level == "L2"
        and permission.status is PermissionStatus.APPROVED
        and permission.decision == "allow_once"
        and scope.get("type") == "once"
        and scope.get("workspace_id") == str(workspace_id)
        and scope.get("artifact_id") == str(artifact.id)
        and checkpoint.get("version") == 1
        and checkpoint.get("action") == "rag_upload_pdf"
        and checkpoint.get("workspace_id") == str(workspace_id)
        and checkpoint.get("artifact_id") == str(artifact.id)
        and checkpoint.get("filename") == artifact.title
        and checkpoint.get("size_bytes") == artifact.file_size_bytes
        and checkpoint.get("sha256") == artifact.content_hash
    )


def has_trusted_lineage(artifact: Artifact, result: dict | None) -> bool:
    if not isinstance(result, dict):
        return False
    artifact_ids = result.get("artifact_ids")
    data = result.get("data")
    deliverables = result.get("deliverables")
    if (
        artifact_ids != [str(artifact.id)]
        or not isinstance(data, dict)
        or not isinstance(deliverables, list)
        or len(deliverables) != 1
        or not isinstance(deliverables[0], dict)
    ):
        return False
    deliverable = deliverables[0]
    return (
        data.get("path") == artifact.file_path
        and data.get("size_bytes") == artifact.file_size_bytes
        and data.get("sha256") == artifact.content_hash
        and deliverable.get("kind") == "file"
        and deliverable.get("path") == artifact.file_path
        and deliverable.get("size_bytes") == artifact.file_size_bytes
        and deliverable.get("mime_type") == artifact.mime_type
        and deliverable.get("content_hash") == artifact.content_hash
    )


def build_ingestion_idempotency_key(
    *, workspace_id: UUID, artifact_id: UUID, content_hash: str, policy_version: str
) -> str:
    material = f"{workspace_id}:{artifact_id}:{content_hash}:{policy_version}"
    return hashlib.sha256(material.encode("utf-8")).hexdigest()
