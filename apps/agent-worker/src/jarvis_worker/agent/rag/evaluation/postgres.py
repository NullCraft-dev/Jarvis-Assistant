"""PostgreSQL RAG 评估轨迹、反馈与标签仓储。"""

from datetime import datetime, timezone
from uuid import UUID

from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from jarvis_worker.database.models import (
    RagEvaluationFeedbackModel,
    RagEvaluationLabelModel,
    RagEvaluationTraceModel,
    RagQualityGateRunModel,
    RagQualityIssueModel,
)

from .contracts import RagEvaluationFeedback, RagEvaluationLabel, RagEvaluationTrace, RagQualityGateRun, RagQualityIssue
from .repository import (
    RagEvaluationFeedbackRepository,
    RagEvaluationLabelRepository,
    RagEvaluationTraceRepository,
    RagQualityGateRunRepository,
    RagQualityIssueRepository,
)


class PostgresRagEvaluationTraceRepository(RagEvaluationTraceRepository):
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def create(self, trace: RagEvaluationTrace) -> RagEvaluationTrace:
        self._session.add(
            RagEvaluationTraceModel(
                id=trace.id,
                workspace_id=trace.workspace_id,
                task_id=trace.task_id,
                run_id=trace.run_id,
                step_id=trace.step_id,
                query_text=trace.query,
                query_hash=trace.query_hash,
                request_json=trace.request,
                pipeline_versions_json=trace.pipeline_versions,
                candidate_ranking_json=list(trace.candidate_ranking),
                reranked_ranking_json=list(trace.reranked_ranking),
                context_chunk_ids_json=[str(value) for value in trace.context_chunk_ids],
                context_truncated=trace.context_truncated,
                result_count=trace.result_count,
                privacy_status=trace.privacy_status,
                created_at=trace.created_at,
            )
        )
        await self._session.flush()
        return trace

    async def get(self, trace_id: UUID) -> RagEvaluationTrace | None:
        model = await self._session.get(RagEvaluationTraceModel, trace_id)
        return _trace(model) if model else None

    async def list_unreviewed(self, *, limit: int = 100) -> list[RagEvaluationTrace]:
        return await self.list_filtered(privacy_status="pending", limit=limit)

    async def list_filtered(
        self, *, privacy_status: str | None = None, workspace_id: UUID | None = None,
        limit: int = 100
    ) -> list[RagEvaluationTrace]:
        stmt = select(RagEvaluationTraceModel)
        if workspace_id is not None:
            stmt = stmt.where(RagEvaluationTraceModel.workspace_id == workspace_id)
        if privacy_status is not None:
            stmt = stmt.where(RagEvaluationTraceModel.privacy_status == privacy_status)
        result = await self._session.execute(
            stmt.order_by(
                RagEvaluationTraceModel.created_at, RagEvaluationTraceModel.id
            ).limit(min(max(limit, 1), 500))
        )
        return [_trace(value) for value in result.scalars().all()]

    async def set_privacy_status(self, trace_id: UUID, status: str) -> bool:
        result = await self._session.execute(
            update(RagEvaluationTraceModel)
            .where(RagEvaluationTraceModel.id == trace_id)
            .values(privacy_status=status)
        )
        await self._session.flush()
        return bool(result.rowcount)

    async def get_latest_for_run(self, run_id: UUID) -> RagEvaluationTrace | None:
        result = await self._session.execute(
            select(RagEvaluationTraceModel)
            .where(RagEvaluationTraceModel.run_id == run_id)
            .order_by(RagEvaluationTraceModel.created_at.desc(), RagEvaluationTraceModel.id.desc())
            .limit(1)
        )
        model = result.scalar_one_or_none()
        return _trace(model) if model else None


class PostgresRagEvaluationLabelRepository(RagEvaluationLabelRepository):
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def create(self, label: RagEvaluationLabel) -> RagEvaluationLabel:
        self._session.add(
            RagEvaluationLabelModel(
                id=label.id,
                trace_id=label.trace_id,
                positive_chunk_ids_json=[str(value) for value in label.positive_chunk_ids],
                hard_negative_chunk_ids_json=[
                    str(value) for value in label.hard_negative_chunk_ids
                ],
                source=label.source,
                status=label.status,
                notes=label.notes,
                created_at=label.created_at,
                updated_at=label.updated_at,
            )
        )
        await self._session.flush()
        return label

    async def save(self, label: RagEvaluationLabel) -> RagEvaluationLabel:
        model = await self._session.get(RagEvaluationLabelModel, label.id)
        if model is None or model.trace_id != label.trace_id:
            raise ValueError("RAG evaluation label 不存在或 trace 不匹配")
        model.positive_chunk_ids_json = [str(value) for value in label.positive_chunk_ids]
        model.hard_negative_chunk_ids_json = [
            str(value) for value in label.hard_negative_chunk_ids
        ]
        model.source = label.source
        model.status = label.status
        model.notes = label.notes
        model.updated_at = label.updated_at
        await self._session.flush()
        return label

    async def get_for_trace(self, trace_id: UUID) -> RagEvaluationLabel | None:
        result = await self._session.execute(
            select(RagEvaluationLabelModel).where(
                RagEvaluationLabelModel.trace_id == trace_id
            )
        )
        model = result.scalar_one_or_none()
        return _label(model) if model else None

    async def get_confirmed_for_trace(self, trace_id: UUID) -> RagEvaluationLabel | None:
        result = await self._session.execute(
            select(RagEvaluationLabelModel).where(
                RagEvaluationLabelModel.trace_id == trace_id,
                RagEvaluationLabelModel.status.in_(("confirmed", "promoted")),
            )
        )
        model = result.scalar_one_or_none()
        return _label(model) if model else None

    async def list_confirmed(self, *, limit: int = 100) -> list[RagEvaluationLabel]:
        result = await self._session.execute(
            select(RagEvaluationLabelModel)
            .where(RagEvaluationLabelModel.status.in_(("confirmed", "promoted")))
            .order_by(RagEvaluationLabelModel.updated_at, RagEvaluationLabelModel.id)
            .limit(min(max(limit, 1), 500))
        )
        return [_label(value) for value in result.scalars().all()]


class PostgresRagEvaluationFeedbackRepository(RagEvaluationFeedbackRepository):
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def create_or_get(self, feedback: RagEvaluationFeedback) -> RagEvaluationFeedback:
        values = {
            "id": feedback.id,
            "trace_id": feedback.trace_id,
            "workspace_id": feedback.workspace_id,
            "task_id": feedback.task_id,
            "run_id": feedback.run_id,
            "message_id": feedback.message_id,
            "kind": feedback.kind,
            "citation_chunk_id": feedback.citation_chunk_id,
            "status": feedback.status,
            "failure_category": feedback.failure_category,
            "fingerprint": feedback.fingerprint,
            "created_at": feedback.created_at,
            "updated_at": feedback.updated_at,
        }
        statement = (
            insert(RagEvaluationFeedbackModel)
            .values(**values)
            .on_conflict_do_update(
                index_elements=["fingerprint"],
                set_={
                    "kind": feedback.kind,
                    "status": "pending",
                    "failure_category": None,
                    "updated_at": feedback.updated_at,
                },
            )
            .returning(RagEvaluationFeedbackModel)
        )
        result = await self._session.execute(statement)
        model = result.scalar_one()
        await self._session.flush()
        return _feedback(model)

    async def get(self, feedback_id: UUID) -> RagEvaluationFeedback | None:
        model = await self._session.get(RagEvaluationFeedbackModel, feedback_id)
        return _feedback(model) if model else None


    async def list_by_workspace(
        self, *, workspace_id: UUID, status: str | None = "pending", limit: int = 100
    ) -> list[RagEvaluationFeedback]:
        statement = select(RagEvaluationFeedbackModel).where(
            RagEvaluationFeedbackModel.workspace_id == workspace_id
        )
        if status is not None:
            statement = statement.where(RagEvaluationFeedbackModel.status == status)
        result = await self._session.execute(
            statement.order_by(
                RagEvaluationFeedbackModel.created_at.desc(),
                RagEvaluationFeedbackModel.id.desc(),
            ).limit(min(max(limit, 1), 100))
        )
        return [_feedback(value) for value in result.scalars().all()]

    async def set_review(
        self, feedback_id: UUID, *, status: str, failure_category: str | None = None
    ) -> RagEvaluationFeedback | None:
        result = await self._session.execute(
            update(RagEvaluationFeedbackModel)
            .where(RagEvaluationFeedbackModel.id == feedback_id)
            .values(
                status=status,
                failure_category=failure_category,
                updated_at=datetime.now(timezone.utc),
            )
            .returning(RagEvaluationFeedbackModel)
        )
        await self._session.flush()
        model = result.scalar_one_or_none()
        return _feedback(model) if model else None


class PostgresRagQualityGateRunRepository(RagQualityGateRunRepository):
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def create(self, run: RagQualityGateRun) -> RagQualityGateRun:
        self._session.add(
            RagQualityGateRunModel(
                id=run.id,
                gate_id=run.gate_id,
                cohort_id=run.cohort_id,
                baseline_id=run.baseline_id,
                revision=run.revision,
                status=run.status,
                sample_count=run.sample_count,
                metrics_json=run.metrics,
                checks_json=list(run.checks),
                failure_targets_json=list(run.failure_targets),
                generated_at=run.generated_at,
                created_at=run.created_at,
            )
        )
        await self._session.flush()
        return run

    async def get(self, run_id: UUID) -> RagQualityGateRun | None:
        model = await self._session.get(RagQualityGateRunModel, run_id)
        return _quality_gate_run(model) if model else None

    async def list_latest(self, *, limit: int = 20) -> list[RagQualityGateRun]:
        result = await self._session.execute(
            select(RagQualityGateRunModel)
            .order_by(RagQualityGateRunModel.generated_at.desc(), RagQualityGateRunModel.id.desc())
            .limit(min(max(limit, 1), 100))
        )
        return [_quality_gate_run(value) for value in result.scalars().all()]


class PostgresRagQualityIssueRepository(RagQualityIssueRepository):
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def create(self, issue: RagQualityIssue) -> RagQualityIssue:
        self._session.add(RagQualityIssueModel(
            id=issue.id, candidate_id=issue.candidate_id, trace_id=issue.trace_id,
            gate_id=issue.gate_id, cohort_id=issue.cohort_id, failure_type=issue.failure_type,
            owner=issue.owner, status=issue.status, occurrence_count=issue.occurrence_count,
            first_seen_run_id=issue.first_seen_run_id, last_seen_run_id=issue.last_seen_run_id,
            verified_run_id=issue.verified_run_id, resolution_note=issue.resolution_note,
            version=issue.version, created_at=issue.created_at, updated_at=issue.updated_at,
        ))
        await self._session.flush()
        return issue

    async def get(self, issue_id: UUID) -> RagQualityIssue | None:
        model = await self._session.get(RagQualityIssueModel, issue_id)
        return _quality_issue(model) if model else None

    async def get_by_candidate_id(self, candidate_id: str) -> RagQualityIssue | None:
        result = await self._session.execute(select(RagQualityIssueModel).where(RagQualityIssueModel.candidate_id == candidate_id))
        model = result.scalar_one_or_none()
        return _quality_issue(model) if model else None

    async def list_resolved(self, *, gate_id: str, cohort_id: str) -> list[RagQualityIssue]:
        result = await self._session.execute(select(RagQualityIssueModel).where(
            RagQualityIssueModel.gate_id == gate_id, RagQualityIssueModel.cohort_id == cohort_id,
            RagQualityIssueModel.status == "resolved",
        ))
        return [_quality_issue(value) for value in result.scalars().all()]

    async def list_filtered(
        self, *, status: str | None, owner: str | None,
        failure_type: str | None, limit: int,
    ) -> list[RagQualityIssue]:
        query = select(RagQualityIssueModel)
        if status is not None:
            query = query.where(RagQualityIssueModel.status == status)
        if owner is not None:
            query = query.where(RagQualityIssueModel.owner == owner)
        if failure_type is not None:
            query = query.where(RagQualityIssueModel.failure_type == failure_type)
        result = await self._session.execute(
            query.order_by(RagQualityIssueModel.updated_at.desc(), RagQualityIssueModel.id.desc())
            .limit(min(max(limit, 1), 100))
        )
        return [_quality_issue(value) for value in result.scalars().all()]

    async def count_by_status(self) -> dict[str, int]:
        result = await self._session.execute(
            select(RagQualityIssueModel.status, func.count(RagQualityIssueModel.id))
            .group_by(RagQualityIssueModel.status)
        )
        return {status: int(count) for status, count in result.all()}

    async def save(self, issue: RagQualityIssue, *, expected_version: int) -> RagQualityIssue | None:
        result = await self._session.execute(
            update(RagQualityIssueModel).where(
                RagQualityIssueModel.id == issue.id, RagQualityIssueModel.version == expected_version,
            ).values(
                owner=issue.owner, status=issue.status, occurrence_count=issue.occurrence_count,
                last_seen_run_id=issue.last_seen_run_id, verified_run_id=issue.verified_run_id,
                resolution_note=issue.resolution_note, version=issue.version, updated_at=issue.updated_at,
            ).returning(RagQualityIssueModel)
        )
        model = result.scalar_one_or_none()
        await self._session.flush()
        return _quality_issue(model) if model else None


def _trace(model: RagEvaluationTraceModel) -> RagEvaluationTrace:
    return RagEvaluationTrace(
        id=model.id,
        workspace_id=model.workspace_id,
        task_id=model.task_id,
        run_id=model.run_id,
        step_id=model.step_id,
        query=model.query_text,
        query_hash=model.query_hash,
        request=model.request_json,
        pipeline_versions=model.pipeline_versions_json,
        candidate_ranking=tuple(model.candidate_ranking_json),
        reranked_ranking=tuple(model.reranked_ranking_json),
        context_chunk_ids=tuple(UUID(value) for value in model.context_chunk_ids_json),
        context_truncated=model.context_truncated,
        result_count=model.result_count,
        privacy_status=model.privacy_status,
        created_at=model.created_at,
    )


def _label(model: RagEvaluationLabelModel) -> RagEvaluationLabel:
    return RagEvaluationLabel(
        id=model.id,
        trace_id=model.trace_id,
        positive_chunk_ids=tuple(UUID(value) for value in model.positive_chunk_ids_json),
        hard_negative_chunk_ids=tuple(
            UUID(value) for value in model.hard_negative_chunk_ids_json
        ),
        source=model.source,
        status=model.status,
        notes=model.notes,
        created_at=model.created_at,
        updated_at=model.updated_at,
    )


def _feedback(model: RagEvaluationFeedbackModel) -> RagEvaluationFeedback:
    return RagEvaluationFeedback(
        id=model.id,
        trace_id=model.trace_id,
        workspace_id=model.workspace_id,
        task_id=model.task_id,
        run_id=model.run_id,
        message_id=model.message_id,
        kind=model.kind,
        citation_chunk_id=model.citation_chunk_id,
        status=model.status,
        failure_category=model.failure_category,
        fingerprint=model.fingerprint,
        created_at=model.created_at,
        updated_at=model.updated_at,
    )


def _quality_gate_run(model: RagQualityGateRunModel) -> RagQualityGateRun:
    return RagQualityGateRun(
        id=model.id,
        gate_id=model.gate_id,
        cohort_id=model.cohort_id,
        baseline_id=model.baseline_id,
        revision=model.revision,
        status=model.status,
        sample_count=model.sample_count,
        metrics={key: float(value) for key, value in model.metrics_json.items()},
        checks=tuple(model.checks_json),
        generated_at=model.generated_at,
        failure_targets=tuple(model.failure_targets_json),
        created_at=model.created_at,
    )


def _quality_issue(model: RagQualityIssueModel) -> RagQualityIssue:
    return RagQualityIssue(
        id=model.id, candidate_id=model.candidate_id, trace_id=model.trace_id,
        gate_id=model.gate_id, cohort_id=model.cohort_id, failure_type=model.failure_type,
        owner=model.owner, status=model.status, occurrence_count=model.occurrence_count,
        first_seen_run_id=model.first_seen_run_id, last_seen_run_id=model.last_seen_run_id,
        verified_run_id=model.verified_run_id, resolution_note=model.resolution_note,
        version=model.version, created_at=model.created_at, updated_at=model.updated_at,
    )
