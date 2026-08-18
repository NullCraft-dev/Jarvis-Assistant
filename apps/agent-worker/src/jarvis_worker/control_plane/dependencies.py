"""FastAPI 依赖注入。

为每个请求提供 Application Service 实例。
"""

import os
import platform
from pathlib import Path

from jarvis_worker.agent.artifacts.file_store import LocalArtifactFileStore
from jarvis_worker.agent.artifacts.service import ArtifactApplicationService
from jarvis_worker.agent.artifacts.workspace_file_reader import WorkspaceArtifactFileReader
from jarvis_worker.agent.knowledge.service import KnowledgeApplicationService
from jarvis_worker.agent.mcp.service import McpApplicationService
from jarvis_worker.agent.memory.candidate_service import MemoryCandidateApplicationService
from jarvis_worker.agent.memory.service import MemoryApplicationService
from jarvis_worker.agent.rag.evaluation.feedback_service import RagEvaluationFeedbackService
from jarvis_worker.agent.rag.evaluation.gate_service import RagQualityGateService
from jarvis_worker.agent.rag.evaluation.review_service import RagEvaluationReviewService
from jarvis_worker.agent.rag.ingestion import RagIngestionCommandService
from jarvis_worker.agent.rag.ingestion.asset_store import LocalRagAssetFileStore
from jarvis_worker.agent.rag.ingestion.upload_service import RagUploadApplicationService
from jarvis_worker.agent.rag.lifecycle import RagDocumentLifecycleService
from jarvis_worker.agent.rag.query import RagDocumentQueryService
from jarvis_worker.agent.rag.worker.config import RagWorkerConfig
from jarvis_worker.database.engine import get_session_factory
from jarvis_worker.database.reconciliation_service import (
    StorageReconciliationApplicationService,
)
from jarvis_worker.runtime.audit.service import AuditQueryApplicationService
from jarvis_worker.runtime.conversations.service import ConversationApplicationService
from jarvis_worker.runtime.event_service import EventApplicationService
from jarvis_worker.runtime.permissions.service import PermissionApplicationService
from jarvis_worker.runtime.runs.service import RunApplicationService
from jarvis_worker.runtime.schedules.service import (
    ScheduledTaskApplicationService,
    ScheduledTaskWorker,
)
from jarvis_worker.runtime.tasks.service import TaskApplicationService
from jarvis_worker.runtime.workspaces.workspace_policy import WorkspacePolicy
from jarvis_worker.runtime.workspaces.workspace_service import WorkspaceApplicationService
from jarvis_worker.shared.config.settings import WorkerConfig

# 全局单例（在 app lifespan 中初始化）
_task_service: TaskApplicationService | None = None
_run_service: RunApplicationService | None = None
_event_service: EventApplicationService | None = None
_permission_service: PermissionApplicationService | None = None
_conversation_service: ConversationApplicationService | None = None
_workspace_service: WorkspaceApplicationService | None = None
_audit_query_service: AuditQueryApplicationService | None = None
_artifact_service: ArtifactApplicationService | None = None
_storage_reconciliation_service: StorageReconciliationApplicationService | None = None
_memory_service: MemoryApplicationService | None = None
_memory_candidate_service: MemoryCandidateApplicationService | None = None
_knowledge_service: KnowledgeApplicationService | None = None
_scheduled_task_service: ScheduledTaskApplicationService | None = None
_scheduled_task_worker: ScheduledTaskWorker | None = None
_mcp_service: McpApplicationService | None = None
_rag_document_query_service: RagDocumentQueryService | None = None
_rag_upload_service: RagUploadApplicationService | None = None
_rag_ingestion_command_service: RagIngestionCommandService | None = None
_rag_document_lifecycle_service: RagDocumentLifecycleService | None = None
_rag_evaluation_feedback_service: RagEvaluationFeedbackService | None = None
_rag_evaluation_review_service: RagEvaluationReviewService | None = None
_rag_quality_gate_service: RagQualityGateService | None = None


def _create_picker():
    """根据平台创建对应的 WorkspacePicker。"""
    if platform.system() == "Darwin":
        from jarvis_worker.runtime.workspaces.workspace_picker_macos import (
            MacOSWorkspacePickerAdapter,
        )
        return MacOSWorkspacePickerAdapter()
    from jarvis_worker.runtime.workspaces.workspace_picker_unsupported import (
        UnsupportedWorkspacePickerAdapter,
    )
    return UnsupportedWorkspacePickerAdapter()


def init_services():
    """同步初始化所有 Application Service（Picker + Service + Policy）。"""
    global _task_service, _run_service, _event_service, _permission_service, _conversation_service, _workspace_service, _audit_query_service, _artifact_service, _storage_reconciliation_service, _memory_service, _memory_candidate_service, _knowledge_service, _scheduled_task_service, _scheduled_task_worker, _mcp_service, _rag_document_query_service, _rag_upload_service, _rag_ingestion_command_service, _rag_document_lifecycle_service, _rag_evaluation_feedback_service, _rag_evaluation_review_service, _rag_quality_gate_service

    picker = _create_picker()
    _workspace_service = WorkspaceApplicationService(get_session_factory, picker=picker)
    workspace_policy = WorkspacePolicy.from_env()

    _task_service = TaskApplicationService(
        get_session_factory,
        workspace_policy=workspace_policy,
        workspace_service=_workspace_service,
    )
    _run_service = RunApplicationService(get_session_factory, workspace_policy=workspace_policy)
    _event_service = EventApplicationService(get_session_factory)
    _permission_service = PermissionApplicationService(get_session_factory)
    _conversation_service = ConversationApplicationService(get_session_factory)
    _audit_query_service = AuditQueryApplicationService(get_session_factory)
    _memory_service = MemoryApplicationService(get_session_factory)
    _memory_candidate_service = MemoryCandidateApplicationService(get_session_factory)
    _knowledge_service = KnowledgeApplicationService(get_session_factory)
    _scheduled_task_service = ScheduledTaskApplicationService(get_session_factory, _task_service)
    _scheduled_task_worker = ScheduledTaskWorker(_scheduled_task_service)
    _mcp_service = McpApplicationService(get_session_factory)
    _rag_document_query_service = RagDocumentQueryService(get_session_factory)
    _rag_evaluation_feedback_service = RagEvaluationFeedbackService(get_session_factory)
    _rag_evaluation_review_service = RagEvaluationReviewService(get_session_factory)
    _rag_quality_gate_service = RagQualityGateService(get_session_factory)
    worker_config = WorkerConfig.from_env()
    artifact_root = Path(
        worker_config.artifact_root
        or Path(os.getenv("JARVIS_WORKSPACE_ROOT") or ".") / ".local" / "artifacts"
    )
    artifact_store = LocalArtifactFileStore(
        artifact_root,
        max_bytes=worker_config.artifact_max_file_bytes,
        max_run_bytes=worker_config.artifact_max_run_bytes,
        max_workspace_bytes=worker_config.artifact_max_workspace_bytes,
        max_total_bytes=worker_config.artifact_max_total_bytes,
    )
    rag_asset_root = Path(
        os.getenv("JARVIS_RAG_ASSET_ROOT")
        or Path(os.getenv("JARVIS_WORKSPACE_ROOT") or ".") / ".local" / "rag-assets"
    )
    rag_worker_config = RagWorkerConfig.from_env()
    rag_asset_store = LocalRagAssetFileStore(
        rag_asset_root,
        max_bytes=rag_worker_config.asset_max_file_bytes,
        max_total_bytes=rag_worker_config.asset_max_total_bytes,
    )
    _artifact_service = ArtifactApplicationService(
        get_session_factory,
        file_store=artifact_store,
        workspace_file_reader=WorkspaceArtifactFileReader(max_bytes=1024 * 1024),
    )
    _rag_ingestion_command_service = RagIngestionCommandService(
        get_session_factory,
        artifact_file_store=artifact_store,
    )
    _rag_upload_service = RagUploadApplicationService(
        get_session_factory,
        artifact_file_store=artifact_store,
        ingestion_service=_rag_ingestion_command_service,
    )
    _rag_document_lifecycle_service = RagDocumentLifecycleService(
        get_session_factory,
        asset_file_store=rag_asset_store,
    )
    _storage_reconciliation_service = StorageReconciliationApplicationService(
        get_session_factory, artifact_file_store=artifact_store
    )


async def bootstrap_workspaces() -> None:
    """启动时幂等注册 JARVIS_ALLOWED_WORKSPACE_PATHS 中的所有路径。

    FastAPI lifespan 必须在 yield 前 await 此函数。
    单个无效路径记录 warning 并跳过；数据库不可用由调用方 fail closed。
    """
    import logging
    logger = logging.getLogger(__name__)

    if _workspace_service is None:
        logger.warning("WorkspaceService 未初始化，跳过 configured workspace bootstrap")
        return

    policy = WorkspacePolicy.from_env()
    if not policy.allowed_workspace_paths:
        return

    registered = await _workspace_service.register_all_configured(list(policy.allowed_workspace_paths))
    logger.info("已注册 %d 个配置工作区", len(registered))


def get_task_service() -> TaskApplicationService:
    return _task_service


def get_run_service() -> RunApplicationService:
    return _run_service


def get_event_service() -> EventApplicationService:
    return _event_service


def get_permission_service() -> PermissionApplicationService:
    return _permission_service


def get_conversation_service() -> ConversationApplicationService:
    return _conversation_service


def get_workspace_service() -> WorkspaceApplicationService:
    return _workspace_service


def get_audit_query_service() -> AuditQueryApplicationService:
    return _audit_query_service


def get_artifact_service() -> ArtifactApplicationService:
    return _artifact_service


def get_storage_reconciliation_service() -> StorageReconciliationApplicationService:
    return _storage_reconciliation_service


def get_memory_service() -> MemoryApplicationService:
    return _memory_service


def get_memory_candidate_service() -> MemoryCandidateApplicationService:
    return _memory_candidate_service


def get_knowledge_service() -> KnowledgeApplicationService:
    return _knowledge_service


def get_scheduled_task_service() -> ScheduledTaskApplicationService:
    return _scheduled_task_service


def get_scheduled_task_worker() -> ScheduledTaskWorker:
    return _scheduled_task_worker


def get_mcp_service() -> McpApplicationService:
    return _mcp_service


def get_rag_document_query_service() -> RagDocumentQueryService:
    return _rag_document_query_service


def get_rag_upload_service() -> RagUploadApplicationService:
    return _rag_upload_service


def get_rag_ingestion_command_service() -> RagIngestionCommandService:
    return _rag_ingestion_command_service


def get_rag_document_lifecycle_service() -> RagDocumentLifecycleService:
    return _rag_document_lifecycle_service


def get_rag_evaluation_feedback_service() -> RagEvaluationFeedbackService:
    return _rag_evaluation_feedback_service


def get_rag_evaluation_review_service() -> RagEvaluationReviewService:
    return _rag_evaluation_review_service


def get_rag_quality_gate_service() -> RagQualityGateService:
    return _rag_quality_gate_service
