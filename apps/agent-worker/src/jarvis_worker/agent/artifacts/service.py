"""Artifact 查询与受控正文读取 Application Service。"""

from __future__ import annotations

from uuid import UUID

from jarvis_worker.shared.errors.application import AppError, not_found
from jarvis_worker.agent.artifacts.file_store import LocalArtifactFileStore
from jarvis_worker.agent.artifacts.workspace_file_reader import WorkspaceArtifactFileReader
from jarvis_worker.shared.domain.models import Artifact
from jarvis_worker.database.unit_of_work import PostgresUnitOfWork

_TEXT_KINDS = {"markdown", "text", "json", "diff"}
_WORKSPACE_TEXT_MIME_TYPES = {
    "application/json",
    "text/markdown",
    "text/plain",
    "text/x-diff",
}


class ArtifactApplicationService:
    """Artifact 读取 owner；数据库保存元数据，文件适配器保存大正文。"""

    def __init__(
        self,
        uow_factory,
        *,
        file_store: LocalArtifactFileStore,
        workspace_file_reader: WorkspaceArtifactFileReader,
    ):
        self._uow_factory = uow_factory
        self._file_store = file_store
        self._workspace_file_reader = workspace_file_reader

    async def get_with_content(self, artifact_id: UUID) -> tuple[Artifact, str]:
        session_factory = self._uow_factory()
        async with session_factory() as session:
            uow = PostgresUnitOfWork(session)
            artifact = await uow.artifacts.get(artifact_id)
            task = None
            source_tool_call = None
            if artifact is not None and self._is_workspace_file_deliverable(artifact):
                task = await uow.tasks.get(artifact.task_id)
                if artifact.source_tool_call_id is not None:
                    source_tool_call = await uow.tool_calls.get(
                        artifact.source_tool_call_id
                    )

        if artifact is None:
            raise not_found("Artifact", str(artifact_id))
        if self._is_workspace_file_deliverable(artifact):
            if (
                task is None
                or not task.workspace_path
                or source_tool_call is None
                or source_tool_call.task_id != artifact.task_id
                or source_tool_call.run_id != artifact.run_id
                or source_tool_call.tool_name != "workspace.create_file"
                or source_tool_call.status != "completed"
                or not self._has_trusted_workspace_lineage(
                    artifact, source_tool_call.result
                )
            ):
                raise self._integrity_error()
            relative_path = artifact.metadata.get("workspace_relative_path")
            mime_type = (artifact.mime_type or "").split(";", 1)[0].strip().lower()
            if (
                not isinstance(relative_path, str)
                or mime_type not in _WORKSPACE_TEXT_MIME_TYPES
                or artifact.file_size_bytes is None
                or artifact.content_hash is None
            ):
                raise self._integrity_error()
            try:
                stored = self._workspace_file_reader.read_text(
                    task.workspace_path,
                    relative_path,
                    expected_size_bytes=artifact.file_size_bytes,
                    expected_sha256=artifact.content_hash,
                )
            except (OSError, UnicodeError, ValueError):
                raise self._integrity_error() from None
            return artifact, stored.content
        if artifact.kind not in _TEXT_KINDS:
            raise AppError(
                code="ARTIFACT_PREVIEW_UNSUPPORTED",
                message="该产物暂不支持文本预览",
                category="validation",
            )
        if artifact.content is not None:
            return artifact, artifact.content
        if not artifact.file_path:
            raise AppError(
                code="ARTIFACT_CONTENT_UNAVAILABLE",
                message="产物正文不可用",
                category="storage",
                recoverable=False,
            )
        try:
            content = self._file_store.read_text(
                artifact.file_path, expected_sha256=artifact.content_hash
            )
        except (OSError, UnicodeError, ValueError):
            raise self._integrity_error() from None
        return artifact, content

    @staticmethod
    def _is_workspace_file_deliverable(artifact: Artifact) -> bool:
        return (
            artifact.kind == "file"
            and artifact.purpose == "deliverable"
            and artifact.producer_type == "tool"
            and artifact.source_tool_call_id is not None
            and artifact.metadata.get("storage") == "workspace"
        )

    @staticmethod
    def _has_trusted_workspace_lineage(
        artifact: Artifact, result: dict | None
    ) -> bool:
        if not isinstance(result, dict):
            return False
        artifact_ids = result.get("artifact_ids")
        data = result.get("data")
        deliverables = result.get("deliverables")
        if (
            not isinstance(artifact_ids, list)
            or artifact_ids != [str(artifact.id)]
            or not isinstance(data, dict)
            or not isinstance(deliverables, list)
            or len(deliverables) != 1
            or not isinstance(deliverables[0], dict)
        ):
            return False
        path = artifact.metadata.get("workspace_relative_path")
        deliverable = deliverables[0]
        return (
            data.get("created") is True
            and data.get("path") == path
            and data.get("size_bytes") == artifact.file_size_bytes
            and data.get("sha256") == artifact.content_hash
            and deliverable.get("kind") == "file"
            and deliverable.get("path") == path
            and deliverable.get("size_bytes") == artifact.file_size_bytes
            and deliverable.get("mime_type") == artifact.mime_type
            and deliverable.get("content_hash") == artifact.content_hash
        )

    @staticmethod
    def _integrity_error() -> AppError:
        return AppError(
            code="ARTIFACT_INTEGRITY_ERROR",
            message="产物文件不可读取或完整性校验失败",
            category="storage",
            recoverable=False,
        )
