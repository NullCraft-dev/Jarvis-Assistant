"""workspace file deliverable 的安全读取与 Application Service 预览测试。"""

from __future__ import annotations

from hashlib import sha256
from types import SimpleNamespace
from uuid import uuid4

import pytest

from jarvis_worker.agent.artifacts import service as artifact_service_module
from jarvis_worker.agent.artifacts.service import ArtifactApplicationService
from jarvis_worker.shared.errors.application import AppError
from jarvis_worker.agent.artifacts.file_store import LocalArtifactFileStore
from jarvis_worker.agent.artifacts.workspace_file_reader import WorkspaceArtifactFileReader
from jarvis_worker.shared.domain.models import Artifact, Task, ToolCall


def test_workspace_artifact_reader_validates_content_and_hash(tmp_path):
    content = "# deliverable\nverified"
    target = tmp_path / "reports"
    target.mkdir()
    (target / "result.md").write_text(content, encoding="utf-8")
    data = content.encode("utf-8")

    result = WorkspaceArtifactFileReader().read_text(
        str(tmp_path),
        "reports/result.md",
        expected_size_bytes=len(data),
        expected_sha256=sha256(data).hexdigest(),
    )

    assert result.content == content
    assert result.size_bytes == len(data)


@pytest.mark.parametrize("relative_path", ["../secret.md", "/tmp/secret.md", "a/../b.md"])
def test_workspace_artifact_reader_rejects_path_escape(tmp_path, relative_path):
    with pytest.raises(ValueError, match="相对路径"):
        WorkspaceArtifactFileReader().read_text(
            str(tmp_path),
            relative_path,
            expected_size_bytes=0,
            expected_sha256=sha256(b"").hexdigest(),
        )


def test_workspace_artifact_reader_rejects_symlink_and_changed_content(tmp_path):
    original = tmp_path / "original.md"
    original.write_text("original", encoding="utf-8")
    link = tmp_path / "link.md"
    link.symlink_to(original)
    reader = WorkspaceArtifactFileReader()

    with pytest.raises(OSError):
        reader.read_text(
            str(tmp_path),
            "link.md",
            expected_size_bytes=8,
            expected_sha256=sha256(b"original").hexdigest(),
        )
    with pytest.raises(ValueError, match="哈希"):
        reader.read_text(
            str(tmp_path),
            "original.md",
            expected_size_bytes=8,
            expected_sha256=sha256(b"different").hexdigest(),
        )


class _AsyncSessionContext:
    async def __aenter__(self):
        return object()

    async def __aexit__(self, *_args):
        return None


class _SessionFactory:
    def __call__(self):
        return _AsyncSessionContext()


class _Repo:
    def __init__(self, value):
        self.value = value

    async def get(self, _record_id):
        return self.value


def _workspace_records(tmp_path):
    task_id, run_id, tool_call_id, artifact_id = (uuid4() for _ in range(4))
    content = "workspace preview"
    target = tmp_path / "preview.txt"
    target.write_text(content, encoding="utf-8")
    data = content.encode("utf-8")
    artifact = Artifact(
        id=artifact_id,
        task_id=task_id,
        run_id=run_id,
        kind="file",
        title="preview.txt",
        purpose="deliverable",
        producer_type="tool",
        source_tool_call_id=tool_call_id,
        file_size_bytes=len(data),
        mime_type="text/plain; charset=utf-8",
        content_hash=sha256(data).hexdigest(),
        metadata={
            "storage": "workspace",
            "workspace_relative_path": "preview.txt",
        },
    )
    task = Task(
        id=task_id,
        title="preview",
        user_goal="preview",
        conversation_id=uuid4(),
        workspace_path=str(tmp_path),
    )
    tool_call = ToolCall(
        id=tool_call_id,
        task_id=task_id,
        run_id=run_id,
        step_id=uuid4(),
        provider="native",
        tool_name="workspace.create_file",
        risk_level="L2",
        arguments={},
        result={
            "artifact_ids": [str(artifact_id)],
            "data": {
                "created": True,
                "path": "preview.txt",
                "size_bytes": len(data),
                "sha256": artifact.content_hash,
            },
            "deliverables": [{
                "kind": "file",
                "title": "preview.txt",
                "path": "preview.txt",
                "size_bytes": len(data),
                "mime_type": artifact.mime_type,
                "content_hash": artifact.content_hash,
            }],
        },
        status="completed",
    )
    return artifact, task, tool_call, content


@pytest.mark.asyncio
async def test_artifact_service_previews_trusted_workspace_deliverable(
    tmp_path, monkeypatch
):
    artifact, task, tool_call, content = _workspace_records(tmp_path)
    fake_uow = SimpleNamespace(
        artifacts=_Repo(artifact),
        tasks=_Repo(task),
        tool_calls=_Repo(tool_call),
    )
    monkeypatch.setattr(
        artifact_service_module, "PostgresUnitOfWork", lambda _session: fake_uow
    )
    service = ArtifactApplicationService(
        lambda: _SessionFactory(),
        file_store=LocalArtifactFileStore(tmp_path / "artifact-store"),
        workspace_file_reader=WorkspaceArtifactFileReader(),
    )

    loaded_artifact, loaded_content = await service.get_with_content(artifact.id)

    assert loaded_artifact.id == artifact.id
    assert loaded_content == content


@pytest.mark.asyncio
async def test_artifact_service_rejects_broken_tool_lineage(tmp_path, monkeypatch):
    artifact, task, tool_call, _ = _workspace_records(tmp_path)
    tool_call.result = {"artifact_ids": []}
    fake_uow = SimpleNamespace(
        artifacts=_Repo(artifact),
        tasks=_Repo(task),
        tool_calls=_Repo(tool_call),
    )
    monkeypatch.setattr(
        artifact_service_module, "PostgresUnitOfWork", lambda _session: fake_uow
    )
    service = ArtifactApplicationService(
        lambda: _SessionFactory(),
        file_store=LocalArtifactFileStore(tmp_path / "artifact-store"),
        workspace_file_reader=WorkspaceArtifactFileReader(),
    )

    with pytest.raises(AppError) as error:
        await service.get_with_content(artifact.id)
    assert error.value.code == "ARTIFACT_INTEGRITY_ERROR"
