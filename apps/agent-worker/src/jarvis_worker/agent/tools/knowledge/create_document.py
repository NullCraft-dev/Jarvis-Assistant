from __future__ import annotations

from uuid import UUID

from jarvis_worker.agent.knowledge.service import CreateKnowledgeDocumentInput
from jarvis_worker.agent.tool_gateway.contracts import ToolRequest, ToolResult
from jarvis_worker.shared.errors.application import AppError


class KnowledgeDocumentToolExecutor:
    """Sync ToolGateway adapter backed by the Worker's shared async service loop."""

    def __init__(self, service, async_bridge):
        self._service = service
        self._bridge = async_bridge

    def __call__(self, request: ToolRequest) -> ToolResult:
        args = request.arguments
        tags = args.get("tags") or []
        source_urls = args.get("source_urls") or []
        provenance_links = args.get("provenance_links") or []
        if (
            not isinstance(tags, list)
            or not all(isinstance(tag, str) for tag in tags)
            or not isinstance(source_urls, list)
            or not all(isinstance(url, str) for url in source_urls)
            or not isinstance(provenance_links, list)
            or not all(isinstance(link, dict) for link in provenance_links)
        ):
            return self._error(
                "TOOL_ARGUMENTS_INVALID", "tags、source_urls 或 provenance_links 格式无效",
                "validation", True,
            )
        try:
            task_id = UUID(request.task_id)
            run_id = UUID(request.run_id)
            vault_id = UUID(str(args["vault_id"])) if args.get("vault_id") else None
        except (TypeError, ValueError):
            return self._error("TOOL_ARGUMENTS_INVALID", "vault_id 或运行来源 ID 无效", "validation", True)
        try:
            if vault_id is None:
                vaults = self._bridge.run(self._service.list_vaults(), timeout=10)
                if len(vaults) != 1:
                    return self._error("KNOWLEDGE_VAULT_REQUIRED", "需要唯一的 active Jarvis Vault", "validation", True)
                vault_id = vaults[0].id
            document = self._bridge.run(self._service.create_document(
                vault_id,
                CreateKnowledgeDocumentInput(
                    title=str(args.get("title", "")), kind=str(args.get("kind", "")),
                    content=str(args.get("content", "")), tags=tags,
                    source_urls=source_urls,
                    provenance_links=provenance_links,
                    source_task_id=task_id, source_run_id=run_id,
                    permission_decision=(
                        "scheduled_task"
                        if request.authorization_scope.get("type") == "scheduled_task"
                        else "allow_once"
                    ),
                ),
            ), timeout=30)
        except AppError as exc:
            return self._error(exc.code, exc.message, exc.category, exc.recoverable)
        return ToolResult(
            ok=True, kind="json", summary=f"已保存到 Obsidian: {document.title}",
            data={
                "document_id": str(document.id), "vault_id": str(document.vault_id),
                "title": document.title, "kind": document.kind.value,
                "relative_path": document.relative_path, "content_hash": document.content_hash,
                "size_bytes": document.size_bytes,
            },
            metadata={"knowledge_document_id": str(document.id)},
        )

    @staticmethod
    def _error(code: str, message: str, category: str, recoverable: bool) -> ToolResult:
        return ToolResult(ok=False, summary=message, error={"code": code, "message": message, "category": category, "recoverable": recoverable})
