"""将当前 Workspace 内的受控 PDF Artifact 加入 RAG 摄取队列。"""

from __future__ import annotations

from uuid import UUID

from jarvis_worker.agent.rag.ingestion import RagIngestionError
from jarvis_worker.agent.tool_gateway.contracts import ToolRequest, ToolResult


class RagIngestArtifactToolExecutor:
    def __init__(self, service, async_bridge) -> None:
        self._service = service
        self._bridge = async_bridge

    def __call__(self, request: ToolRequest) -> ToolResult:
        try:
            task_id = UUID(request.task_id)
            artifact_id = UUID(str(request.arguments.get("artifact_id", "")))
            result = self._bridge.run(
                self._service.enqueue_pdf_for_task(
                    task_id=task_id,
                    source_artifact_id=artifact_id,
                ),
                timeout=30,
            )
        except (TypeError, ValueError):
            return _error(
                "RAG_INGEST_ARGUMENTS_INVALID",
                "artifact_id 必须是有效 UUID",
                recoverable=False,
            )
        except RagIngestionError as exc:
            return _error(exc.code, str(exc), recoverable=exc.recoverable)
        except Exception:
            return _error(
                "RAG_INGEST_FAILED",
                "RAG 文档暂时无法进入摄取队列",
                recoverable=True,
            )

        action = "已创建" if result.created else "已存在"
        document_id = str(result.document_id)
        job_id = str(result.job_id)
        return ToolResult(
            ok=True,
            kind="json",
            summary=(
                f"RAG 摄取作业{action}：document_id={document_id}, "
                f"job_id={job_id}, status={result.status.value}"
            ),
            data={
                "artifact_id": str(artifact_id),
                "document_id": document_id,
                "job_id": job_id,
                "status": result.status.value,
                "created": result.created,
            },
            metadata={
                "document_id": document_id,
                "job_id": job_id,
            },
        )


def _error(code: str, message: str, *, recoverable: bool) -> ToolResult:
    return ToolResult(
        ok=False,
        summary=message,
        error={
            "code": code,
            "message": message,
            "category": "tool",
            "recoverable": recoverable,
        },
    )
