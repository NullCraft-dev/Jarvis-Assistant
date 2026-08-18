"""Application service for the isolated Jarvis Obsidian Vault."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse
from uuid import UUID

from jarvis_worker.agent.knowledge.file_store import ObsidianVaultFileStore
from jarvis_worker.agent.knowledge.index_renderer import KnowledgeIndexRenderer
from jarvis_worker.agent.knowledge.markdown import normalize_obsidian_markdown
from jarvis_worker.agent.knowledge.naming import KnowledgeDocumentNamingPolicy
from jarvis_worker.database.unit_of_work import PostgresUnitOfWork
from jarvis_worker.runtime.workspaces.workspace_service import validate_path_for_registration
from jarvis_worker.shared.domain.models import (
    AuditLog,
    KnowledgeDocument,
    KnowledgeDocumentKind,
    KnowledgeVault,
    new_id,
    utcnow,
)
from jarvis_worker.shared.errors.application import AppError


@dataclass(frozen=True)
class CreateKnowledgeDocumentInput:
    title: str
    kind: str
    content: str
    tags: list[str]
    source_urls: list[str] | None = None
    provenance_links: list[dict[str, str]] | None = None
    source_task_id: UUID | None = None
    source_run_id: UUID | None = None
    permission_decision: str = "user_explicit"


class KnowledgeApplicationService:
    def __init__(self, uow_factory, file_store: ObsidianVaultFileStore | None = None):
        self._uow_factory = uow_factory
        self._files = file_store or ObsidianVaultFileStore()
        self._index = KnowledgeIndexRenderer()
        self._naming = KnowledgeDocumentNamingPolicy()

    @staticmethod
    def suggested_path() -> str:
        return str(Path(os.getenv("JARVIS_OBSIDIAN_VAULT_PATH", "~/Documents/obsidian/Jarvis")).expanduser())

    async def list_vaults(self) -> list[KnowledgeVault]:
        async with self._uow_factory()() as session:
            return await PostgresUnitOfWork(session).knowledge_vaults.list_active()

    async def connect(self, raw_path: str) -> KnowledgeVault:
        canonical = validate_path_for_registration(raw_path)
        if Path(canonical).name.casefold() != "jarvis":
            raise AppError("KNOWLEDGE_VAULT_NAME_REQUIRED", "Jarvis 专用知识库目录必须命名为 Jarvis", "validation")
        self._files.initialize(canonical)
        self._files.ensure_index(canonical, self._index.render([]))
        now = utcnow()
        candidate = KnowledgeVault(
            id=new_id(), name="Jarvis", root_path=raw_path, canonical_path=canonical,
            created_at=now, updated_at=now,
        )
        async with self._uow_factory()() as session:
            uow = PostgresUnitOfWork(session)
            async with uow.transaction() as tx:
                inserted = await tx.knowledge_vaults.insert_if_absent(candidate)
                vault = candidate if inserted else await tx.knowledge_vaults.get_by_canonical_path(canonical)
                if vault is None:
                    raise AppError("STORAGE_ERROR", "知识库注册失败", "storage", recoverable=True)
                await tx.knowledge_vaults.activate_exclusive(vault.id, now)
                await tx.audits.create(AuditLog(
                    id=new_id(), event_type="knowledge.vault.connected", actor="user",
                    risk_level="L2", permission_decision="user_explicit",
                    action_summary="连接并切换当前 Jarvis 专用 Obsidian 知识库",
                    details={"vault_id": str(vault.id), "canonical_path": canonical},
                    result_summary="当前知识库已切换并初始化",
                ))
                await tx.commit()
                active = await tx.knowledge_vaults.get(vault.id)
                if active is None:
                    raise AppError("STORAGE_ERROR", "知识库切换失败", "storage", recoverable=True)
                return active

    async def list_documents(self, vault_id: UUID, limit: int = 100) -> list[KnowledgeDocument]:
        async with self._uow_factory()() as session:
            uow = PostgresUnitOfWork(session)
            if await uow.knowledge_vaults.get(vault_id) is None:
                raise AppError("KNOWLEDGE_VAULT_NOT_FOUND", "知识库不存在", "not_found")
            return await uow.knowledge_documents.list_by_vault(vault_id, min(max(limit, 1), 100))

    async def create_document(self, vault_id: UUID, input: CreateKnowledgeDocumentInput) -> KnowledgeDocument:
        title = _required_bounded_text(input.title, "标题", 200)
        content = normalize_obsidian_markdown(
            _required_bounded_text(input.content, "正文", 200_000)
        )
        try:
            kind = KnowledgeDocumentKind(input.kind)
        except ValueError:
            raise AppError("VALIDATION_ERROR", "不支持的知识文档类型", "validation")
        tags = _bounded_string_list(input.tags, "标签", 20, 64)
        source_urls = _bounded_string_list(
            input.source_urls or [], "来源 URL", 50, 2048
        )
        for url in source_urls:
            parsed = urlparse(url)
            if parsed.scheme not in {"http", "https"} or not parsed.hostname:
                raise AppError(
                    "VALIDATION_ERROR",
                    "来源仅支持公开的 http 或 https URL",
                    "validation",
                )
        provenance_links = _validate_provenance_links(input.provenance_links or [])
        async with self._uow_factory()() as session:
            vault = await PostgresUnitOfWork(session).knowledge_vaults.get(vault_id)
        if vault is None or vault.status.value != "active":
            raise AppError("KNOWLEDGE_VAULT_NOT_FOUND", "知识库不存在或未启用", "not_found")

        document_id = new_id()
        relative = self._naming.relative_path(title=title, kind=kind.value)
        relative, digest, size = self._files.create_markdown(
            vault.canonical_path, document_id, relative_path=relative,
            title=title, kind=kind.value,
            content=content, tags=tags,
            source_urls=source_urls,
            provenance_links=provenance_links,
            source_task_id=input.source_task_id,
            source_run_id=input.source_run_id,
        )
        now = utcnow()
        document = KnowledgeDocument(
            id=document_id, vault_id=vault.id, title=title, kind=kind,
            relative_path=relative, content_hash=digest, size_bytes=size,
            tags=tags,
            source_urls=source_urls,
            source_task_id=input.source_task_id, source_run_id=input.source_run_id,
            created_at=now, updated_at=now,
        )
        try:
            async with self._uow_factory()() as session:
                uow = PostgresUnitOfWork(session)
                async with uow.transaction() as tx:
                    document = await tx.knowledge_documents.create(document)
                    await tx.audits.create(AuditLog(
                        id=new_id(), event_type="knowledge.document.created",
                        actor="agent" if input.source_task_id else "user",
                        task_id=input.source_task_id, run_id=input.source_run_id,
                        risk_level="L2", permission_decision=input.permission_decision,
                        action_summary=f"创建 Obsidian {kind.value} 文档",
                        details={"vault_id": str(vault.id), "document_id": str(document.id), "relative_path": relative},
                        result_summary="Markdown 文档已保存",
                    ))
                    await tx.commit()
        except Exception:
            self._files.delete_created(vault.canonical_path, relative)
            raise

        documents = await self.list_documents(vault.id)
        self._files.write_index(vault.canonical_path, self._index.render(documents))
        return document


def _required_bounded_text(value: object, label: str, maximum: int) -> str:
    if not isinstance(value, str):
        raise AppError("VALIDATION_ERROR", f"{label}必须是字符串", "validation")
    normalized = value.strip()
    if not normalized:
        raise AppError("VALIDATION_ERROR", f"{label}不能为空", "validation")
    if len(normalized) > maximum:
        raise AppError(
            "VALIDATION_ERROR", f"{label}超过长度上限", "validation"
        )
    return normalized


def _bounded_string_list(
    value: object,
    label: str,
    maximum_items: int,
    maximum_length: int,
) -> list[str]:
    if not isinstance(value, list) or len(value) > maximum_items:
        raise AppError(
            "VALIDATION_ERROR",
            f"{label}必须是最多 {maximum_items} 项的数组",
            "validation",
        )
    normalized: list[str] = []
    seen: set[str] = set()
    for item in value:
        if not isinstance(item, str) or not item.strip():
            raise AppError(
                "VALIDATION_ERROR", f"{label}必须包含非空字符串", "validation"
            )
        candidate = item.strip()
        if len(candidate) > maximum_length:
            raise AppError(
                "VALIDATION_ERROR", f"{label}单项超过长度上限", "validation"
            )
        if candidate not in seen:
            seen.add(candidate)
            normalized.append(candidate)
    return normalized


def _validate_provenance_links(values: list[dict[str, str]]) -> list[dict[str, str]]:
    if not isinstance(values, list) or len(values) > 50:
        raise AppError("VALIDATION_ERROR", "原文关联必须是最多 50 项的数组", "validation")
    allowed = {
        "source_id", "source_url", "artifact_id", "artifact_sha256",
        "rag_document_id", "rag_job_id", "rag_status",
        "rag_search_tool_call_id", "rag_chunk_id",
    }
    normalized: list[dict[str, str]] = []
    for value in values:
        if not isinstance(value, dict) or set(value) - allowed:
            raise AppError("VALIDATION_ERROR", "原文关联结构无效", "validation")
        if not all(isinstance(item, str) for item in value.values()):
            raise AppError("VALIDATION_ERROR", "原文关联字段必须是字符串", "validation")
        try:
            for field in (
                "artifact_id",
                "rag_document_id",
                "rag_job_id",
                "rag_search_tool_call_id",
                "rag_chunk_id",
            ):
                if field == "artifact_id" or value.get(field):
                    UUID(value[field])
        except (KeyError, ValueError):
            raise AppError("VALIDATION_ERROR", "原文关联 ID 无效", "validation")
        has_search_call = bool(value.get("rag_search_tool_call_id"))
        has_chunk = bool(value.get("rag_chunk_id"))
        if has_search_call != has_chunk or (has_chunk and not value.get("rag_document_id")):
            raise AppError(
                "VALIDATION_ERROR", "RAG 检索关联结构无效", "validation"
            )
        source_url = value.get("source_url", "").strip()
        parsed = urlparse(source_url)
        if source_url and (parsed.scheme not in {"http", "https"} or not parsed.hostname):
            raise AppError("VALIDATION_ERROR", "原文关联 URL 无效", "validation")
        digest = value.get("artifact_sha256", "")
        if digest and (len(digest) != 64 or any(char not in "0123456789abcdef" for char in digest)):
            raise AppError("VALIDATION_ERROR", "原文关联 SHA-256 无效", "validation")
        normalized.append({key: item.strip()[:2048] for key, item in value.items() if item.strip()})
    return normalized
