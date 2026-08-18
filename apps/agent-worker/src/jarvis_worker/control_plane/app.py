"""Python Control Plane — FastAPI 应用。

Internal API 端点（仅供 Go Gateway 调用，不对外暴露）：
- POST /internal/tasks           — 创建任务
- GET  /internal/tasks           — 任务列表
- GET  /internal/tasks/{id}      — 任务详情
- GET  /internal/tasks/{id}/history — 任务历史（SSE 初始快照）
- POST /internal/runs/{id}/cancel  — 取消运行
- POST /internal/permissions/decide — 权限决定
- GET  /internal/health          — 健康检查
"""

import base64
import binascii
import logging
import sys
import time
from contextlib import asynccontextmanager
from dataclasses import asdict
from typing import Literal, Optional
from uuid import UUID, uuid4

from fastapi import FastAPI, Query, Request
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, Field

from jarvis_worker.agent.knowledge.service import CreateKnowledgeDocumentInput
from jarvis_worker.agent.mcp.service import CreateMcpServerInput
from jarvis_worker.agent.memory.candidate_service import (
    ResolveMemoryCandidateInput,
    UpdateMemoryCandidateInput,
)
from jarvis_worker.agent.memory.service import CreateMemoryInput, UpdateMemoryInput
from jarvis_worker.agent.rag.ingestion import RagIngestionError
from jarvis_worker.control_plane.dependencies import (
    get_artifact_service,
    get_audit_query_service,
    get_conversation_service,
    get_event_service,
    get_knowledge_service,
    get_mcp_service,
    get_memory_candidate_service,
    get_memory_service,
    get_permission_service,
    get_rag_document_lifecycle_service,
    get_rag_document_query_service,
    get_rag_evaluation_feedback_service,
    get_rag_evaluation_review_service,
    get_rag_ingestion_command_service,
    get_rag_quality_gate_service,
    get_rag_upload_service,
    get_run_service,
    get_scheduled_task_service,
    get_scheduled_task_worker,
    get_storage_reconciliation_service,
    get_task_service,
    get_workspace_service,
    init_services,
)
from jarvis_worker.control_plane.model_config import (
    build_model_config,
    test_model_connection,
)
from jarvis_worker.database.engine import check_connection, create_engine
from jarvis_worker.runtime.runs.service import DlqRetryEvidence
from jarvis_worker.runtime.schedules.service import CreateScheduledTaskInput
from jarvis_worker.runtime.tasks.service import CreateTaskInput
from jarvis_worker.shared.errors.application import AppError
from jarvis_worker.shared.observability import clear_log_context, set_log_context, setup_logging
from jarvis_worker.shared.observability.logging import normalize_trace_id

logger = logging.getLogger(__name__)


# ── Request/Response Models ──────────────────────────────────────

class CreateTaskRequest(BaseModel):
    user_goal: str = Field(..., min_length=1, max_length=10000)
    workspace_path: Optional[str] = None
    workspace_id: Optional[str] = None  # 优先于 workspace_path
    conversation_id: Optional[str] = None
    title: Optional[str] = Field(None, max_length=500)


class CancelRunRequest(BaseModel):
    reason: str = ""


class PauseRunRequest(BaseModel):
    reason: str = ""


class PermissionDecisionRequest(BaseModel):
    request_id: str
    decision: Literal[
        "allow_once",
        "allow_for_task",
        "always_allow_for_tool_and_path",
        "always_allow_for_workspace",
        "deny",
    ]
    note: str = ""


class CreateMcpServerRequest(BaseModel):
    slug: str = Field(..., min_length=1, max_length=64)
    name: str = Field(..., min_length=1, max_length=200)
    command: str = Field(..., min_length=1, max_length=2048)
    args: list[str] = Field(default_factory=list, max_length=50)
    env_keys: list[str] = Field(default_factory=list, max_length=30)


class UpdateMcpServerRequest(BaseModel):
    enabled: bool
    expected_version: int = Field(..., ge=1)


class UploadRagDocumentRequest(BaseModel):
    workspace_id: str
    permission_request_id: str
    filename: str = Field(..., min_length=1, max_length=500)
    # 50 MiB PDF 经 base64 编码约为 66.7 MiB，预留 JSON 编码余量。
    content_base64: str = Field(..., min_length=1, max_length=70_000_000)


class CreateRagUploadRequest(BaseModel):
    workspace_id: str
    filename: str = Field(..., min_length=1, max_length=500)
    size_bytes: int = Field(..., ge=1, le=50 * 1024 * 1024)
    content_sha256: str = Field(..., min_length=64, max_length=64)


class ResolveRagUploadRequest(BaseModel):
    decision: Literal["allow_once", "deny"]
    note: str = Field("", max_length=500)


class RestartRagDocumentRequest(BaseModel):
    workspace_id: str
    expected_version: int = Field(..., ge=1)


class UpdateRagDocumentRequest(BaseModel):
    workspace_id: str
    expected_version: int = Field(..., ge=1)
    enabled: bool


class CancelRagDocumentRequest(BaseModel):
    workspace_id: str
    expected_version: int = Field(..., ge=1)


class CreateRagDeleteRequest(BaseModel):
    workspace_id: str
    expected_version: int = Field(..., ge=1)


class ResolveRagDeleteRequest(BaseModel):
    decision: Literal["allow_once", "deny"]
    note: str = Field("", max_length=500)


class SubmitRagFeedbackRequest(BaseModel):
    message_id: str
    kind: Literal["helpful", "unhelpful", "citation_incorrect", "evidence_insufficient"]
    citation_chunk_id: str | None = None


class ResolveRagFeedbackRequest(BaseModel):
    status: Literal["reviewed", "dismissed"]


class TriageRagFeedbackRequest(BaseModel):
    failure_category: Literal[
        "candidate_miss", "reranker_miss", "context_omission", "context_truncated",
        "citation_mismatch", "answer_generation", "insufficient_evidence", "other",
    ]
    positive_chunk_ids: list[str] = Field(default_factory=list, max_length=100)
    hard_negative_chunk_ids: list[str] = Field(default_factory=list, max_length=100)


class ReviewRagTracePrivacyRequest(BaseModel):
    workspace_id: str
    decision: Literal["approved", "rejected"]


class ReviewRagTraceLabelRequest(BaseModel):
    workspace_id: str
    status: Literal["draft", "confirmed", "rejected"]
    positive_chunk_ids: list[str] = Field(..., min_length=1, max_length=100)
    hard_negative_chunk_ids: list[str] = Field(default_factory=list, max_length=100)
    notes: str = Field("", max_length=500)


class PromoteRagTraceRequest(BaseModel):
    workspace_id: str


class UpdateRagQualityIssueRequest(BaseModel):
    expected_version: int = Field(..., ge=1)
    owner: Literal["data_quality", "candidate_recall", "reranker", "context_assembly"]
    status: Literal["open", "in_progress", "resolved", "dismissed"]
    resolution_note: str = Field("", max_length=500)


class CreateAuditRetentionRequest(BaseModel):
    standard_days: int = Field(90, ge=30, le=3_650)
    extended_days: int = Field(365, ge=30, le=3_650)
    max_scan: int = Field(1_000, ge=1, le=10_000)
    max_candidates: int = Field(100, ge=1, le=1_000)


class ResolveAuditRetentionRequest(BaseModel):
    decision: Literal["allow_once", "deny"]
    note: str = Field("", max_length=500)


class DlqRetryEvidenceRequest(BaseModel):
    source: Literal["run_queue", "worker_command", "runtime_event"]
    record_id: str = Field(..., pattern=r"^[0-9]+-[0-9]+$")
    original_message_id: str = Field(..., min_length=1, max_length=64)
    error_code: str = Field(..., min_length=1, max_length=80)
    task_id: str
    run_id: str
    payload_sha256: str = Field("", max_length=64)


class DlqRetryDecisionRequest(BaseModel):
    decision: Literal["allow_once", "deny"]
    note: str = Field("", max_length=500)


class TerminalEventRepairRequest(BaseModel):
    run_id: str


class CreateMemoryRequest(BaseModel):
    scope_type: Literal["global", "workspace"]
    workspace_id: Optional[str] = None
    category: Literal["preference", "user_fact", "project_fact", "rule"]
    key: str = Field(..., min_length=1, max_length=128)
    content: str = Field(..., min_length=1, max_length=4000)
    importance: int = Field(50, ge=0, le=100)


class UpdateMemoryRequest(BaseModel):
    expected_version: int = Field(..., ge=1)
    content: Optional[str] = Field(None, min_length=1, max_length=4000)
    status: Optional[Literal["active", "disabled"]] = None
    importance: Optional[int] = Field(None, ge=0, le=100)


class UpdateMemoryCandidateRequest(BaseModel):
    expected_version: int = Field(..., ge=1)
    scope_type: Optional[Literal["global", "workspace"]] = None
    workspace_id: Optional[str] = None
    category: Optional[Literal["preference", "user_fact", "project_fact", "rule"]] = None
    suggested_key: Optional[str] = Field(None, min_length=1, max_length=128)
    content: Optional[str] = Field(None, min_length=1, max_length=4000)
    importance: Optional[int] = Field(None, ge=0, le=100)


class ResolveMemoryCandidateRequest(BaseModel):
    expected_version: int = Field(..., ge=1)
    note: str = Field("", max_length=500)


class ConnectKnowledgeVaultRequest(BaseModel):
    path: str = Field(..., min_length=1, max_length=4096)


class CreateKnowledgeDocumentRequest(BaseModel):
    title: str = Field(..., min_length=1, max_length=500)
    kind: Literal["report", "note", "source"]
    content: str = Field(..., min_length=1, max_length=524288)
    tags: list[str] = Field(default_factory=list, max_length=20)
    source_urls: list[str] = Field(default_factory=list, max_length=50)


class CreateScheduledTaskRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=200)
    user_goal: str = Field(..., min_length=1, max_length=10000)
    recurrence: Literal["daily", "weekly"]
    timezone: str = Field("Asia/Shanghai", min_length=1, max_length=100)
    hour: int = Field(..., ge=0, le=23)
    minute: int = Field(..., ge=0, le=59)
    weekday: Optional[int] = Field(None, ge=0, le=6)
    workspace_id: Optional[str] = None
    task_kind: Literal["knowledge_report", "source_report"] = "knowledge_report"
    source_query: Optional[str] = Field(None, max_length=300)
    source_max_results: int = Field(5, ge=1, le=10)


class UpdateScheduledTaskRequest(BaseModel):
    expected_version: int = Field(..., ge=1)
    status: Literal["active", "paused"]


class ErrorResponse(BaseModel):
    code: str
    message: str
    category: str
    recoverable: bool
    details: dict = {}


# ── FastAPI App ──────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    """应用生命周期：启动时初始化日志、数据库、服务和 Outbox Publisher。"""
    # 日志必须最早初始化
    setup_logging(service_name="control-plane", log_basename="control-plane.log")

    # 加载 .env，确保与 Worker 使用同一套模型配置源
    from jarvis_worker.control_plane.model_config import ensure_config_loaded
    ensure_config_loaded()

    create_engine()
    if not await check_connection():
        raise RuntimeError("PostgreSQL 不可用，Control Plane 拒绝启动")
    init_services()

    # 在 yield 前完成 configured Workspace bootstrap（确保首个 GET /workspaces 可见）
    from jarvis_worker.control_plane.dependencies import bootstrap_workspaces
    await bootstrap_workspaces()
    logger.info("Control Plane 已启动（configured Workspace bootstrap 完成）")

    # 启动 Outbox Publisher + Reconciliation（async Redis）
    import os as _os

    from jarvis_worker.database.outbox.publisher import OutboxPublisher
    from jarvis_worker.database.outbox.reconciliation import ReconciliationJob
    from jarvis_worker.runtime_bus import create_async_redis_client
    from jarvis_worker.shared.config.redis import (
        redis_db_from_env,
        redis_password_from_env,
    )
    redis_addr = _os.getenv("JARVIS_REDIS_ADDR", "127.0.0.1:6379")
    redis_client = create_async_redis_client(
        redis_addr,
        password=redis_password_from_env(),
        db=redis_db_from_env(),
    )
    publisher = OutboxPublisher(redis_client)
    from jarvis_worker.control_plane.dependencies import (
        get_permission_service,
        get_run_service,
    )
    reconciliation = ReconciliationJob(
        redis_client=redis_client,
        run_service=get_run_service(),
        permission_service=get_permission_service(),
    )
    scheduled_worker = get_scheduled_task_worker()
    await publisher.start()
    await reconciliation.start()
    await scheduled_worker.start()
    app.state.redis_client = redis_client
    app.state.outbox_publisher = publisher
    logger.info("Outbox Publisher + Reconciliation 已启动（Redis 断线时自动重试）")

    yield

    if publisher:
        await publisher.stop()
    if reconciliation:
        await reconciliation.stop()
    if scheduled_worker:
        await scheduled_worker.stop()
    if redis_client:
        await redis_client.aclose()
    from jarvis_worker.database.engine import dispose_engine
    await dispose_engine()
    logger.info("Control Plane 已关闭")


app = FastAPI(
    title="Jarvis Control Plane",
    version="1.0.0",
    lifespan=lifespan,
)


@app.middleware("http")
async def attach_log_trace_context(request: Request, call_next):
    """绑定端到端 trace id 与单次 HTTP request id。"""
    trace_id = normalize_trace_id(request.headers.get("X-Trace-ID"))
    request_id = normalize_trace_id(request.headers.get("X-Request-ID")) or str(uuid4())
    if trace_id:
        try:
            trace_id = str(UUID(trace_id))
        except ValueError:
            trace_id = None
    if not trace_id:
        try:
            trace_id = str(UUID(request_id))
        except ValueError:
            trace_id = str(uuid4())
    request.state.trace_id = trace_id
    request.state.request_id = request_id
    set_log_context(trace_id=trace_id, request_id=request_id)
    started_at = time.monotonic()
    try:
        response = await call_next(request)
        response.headers["X-Trace-ID"] = trace_id
        response.headers["X-Request-ID"] = request_id
        duration_ms = int((time.monotonic() - started_at) * 1000)
        # Control Plane 是业务 owner：写请求在默认 INFO 中保留，便于看到
        # 后端执行入口；只读 GET 成功请求降到 DEBUG，避免健康检查和刷新噪音。
        log_method = logger.info
        if request.method == "GET" and response.status_code < 400:
            log_method = logger.debug
        if response.status_code >= 500:
            log_method = logger.error
        elif response.status_code >= 400:
            log_method = logger.info
        log_method(
            "HTTP: method=%s path=%s status=%d duration_ms=%d",
            request.method,
            request.url.path,
            response.status_code,
            duration_ms,
        )
        return response
    finally:
        clear_log_context()

# JSON 上传请求以 base64 携带最多 50 MiB 的 PDF，预留编码及字段开销。
app.state.max_body_size = 72 * 1024 * 1024


# ── 错误处理 ─────────────────────────────────────────────────────

@app.exception_handler(AppError)
async def app_error_handler(request: Request, exc: AppError):
    """结构化 AppError 映射为 HTTP 响应。"""
    status_map = {
        "validation": 400,
        "not_found": 404,
        "permission": 403,
        "storage": 503,
        "runtime": 500,
    }
    status_code = (
        409
        if exc.code in {
            "MEMORY_VERSION_CONFLICT",
            "SCHEDULE_VERSION_CONFLICT",
            "RAG_RESTART_CONFLICT",
        }
        else status_map.get(exc.category, 500)
    )
    return JSONResponse(
        status_code=status_code,
        content={
            "ok": False,
            "error": {
                "code": exc.code,
                "message": exc.message,
                "category": exc.category,
                "recoverable": exc.recoverable,
                "details": exc.details,
            },
        },
    )


@app.exception_handler(Exception)
async def generic_error_handler(request: Request, exc: Exception):
    """未分类异常 → 500。不泄漏内部细节。"""
    logger.error("未处理异常: %s", exc, exc_info=True)
    return JSONResponse(
        status_code=500,
        content={
            "ok": False,
            "error": {
                "code": "INTERNAL_ERROR",
                "message": "内部错误",
                "category": "internal",
                "recoverable": False,
                "details": {},
            },
        },
    )


# ── Internal API Endpoints ───────────────────────────────────────

@app.get("/internal/health")
async def health_check():
    """健康检查 — 返回统一 ApiResult shape。"""
    db_ok = await check_connection()
    redis_ok = False
    redis_client = getattr(app.state, "redis_client", None)
    if redis_client is not None:
        try:
            redis_ok = bool(await redis_client.ping())
        except Exception:
            redis_ok = False
    publisher = getattr(app.state, "outbox_publisher", None)
    publisher_ok = bool(publisher and publisher.ready)
    healthy = db_ok and redis_ok and publisher_ok
    return {
        "ok": True,
        "data": {
            "status": "ok" if healthy else "degraded",
            "database": "connected" if db_ok else "disconnected",
            "redis": "connected" if redis_ok else "disconnected",
            "outbox_publisher": "ready" if publisher_ok else "reconnecting",
        },
    }


@app.get("/internal/artifacts/{artifact_id}")
async def get_artifact(artifact_id: str):
    """返回可安全展示的 Artifact；不暴露本地文件路径。"""
    try:
        aid = UUID(artifact_id)
    except ValueError:
        raise AppError(
            code="VALIDATION_ERROR",
            message="无效的 artifact_id",
            category="validation",
        ) from None
    artifact, content = await get_artifact_service().get_with_content(aid)
    return {
        "ok": True,
        "data": {
            "artifact": {
                "id": str(artifact.id),
                "task_id": str(artifact.task_id),
                "run_id": str(artifact.run_id),
                "kind": artifact.kind,
                "title": artifact.title,
                "purpose": artifact.purpose,
                "producer": {
                    "type": artifact.producer_type,
                    **(
                        {"tool_call_id": str(artifact.source_tool_call_id)}
                        if artifact.source_tool_call_id is not None
                        else {}
                    ),
                },
                "content": content,
                "file_size_bytes": artifact.file_size_bytes,
                "mime_type": artifact.mime_type,
                "content_hash": artifact.content_hash,
                "metadata": artifact.metadata,
                "created_at": artifact.created_at.isoformat(),
            }
        },
    }


@app.get("/internal/runtime/storage-reconciliation")
async def inspect_storage_reconciliation(
    limit: int = Query(default=50, ge=1, le=100),
):
    """有限、只读地核对最近 Run 的 PostgreSQL 业务状态与 Artifact 文件。"""
    result = await get_storage_reconciliation_service().inspect(limit=limit)
    return {"ok": True, "data": result}


@app.post("/internal/tasks")
async def create_task(req: CreateTaskRequest, request: Request):
    """创建任务（权威入口，单一 Owner）。

    Go Gateway 校验 DTO 后调用此端点。
    在同一 PostgreSQL 事务中写入所有数据，提交后才返回完整权威 DTO。
    Outbox Publisher 异步发布 Redis RunJob。

    返回的 ID 是权威 ID，Go 不得重新生成或用 PrepareRun 覆盖。
    """
    task_svc = get_task_service()
    try:
        conv_id = UUID(req.conversation_id) if req.conversation_id else None
    except ValueError:
        raise AppError(code="VALIDATION_ERROR", message="无效的 conversation_id", category="validation")

    # workspace_id 优先
    ws_id: UUID | None = None
    if req.workspace_id:
        try:
            ws_id = UUID(req.workspace_id)
        except ValueError:
            raise AppError(code="VALIDATION_ERROR", message="无效的 workspace_id", category="validation")

    input_data = CreateTaskInput(
        user_goal=req.user_goal,
        workspace_path=req.workspace_path,
        workspace_id=ws_id,
        conversation_id=conv_id,
        title=req.title,
        trace_id=UUID(request.state.trace_id),
    )
    result = await task_svc.create_task(input_data)

    # 返回完整权威 DTO — Go 必须使用这些 ID，不得重新生成
    return {
        "ok": True,
        "data": {
            "task": {
                "id": str(result.task.id),
                "conversation_id": str(result.task.conversation_id),
                "title": result.task.title,
                "user_goal": result.task.user_goal,
                "status": result.task.status.value,
                "workspace_path": result.task.workspace_path,
                "workspace_id": str(result.task.workspace_id) if result.task.workspace_id else None,
                "active_run_id": str(result.run.id),
                "created_at": result.task.created_at.isoformat(),
                "updated_at": result.task.updated_at.isoformat(),
            },
            "run": {
                "id": str(result.run.id),
                "task_id": str(result.task.id),
                "agent_id": result.run.agent_id,
                "mode": result.run.mode,
                "status": result.run.status.value,
                "version": result.run.version,
                "created_at": result.run.created_at.isoformat(),
                "updated_at": result.run.updated_at.isoformat(),
            },
            "conversation": {
                "id": str(result.conversation.id),
                "title": result.conversation.title,
                "created_at": result.conversation.created_at.isoformat(),
                "updated_at": result.conversation.updated_at.isoformat(),
            },
            "message": {
                "id": str(result.message.id),
                "role": result.message.role,
                "content": result.message.content,
                "conversation_id": str(result.message.conversation_id),
                "task_id": str(result.task.id),
                "created_at": result.message.created_at.isoformat(),
            },
            "initial_event": {
                "id": str(result.initial_event.id),
                "event_id": str(result.initial_event.event_id),
                "type": result.initial_event.type,
                "run_id": str(result.run.id),
                "event_sequence": result.initial_event.event_sequence,
                "payload": result.initial_event.payload,
                "created_at": result.initial_event.created_at.isoformat(),
            },
            "trace_id": str(result.trace_id),
        },
    }


@app.get("/internal/tasks")
async def list_tasks(limit: int = Query(50, le=100), offset: int = Query(0, ge=0)):
    """任务列表。"""
    run_svc = get_run_service()
    tasks = await run_svc.list_tasks(limit=limit, offset=offset)
    return {
        "ok": True,
        "data": {
            "tasks": [
                {
                    "id": str(t.id),
                    "conversation_id": str(t.conversation_id),
                    "title": t.title,
                    "user_goal": t.user_goal,
                    "status": t.status.value,
                    "workspace_path": t.workspace_path,
                    "workspace_id": str(t.workspace_id) if t.workspace_id else None,
                    "active_run_id": str(t.active_run_id) if t.active_run_id else None,
                    "created_at": t.created_at.isoformat(),
                    "updated_at": t.updated_at.isoformat(),
                }
                for t in tasks
            ],
        },
    }


@app.get("/internal/tasks/{task_id}")
async def get_task(task_id: str):
    """任务详情。"""
    run_svc = get_run_service()
    try:
        tid = UUID(task_id)
    except ValueError:
        raise AppError(code="VALIDATION_ERROR", message="无效的 task_id", category="validation")

    task = await run_svc.get_task(tid)
    if task is None:
        raise AppError(code="NOT_FOUND", message=f"任务不存在: {task_id}", category="not_found")

    runs = await run_svc.get_runs_by_task(tid)
    return {
        "ok": True,
        "data": {
            "task": {
                "id": str(task.id),
                "conversation_id": str(task.conversation_id),
                "title": task.title,
                "user_goal": task.user_goal,
                "status": task.status.value,
                "workspace_path": task.workspace_path,
                "workspace_id": str(task.workspace_id) if task.workspace_id else None,
                "active_run_id": str(task.active_run_id) if task.active_run_id else None,
                "created_at": task.created_at.isoformat(),
                "updated_at": task.updated_at.isoformat(),
            },
            "runs": [
                {
                    "id": str(r.id),
                    "task_id": str(r.task_id),
                    "status": r.status.value,
                    "version": r.version,
                    "created_at": r.created_at.isoformat(),
                    "updated_at": r.updated_at.isoformat(),
                }
                for r in runs
            ],
        },
    }


@app.get("/internal/tasks/{task_id}/history")
async def get_task_history(task_id: str):
    """任务历史（SSE 初始快照用）。

    返回 task + runs + events + messages。
    """
    run_svc = get_run_service()
    event_svc = get_event_service()
    try:
        tid = UUID(task_id)
    except ValueError:
        raise AppError(code="VALIDATION_ERROR", message="无效的 task_id", category="validation")

    task = await run_svc.get_task(tid)
    if task is None:
        raise AppError(code="NOT_FOUND", message=f"任务不存在: {task_id}", category="not_found")

    runs = await run_svc.get_runs_by_task(tid)
    # 收集所有 run 的事件
    all_events = []
    for r in runs:
        events = await event_svc.get_events_by_run(r.id)
        all_events.extend(events)
    messages = await event_svc.get_messages_by_task(tid)

    return {
        "ok": True,
        "data": {
            "task": {
                "id": str(task.id), "conversation_id": str(task.conversation_id),
                "title": task.title, "user_goal": task.user_goal,
                "status": task.status.value,
                "workspace_path": task.workspace_path,
                "workspace_id": str(task.workspace_id) if task.workspace_id else None,
                "active_run_id": str(task.active_run_id) if task.active_run_id else None,
                "created_at": task.created_at.isoformat(),
                "updated_at": task.updated_at.isoformat(),
            },
            "runs": [
                {"id": str(r.id), "status": r.status.value, "version": r.version}
                for r in runs
            ],
            "events": [
                {
                    "id": str(e.id),
                    "event_id": str(e.event_id),
                    "type": e.type,
                    "run_id": str(e.run_id) if e.run_id else None,
                    "task_id": str(e.task_id) if e.task_id else None,
                    "step_id": str(e.step_id) if e.step_id else None,
                    "sequence": e.event_sequence,
                    "payload": e.payload,
                    "timestamp": e.created_at.isoformat(),
                    "created_at": e.created_at.isoformat(),
                }
                for e in sorted(all_events, key=lambda x: x.event_sequence)
            ],
            "messages": [_message_to_dict(m) for m in messages],
        },
    }


@app.get("/internal/runs/{run_id}/history")
async def get_run_history(run_id: str):
    """返回单个 Run 的 PostgreSQL 历史快照，供 Gateway SSE 恢复。"""
    run_svc = get_run_service()
    event_svc = get_event_service()
    try:
        rid = UUID(run_id)
    except ValueError:
        raise AppError(code="VALIDATION_ERROR", message="无效的 run_id", category="validation")

    run = await run_svc.get_run(rid)
    if run is None:
        raise AppError(code="NOT_FOUND", message=f"运行不存在: {run_id}", category="not_found")
    task = await run_svc.get_task(run.task_id)
    if task is None:
        raise AppError(code="NOT_FOUND", message=f"任务不存在: {run.task_id}", category="not_found")
    events = await event_svc.get_events_by_run(rid)
    messages = await event_svc.get_messages_by_task(run.task_id)

    return {
        "ok": True,
        "data": {
            "run": {
                "id": str(run.id), "task_id": str(run.task_id),
                "status": run.status.value, "version": run.version,
                "created_at": run.created_at.isoformat(),
                "updated_at": run.updated_at.isoformat(),
            },
            "task": {
                "id": str(task.id), "conversation_id": str(task.conversation_id),
                "title": task.title, "user_goal": task.user_goal,
                "workspace_path": task.workspace_path,
                "workspace_id": str(task.workspace_id) if task.workspace_id else None,
                "status": task.status.value,
                "active_run_id": str(task.active_run_id) if task.active_run_id else None,
                "created_at": task.created_at.isoformat(),
                "updated_at": task.updated_at.isoformat(),
            },
            "events": [_event_to_dict(e) for e in events],
            "messages": [_message_to_dict(m) for m in messages],
        },
    }


def _event_to_dict(event):
    return {
        "id": str(event.event_id),
        "event_id": str(event.event_id),
        "type": event.type,
        "task_id": str(event.task_id) if event.task_id else None,
        "run_id": str(event.run_id) if event.run_id else None,
        "step_id": str(event.step_id) if event.step_id else None,
        "sequence": event.event_sequence,
        "payload": event.payload,
        "timestamp": event.created_at.isoformat(),
        "created_at": event.created_at.isoformat(),
    }


def _message_to_dict(message):
    return {
        "id": str(message.id),
        "conversation_id": str(message.conversation_id),
        "task_id": str(message.task_id) if message.task_id else None,
        "run_id": str(message.run_id) if message.run_id else None,
        "role": message.role,
        "content": message.content,
        "created_at": message.created_at.isoformat(),
    }


@app.post("/internal/runs/{run_id}/cancel")
async def cancel_run(run_id: str, req: CancelRunRequest = CancelRunRequest()):
    """取消运行。

    语义：
    - PostgreSQL 中更新 AgentRun → cancel_requested
    - 写入 OutboxEvent (event_type=run.cancel.requested)
    - Outbox Publisher 异步发布到 Redis worker-command stream
    """
    run_svc = get_run_service()
    try:
        rid = UUID(run_id)
    except ValueError:
        raise AppError(code="VALIDATION_ERROR", message="无效的 run_id", category="validation")

    run = await run_svc.cancel_run(rid, req.reason)
    return {
        "ok": True,
        "data": {
            "run_id": str(run.id),
            "status": run.status.value,
            "version": run.version,
        },
    }


@app.post("/internal/runs/{run_id}/pause")
async def pause_run(run_id: str, req: PauseRunRequest = PauseRunRequest()):
    """持久化 pause_requested，并通过 Outbox 发布 run.pause。"""
    try:
        rid = UUID(run_id)
    except ValueError:
        raise AppError(
            code="VALIDATION_ERROR", message="无效的 run_id", category="validation"
        )
    run = await get_run_service().pause_run(rid, req.reason)
    return {"ok": True, "data": {
        "run_id": str(run.id), "status": run.status.value, "version": run.version,
    }}


@app.post("/internal/runs/{run_id}/resume")
async def resume_run(run_id: str):
    """从 PostgreSQL 安全 checkpoint 请求恢复 paused Run。"""
    try:
        rid = UUID(run_id)
    except ValueError:
        raise AppError(
            code="VALIDATION_ERROR", message="无效的 run_id", category="validation"
        )
    run = await get_run_service().resume_run(rid)
    return {"ok": True, "data": {
        "run_id": str(run.id), "status": run.status.value, "version": run.version,
    }}


@app.post("/internal/runs/{run_id}/steps/{step_id}/retry")
async def retry_failed_step(run_id: str, step_id: str):
    """从可恢复模型失败步骤创建新的 replacement Run。"""
    try:
        rid, sid = UUID(run_id), UUID(step_id)
    except ValueError:
        raise AppError(
            code="VALIDATION_ERROR", message="无效的 run_id 或 step_id",
            category="validation",
        )
    run = await get_run_service().retry_failed_step(rid, sid)
    return {"ok": True, "data": {
        "run_id": str(run.id), "task_id": str(run.task_id),
        "agent_id": run.agent_id, "mode": run.mode,
        "status": run.status.value, "version": run.version,
        "created_at": run.created_at.isoformat(),
        "updated_at": run.updated_at.isoformat(),
    }}


def _parse_dlq_retry_evidence(req: DlqRetryEvidenceRequest) -> DlqRetryEvidence:
    try:
        task_id = UUID(req.task_id)
        run_id = UUID(req.run_id)
    except ValueError:
        raise AppError(
            code="VALIDATION_ERROR",
            message="无效的 task_id 或 run_id",
            category="validation",
        )
    return DlqRetryEvidence(
        source=req.source,
        record_id=req.record_id,
        original_message_id=req.original_message_id,
        error_code=req.error_code,
        task_id=task_id,
        run_id=run_id,
        payload_sha256=req.payload_sha256,
    )


def _permission_request_to_dict(request) -> dict:
    return {
        "id": str(request.id),
        "task_id": str(request.task_id),
        "run_id": str(request.run_id),
        "step_id": str(request.step_id) if request.step_id else None,
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


@app.post("/internal/runtime/dlq-retry/inspect")
async def inspect_dlq_retry(req: DlqRetryEvidenceRequest):
    """核对 DLQ 诊断记录是否可基于 PostgreSQL 权威状态重试。"""
    inspection = await get_run_service().inspect_dlq_retry(
        _parse_dlq_retry_evidence(req)
    )
    return {"ok": True, "data": asdict(inspection)}


@app.post("/internal/runtime/dlq-retry/requests")
async def create_dlq_retry_request(req: DlqRetryEvidenceRequest):
    """创建持久化 L3 单次权限请求；不执行重试。"""
    request = await get_run_service().create_dlq_retry_request(
        _parse_dlq_retry_evidence(req)
    )
    return {"ok": True, "data": {"request": _permission_request_to_dict(request)}}


@app.post("/internal/runtime/dlq-retry/requests/{request_id}/resolve")
async def resolve_dlq_retry_request(request_id: str, req: DlqRetryDecisionRequest):
    """拒绝或单次批准受控重试；批准后创建新 Run。"""
    try:
        parsed_request_id = UUID(request_id)
    except ValueError:
        raise AppError(
            code="VALIDATION_ERROR", message="无效的 request_id", category="validation"
        )
    resolution = await get_run_service().resolve_dlq_retry_request(
        parsed_request_id, req.decision, req.note
    )
    return {
        "ok": True,
        "data": {
            "request": _permission_request_to_dict(resolution.request),
            "previous_run_id": str(resolution.previous_run_id),
            "new_run": (
                {
                    "id": str(resolution.new_run.id),
                    "task_id": str(resolution.new_run.task_id),
                    "agent_id": resolution.new_run.agent_id,
                    "mode": resolution.new_run.mode,
                    "status": resolution.new_run.status.value,
                    "version": resolution.new_run.version,
                    "created_at": resolution.new_run.created_at.isoformat(),
                    "updated_at": resolution.new_run.updated_at.isoformat(),
                }
                if resolution.new_run is not None else None
            ),
        },
    }


def _parse_repair_run_id(run_id: str) -> UUID:
    try:
        return UUID(run_id)
    except ValueError:
        raise AppError(
            code="VALIDATION_ERROR", message="无效的 run_id", category="validation"
        )


@app.post("/internal/runtime/storage-reconciliation/repairs/inspect")
async def inspect_terminal_event_repair(req: TerminalEventRepairRequest):
    inspection = await get_run_service().inspect_terminal_event_repair(
        _parse_repair_run_id(req.run_id)
    )
    return {"ok": True, "data": asdict(inspection)}


@app.post("/internal/runtime/storage-reconciliation/repairs/requests")
async def create_terminal_event_repair_request(req: TerminalEventRepairRequest):
    request = await get_run_service().create_terminal_event_repair_request(
        _parse_repair_run_id(req.run_id)
    )
    return {"ok": True, "data": {"request": _permission_request_to_dict(request)}}


@app.post("/internal/runtime/storage-reconciliation/repairs/requests/{request_id}/resolve")
async def resolve_terminal_event_repair_request(
    request_id: str, req: DlqRetryDecisionRequest
):
    try:
        parsed_request_id = UUID(request_id)
    except ValueError:
        raise AppError(
            code="VALIDATION_ERROR", message="无效的 request_id", category="validation"
        )
    resolution = await get_run_service().resolve_terminal_event_repair_request(
        parsed_request_id, req.decision, req.note
    )
    return {
        "ok": True,
        "data": {
            "request": _permission_request_to_dict(resolution.request),
            "repaired_event_id": (
                str(resolution.repaired_event_id)
                if resolution.repaired_event_id else None
            ),
            "repaired_event_type": resolution.repaired_event_type,
        },
    }


# ── Workspace API ─────────────────────────────────────────────


def _workspace_to_dict(ws) -> dict:
    return {
        "id": str(ws.id),
        "name": ws.name,
        "root_path": ws.root_path,
        "canonical_path": ws.canonical_path,
        "status": ws.status.value,
        "source": ws.source.value,
        "created_at": ws.created_at.isoformat(),
        "updated_at": ws.updated_at.isoformat(),
        "revoked_at": ws.revoked_at.isoformat() if ws.revoked_at else None,
    }


@app.get("/internal/workspaces")
async def list_workspaces(include_revoked: bool = Query(False)):
    """列出已注册的 Workspace。"""
    ws_svc = get_workspace_service()
    workspaces = await ws_svc.list_workspaces(include_revoked=include_revoked)
    return {
        "ok": True,
        "data": {
            "workspaces": [_workspace_to_dict(ws) for ws in workspaces],
        },
    }


@app.post("/internal/workspaces/pick")
async def pick_workspace():
    """弹出系统目录选择器并注册 Workspace。

    Returns:
        {"workspace": WorkspaceDTO | null, "cancelled": bool}
    """
    ws_svc = get_workspace_service()
    result = await ws_svc.pick_workspace()
    return {
        "ok": True,
        "data": {
            "workspace": _workspace_to_dict(result.workspace) if result.workspace else None,
            "cancelled": result.cancelled,
        },
    }


@app.delete("/internal/workspaces/{workspace_id}")
async def revoke_workspace(workspace_id: str):
    """撤销 Workspace（不物理删除）。"""
    ws_svc = get_workspace_service()
    try:
        ws_id = UUID(workspace_id)
    except ValueError:
        raise AppError(code="VALIDATION_ERROR", message="无效的 workspace_id", category="validation")

    workspace = await ws_svc.revoke_workspace(ws_id)
    return {
        "ok": True,
        "data": {
            "workspace": _workspace_to_dict(workspace),
        },
    }


# ── RAG Document Library API ─────────────────────────────────

def _rag_job_to_dict(job) -> dict | None:
    if job is None:
        return None
    return {
        "id": str(job.id),
        "status": job.status.value,
        "attempts": job.attempts,
        "max_attempts": job.max_attempts,
        "embedding_attempts": job.embedding_attempts,
        "embedding_max_attempts": job.embedding_max_attempts,
        "progress": {
            "active_executor": job.progress.active_executor,
            "page_count": job.progress.page_count,
            "native_extraction_done": job.progress.native_extraction_done,
            "visual_pages_total": job.progress.visual_pages_total,
            "visual_pages_completed": job.progress.visual_pages_completed,
            "visual_route_counts": job.progress.visual_route_counts,
            "chunks_total": job.progress.chunks_total,
            "embedding_total": job.progress.embedding_total,
            "embedding_completed": job.progress.embedding_completed,
        },
        "error_code": job.error_code,
        "next_retry_at": job.next_retry_at.isoformat() if job.next_retry_at else None,
        "started_at": job.started_at.isoformat() if job.started_at else None,
        "completed_at": job.completed_at.isoformat() if job.completed_at else None,
        "failed_at": job.failed_at.isoformat() if job.failed_at else None,
        "created_at": job.created_at.isoformat(),
        "updated_at": job.updated_at.isoformat(),
    }


def _rag_document_item_to_dict(item) -> dict:
    document = item.document
    return {
        "id": str(document.id),
        "workspace_id": str(document.workspace_id),
        "source_artifact_id": str(document.source_artifact_id),
        "title": document.title,
        "mime_type": document.mime_type,
        "status": document.status.value,
        "ingestion_policy_version": document.ingestion_policy_version,
        "parser_version": document.parser_version,
        "chunker_version": document.chunker_version,
        "embedding_provider": document.embedding_provider,
        "embedding_model": document.embedding_model,
        "embedding_dimensions": document.embedding_dimensions,
        "chunk_count": document.chunk_count,
        "indexed_at": document.indexed_at.isoformat() if document.indexed_at else None,
        "version": document.version,
        "created_at": document.created_at.isoformat(),
        "updated_at": document.updated_at.isoformat(),
        "latest_job": _rag_job_to_dict(item.latest_job),
        "index_state": item.index_freshness.state,
        "index_stale_reasons": list(item.index_freshness.stale_reasons),
        "index_target": item.index_freshness.target.as_dict(),
    }


def _rag_app_error(exc: RagIngestionError) -> AppError:
    category = (
        "not_found"
        if exc.code in {"RAG_DOCUMENT_NOT_FOUND", "RAG_JOB_NOT_FOUND"}
        else "conflict"
        if exc.code in {
            "RAG_DOCUMENT_VERSION_CONFLICT",
            "RAG_DOCUMENT_BUSY",
            "RAG_RESTART_CONFLICT",
        }
        else "validation"
    )
    return AppError(
        code=exc.code,
        message=str(exc),
        category=category,
        recoverable=exc.recoverable,
    )


@app.get("/internal/rag/documents")
async def list_rag_documents(
    workspace_id: str,
    include_disabled: bool = Query(False),
    limit: int = Query(100, ge=1, le=100),
):
    try:
        parsed_workspace_id = UUID(workspace_id)
    except ValueError:
        raise AppError(
            code="VALIDATION_ERROR",
            message="无效的 workspace_id",
            category="validation",
        )
    items = await get_rag_document_query_service().list_documents(
        workspace_id=parsed_workspace_id,
        include_disabled=include_disabled,
        limit=limit,
    )
    return {
        "ok": True,
        "data": {"documents": [_rag_document_item_to_dict(item) for item in items]},
    }


@app.post("/internal/rag/documents/upload")
async def upload_rag_document(req: UploadRagDocumentRequest):
    try:
        workspace_id = UUID(req.workspace_id)
        permission_request_id = UUID(req.permission_request_id)
    except ValueError:
        raise AppError(
            code="VALIDATION_ERROR",
            message="无效的 workspace_id 或 permission_request_id",
            category="validation",
        )
    try:
        content = base64.b64decode(req.content_base64, validate=True)
    except (binascii.Error, ValueError):
        raise AppError(
            code="RAG_UPLOAD_ENCODING_INVALID",
            message="上传文件编码无效",
            category="validation",
        ) from None
    try:
        result = await get_rag_upload_service().upload_pdf(
            workspace_id=workspace_id,
            filename=req.filename,
            content=content,
            permission_request_id=permission_request_id,
        )
    except RagIngestionError as exc:
        raise _rag_app_error(exc) from None
    return {
        "ok": True,
        "data": {
            "artifact_id": str(result.artifact_id),
            "document_id": str(result.enqueue.document_id),
            "job_id": str(result.enqueue.job_id),
            "status": result.enqueue.status.value,
            "uploaded": result.uploaded,
            "created": result.enqueue.created,
        },
    }


@app.post("/internal/rag/upload-requests")
async def create_rag_upload_request(req: CreateRagUploadRequest):
    try:
        workspace_id = UUID(req.workspace_id)
    except ValueError:
        raise AppError("VALIDATION_ERROR", "无效的 workspace_id", "validation") from None
    request = await get_rag_upload_service().create_upload_request(
        workspace_id=workspace_id,
        filename=req.filename,
        size_bytes=req.size_bytes,
        content_sha256=req.content_sha256,
    )
    return {"ok": True, "data": _permission_request_to_dict(request)}


@app.post("/internal/rag/upload-requests/{request_id}/resolve")
async def resolve_rag_upload_request(request_id: str, req: ResolveRagUploadRequest):
    try:
        parsed_request_id = UUID(request_id)
    except ValueError:
        raise AppError("VALIDATION_ERROR", "无效的 request_id", "validation") from None
    request = await get_rag_upload_service().resolve_upload_request(
        parsed_request_id, req.decision, req.note
    )
    return {"ok": True, "data": _permission_request_to_dict(request)}


@app.post("/internal/rag/documents/{document_id}/restart")
async def restart_rag_document(document_id: str, req: RestartRagDocumentRequest):
    try:
        parsed_document_id = UUID(document_id)
        parsed_workspace_id = UUID(req.workspace_id)
    except ValueError:
        raise AppError(
            code="VALIDATION_ERROR",
            message="无效的 document_id 或 workspace_id",
            category="validation",
        ) from None
    try:
        result = await get_rag_ingestion_command_service().restart_document(
            workspace_id=parsed_workspace_id,
            document_id=parsed_document_id,
            expected_version=req.expected_version,
        )
    except RagIngestionError as exc:
        category = (
            "not_found"
            if exc.code in {"RAG_DOCUMENT_NOT_FOUND", "RAG_JOB_NOT_FOUND"}
            else "validation"
        )
        raise AppError(
            code=exc.code,
            message=str(exc),
            category=category,
            recoverable=exc.recoverable,
        ) from None
    return {
        "ok": True,
        "data": {
            "document_id": str(result.document_id),
            "job_id": str(result.job_id),
            "status": result.status.value,
        },
    }


@app.patch("/internal/rag/documents/{document_id}")
async def update_rag_document(document_id: str, req: UpdateRagDocumentRequest):
    try:
        parsed_document_id = UUID(document_id)
        parsed_workspace_id = UUID(req.workspace_id)
    except ValueError:
        raise AppError(
            code="VALIDATION_ERROR",
            message="无效的 document_id 或 workspace_id",
            category="validation",
        ) from None
    try:
        result = await get_rag_ingestion_command_service().set_document_enabled(
            workspace_id=parsed_workspace_id,
            document_id=parsed_document_id,
            expected_version=req.expected_version,
            enabled=req.enabled,
        )
    except RagIngestionError as exc:
        raise _rag_app_error(exc) from None
    return {
        "ok": True,
        "data": {
            "document_id": str(result.document_id),
            "status": result.status.value,
            "version": result.version,
        },
    }


@app.post("/internal/rag/documents/{document_id}/cancel")
async def cancel_rag_document(document_id: str, req: CancelRagDocumentRequest):
    try:
        parsed_document_id = UUID(document_id)
        parsed_workspace_id = UUID(req.workspace_id)
    except ValueError:
        raise AppError(
            code="VALIDATION_ERROR",
            message="无效的 document_id 或 workspace_id",
            category="validation",
        ) from None
    try:
        result = await get_rag_ingestion_command_service().cancel_document(
            workspace_id=parsed_workspace_id,
            document_id=parsed_document_id,
            expected_version=req.expected_version,
        )
    except RagIngestionError as exc:
        raise _rag_app_error(exc) from None
    return {
        "ok": True,
        "data": {
            "document_id": str(result.document_id),
            "status": result.status.value,
            "version": result.version,
            "job_id": str(result.job_id),
            "job_status": result.job_status.value if result.job_status else None,
        },
    }


@app.post("/internal/rag/documents/{document_id}/delete-requests")
async def create_rag_delete_request(document_id: str, req: CreateRagDeleteRequest):
    try:
        parsed_document_id = UUID(document_id)
        parsed_workspace_id = UUID(req.workspace_id)
    except ValueError:
        raise AppError(
            code="VALIDATION_ERROR",
            message="无效的 document_id 或 workspace_id",
            category="validation",
        ) from None
    request = await get_rag_document_lifecycle_service().create_delete_request(
        workspace_id=parsed_workspace_id,
        document_id=parsed_document_id,
        expected_version=req.expected_version,
    )
    return {"ok": True, "data": _permission_request_to_dict(request)}


@app.post("/internal/rag/delete-requests/{request_id}/resolve")
async def resolve_rag_delete_request(request_id: str, req: ResolveRagDeleteRequest):
    try:
        parsed_request_id = UUID(request_id)
    except ValueError:
        raise AppError(
            code="VALIDATION_ERROR",
            message="无效的 request_id",
            category="validation",
        ) from None
    result = await get_rag_document_lifecycle_service().resolve_delete_request(
        parsed_request_id,
        req.decision,
        req.note,
    )
    return {
        "ok": True,
        "data": {
            "permission": _permission_request_to_dict(result.request),
            "document_id": str(result.document_id),
            "deleted": result.deleted,
            "cleanup_pending_count": result.cleanup_pending_count,
            "source_artifact_retained": result.source_artifact_retained,
        },
    }


def _rag_feedback_to_dict(feedback, *, review=None) -> dict:
    data = {
        "id": str(feedback.id),
        "trace_id": str(feedback.trace_id),
        "workspace_id": str(feedback.workspace_id),
        "task_id": str(feedback.task_id),
        "run_id": str(feedback.run_id),
        "message_id": str(feedback.message_id),
        "kind": feedback.kind,
        "citation_chunk_id": str(feedback.citation_chunk_id) if feedback.citation_chunk_id else None,
        "status": feedback.status,
        "failure_category": feedback.failure_category,
        "created_at": feedback.created_at.isoformat(),
        "updated_at": feedback.updated_at.isoformat(),
    }
    if review is not None:
        data.update(
            query_hash=review.query_hash,
            pipeline_versions=review.pipeline_versions,
            result_count=review.result_count,
            context_truncated=review.context_truncated,
        )
    return data


def _rag_feedback_detail_to_dict(detail) -> dict:
    return {
        "feedback": _rag_feedback_to_dict(detail.feedback),
        "query_hash": detail.query_hash,
        "query": detail.query,
        "privacy_status": detail.privacy_status,
        "pipeline_versions": detail.pipeline_versions,
        "result_count": detail.result_count,
        "context_truncated": detail.context_truncated,
        "label": ({
            "id": str(detail.label.id), "source": detail.label.source,
            "status": detail.label.status,
            "positive_chunk_ids": [str(value) for value in detail.label.positive_chunk_ids],
            "hard_negative_chunk_ids": [str(value) for value in detail.label.hard_negative_chunk_ids],
        } if detail.label else None),
        "evidence": [{
            "chunk_id": str(item.chunk_id), "document_id": str(item.document_id),
            "content_hash": item.content_hash, "candidate_rank": item.candidate_rank,
            "reranked_rank": item.reranked_rank, "in_context": item.in_context,
            "sources": list(item.sources), "snippet": item.snippet,
        } for item in detail.evidence],
    }


def _rag_review_summary(trace, label) -> dict:
    return {
        "trace_id": str(trace.id), "workspace_id": str(trace.workspace_id),
        "task_id": str(trace.task_id), "run_id": str(trace.run_id),
        "query_hash": trace.query_hash, "privacy_status": trace.privacy_status,
        "label_status": label.status if label else None,
        "label_source": label.source if label else None,
        "candidate_count": len(trace.candidate_ranking),
        "reranked_count": len(trace.reranked_ranking),
        "context_chunk_count": len(trace.context_chunk_ids),
        "context_truncated": trace.context_truncated,
        "pipeline_versions": trace.pipeline_versions,
        "created_at": trace.created_at.isoformat(),
    }


def _rag_review_detail_to_dict(review) -> dict:
    trace = review.trace
    chunks = {chunk.id: chunk for chunk in review.chunks}
    candidate = {UUID(str(value["chunk_id"])): value for value in trace.candidate_ranking}
    reranked = {UUID(str(value["chunk_id"])): value for value in trace.reranked_ranking}
    chunk_ids = list(dict.fromkeys((*candidate, *reranked, *trace.context_chunk_ids)))[:100]
    approved = trace.privacy_status == "approved"
    return {
        "trace": _rag_review_summary(trace, review.label),
        "query": trace.query if approved else None,
        "request": trace.request if approved else None,
        "evidence": [{
            "chunk_id": str(chunk_id), "document_id": str(chunks[chunk_id].document_id),
            "content_hash": chunks[chunk_id].content_hash,
            "candidate_rank": candidate.get(chunk_id, {}).get("rank"),
            "reranked_rank": reranked.get(chunk_id, {}).get("rank"),
            "in_context": chunk_id in trace.context_chunk_ids,
            "sources": list(dict.fromkeys((
                *candidate.get(chunk_id, {}).get("sources", ()),
                *reranked.get(chunk_id, {}).get("sources", ()),
            ))),
            "snippet": _bounded_text(chunks[chunk_id].content, 320) if approved else None,
        } for chunk_id in chunk_ids if chunk_id in chunks],
        "label": ({
            "id": str(review.label.id), "source": review.label.source,
            "status": review.label.status,
            "positive_chunk_ids": [str(value) for value in review.label.positive_chunk_ids],
            "hard_negative_chunk_ids": [str(value) for value in review.label.hard_negative_chunk_ids],
            "notes": review.label.notes,
        } if review.label else None),
        "promotion_candidate": ({
            "schema_version": 1, "trace_id": str(trace.id),
            "query_hash": trace.query_hash,
            "raw_query_included": False, "raw_chunk_content_included": False,
        } if review.label and review.label.status == "promoted" else None),
    }


def _bounded_text(value: str, limit: int) -> str:
    normalized = " ".join(value.split())
    return normalized if len(normalized) <= limit else normalized[:limit].rstrip() + "…"


@app.post("/internal/rag/feedback")
async def submit_rag_feedback(req: SubmitRagFeedbackRequest):
    try:
        message_id = UUID(req.message_id)
        citation_chunk_id = UUID(req.citation_chunk_id) if req.citation_chunk_id else None
    except ValueError:
        raise AppError(
            code="VALIDATION_ERROR", message="无效的 message_id 或 citation_chunk_id", category="validation"
        ) from None
    try:
        feedback = await get_rag_evaluation_feedback_service().submit(
            message_id=message_id,
            kind=req.kind,
            citation_chunk_id=citation_chunk_id,
        )
    except ValueError as exc:
        raise AppError(
            code="RAG_FEEDBACK_INVALID", message=str(exc), category="validation"
        ) from None
    return {"ok": True, "data": {"feedback": _rag_feedback_to_dict(feedback)}}


@app.get("/internal/rag/feedback")
async def list_rag_feedback(
    workspace_id: str,
    status: Literal["pending", "reviewed", "dismissed"] | None = Query("pending"),
    limit: int = Query(50, ge=1, le=100),
):
    try:
        parsed_workspace_id = UUID(workspace_id)
    except ValueError:
        raise AppError(code="VALIDATION_ERROR", message="无效的 workspace_id", category="validation") from None
    items = await get_rag_evaluation_feedback_service().list_queue(
        workspace_id=parsed_workspace_id, status=status, limit=limit
    )
    return {
        "ok": True,
        "data": {
            "feedback": [
                _rag_feedback_to_dict(item.feedback, review=item) for item in items
            ]
        },
    }


@app.patch("/internal/rag/feedback/{feedback_id}")
async def resolve_rag_feedback(feedback_id: str, req: ResolveRagFeedbackRequest):
    try:
        parsed_feedback_id = UUID(feedback_id)
    except ValueError:
        raise AppError(code="VALIDATION_ERROR", message="无效的 feedback_id", category="validation") from None
    try:
        feedback = await get_rag_evaluation_feedback_service().resolve(
            parsed_feedback_id, status=req.status
        )
    except ValueError as exc:
        raise AppError(code="RAG_FEEDBACK_INVALID", message=str(exc), category="validation") from None
    return {"ok": True, "data": {"feedback": _rag_feedback_to_dict(feedback)}}


@app.get("/internal/rag/feedback/{feedback_id}")
async def inspect_rag_feedback(feedback_id: str):
    try:
        parsed_feedback_id = UUID(feedback_id)
    except ValueError:
        raise AppError(code="VALIDATION_ERROR", message="无效的 feedback_id", category="validation") from None
    try:
        detail = await get_rag_evaluation_feedback_service().inspect(parsed_feedback_id)
    except ValueError as exc:
        raise AppError(code="RAG_FEEDBACK_INVALID", message=str(exc), category="validation") from None
    return {"ok": True, "data": _rag_feedback_detail_to_dict(detail)}


@app.post("/internal/rag/feedback/{feedback_id}/triage")
async def triage_rag_feedback(feedback_id: str, req: TriageRagFeedbackRequest):
    try:
        parsed_feedback_id = UUID(feedback_id)
        positives = tuple(UUID(value) for value in req.positive_chunk_ids)
        negatives = tuple(UUID(value) for value in req.hard_negative_chunk_ids)
    except ValueError:
        raise AppError(code="VALIDATION_ERROR", message="无效的 feedback_id 或 chunk_id", category="validation") from None
    try:
        feedback, label = await get_rag_evaluation_feedback_service().triage(
            parsed_feedback_id,
            failure_category=req.failure_category,
            positive_chunk_ids=positives,
            hard_negative_chunk_ids=negatives,
        )
    except ValueError as exc:
        raise AppError(code="RAG_FEEDBACK_INVALID", message=str(exc), category="validation") from None
    return {
        "ok": True,
        "data": {
            "feedback": _rag_feedback_to_dict(feedback),
            "label_status": label.status if label else None,
        },
    }


@app.get("/internal/rag/evaluation/traces")
async def list_rag_evaluation_traces(
    workspace_id: str,
    privacy_status: Literal["pending", "approved", "rejected", "all"] = Query("pending"),
    limit: int = Query(50, ge=1, le=100),
):
    try:
        parsed_workspace_id = UUID(workspace_id)
    except ValueError:
        raise AppError(code="VALIDATION_ERROR", message="无效的 workspace_id", category="validation") from None
    values = await get_rag_evaluation_review_service().list_traces(
        workspace_id=parsed_workspace_id,
        privacy_status=None if privacy_status == "all" else privacy_status,
        limit=limit,
    )
    return {
        "ok": True,
        "data": {"traces": [_rag_review_summary(trace, label) for trace, label in values]},
    }


@app.get("/internal/rag/evaluation/gates")
async def list_rag_quality_gate_runs(limit: int = Query(20, ge=1, le=100)):
    values, insights = await get_rag_quality_gate_service().get_overview(limit=limit)
    return {
        "ok": True,
        "data": {
            "runs": [
                {
                    "id": str(value.id),
                    "gate_id": value.gate_id,
                    "cohort_id": value.cohort_id,
                    "baseline_id": value.baseline_id,
                    "revision": value.revision,
                    "status": value.status,
                    "sample_count": value.sample_count,
                    "metrics": value.metrics,
                    "checks": list(value.checks),
                    "generated_at": value.generated_at.isoformat(),
                }
                for value in values
            ],
            "insights": {
                "comparison_state": insights.comparison_state,
                "compatible_history_count": insights.compatible_history_count,
                "previous_run_id": (
                    str(insights.previous_run_id) if insights.previous_run_id else None
                ),
                "metric_trends": [asdict(value) for value in insights.metric_trends],
                "alerts": [asdict(value) for value in insights.alerts],
                "failure_clusters": [
                    asdict(value) for value in insights.failure_clusters
                ],
            },
        },
    }


@app.get("/internal/rag/evaluation/gates/{run_id}/failure-targets")
async def list_rag_quality_failure_targets(
    run_id: str, failure_type: str, limit: int = Query(50, ge=1, le=100)
):
    try:
        parsed_run_id = UUID(run_id)
        values = await get_rag_quality_gate_service().list_failure_targets(
            run_id=parsed_run_id, failure_type=failure_type, limit=limit
        )
    except ValueError as exc:
        raise AppError(code="VALIDATION_ERROR", message=str(exc), category="validation") from None
    except LookupError as exc:
        raise AppError(code="RAG_GATE_NOT_FOUND", message=str(exc), category="not_found") from None
    targets = []
    for value in values:
        projected = {**asdict(value), "trace_id": str(value.trace_id), "workspace_id": str(value.workspace_id)}
        if value.issue:
            projected["issue"] = {
                **asdict(value.issue), "id": str(value.issue.id),
                "trace_id": str(value.issue.trace_id),
                "first_seen_run_id": str(value.issue.first_seen_run_id),
                "last_seen_run_id": str(value.issue.last_seen_run_id),
                "verified_run_id": str(value.issue.verified_run_id) if value.issue.verified_run_id else None,
                "created_at": value.issue.created_at.isoformat(), "updated_at": value.issue.updated_at.isoformat(),
            }
        targets.append(projected)
    return {"ok": True, "data": {"targets": targets}}


def _rag_quality_issue_to_dict(issue) -> dict:
    return {
        **asdict(issue), "id": str(issue.id), "trace_id": str(issue.trace_id),
        "first_seen_run_id": str(issue.first_seen_run_id),
        "last_seen_run_id": str(issue.last_seen_run_id),
        "verified_run_id": str(issue.verified_run_id) if issue.verified_run_id else None,
        "created_at": issue.created_at.isoformat(), "updated_at": issue.updated_at.isoformat(),
    }


@app.get("/internal/rag/evaluation/issues")
async def list_rag_quality_issues(
    status: str = "all", owner: str = "all", failure_type: str = "all",
    limit: int = Query(50, ge=1, le=100),
):
    try:
        values, summary = await get_rag_quality_gate_service().list_issues(
            status=status, owner=owner, failure_type=failure_type, limit=limit,
        )
    except ValueError as exc:
        raise AppError(code="VALIDATION_ERROR", message=str(exc), category="validation") from None
    return {"ok": True, "data": {"issues": [{
        "issue": _rag_quality_issue_to_dict(value.issue),
        "trace_id": str(value.issue.trace_id), "workspace_id": str(value.workspace_id), "query_hash": value.query_hash,
        "privacy_status": value.privacy_status, "label_status": value.label_status,
        "review_state": value.review_state,
        "first_seen_revision": value.first_seen_revision,
        "last_seen_revision": value.last_seen_revision,
        "verified_revision": value.verified_revision,
    } for value in values], "summary": summary}}


@app.patch("/internal/rag/evaluation/issues/{issue_id}")
async def update_rag_quality_issue(issue_id: str, req: UpdateRagQualityIssueRequest):
    try:
        issue = await get_rag_quality_gate_service().update_issue(
            UUID(issue_id), expected_version=req.expected_version, owner=req.owner,
            status=req.status, resolution_note=req.resolution_note,
        )
    except ValueError as exc:
        raise AppError(code="RAG_QUALITY_ISSUE_INVALID", message=str(exc), category="validation") from None
    except LookupError as exc:
        raise AppError(code="RAG_QUALITY_ISSUE_NOT_FOUND", message=str(exc), category="not_found") from None
    except RuntimeError as exc:
        raise AppError(code="RAG_QUALITY_ISSUE_CONFLICT", message=str(exc), category="conflict") from None
    return {"ok": True, "data": {"issue": _rag_quality_issue_to_dict(issue)}}


@app.get("/internal/rag/evaluation/traces/{trace_id}")
async def inspect_rag_evaluation_trace(trace_id: str, workspace_id: str):
    try:
        parsed_trace_id, parsed_workspace_id = UUID(trace_id), UUID(workspace_id)
    except ValueError:
        raise AppError(code="VALIDATION_ERROR", message="无效的 trace_id 或 workspace_id", category="validation") from None
    try:
        review = await get_rag_evaluation_review_service().inspect(
            parsed_trace_id, workspace_id=parsed_workspace_id
        )
    except ValueError as exc:
        raise AppError(code="RAG_REVIEW_INVALID", message=str(exc), category="validation") from None
    return {"ok": True, "data": _rag_review_detail_to_dict(review)}


@app.post("/internal/rag/evaluation/traces/{trace_id}/privacy")
async def review_rag_evaluation_privacy(trace_id: str, req: ReviewRagTracePrivacyRequest):
    try:
        parsed_trace_id, workspace_id = UUID(trace_id), UUID(req.workspace_id)
    except ValueError:
        raise AppError(code="VALIDATION_ERROR", message="无效的 trace_id 或 workspace_id", category="validation") from None
    try:
        await get_rag_evaluation_review_service().review_privacy(
            parsed_trace_id, approved=req.decision == "approved", workspace_id=workspace_id
        )
        review = await get_rag_evaluation_review_service().inspect(
            parsed_trace_id, workspace_id=workspace_id
        )
    except ValueError as exc:
        raise AppError(code="RAG_REVIEW_INVALID", message=str(exc), category="validation") from None
    return {"ok": True, "data": _rag_review_detail_to_dict(review)}


@app.post("/internal/rag/evaluation/traces/{trace_id}/label")
async def review_rag_evaluation_label(trace_id: str, req: ReviewRagTraceLabelRequest):
    try:
        parsed_trace_id, workspace_id = UUID(trace_id), UUID(req.workspace_id)
        positives = tuple(UUID(value) for value in req.positive_chunk_ids)
        negatives = tuple(UUID(value) for value in req.hard_negative_chunk_ids)
    except ValueError:
        raise AppError(code="VALIDATION_ERROR", message="无效的 trace_id、workspace_id 或 chunk_id", category="validation") from None
    try:
        await get_rag_evaluation_review_service().set_label(
            trace_id=parsed_trace_id, workspace_id=workspace_id,
            positive_chunk_ids=positives, hard_negative_chunk_ids=negatives,
            notes=req.notes, status=req.status,
        )
        review = await get_rag_evaluation_review_service().inspect(
            parsed_trace_id, workspace_id=workspace_id
        )
    except ValueError as exc:
        raise AppError(code="RAG_REVIEW_INVALID", message=str(exc), category="validation") from None
    return {"ok": True, "data": _rag_review_detail_to_dict(review)}


@app.post("/internal/rag/evaluation/traces/{trace_id}/promote")
async def promote_rag_evaluation_label(trace_id: str, req: PromoteRagTraceRequest):
    try:
        parsed_trace_id, workspace_id = UUID(trace_id), UUID(req.workspace_id)
    except ValueError:
        raise AppError(code="VALIDATION_ERROR", message="无效的 trace_id 或 workspace_id", category="validation") from None
    try:
        review = await get_rag_evaluation_review_service().promote(
            parsed_trace_id, workspace_id=workspace_id
        )
    except ValueError as exc:
        raise AppError(code="RAG_REVIEW_INVALID", message=str(exc), category="validation") from None
    return {"ok": True, "data": _rag_review_detail_to_dict(review)}


# ── Personal Knowledge Base API ───────────────────────────────

# ── MCP Server API ────────────────────────────────────────────

def _mcp_server_to_dict(server, tools=()) -> dict:
    return {
        "id": str(server.id), "slug": server.slug, "name": server.name,
        "transport": server.transport.value, "command": server.command,
        "args": server.args, "env_keys": server.env_keys, "enabled": server.enabled,
        "status": server.status.value, "last_error_code": server.last_error_code,
        "last_connected_at": server.last_connected_at.isoformat() if server.last_connected_at else None,
        "version": server.version, "created_at": server.created_at.isoformat(),
        "updated_at": server.updated_at.isoformat(),
        "tools": [{"id": str(item.id), "original_name": item.original_name,
                   "internal_name": item.internal_name, "description": item.description,
                   "input_schema": item.input_schema, "risk_level": item.risk_level,
                   "enabled": item.enabled} for item in tools],
    }


@app.get("/internal/mcp-servers")
async def list_mcp_servers():
    service = get_mcp_service()
    servers = await service.list_servers()
    return {"ok": True, "data": {"servers": [
        _mcp_server_to_dict(server, await service.list_tools(server.id)) for server in servers
    ]}}


@app.post("/internal/mcp-servers")
async def create_mcp_server(req: CreateMcpServerRequest):
    service = get_mcp_service()
    server = await service.create_server(CreateMcpServerInput(
        slug=req.slug, name=req.name, command=req.command, args=req.args, env_keys=req.env_keys,
    ))
    return {"ok": True, "data": {"server": _mcp_server_to_dict(server), "worker_restart_required": True}}


@app.post("/internal/mcp-servers/builtin/literature")
async def connect_builtin_literature_server():
    service = get_mcp_service()
    for current in await service.list_servers():
        if current.slug == "jarvis_literature":
            return {"ok": True, "data": {
                "server": _mcp_server_to_dict(
                    current, await service.list_tools(current.id)
                ),
                "worker_restart_required": True,
            }}
    server = await service.create_server(CreateMcpServerInput(
        slug="jarvis_literature",
        name="Jarvis 权威文献来源",
        command=sys.executable,
        args=["-m", "jarvis_worker.mcp_servers.literature"],
        env_keys=[],
    ))
    return {"ok": True, "data": {
        "server": _mcp_server_to_dict(server),
        "worker_restart_required": True,
    }}


@app.patch("/internal/mcp-servers/{server_id}")
async def update_mcp_server(server_id: str, req: UpdateMcpServerRequest):
    try:
        parsed_id = UUID(server_id)
    except ValueError:
        raise AppError("VALIDATION_ERROR", "无效的 MCP server_id", "validation")
    server = await get_mcp_service().set_enabled(parsed_id, req.enabled, req.expected_version)
    return {"ok": True, "data": {"server": _mcp_server_to_dict(server), "worker_restart_required": True}}


def _knowledge_vault_to_dict(vault) -> dict:
    return {
        "id": str(vault.id), "name": vault.name, "root_path": vault.root_path,
        "canonical_path": vault.canonical_path, "status": vault.status.value,
        "source": vault.source.value, "created_at": vault.created_at.isoformat(),
        "updated_at": vault.updated_at.isoformat(),
    }


def _knowledge_document_to_dict(document) -> dict:
    return {
        "id": str(document.id), "vault_id": str(document.vault_id),
        "title": document.title, "kind": document.kind.value,
        "relative_path": document.relative_path, "content_hash": document.content_hash,
        "size_bytes": document.size_bytes, "tags": document.tags,
        "source_urls": document.source_urls,
        "source_task_id": str(document.source_task_id) if document.source_task_id else None,
        "source_run_id": str(document.source_run_id) if document.source_run_id else None,
        "created_at": document.created_at.isoformat(), "updated_at": document.updated_at.isoformat(),
    }


@app.get("/internal/knowledge-vaults")
async def list_knowledge_vaults():
    service = get_knowledge_service()
    return {"ok": True, "data": {
        "vaults": [_knowledge_vault_to_dict(item) for item in await service.list_vaults()],
        "suggested_path": service.suggested_path(),
    }}


@app.post("/internal/knowledge-vaults/connect")
async def connect_knowledge_vault(req: ConnectKnowledgeVaultRequest):
    vault = await get_knowledge_service().connect(req.path)
    return {"ok": True, "data": {"vault": _knowledge_vault_to_dict(vault)}}


@app.get("/internal/knowledge-vaults/{vault_id}/documents")
async def list_knowledge_documents(vault_id: str, limit: int = Query(100, ge=1, le=100)):
    try:
        parsed_id = UUID(vault_id)
    except ValueError:
        raise AppError("VALIDATION_ERROR", "无效的 vault_id", "validation")
    documents = await get_knowledge_service().list_documents(parsed_id, limit)
    return {"ok": True, "data": {"documents": [_knowledge_document_to_dict(item) for item in documents]}}


@app.post("/internal/knowledge-vaults/{vault_id}/documents")
async def create_knowledge_document(vault_id: str, req: CreateKnowledgeDocumentRequest):
    try:
        parsed_id = UUID(vault_id)
    except ValueError:
        raise AppError("VALIDATION_ERROR", "无效的 vault_id", "validation")
    document = await get_knowledge_service().create_document(parsed_id, CreateKnowledgeDocumentInput(
        title=req.title, kind=req.kind, content=req.content, tags=req.tags, source_urls=req.source_urls,
    ))
    return {"ok": True, "data": {"document": _knowledge_document_to_dict(document)}}


# ── Scheduled Task API ───────────────────────────────────────

def _scheduled_task_to_dict(item) -> dict:
    return {
        "id": str(item.id), "name": item.name, "user_goal": item.user_goal,
        "recurrence": item.recurrence.value, "timezone": item.timezone,
        "hour": item.hour, "minute": item.minute, "weekday": item.weekday,
        "workspace_id": str(item.workspace_id) if item.workspace_id else None,
        "status": item.status.value, "authorized_tools": item.authorized_tools,
        "task_kind": item.task_kind, "source_policy": item.source_policy,
        "next_run_at": item.next_run_at.isoformat(),
        "last_run_at": item.last_run_at.isoformat() if item.last_run_at else None,
        "last_task_id": str(item.last_task_id) if item.last_task_id else None,
        "last_run_id": str(item.last_run_id) if item.last_run_id else None,
        "version": item.version, "created_at": item.created_at.isoformat(),
        "updated_at": item.updated_at.isoformat(),
    }


def _scheduled_execution_to_dict(item) -> dict:
    return {"id": str(item.id), "scheduled_task_id": str(item.scheduled_task_id),
        "scheduled_for": item.scheduled_for.isoformat(), "status": item.status.value,
        "task_id": str(item.task_id) if item.task_id else None,
        "run_id": str(item.run_id) if item.run_id else None,
        "attempts": item.attempts, "error_code": item.error_code,
        "created_at": item.created_at.isoformat(), "updated_at": item.updated_at.isoformat()}


@app.get("/internal/scheduled-tasks")
async def list_scheduled_tasks():
    items = await get_scheduled_task_service().list_tasks()
    return {"ok": True, "data": {"scheduled_tasks": [_scheduled_task_to_dict(item) for item in items]}}


@app.post("/internal/scheduled-tasks")
async def create_scheduled_task(req: CreateScheduledTaskRequest):
    try:
        workspace_id = UUID(req.workspace_id) if req.workspace_id else None
    except ValueError:
        raise AppError(
            "VALIDATION_ERROR", "无效的 workspace_id", "validation"
        ) from None
    item = await get_scheduled_task_service().create(CreateScheduledTaskInput(
        name=req.name, user_goal=req.user_goal, recurrence=req.recurrence,
        timezone=req.timezone, hour=req.hour, minute=req.minute,
        weekday=req.weekday, workspace_id=workspace_id,
        task_kind=req.task_kind, source_query=req.source_query,
        source_max_results=req.source_max_results,
    ))
    return {"ok": True, "data": {"scheduled_task": _scheduled_task_to_dict(item)}}


@app.patch("/internal/scheduled-tasks/{scheduled_task_id}")
async def update_scheduled_task(scheduled_task_id: str, req: UpdateScheduledTaskRequest):
    try:
        item_id = UUID(scheduled_task_id)
    except ValueError:
        raise AppError(
            "VALIDATION_ERROR", "无效的 scheduled_task_id", "validation"
        ) from None
    item = await get_scheduled_task_service().set_status(item_id, req.status, req.expected_version)
    return {"ok": True, "data": {"scheduled_task": _scheduled_task_to_dict(item)}}


@app.post("/internal/scheduled-tasks/{scheduled_task_id}/trigger")
async def trigger_scheduled_task(scheduled_task_id: str):
    try:
        item_id = UUID(scheduled_task_id)
    except ValueError:
        raise AppError(
            "VALIDATION_ERROR", "无效的 scheduled_task_id", "validation"
        ) from None
    execution = await get_scheduled_task_service().trigger_now(item_id)
    return {"ok": True, "data": {"execution": _scheduled_execution_to_dict(execution)}}


# ── Memory API ────────────────────────────────────────────────

def _memory_to_dict(memory) -> dict:
    return {
        "id": str(memory.id),
        "scope_type": memory.scope_type.value,
        "workspace_id": str(memory.workspace_id) if memory.workspace_id else None,
        "category": memory.category.value,
        "key": memory.key,
        "content": memory.content,
        "status": memory.status.value,
        "source_type": memory.source_type.value,
        "importance": memory.importance,
        "version": memory.version,
        "created_at": memory.created_at.isoformat(),
        "updated_at": memory.updated_at.isoformat(),
    }


def _memory_candidate_to_dict(candidate) -> dict:
    return {
        "id": str(candidate.id),
        "scope_type": candidate.scope_type.value,
        "workspace_id": str(candidate.workspace_id) if candidate.workspace_id else None,
        "category": candidate.category.value,
        "suggested_key": candidate.suggested_key,
        "content": candidate.content,
        "status": candidate.status.value,
        "source_task_id": str(candidate.source_task_id),
        "source_run_id": str(candidate.source_run_id),
        "confidence": candidate.confidence,
        "importance": candidate.importance,
        "sensitivity": candidate.sensitivity.value,
        "conflict_memory_id": str(candidate.conflict_memory_id) if candidate.conflict_memory_id else None,
        "approved_memory_id": str(candidate.approved_memory_id) if candidate.approved_memory_id else None,
        "extraction_policy_version": candidate.extraction_policy_version,
        "expires_at": candidate.expires_at.isoformat() if candidate.expires_at else None,
        "resolved_at": candidate.resolved_at.isoformat() if candidate.resolved_at else None,
        "resolution_note": candidate.resolution_note,
        "version": candidate.version,
        "created_at": candidate.created_at.isoformat(),
        "updated_at": candidate.updated_at.isoformat(),
    }


@app.get("/internal/memories")
async def list_memories(
    scope_type: Optional[Literal["global", "workspace"]] = None,
    workspace_id: Optional[str] = None,
    status: Optional[Literal["active", "disabled"]] = None,
    category: Optional[Literal["preference", "user_fact", "project_fact", "rule"]] = None,
    query: Optional[str] = Query(None, max_length=200),
    limit: int = Query(100, ge=1, le=100),
):
    try:
        parsed_workspace_id = UUID(workspace_id) if workspace_id else None
    except ValueError:
        raise AppError("VALIDATION_ERROR", "无效的 workspace_id", "validation")
    memories = await get_memory_service().list_memories(
        scope_type=scope_type, workspace_id=parsed_workspace_id, status=status,
        category=category, query=query, limit=limit,
    )
    return {"ok": True, "data": {"memories": [_memory_to_dict(item) for item in memories]}}


@app.post("/internal/memories")
async def create_memory(req: CreateMemoryRequest):
    try:
        workspace_id = UUID(req.workspace_id) if req.workspace_id else None
    except ValueError:
        raise AppError("VALIDATION_ERROR", "无效的 workspace_id", "validation")
    memory = await get_memory_service().create_memory(CreateMemoryInput(
        scope_type=req.scope_type, workspace_id=workspace_id,
        category=req.category, key=req.key, content=req.content,
        importance=req.importance,
    ))
    return {"ok": True, "data": {"memory": _memory_to_dict(memory)}}


@app.patch("/internal/memories/{memory_id}")
async def update_memory(memory_id: str, req: UpdateMemoryRequest):
    try:
        parsed_id = UUID(memory_id)
    except ValueError:
        raise AppError("VALIDATION_ERROR", "无效的 memory_id", "validation")
    memory = await get_memory_service().update_memory(parsed_id, UpdateMemoryInput(
        expected_version=req.expected_version, content=req.content,
        status=req.status, importance=req.importance,
    ))
    return {"ok": True, "data": {"memory": _memory_to_dict(memory)}}


@app.delete("/internal/memories/{memory_id}")
async def delete_memory(memory_id: str):
    try:
        parsed_id = UUID(memory_id)
    except ValueError:
        raise AppError("VALIDATION_ERROR", "无效的 memory_id", "validation")
    memory = await get_memory_service().delete_memory(parsed_id)
    return {"ok": True, "data": {"memory": _memory_to_dict(memory)}}


@app.get("/internal/memory-candidates")
async def list_memory_candidates(
    status: Optional[Literal["pending", "approved", "rejected", "expired"]] = None,
    workspace_id: Optional[str] = None,
    limit: int = Query(100, ge=1, le=100),
):
    try:
        parsed_workspace_id = UUID(workspace_id) if workspace_id else None
    except ValueError:
        raise AppError("VALIDATION_ERROR", "无效的 workspace_id", "validation")
    candidates = await get_memory_candidate_service().list_candidates(
        status=status, workspace_id=parsed_workspace_id, limit=limit
    )
    return {
        "ok": True,
        "data": {"candidates": [_memory_candidate_to_dict(item) for item in candidates]},
    }


@app.patch("/internal/memory-candidates/{candidate_id}")
async def update_memory_candidate(candidate_id: str, req: UpdateMemoryCandidateRequest):
    try:
        parsed_id = UUID(candidate_id)
        workspace_id = UUID(req.workspace_id) if req.workspace_id else None
    except ValueError:
        raise AppError("VALIDATION_ERROR", "无效的 candidate_id 或 workspace_id", "validation")
    candidate = await get_memory_candidate_service().update_candidate(
        parsed_id,
        UpdateMemoryCandidateInput(
            expected_version=req.expected_version,
            scope_type=req.scope_type,
            workspace_id=workspace_id,
            category=req.category,
            suggested_key=req.suggested_key,
            content=req.content,
            importance=req.importance,
        ),
    )
    return {"ok": True, "data": {"candidate": _memory_candidate_to_dict(candidate)}}


@app.post("/internal/memory-candidates/{candidate_id}/approve")
async def approve_memory_candidate(candidate_id: str, req: ResolveMemoryCandidateRequest):
    try:
        parsed_id = UUID(candidate_id)
    except ValueError:
        raise AppError("VALIDATION_ERROR", "无效的 candidate_id", "validation")
    candidate, memory = await get_memory_candidate_service().approve_candidate(
        parsed_id,
        ResolveMemoryCandidateInput(expected_version=req.expected_version, note=req.note),
    )
    return {
        "ok": True,
        "data": {
            "candidate": _memory_candidate_to_dict(candidate),
            "memory": _memory_to_dict(memory),
        },
    }


@app.post("/internal/memory-candidates/{candidate_id}/reject")
async def reject_memory_candidate(candidate_id: str, req: ResolveMemoryCandidateRequest):
    try:
        parsed_id = UUID(candidate_id)
    except ValueError:
        raise AppError("VALIDATION_ERROR", "无效的 candidate_id", "validation")
    candidate = await get_memory_candidate_service().reject_candidate(
        parsed_id,
        ResolveMemoryCandidateInput(expected_version=req.expected_version, note=req.note),
    )
    return {"ok": True, "data": {"candidate": _memory_candidate_to_dict(candidate)}}


# ── Conversation API ────────────────────────────────────────────

# ── Audit Log API ───────────────────────────────────────────────

@app.get("/internal/audit-logs")
async def list_audit_logs(
    limit: int = Query(50, ge=1, le=100),
    event_type: Optional[str] = Query(None, min_length=1, max_length=50),
    actor: Optional[str] = Query(None, min_length=1, max_length=100),
    task_id: Optional[str] = Query(None),
    run_id: Optional[str] = Query(None),
    before: Optional[str] = Query(None, max_length=512),
):
    """查询经安全投影的审计日志；不返回原始 details/error。"""
    try:
        parsed_task_id = UUID(task_id) if task_id else None
        parsed_run_id = UUID(run_id) if run_id else None
    except ValueError:
        raise AppError(code="VALIDATION_ERROR", message="无效的 task_id 或 run_id", category="validation")

    audit_svc = get_audit_query_service()
    page = await audit_svc.list_audit_logs(
        limit=limit,
        event_type=event_type,
        actor=actor,
        task_id=parsed_task_id,
        run_id=parsed_run_id,
        before=before,
    )
    return {
        "ok": True,
        "data": {
            "audit_logs": [asdict(item) for item in page["audit_logs"]],
            "next_cursor": page["next_cursor"],
        },
    }


@app.get("/internal/audit-logs/export")
async def export_audit_logs(
    export_format: Literal["jsonl", "csv"] = Query("jsonl", alias="format"),
    max_rows: int = Query(5_000, ge=1, le=10_000),
    max_bytes: int = Query(5 * 1024 * 1024, ge=1_024, le=10 * 1024 * 1024),
    event_type: Optional[str] = Query(None, min_length=1, max_length=50),
    actor: Optional[str] = Query(None, min_length=1, max_length=100),
    task_id: Optional[str] = Query(None),
    run_id: Optional[str] = Query(None),
    before: Optional[str] = Query(None, max_length=512),
):
    """分页生成安全审计投影；响应正文不进入应用日志或 AuditLog。"""
    try:
        parsed_task_id = UUID(task_id) if task_id else None
        parsed_run_id = UUID(run_id) if run_id else None
    except ValueError:
        raise AppError(
            code="VALIDATION_ERROR",
            message="无效的 task_id 或 run_id",
            category="validation",
        )

    stream = get_audit_query_service().export_audit_logs(
        export_format=export_format,
        max_rows=max_rows,
        max_bytes=max_bytes,
        event_type=event_type,
        actor=actor,
        task_id=parsed_task_id,
        run_id=parsed_run_id,
        before=before,
    )
    media_type = "application/x-ndjson" if export_format == "jsonl" else "text/csv"
    return StreamingResponse(
        stream,
        media_type=media_type,
        headers={
            "Cache-Control": "no-store",
            "Content-Disposition": (
                f'attachment; filename="jarvis-audit-export.{export_format}"'
            ),
            "X-Content-Type-Options": "nosniff",
            "X-Audit-Export-Max-Rows": str(max_rows),
            "X-Audit-Export-Max-Bytes": str(max_bytes),
        },
    )


@app.get("/internal/audit-logs/retention/preview")
async def preview_audit_retention(
    standard_days: int = Query(90, ge=30, le=3_650),
    extended_days: int = Query(365, ge=30, le=3_650),
    max_scan: int = Query(1_000, ge=1, le=10_000),
    max_candidates: int = Query(100, ge=1, le=1_000),
):
    """只读预演审计保留策略；不删除或归档。"""
    preview = await get_audit_query_service().preview_retention(
        standard_days=standard_days,
        extended_days=extended_days,
        max_scan=max_scan,
        max_candidates=max_candidates,
    )
    return {"ok": True, "data": asdict(preview)}


@app.post("/internal/audit-logs/retention/requests")
async def create_audit_retention_request(req: CreateAuditRetentionRequest):
    """冻结候选快照并创建 L4 单次确认，不执行删除。"""
    request = await get_audit_query_service().create_retention_request(
        standard_days=req.standard_days,
        extended_days=req.extended_days,
        max_scan=req.max_scan,
        max_candidates=req.max_candidates,
    )
    return {"ok": True, "data": {"request": _permission_request_to_dict(request)}}


@app.post("/internal/audit-logs/retention/requests/{request_id}/resolve")
async def resolve_audit_retention_request(
    request_id: str,
    req: ResolveAuditRetentionRequest,
):
    try:
        parsed_request_id = UUID(request_id)
    except ValueError:
        raise AppError(
            code="VALIDATION_ERROR",
            message="无效的 request_id",
            category="validation",
        ) from None
    result = await get_audit_query_service().resolve_retention_request(
        parsed_request_id,
        req.decision,
        req.note,
    )
    return {
        "ok": True,
        "data": {
            "permission": _permission_request_to_dict(result.request),
            "deleted_records": result.deleted_records,
            "has_more": result.has_more,
        },
    }


@app.get("/internal/conversations")
async def list_conversations(limit: int = Query(50, le=100), offset: int = Query(0, ge=0)):
    """会话列表（多轮对话 MVP）。

    返回按 updated_at 倒序排列的会话列表。
    """
    conv_svc = get_conversation_service()
    conversations = await conv_svc.list_conversations(limit=limit, offset=offset)

    return {
        "ok": True,
        "data": {
            "conversations": [
                {
                    "id": str(c.id),
                    "title": c.title,
                    "created_at": c.created_at.isoformat(),
                    "updated_at": c.updated_at.isoformat(),
                }
                for c in conversations
            ],
        },
    }


@app.get("/internal/conversations/{conversation_id}")
async def get_conversation(
    conversation_id: str,
    limit: int = Query(50, ge=1, le=100),
    before: Optional[str] = Query(None),
):
    """会话详情（有界分页，向后兼容）。

    不传参数时返回最近 limit 条消息（默认 50，最大 100）。
    传入 ?limit=N&before=<cursor> 可分页加载更早消息。

    cursor 格式：base64(json([created_at_iso, message_id]))。
    """
    try:
        cid = UUID(conversation_id)
    except ValueError:
        raise AppError(code="VALIDATION_ERROR", message="无效的 conversation_id", category="validation")

    conv_svc = get_conversation_service()
    detail = await conv_svc.get_conversation_detail(
        cid,
        limit=limit,
        before=before,
    )

    return {
        "ok": True,
        "data": {
            "conversation": {
                "id": str(detail["conversation"].id),
                "title": detail["conversation"].title,
                "created_at": detail["conversation"].created_at.isoformat(),
                "updated_at": detail["conversation"].updated_at.isoformat(),
            },
            "messages": [_message_to_dict(m) for m in detail["messages"]],
            "next_cursor": detail.get("next_cursor"),
        },
    }


@app.post("/internal/permissions/decide")
async def decide_permission(req: PermissionDecisionRequest):
    """权限决定。

    语义：
    - 更新 permission_requests.status
    - 写入 OutboxEvent (event_type=permission.decision)
    - 幂等：重复相同 decision → 返回已有结果
    - 冲突：提交冲突 decision → 409
    """
    perm_svc = get_permission_service()
    try:
        request_id = UUID(req.request_id)
    except ValueError:
        raise AppError(code="VALIDATION_ERROR", message="无效的 request_id", category="validation")

    result = await perm_svc.decide(request_id, req.decision, req.note)
    return {
        "ok": True,
        "data": {
            "request": {
                "id": str(result.id), "task_id": str(result.task_id),
                "run_id": str(result.run_id),
                "step_id": str(result.step_id) if result.step_id else None,
                "tool_name": result.tool_name,
                "action_summary": result.action_summary,
                "reason": result.reason, "risk_level": result.risk_level,
                "scope": result.scope,
                "arguments_summary": result.arguments_summary,
                "allowed_decisions": result.allowed_decisions,
                "created_at": result.created_at.isoformat(),
                "expires_at": result.expires_at.isoformat(),
                "status": result.status.value,
                "decision": result.decision,
            },
            # 后续 permission.resolved/tool/run 事件统一经 Outbox + SSE 到达。
            "events": [],
        },
    }


@app.get("/internal/runs/{run_id}/permissions")
async def list_pending_permissions(run_id: str):
    """从 PostgreSQL 权威读取指定 Run 的待处理权限请求。"""
    try:
        rid = UUID(run_id)
    except ValueError:
        raise AppError(
            code="VALIDATION_ERROR",
            message="无效的 run_id",
            category="validation",
        )
    requests = await get_permission_service().get_pending_by_run(rid)
    return {
        "ok": True,
        "data": {
            "requests": [
                {
                    "id": str(item.id),
                    "task_id": str(item.task_id),
                    "run_id": str(item.run_id),
                    "step_id": str(item.step_id) if item.step_id else None,
                    "tool_name": item.tool_name,
                    "action_summary": item.action_summary,
                    "reason": item.reason,
                    "risk_level": item.risk_level,
                    "scope": item.scope,
                    "arguments_summary": item.arguments_summary,
                    "allowed_decisions": item.allowed_decisions,
                    "created_at": item.created_at.isoformat(),
                    "expires_at": item.expires_at.isoformat(),
                    "status": item.status.value,
                    "decision": item.decision,
                }
                for item in requests
            ]
        },
    }


# ── Model Config API (Phase 6) ─────────────────────────────────────


@app.get("/internal/model-config")
async def get_model_config():
    """返回当前模型配置安全投影。

    绝不包含 API key 原值或环境变量名。
    base URL 经过 sanitize_base_url 脱敏。
    """
    # 读取 worker 状态信息（从 heartbeat 投影，由 Gateway 单独提供）
    # Control Plane 无法直接获取 worker heartbeat，返回未知状态
    config = build_model_config(
        worker_status="unknown",
        last_heartbeat_at=None,
        last_error_code=None,
    )
    return {
        "ok": True,
        "data": {
            "provider": config.provider,
            "protocol": config.protocol,
            "model_name": config.model_name,
            "base_url_display": config.base_url_display,
            "api_key_configured": config.api_key_configured,
            "timeout_seconds": config.timeout_seconds,
            "max_retries": config.max_retries,
            "max_tokens": config.max_tokens,
            "thinking_mode": config.thinking_mode,
            "worker_status": config.worker_status,
            "last_heartbeat_at": config.last_heartbeat_at,
            "last_error_code": config.last_error_code,
        },
    }


@app.post("/internal/model-config/test")
async def test_model_config():
    """发起模型连通性测试。

    使用短超时（5s）、不重试、固定最小 prompt（max_tokens=1）。
    结果写入 AuditLog（仅安全摘要，不含 API key 或原始响应）。
    """
    try:
        result = await test_model_connection()
    except Exception:
        logger.exception("模型连通性测试异常")
        return JSONResponse(
            status_code=500,
            content={
                "ok": False,
                "error": {
                    "code": "INTERNAL_ERROR",
                    "message": "连通性测试内部错误",
                    "category": "internal",
                    "recoverable": False,
                    "details": {},
                },
            },
        )

    if result.status == "ok":
        return {
            "ok": True,
            "data": {
                "provider": result.provider,
                "model": result.model,
                "latency_ms": result.latency_ms,
                "tested_at": result.tested_at,
                "status": "ok",
                "error": None,
            },
        }

    return {
        "ok": True,
        "data": {
            "provider": result.provider,
            "model": result.model,
            "latency_ms": result.latency_ms,
            "tested_at": result.tested_at,
            "status": "failed",
            "error": {
                "code": result.error_code,
                "message": result.error_message,
                "category": result.error_category,
                "recoverable": result.error_recoverable,
            },
        },
    }
