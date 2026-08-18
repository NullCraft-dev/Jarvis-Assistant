"""Renderer for the Jarvis-managed Obsidian vault index."""

from __future__ import annotations

from collections.abc import Iterable

from jarvis_worker.shared.domain.models import KnowledgeDocument

_SECTIONS = (
    ("report", "报告", "暂无报告。"),
    ("note", "笔记", "暂无笔记。"),
    ("source", "来源", "暂无来源文档。"),
)


class KnowledgeIndexRenderer:
    def render(self, documents: Iterable[KnowledgeDocument]) -> str:
        grouped: dict[str, list[KnowledgeDocument]] = {kind: [] for kind, _, _ in _SECTIONS}
        for document in documents:
            kind = document.kind.value
            if kind in grouped:
                grouped[kind].append(document)

        lines = ["# Jarvis 知识库", "", "由 Jarvis 自动维护。", ""]
        for kind, label, empty_message in _SECTIONS:
            lines.extend([f"## {label}", ""])
            items = grouped[kind]
            if kind == "report":
                items.sort(
                    key=lambda item: (
                        -item.created_at.timestamp(),
                        item.title.casefold(),
                    )
                )
            else:
                items.sort(key=lambda item: item.title.casefold())
            if not items:
                lines.extend([empty_message, ""])
                continue
            for document in items:
                note_path = document.relative_path.removesuffix(".md")
                alias = document.title.replace("|", "\\|").replace("]", "\\]")
                lines.append(f"- [[{note_path}|{alias}]]")
            lines.append("")
        return "\n".join(lines)
