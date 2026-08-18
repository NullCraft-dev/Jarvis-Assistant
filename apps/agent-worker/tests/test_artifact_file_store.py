import multiprocessing
from datetime import datetime, timezone
from hashlib import sha256
from pathlib import Path
from uuid import UUID, uuid4

import pytest

from jarvis_worker.agent.artifacts.file_store import LocalArtifactFileStore
from jarvis_worker.runtime.service import RuntimeApplicationService
from jarvis_worker.runtime_bus.messages import RuntimeEventEnvelope
from jarvis_worker.shared.storage_capacity import (
    StorageCapacityExceeded,
    directory_size_bytes,
)

RUN_ID = uuid4()
WORKSPACE_ID = uuid4()


def _write_competing_artifact(
    root: str,
    ready_queue,
    start_event,
    result_queue,
) -> None:
    store = LocalArtifactFileStore(
        Path(root),
        max_bytes=10,
        max_run_bytes=10,
        max_workspace_bytes=10,
        max_total_bytes=10,
    )
    ready_queue.put(True)
    start_event.wait(timeout=10)
    try:
        store.write_text(
            uuid4(),
            "x" * 10,
            run_id=uuid4(),
            workspace_id=uuid4(),
        )
    except StorageCapacityExceeded as exc:
        result_queue.put(exc.code)
    else:
        result_queue.put("stored")


def test_write_and_read_text_artifact(tmp_path):
    store = LocalArtifactFileStore(tmp_path, max_bytes=1024)
    artifact_id = uuid4()
    stored = store.write_text(
        artifact_id,
        "# result\nhello",
        run_id=RUN_ID,
        workspace_id=WORKSPACE_ID,
        suffix=".md",
        mime_type="text/markdown; charset=utf-8",
    )

    assert stored.relative_path.endswith(f"{artifact_id}.md")
    assert stored.size_bytes == len("# result\nhello".encode())
    assert stored.sha256 == sha256("# result\nhello".encode()).hexdigest()
    assert store.read_text(
        stored.relative_path, expected_sha256=stored.sha256
    ) == "# result\nhello"


def test_write_and_read_binary_pdf_artifact(tmp_path):
    store = LocalArtifactFileStore(tmp_path, max_bytes=1024)
    artifact_id = uuid4()
    content = b"%PDF-1.7\nbinary"
    stored = store.write_bytes(
        artifact_id,
        content,
        run_id=RUN_ID,
        workspace_id=WORKSPACE_ID,
        suffix=".pdf",
        mime_type="application/pdf",
    )

    assert stored.relative_path.endswith(f"{artifact_id}.pdf")
    assert store.read_bytes(
        stored.relative_path, expected_sha256=stored.sha256
    ) == content


@pytest.mark.parametrize("path", ["../secret", "/tmp/secret"])
def test_read_rejects_path_escape(tmp_path, path):
    store = LocalArtifactFileStore(tmp_path)
    with pytest.raises(ValueError, match="非法|越界"):
        store.read_text(path)


def test_write_rejects_oversized_content(tmp_path):
    store = LocalArtifactFileStore(tmp_path, max_bytes=4)
    with pytest.raises(ValueError, match="上限"):
        store.write_text(
            uuid4(),
            "12345",
            run_id=RUN_ID,
            workspace_id=WORKSPACE_ID,
        )


def test_read_rejects_hash_mismatch(tmp_path):
    store = LocalArtifactFileStore(tmp_path)
    stored = store.write_text(
        uuid4(),
        "content",
        run_id=RUN_ID,
        workspace_id=WORKSPACE_ID,
    )
    with pytest.raises(ValueError, match="哈希不一致"):
        store.read_text(stored.relative_path, expected_sha256="0" * 64)


def test_capacity_scan_is_bounded_and_fails_closed(tmp_path):
    for index in range(3):
        (tmp_path / f"{index}.txt").write_text("x")

    with pytest.raises(StorageCapacityExceeded) as error:
        directory_size_bytes(tmp_path, max_entries=2)

    assert error.value.code == "STORAGE_CAPACITY_SCAN_LIMIT_EXCEEDED"
    assert error.value.limit == 2
    assert error.value.unit == "entries"


def test_artifact_store_enforces_run_workspace_and_total_capacity(tmp_path):
    workspace_a, workspace_b = uuid4(), uuid4()
    run_a, run_b, run_c = uuid4(), uuid4(), uuid4()

    run_store = LocalArtifactFileStore(
        tmp_path / "run",
        max_bytes=10,
        max_run_bytes=15,
        max_workspace_bytes=20,
        max_total_bytes=25,
    )
    run_store.write_text(
        uuid4(), "a" * 10, run_id=run_a, workspace_id=workspace_a
    )
    with pytest.raises(StorageCapacityExceeded) as run_error:
        run_store.write_text(
            uuid4(), "b" * 6, run_id=run_a, workspace_id=workspace_a
        )
    assert run_error.value.code == "ARTIFACT_RUN_CAPACITY_EXCEEDED"

    workspace_store = LocalArtifactFileStore(
        tmp_path / "workspace",
        max_bytes=11,
        max_run_bytes=15,
        max_workspace_bytes=20,
        max_total_bytes=25,
    )
    workspace_store.write_text(
        uuid4(), "a" * 10, run_id=run_a, workspace_id=workspace_a
    )
    with pytest.raises(StorageCapacityExceeded) as workspace_error:
        workspace_store.write_text(
            uuid4(), "b" * 11, run_id=run_b, workspace_id=workspace_a
        )
    assert workspace_error.value.code == "ARTIFACT_WORKSPACE_CAPACITY_EXCEEDED"

    total_store = LocalArtifactFileStore(
        tmp_path / "total",
        max_bytes=13,
        max_run_bytes=15,
        max_workspace_bytes=20,
        max_total_bytes=25,
    )
    total_store.write_text(
        uuid4(), "a" * 13, run_id=run_a, workspace_id=workspace_a
    )
    with pytest.raises(StorageCapacityExceeded) as total_error:
        total_store.write_text(
            uuid4(), "b" * 13, run_id=run_c, workspace_id=workspace_b
        )
    assert total_error.value.code == "ARTIFACT_TOTAL_CAPACITY_EXCEEDED"


def test_artifact_store_overwrite_does_not_double_count_capacity(tmp_path):
    store = LocalArtifactFileStore(
        tmp_path,
        max_bytes=10,
        max_run_bytes=10,
        max_workspace_bytes=10,
        max_total_bytes=10,
    )
    artifact_id = uuid4()
    store.write_text(
        artifact_id,
        "a" * 10,
        run_id=RUN_ID,
        workspace_id=WORKSPACE_ID,
    )

    stored = store.write_text(
        artifact_id,
        "b" * 10,
        run_id=RUN_ID,
        workspace_id=WORKSPACE_ID,
    )

    assert store.read_text(stored.relative_path) == "b" * 10


def test_artifact_total_capacity_is_atomic_across_processes(tmp_path):
    context = multiprocessing.get_context("spawn")
    ready_queue = context.Queue()
    result_queue = context.Queue()
    start_event = context.Event()
    processes = [
        context.Process(
            target=_write_competing_artifact,
            args=(str(tmp_path), ready_queue, start_event, result_queue),
        )
        for _ in range(2)
    ]
    for process in processes:
        process.start()
    for _ in processes:
        assert ready_queue.get(timeout=10) is True
    start_event.set()
    for process in processes:
        process.join(timeout=10)
        assert process.exitcode == 0

    outcomes = sorted(result_queue.get(timeout=10) for _ in processes)
    assert outcomes == ["ARTIFACT_TOTAL_CAPACITY_EXCEEDED", "stored"]


def test_large_artifact_falls_back_to_bounded_inline_when_quota_is_full(tmp_path):
    store = LocalArtifactFileStore(
        tmp_path,
        max_bytes=64,
        max_run_bytes=64,
        max_workspace_bytes=64,
        max_total_bytes=64,
    )
    service = RuntimeApplicationService(
        lambda: None,
        artifact_file_store=store,
        artifact_inline_max_bytes=4,
    )
    envelope = _envelope(
        "artifact.created",
        {
            "artifact": {
                "id": str(uuid4()),
                "kind": "markdown",
                "purpose": "final_response",
                "producer": {"type": "runtime"},
                "content": "long enough",
                "metadata": {},
            }
        },
    )
    store.write_text(
        uuid4(),
        "x" * 60,
        run_id=UUID(envelope.run_id),
        workspace_id=WORKSPACE_ID,
    )

    result = service._externalize_large_artifact(
        envelope,
        workspace_id=WORKSPACE_ID,
    )
    artifact = result.runtime_event["payload"]["artifact"]

    assert artifact["content"] == "long enough"
    assert artifact["metadata"] == {
        "storage": "inline",
        "capacity_fallback": "ARTIFACT_RUN_CAPACITY_EXCEEDED",
    }


def _envelope(event_type: str, payload: dict) -> RuntimeEventEnvelope:
    task_id, run_id, event_id, trace_id = (str(uuid4()) for _ in range(4))
    return RuntimeEventEnvelope(
        event_id=event_id,
        trace_id=trace_id,
        task_id=task_id,
        run_id=run_id,
        event_type=event_type,
        runtime_event={
            "id": event_id,
            "type": event_type,
            "task_id": task_id,
            "run_id": run_id,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "payload": payload,
        },
        produced_by="worker-test",
    )


def test_large_artifact_is_replaced_with_bounded_file_reference(tmp_path):
    artifact_id = uuid4()
    service = RuntimeApplicationService(
        lambda: None,
        artifact_file_store=LocalArtifactFileStore(tmp_path),
        artifact_inline_max_bytes=4,
    )
    result = service._externalize_large_artifact(_envelope(
        "artifact.created",
        {"artifact": {
            "id": str(artifact_id),
            "kind": "markdown",
            "purpose": "final_response",
            "producer": {"type": "runtime"},
            "content": "长文本内容",
            "metadata": {},
        }},
    ))
    artifact = result.runtime_event["payload"]["artifact"]

    assert "content" not in artifact
    assert artifact["file_path"].endswith(f"{artifact_id}.md")
    assert artifact["file_size_bytes"] == len("长文本内容".encode())
    assert artifact["metadata"]["storage"] == "local_file"
    assert len(artifact["content_hash"]) == 64


def test_large_completed_output_keeps_only_final_artifact_reference():
    final_id = uuid4()
    service = RuntimeApplicationService(lambda: None, artifact_inline_max_bytes=4)
    result = service._bound_large_completed_output(
        _envelope(
            "agent.run.completed",
            {"output": "long output", "total_steps": 1},
        ),
        final_id,
    )

    payload = result.runtime_event["payload"]
    assert "output" not in payload
    assert payload["output_externalized"] is True
    assert payload["final_output_artifact_id"] == str(final_id)
