"""Narrow filesystem adapter for a dedicated Jarvis Obsidian Vault."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from pathlib import Path
from uuid import UUID

from jarvis_worker.agent.knowledge.naming import KIND_DIRECTORIES
from jarvis_worker.shared.errors.application import AppError

MAX_MARKDOWN_BYTES = 512 * 1024


class ObsidianVaultFileStore:
    def initialize(self, canonical_root: str) -> None:
        root = self._root(canonical_root)
        for name in (".obsidian", *KIND_DIRECTORIES.values()):
            target = root / name
            if target.exists() and (target.is_symlink() or not target.is_dir()):
                raise AppError("KNOWLEDGE_PATH_UNSAFE", f"知识库目录不安全: {name}", "permission")
            target.mkdir(mode=0o700, exist_ok=True)

    def ensure_index(self, canonical_root: str, content: str) -> None:
        target = self._root(canonical_root) / "索引.md"
        if not target.exists():
            self._atomic_write(target, content)

    def write_index(self, canonical_root: str, content: str) -> None:
        self._atomic_write(self._root(canonical_root) / "索引.md", content)

    def create_markdown(self, canonical_root: str, document_id: UUID, *, relative_path: str, title: str, kind: str, content: str, tags: list[str], source_urls: list[str] | None = None, provenance_links: list[dict[str, str]] | None = None, source_task_id: UUID | None = None, source_run_id: UUID | None = None) -> tuple[str, str, int]:
        root = self._root(canonical_root)
        directory = KIND_DIRECTORIES.get(kind)
        if not directory:
            raise AppError("VALIDATION_ERROR", "不支持的知识文档类型", "validation")
        target_directory = self._safe_directory(root, directory)
        title = title.strip()
        content = content.strip()
        if not title or not content:
            raise AppError("VALIDATION_ERROR", "标题和正文不能为空", "validation")
        relative = self._validate_relative_path(relative_path, directory)
        target = target_directory / Path(relative).name
        payload = self._render(
            document_id, title, kind, content, tags, source_urls or [],
            provenance_links or [], source_task_id, source_run_id,
        )
        data = payload.encode("utf-8")
        if len(data) > MAX_MARKDOWN_BYTES:
            raise AppError("KNOWLEDGE_DOCUMENT_TOO_LARGE", "Markdown 文档不能超过 512 KiB", "validation")
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        try:
            fd = os.open(target, flags, 0o600)
            with os.fdopen(fd, "wb") as output:
                output.write(data)
                output.flush()
                os.fsync(output.fileno())
        except FileExistsError:
            raise AppError(
                "KNOWLEDGE_DOCUMENT_EXISTS", "同名知识文档已存在，未执行覆盖", "storage"
            )
        return relative, hashlib.sha256(data).hexdigest(), len(data)

    def delete_created(self, canonical_root: str, relative_path: str) -> None:
        target = self._root(canonical_root) / relative_path
        if target.is_file() and not target.is_symlink():
            target.unlink()

    @staticmethod
    def _render(document_id: UUID, title: str, kind: str, content: str, tags: list[str], source_urls: list[str], provenance_links: list[dict[str, str]], source_task_id: UUID | None, source_run_id: UUID | None) -> str:
        safe_tags = [tag.strip()[:50] for tag in tags[:20] if tag.strip()]
        safe_sources = [url.strip()[:2048] for url in source_urls[:50] if url.strip()]
        lines = [
            "---", f"jarvis_id: {document_id}", f"title: {json.dumps(title, ensure_ascii=False)}",
            f"type: {kind}", f"tags: {json.dumps(safe_tags, ensure_ascii=False)}",
            f"sources: {json.dumps(safe_sources, ensure_ascii=False)}",
        ]
        if source_task_id is not None:
            lines.append(f"source_task_id: {source_task_id}")
        if source_run_id is not None:
            lines.append(f"source_run_id: {source_run_id}")
        lines.extend(["---", "", f"# {title}", "", content, ""])
        if provenance_links:
            lines.extend([
                "## Jarvis Provenance",
                "",
                "以下关联由 Jarvis 根据本次运行的可信工具结果生成。",
                "",
                "| 原文来源 | Artifact | RAG 文档 | RAG 作业 | 检索 ToolCall | RAG Chunk | 状态 |",
                "| --- | --- | --- | --- | --- | --- | --- |",
            ])
            for link in provenance_links[:50]:
                source_id = _table_cell(link.get("source_id", ""))
                source_url = link.get("source_url", "")
                source = f"[{source_id}]({source_url})" if source_url else source_id
                lines.append(
                    "| " + " | ".join([
                        source,
                        _code_cell(link.get("artifact_id", "")),
                        _code_cell(link.get("rag_document_id", "")),
                        _code_cell(link.get("rag_job_id", "")),
                        _code_cell(link.get("rag_search_tool_call_id", "")),
                        _code_cell(link.get("rag_chunk_id", "")),
                        _table_cell(
                            link.get(
                                "rag_status",
                                "" if link.get("rag_chunk_id") else "not_submitted",
                            )
                        ),
                    ]) + " |"
                )
            lines.append("")
        return "\n".join(lines)

    @staticmethod
    def _validate_relative_path(relative_path: str, directory: str) -> str:
        if not isinstance(relative_path, str) or not relative_path:
            raise AppError("KNOWLEDGE_PATH_UNSAFE", "知识文档路径无效", "permission")
        path = Path(relative_path)
        if (
            path.is_absolute()
            or len(path.parts) != 2
            or path.parts[0] != directory
            or path.suffix != ".md"
            or path.name in {"", ".md"}
            or path.as_posix() != relative_path
        ):
            raise AppError("KNOWLEDGE_PATH_UNSAFE", "知识文档路径无效", "permission")
        return relative_path

    @staticmethod
    def _root(canonical_root: str) -> Path:
        root = Path(canonical_root)
        if not root.is_absolute() or not root.is_dir() or root.is_symlink() or str(root.resolve()) != canonical_root:
            raise AppError("KNOWLEDGE_VAULT_UNAVAILABLE", "知识库路径不可用或已被替换", "permission")
        return root

    @staticmethod
    def _safe_directory(root: Path, name: str) -> Path:
        directory = root / name
        if not directory.is_dir() or directory.is_symlink() or directory.resolve() != root / name:
            raise AppError("KNOWLEDGE_PATH_UNSAFE", f"知识库目录不安全: {name}", "permission")
        return directory

    @staticmethod
    def _atomic_write(target: Path, content: str) -> None:
        if target.exists() and (target.is_symlink() or not target.is_file()):
            raise AppError("KNOWLEDGE_PATH_UNSAFE", "知识库索引路径不安全", "permission")
        fd, temp_name = tempfile.mkstemp(prefix=".jarvis-index-", dir=target.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as output:
                output.write(content)
                output.flush()
                os.fsync(output.fileno())
            os.chmod(temp_name, 0o600)
            os.replace(temp_name, target)
        finally:
            if os.path.exists(temp_name):
                os.unlink(temp_name)


def _table_cell(value: str) -> str:
    return value.replace("|", "\\|").replace("\n", " ").strip()


def _code_cell(value: str) -> str:
    safe = _table_cell(value).replace("`", "")
    return f"`{safe}`" if safe else "—"
