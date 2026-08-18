"""等待已提交的 RAG 入库作业到达真实终态。"""

from __future__ import annotations

from uuid import UUID

from jarvis_worker.agent.rag.contracts import RagIngestionStatus
from jarvis_worker.agent.rag.query.ingestion_status import RagIngestionMonitorError
from jarvis_worker.agent.tool_gateway.contracts import ToolRequest, ToolResult


class RagAwaitIngestionToolExecutor:
    def __init__(self, service, async_bridge, *, bridge_timeout_seconds: float = 910) -> None:
        self._service = service
        self._bridge = async_bridge
        self._bridge_timeout_seconds = bridge_timeout_seconds

    def __call__(self, request: ToolRequest) -> ToolResult:
        try:
            task_id = UUID(request.task_id)
            job_id = UUID(str(request.arguments.get("job_id", "")))
            result = self._bridge.run(
                self._service.wait_for_task_job(task_id=task_id, job_id=job_id),
                timeout=self._bridge_timeout_seconds,
            )
        except (TypeError, ValueError):
            return _error("RAG_WAIT_ARGUMENTS_INVALID", "job_id 必须是有效 UUID", False)
        except RagIngestionMonitorError as exc:
            return _error(exc.code, str(exc), exc.recoverable)
        except Exception:
            return _error("RAG_WAIT_FAILED", "暂时无法确认 RAG 入库状态", True)

        data = {
            "job_id": str(result.job_id),
            "document_id": str(result.document_id),
            "status": result.status.value,
            "document_status": result.document_status.value,
            "chunk_count": result.chunk_count,
            "embedding_completed": result.embedding_completed,
            "ready": result.ready,
        }
        if result.status in {RagIngestionStatus.FAILED, RagIngestionStatus.CANCELLED}:
            return ToolResult(
                ok=False,
                kind="json",
                summary=f"RAG 入库未完成：status={result.status.value}",
                data=data,
                error={
                    "code": "RAG_INGESTION_TERMINAL_FAILURE",
                    "message": "RAG 入库作业已失败或取消",
                    "category": "tool",
                    "recoverable": False,
                },
            )
        return ToolResult(
            ok=True,
            kind="json",
            summary=(
                f"RAG 入库已完成：document_id={result.document_id}, "
                f"chunks={result.chunk_count}, vectors={result.embedding_completed}"
            ),
            data=data,
            metadata={"document_id": str(result.document_id), "job_id": str(result.job_id)},
        )


def _error(code: str, message: str, recoverable: bool) -> ToolResult:
    return ToolResult(
        ok=False,
        kind="empty",
        summary=message,
        error={
            "code": code,
            "message": message,
            "category": "tool",
            "recoverable": recoverable,
        },
    )
