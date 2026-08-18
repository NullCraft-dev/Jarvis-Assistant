from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

import pytest

from jarvis_worker.agent.knowledge.file_store import ObsidianVaultFileStore
from jarvis_worker.agent.knowledge.index_renderer import KnowledgeIndexRenderer
from jarvis_worker.agent.knowledge.markdown import normalize_obsidian_markdown
from jarvis_worker.agent.knowledge.naming import KnowledgeDocumentNamingPolicy
from jarvis_worker.agent.knowledge.service import (
    _bounded_string_list,
    _required_bounded_text,
    _validate_provenance_links,
)
from jarvis_worker.shared.errors.application import AppError


@dataclass
class _Doc:
    title: str
    relative_path: str
    kind: object
    created_at: datetime


def _relative(title: str, kind: str) -> str:
    return KnowledgeDocumentNamingPolicy().relative_path(title=title, kind=kind)


def test_initializes_isolated_vault_and_creates_markdown(tmp_path: Path):
    root = tmp_path / "Jarvis"
    root.mkdir()
    store = ObsidianVaultFileStore()
    store.initialize(str(root.resolve()))

    assert {".obsidian", "Reports", "Notes", "Sources"}.issubset(
        {item.name for item in root.iterdir() if item.is_dir()}
    )
    relative, digest, size = store.create_markdown(
        str(root.resolve()), uuid4(),
        relative_path=_relative("周报 / 安全", "report"),
        title="周报 / 安全", kind="report",
        content="这是正文。", tags=["AI", "安全"],
    )
    target = root / relative
    assert target.is_file()
    assert relative == "Reports/周报 - 安全.md"
    assert not any(character.isdigit() for character in Path(relative).stem)
    assert len(digest) == 64 and size == len(target.read_bytes())
    assert "# 周报 / 安全" in target.read_text()


def test_index_groups_document_kinds_and_uses_kind_specific_sorting(tmp_path: Path):
    root = tmp_path / "Jarvis"
    root.mkdir()
    store = ObsidianVaultFileStore()
    store.initialize(str(root.resolve()))
    report_kind = type("Kind", (), {"value": "report"})()
    note_kind = type("Kind", (), {"value": "note"})()
    source_kind = type("Kind", (), {"value": "source"})()
    now = datetime.now(timezone.utc)
    documents = [
        _Doc("旧报告", "Reports/旧报告.md", report_kind, now.replace(year=2025)),
        _Doc("新报告", "Reports/新报告.md", report_kind, now),
        _Doc("B 笔记", "Notes/B 笔记.md", note_kind, now),
        _Doc("A 笔记", "Notes/A 笔记.md", note_kind, now),
        _Doc("来源", "Sources/来源.md", source_kind, now),
    ]
    store.write_index(str(root.resolve()), KnowledgeIndexRenderer().render(documents))
    index = (root / "索引.md").read_text()

    assert index.index("## 报告") < index.index("## 笔记") < index.index("## 来源")
    assert index.index("[[Reports/新报告|新报告]]") < index.index("[[Reports/旧报告|旧报告]]")
    assert index.index("[[Notes/A 笔记|A 笔记]]") < index.index("[[Notes/B 笔记|B 笔记]]")


def test_rejects_symlinked_vault_root(tmp_path: Path):
    actual = tmp_path / "actual"
    actual.mkdir()
    linked = tmp_path / "Jarvis"
    linked.symlink_to(actual, target_is_directory=True)
    with pytest.raises(AppError) as error:
        ObsidianVaultFileStore().initialize(str(linked))
    assert error.value.code == "KNOWLEDGE_VAULT_UNAVAILABLE"


def test_rejects_kind_directory_replaced_by_symlink(tmp_path: Path):
    root = tmp_path / "Jarvis"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    store = ObsidianVaultFileStore()
    store.initialize(str(root.resolve()))
    (root / "Reports").rmdir()
    (root / "Reports").symlink_to(outside, target_is_directory=True)
    with pytest.raises(AppError) as error:
        store.create_markdown(
            str(root.resolve()), uuid4(), relative_path="Reports/escape.md",
            title="escape", kind="report", content="body", tags=[],
        )
    assert error.value.code == "KNOWLEDGE_PATH_UNSAFE"
    assert list(outside.iterdir()) == []


def test_rejects_oversized_markdown(tmp_path: Path):
    root = tmp_path / "Jarvis"
    root.mkdir()
    store = ObsidianVaultFileStore()
    store.initialize(str(root.resolve()))
    with pytest.raises(AppError) as error:
        store.create_markdown(
            str(root.resolve()), uuid4(), relative_path="Notes/large.md",
            title="large", kind="note", content="x" * (513 * 1024), tags=[],
        )
    assert error.value.code == "KNOWLEDGE_DOCUMENT_TOO_LARGE"


def test_markdown_records_trusted_artifact_and_rag_relationship(tmp_path: Path):
    root = tmp_path / "Jarvis"
    root.mkdir()
    store = ObsidianVaultFileStore()
    store.initialize(str(root.resolve()))
    artifact_id = str(uuid4())
    document_id = str(uuid4())
    job_id = str(uuid4())

    relative, _, _ = store.create_markdown(
        str(root.resolve()), uuid4(), relative_path="Reports/Agent Memory.md",
        title="Agent Memory", kind="report",
        content="摘要结论。", tags=["memory"],
        source_urls=["https://arxiv.org/abs/2607.24368v1"],
        provenance_links=[{
            "source_id": "arxiv:2607.24368v1",
            "source_url": "https://arxiv.org/abs/2607.24368v1",
            "artifact_id": artifact_id,
            "artifact_sha256": "a" * 64,
            "rag_document_id": document_id,
            "rag_job_id": job_id,
            "rag_status": "pending",
        }],
    )

    markdown = (root / relative).read_text()
    assert "## Jarvis Provenance" in markdown
    assert "[arxiv:2607.24368v1](https://arxiv.org/abs/2607.24368v1)" in markdown
    assert artifact_id in markdown
    assert document_id in markdown
    assert job_id in markdown
    assert "pending" in markdown


def test_markdown_records_rag_retrieval_evidence_without_fake_ingestion_status(
    tmp_path: Path,
):
    root = tmp_path / "Jarvis"
    root.mkdir()
    store = ObsidianVaultFileStore()
    store.initialize(str(root.resolve()))
    artifact_id = str(uuid4())
    document_id = str(uuid4())
    tool_call_id = str(uuid4())
    chunk_id = str(uuid4())

    links = _validate_provenance_links([{
        "artifact_id": artifact_id,
        "rag_document_id": document_id,
        "rag_search_tool_call_id": tool_call_id,
        "rag_chunk_id": chunk_id,
    }])
    relative, _, _ = store.create_markdown(
        str(root.resolve()), uuid4(), relative_path="Notes/RAG evidence.md",
        title="RAG evidence", kind="note",
        content="检索摘要。", tags=[], provenance_links=links,
    )

    markdown = (root / relative).read_text()
    assert "检索 ToolCall" in markdown
    assert tool_call_id in markdown
    assert chunk_id in markdown
    assert "not_submitted" not in markdown


def test_knowledge_provenance_rejects_incomplete_rag_retrieval_identity():
    with pytest.raises(AppError, match="RAG 检索关联结构无效"):
        _validate_provenance_links([{
            "artifact_id": str(uuid4()),
            "rag_document_id": str(uuid4()),
            "rag_chunk_id": str(uuid4()),
        }])


def test_obsidian_math_normalization_skips_code_escaped_and_unbalanced_delimiters():
    source = r"""Inline \(x + y\) and existing $z$.

\[
\frac{1}{N}
\]

`code \(not_math\)`

``multiline
\(also_not_math\)
code``

```md
\[also_not_math\]
```

Before \(must_not_pair
```text
protected
```
after must_not_pair\).

Escaped \\(literal\\) and unbalanced \(opening.
"""

    normalized = normalize_obsidian_markdown(source)

    assert "Inline $x + y$ and existing $z$." in normalized
    assert "$$\n\\frac{1}{N}\n$$" in normalized
    assert r"`code \(not_math\)`" in normalized
    assert "``multiline\n\\(also_not_math\\)\ncode``" in normalized
    assert "```md\n\\[also_not_math\\]\n```" in normalized
    assert "Before \\(must_not_pair" in normalized
    assert r"after must_not_pair\)" in normalized
    assert r"Escaped \\(literal\\)" in normalized
    assert r"unbalanced \(opening" in normalized


def test_pure_semantic_name_collisions_never_overwrite_existing_file(tmp_path: Path):
    root = tmp_path / "Jarvis"
    root.mkdir()
    store = ObsidianVaultFileStore()
    store.initialize(str(root.resolve()))
    relative = _relative("MobileNet 深度可分离卷积", "note")

    store.create_markdown(
        str(root.resolve()), uuid4(), relative_path=relative,
        title="MobileNet 深度可分离卷积", kind="note", content="first", tags=[],
    )
    with pytest.raises(AppError) as error:
        store.create_markdown(
            str(root.resolve()), uuid4(), relative_path=relative,
            title="MobileNet 深度可分离卷积", kind="note", content="second", tags=[],
        )

    assert error.value.code == "KNOWLEDGE_DOCUMENT_EXISTS"
    assert "first" in (root / relative).read_text()
    assert "second" not in (root / relative).read_text()


def test_knowledge_service_owns_document_validation_without_skill_tool():
    assert _required_bounded_text("  Durable note  ", "标题", 200) == "Durable note"
    assert _bounded_string_list(
        ["AI", "AI", "Memory"], "标签", 20, 64
    ) == ["AI", "Memory"]

    with pytest.raises(AppError) as empty:
        _required_bounded_text("   ", "正文", 200_000)
    with pytest.raises(AppError) as too_many:
        _bounded_string_list(["x"] * 21, "标签", 20, 64)

    assert empty.value.code == "VALIDATION_ERROR"
    assert too_many.value.code == "VALIDATION_ERROR"
