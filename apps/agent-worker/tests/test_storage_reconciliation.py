from uuid import uuid4

from jarvis_worker.agent.artifacts.file_store import LocalArtifactFileStore
from jarvis_worker.database.reconciliation_service import (
    StorageReconciliationApplicationService,
)
from jarvis_worker.shared.domain.models import (
    AgentRun,
    Artifact,
    ExecutionStep,
    RunStatus,
    RuntimeEvent,
    StepStatus,
    StepType,
    Task,
    TaskStatus,
)


def _completed_fixture():
    task_id, run_id, conversation_id, step_id, artifact_id = (
        uuid4(), uuid4(), uuid4(), uuid4(), uuid4()
    )
    task = Task(
        id=task_id,
        title="test",
        user_goal="test",
        conversation_id=conversation_id,
        status=TaskStatus.COMPLETED,
        active_run_id=run_id,
    )
    run = AgentRun(
        id=run_id,
        task_id=task_id,
        status=RunStatus.COMPLETED,
        current_step_id=step_id,
        final_output_artifact_id=artifact_id,
        step_count=1,
    )
    step = ExecutionStep(
        id=step_id,
        run_id=run_id,
        task_id=task_id,
        type=StepType.FINAL_OUTPUT,
        status=StepStatus.COMPLETED,
    )
    events = [
        RuntimeEvent(
            id=uuid4(), event_id=uuid4(), type="agent.run.started",
            payload={}, task_id=task_id, run_id=run_id,
            step_id=step_id, event_sequence=1,
        ),
        RuntimeEvent(
            id=uuid4(), event_id=uuid4(), type="agent.run.completed",
            payload={}, task_id=task_id, run_id=run_id,
            step_id=step_id, event_sequence=2,
        ),
    ]
    artifact = Artifact(
        id=artifact_id,
        task_id=task_id,
        run_id=run_id,
        kind="markdown",
        title="result",
        purpose="final_response",
        producer_type="runtime",
        content="done",
        metadata={"storage": "inline"},
    )
    return task, run, events, [step], [artifact]


def test_healthy_run_has_no_reconciliation_issues(tmp_path):
    service = StorageReconciliationApplicationService(
        lambda: None, artifact_file_store=LocalArtifactFileStore(tmp_path)
    )
    task, run, events, steps, artifacts = _completed_fixture()

    assert service._inspect_run(run, task, events, steps, artifacts) == []


def test_reconciliation_detects_cross_entity_breaks_without_sensitive_details(tmp_path):
    service = StorageReconciliationApplicationService(
        lambda: None, artifact_file_store=LocalArtifactFileStore(tmp_path)
    )
    task, run, events, _, artifacts = _completed_fixture()
    task.status = TaskStatus.RUNNING
    events[0].event_sequence = 2
    events[1].type = "agent.run.failed"

    issues = service._inspect_run(run, task, events, [], artifacts)
    codes = {issue.code for issue in issues}

    assert {
        "ACTIVE_TASK_STATUS_MISMATCH",
        "EVENT_SEQUENCE_GAP",
        "TERMINAL_EVENT_MISSING",
        "TERMINAL_EVENT_CONFLICT",
        "CURRENT_STEP_MISSING",
        "EVENT_STEP_MISSING",
    }.issubset(codes)
    assert all(str(tmp_path) not in issue.summary for issue in issues)


def test_reconciliation_detects_external_artifact_integrity_failure(tmp_path):
    service = StorageReconciliationApplicationService(
        lambda: None, artifact_file_store=LocalArtifactFileStore(tmp_path)
    )
    task, run, events, steps, artifacts = _completed_fixture()
    artifact = artifacts[0]
    artifact.content = None
    artifact.file_path = f"{artifact.id}.md"
    artifact.file_size_bytes = 4
    artifact.mime_type = "text/markdown; charset=utf-8"
    artifact.content_hash = "0" * 64
    artifact.metadata["storage"] = "local_file"

    issues = service._inspect_run(run, task, events, steps, artifacts)

    assert [issue.code for issue in issues] == ["ARTIFACT_FILE_INTEGRITY_ERROR"]
    assert "path" not in issues[0].summary.lower()


def test_reconciliation_distinguishes_duplicate_terminal_event(tmp_path):
    service = StorageReconciliationApplicationService(
        lambda: None, artifact_file_store=LocalArtifactFileStore(tmp_path)
    )
    task, run, events, steps, artifacts = _completed_fixture()
    duplicate = RuntimeEvent(
        id=uuid4(), event_id=uuid4(), type="agent.run.completed",
        payload={}, task_id=task.id, run_id=run.id,
        step_id=steps[0].id, event_sequence=3,
    )
    issues = service._inspect_run(
        run, task, [*events, duplicate], steps, artifacts
    )
    assert [issue.code for issue in issues] == ["TERMINAL_EVENT_DUPLICATE"]


def test_reconciliation_detects_step_count_order_and_event_type_corruption(tmp_path):
    service = StorageReconciliationApplicationService(
        lambda: None, artifact_file_store=LocalArtifactFileStore(tmp_path)
    )
    task, run, events, steps, artifacts = _completed_fixture()
    run.step_count = 0
    steps[0].order_index = 4
    steps[0].type = StepType.TOOL_CALL
    events.append(RuntimeEvent(
        id=uuid4(), event_id=uuid4(), type="model.call.completed",
        payload={}, task_id=task.id, run_id=run.id,
        step_id=steps[0].id, event_sequence=3,
    ))

    issues = service._inspect_run(run, task, events, steps, artifacts)

    assert {issue.code for issue in issues} == {
        "RUN_STEP_COUNT_MISMATCH",
        "STEP_ORDER_INVALID",
        "STEP_EVENT_TYPE_MISMATCH",
    }
