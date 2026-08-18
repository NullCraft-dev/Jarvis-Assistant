"""PostgreSQL 业务真源的只读一致性对账。

本服务只消费 Repository 与 Artifact file adapter，不写数据库、不发布事件、不执行修复。
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timezone

from jarvis_worker.agent.artifacts.file_store import LocalArtifactFileStore
from jarvis_worker.database.unit_of_work import PostgresUnitOfWork
from jarvis_worker.shared.domain.models import (
    AgentRun,
    Artifact,
    ExecutionStep,
    RunStatus,
    RuntimeEvent,
    StepType,
    Task,
    TaskStatus,
)

DEFAULT_SCAN_LIMIT = 50
MAX_SCAN_LIMIT = 100
MAX_ISSUES = 200

_TERMINAL_EVENT_BY_STATUS = {
    RunStatus.COMPLETED: "agent.run.completed",
    RunStatus.FAILED: "agent.run.failed",
    RunStatus.CANCELLED: "agent.run.cancelled",
}
_TERMINAL_EVENT_TYPES = set(_TERMINAL_EVENT_BY_STATUS.values())
_ACTIVE_TASK_STATUS_BY_RUN = {
    RunStatus.QUEUED: TaskStatus.PENDING,
    RunStatus.RUNNING: TaskStatus.RUNNING,
    RunStatus.WAITING_PERMISSION: TaskStatus.WAITING_FOR_USER,
    RunStatus.PAUSE_REQUESTED: TaskStatus.RUNNING,
    RunStatus.PAUSED: TaskStatus.RUNNING,
    RunStatus.RESUME_REQUESTED: TaskStatus.RUNNING,
    RunStatus.CANCEL_REQUESTED: TaskStatus.RUNNING,
    RunStatus.CANCELLING: TaskStatus.RUNNING,
    RunStatus.COMPLETED: TaskStatus.COMPLETED,
    RunStatus.FAILED: TaskStatus.FAILED,
    RunStatus.CANCELLED: TaskStatus.CANCELLED,
}


@dataclass(frozen=True)
class ReconciliationIssue:
    code: str
    severity: str
    entity_type: str
    entity_id: str
    summary: str
    task_id: str | None = None
    run_id: str | None = None


class StorageReconciliationApplicationService:
    """最近 Run 的有限、只读、一致性快照 owner。"""

    def __init__(self, uow_factory, *, artifact_file_store: LocalArtifactFileStore):
        self._uow_factory = uow_factory
        self._artifact_file_store = artifact_file_store

    async def inspect(self, limit: int = DEFAULT_SCAN_LIMIT) -> dict:
        limit = min(max(limit, 1), MAX_SCAN_LIMIT)
        session_factory = self._uow_factory()
        async with session_factory() as session:
            uow = PostgresUnitOfWork(session)
            candidates = await uow.runs.list_recent(limit + 1)
            has_more_runs = len(candidates) > limit
            runs = candidates[:limit]
            issues: list[ReconciliationIssue] = []
            scanned_events = 0
            scanned_steps = 0
            scanned_artifacts = 0

            for run in runs:
                task = await uow.tasks.get(run.task_id)
                events = await uow.events.list_by_run(run.id)
                steps = await uow.steps.list_by_run(run.id)
                artifacts = await uow.artifacts.list_by_run(run.id)
                scanned_events += len(events)
                scanned_steps += len(steps)
                scanned_artifacts += len(artifacts)
                issues.extend(self._inspect_run(run, task, events, steps, artifacts))

        total_issue_count = len(issues)
        visible_issues = issues[:MAX_ISSUES]
        generated_at = datetime.now(timezone.utc).isoformat()
        return {
            "status": "healthy" if total_issue_count == 0 else "degraded",
            "generated_at": generated_at,
            "scanned_runs": len(runs),
            "scanned_events": scanned_events,
            "scanned_steps": scanned_steps,
            "scanned_artifacts": scanned_artifacts,
            "issue_count": total_issue_count,
            "truncated": has_more_runs or total_issue_count > len(visible_issues),
            "issues": [asdict(issue) for issue in visible_issues],
        }

    def _inspect_run(
        self,
        run: AgentRun,
        task: Task | None,
        events: list[RuntimeEvent],
        steps: list[ExecutionStep],
        artifacts: list[Artifact],
    ) -> list[ReconciliationIssue]:
        issues: list[ReconciliationIssue] = []
        run_id, task_id = str(run.id), str(run.task_id)

        def add(code: str, severity: str, entity_type: str, entity_id: str, summary: str):
            issues.append(
                ReconciliationIssue(
                    code=code,
                    severity=severity,
                    entity_type=entity_type,
                    entity_id=entity_id,
                    summary=summary,
                    task_id=task_id,
                    run_id=run_id,
                )
            )

        if task is None:
            add("RUN_TASK_MISSING", "error", "run", run_id, "Run 关联的 Task 不存在")
        elif task.active_run_id == run.id:
            expected = _ACTIVE_TASK_STATUS_BY_RUN[run.status]
            if task.status != expected:
                add(
                    "ACTIVE_TASK_STATUS_MISMATCH", "error", "task", str(task.id),
                    f"活跃 Task 状态 {task.status.value} 与 Run 状态 {run.status.value} 不一致",
                )

        sequences = [event.event_sequence for event in events]
        expected_sequences = list(range(1, len(events) + 1))
        if sequences != expected_sequences:
            add(
                "EVENT_SEQUENCE_GAP", "error", "run", run_id,
                "RuntimeEvent sequence 必须从 1 连续递增",
            )

        event_types = [event.type for event in events]
        terminal_types = [event_type for event_type in event_types if event_type in _TERMINAL_EVENT_TYPES]
        expected_terminal = _TERMINAL_EVENT_BY_STATUS.get(run.status)
        if expected_terminal is not None:
            expected_terminal_count = event_types.count(expected_terminal)
            if expected_terminal_count == 0:
                add(
                    "TERMINAL_EVENT_MISSING", "error", "run", run_id,
                    f"终态 Run 缺少 {expected_terminal}",
                )
            elif expected_terminal_count > 1:
                add(
                    "TERMINAL_EVENT_DUPLICATE", "error", "run", run_id,
                    f"终态 Run 存在多个 {expected_terminal}",
                )
            if any(event_type != expected_terminal for event_type in terminal_types):
                add(
                    "TERMINAL_EVENT_CONFLICT", "error", "run", run_id,
                    "Run 同时存在与当前状态冲突的终态事件",
                )
        elif terminal_types:
            add(
                "NON_TERMINAL_RUN_HAS_TERMINAL_EVENT", "error", "run", run_id,
                "非终态 Run 不应包含终态 RuntimeEvent",
            )

        step_ids = {step.id for step in steps}
        if run.step_count != len(steps):
            add(
                "RUN_STEP_COUNT_MISMATCH", "error", "run", run_id,
                "Run.step_count 与持久化 ExecutionStep 数量不一致",
            )
        order_indexes = [step.order_index for step in steps]
        if order_indexes != list(range(len(steps))):
            add(
                "STEP_ORDER_INVALID", "error", "run", run_id,
                "ExecutionStep.order_index 必须从 0 连续且唯一递增",
            )
        if run.current_step_id is not None and run.current_step_id not in step_ids:
            add(
                "CURRENT_STEP_MISSING", "error", "run", run_id,
                "Run.current_step_id 未关联到本 Run 的 ExecutionStep",
            )
        missing_event_step_ids = {
            event.step_id for event in events
            if event.step_id is not None and event.step_id not in step_ids
        }
        for step_id in sorted(missing_event_step_ids, key=str):
            add(
                "EVENT_STEP_MISSING", "error", "step", str(step_id),
                "RuntimeEvent 引用的 ExecutionStep 不存在",
            )
        steps_by_id = {step.id: step for step in steps}
        type_mismatches: set[tuple[object, StepType]] = set()
        for event in events:
            step = steps_by_id.get(event.step_id)
            if step is None:
                continue
            expected_type = None
            if event.type.startswith("model."):
                expected_type = StepType.MODEL_CALL
            elif event.type.startswith("tool.call."):
                expected_type = StepType.TOOL_CALL
            if expected_type is not None and step.type != expected_type:
                type_mismatches.add((step.id, expected_type))
        for step_id, expected_type in sorted(
            type_mismatches, key=lambda item: (str(item[0]), item[1].value)
        ):
            add(
                "STEP_EVENT_TYPE_MISMATCH", "error", "step", str(step_id),
                "ExecutionStep 类型与关联 RuntimeEvent 生命周期不一致: "
                f"expected={expected_type.value}",
            )

        artifacts_by_id = {artifact.id: artifact for artifact in artifacts}
        if run.final_output_artifact_id is not None:
            final_artifact = artifacts_by_id.get(run.final_output_artifact_id)
            if final_artifact is None:
                add(
                    "FINAL_ARTIFACT_MISSING", "error", "artifact",
                    str(run.final_output_artifact_id),
                    "Run.final_output_artifact_id 未关联到本 Run 的 Artifact",
                )
            elif final_artifact.purpose != "final_response":
                add(
                    "FINAL_ARTIFACT_PURPOSE_MISMATCH", "error", "artifact",
                    str(final_artifact.id),
                    "Run 最终 Artifact 的 purpose 不是 final_response",
                )

        for artifact in artifacts:
            if artifact.purpose == "final_response":
                if run.final_output_artifact_id != artifact.id:
                    add(
                        "FINAL_ARTIFACT_REFERENCE_MISMATCH", "error", "artifact",
                        str(artifact.id), "最终 Artifact 与 Run 引用不一致",
                    )
            if artifact.file_path:
                if not artifact.content_hash or not artifact.mime_type or artifact.file_size_bytes is None:
                    add(
                        "ARTIFACT_FILE_METADATA_INCOMPLETE", "error", "artifact",
                        str(artifact.id), "外置 Artifact 缺少 size、MIME 或 SHA-256",
                    )
                    continue
                try:
                    content = self._artifact_file_store.read_bytes(
                        artifact.file_path, expected_sha256=artifact.content_hash
                    )
                    if len(content) != artifact.file_size_bytes:
                        raise ValueError("size mismatch")
                except (OSError, ValueError):
                    add(
                        "ARTIFACT_FILE_INTEGRITY_ERROR", "error", "artifact",
                        str(artifact.id), "外置 Artifact 文件缺失或完整性校验失败",
                    )
            elif artifact.metadata.get("storage") == "local_file":
                add(
                    "ARTIFACT_FILE_REFERENCE_MISSING", "error", "artifact",
                    str(artifact.id), "Artifact 标记为本地文件存储但缺少文件引用",
                )
        return issues
