"""PostgreSQL MemoryCandidate / MemoryExtractionJob repositories。"""

from uuid import UUID

from datetime import datetime

from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from jarvis_worker.agent.memory.candidate_repository import (
    MemoryCandidateRepository,
    MemoryExtractionJobRepository,
)
from jarvis_worker.database.models import MemoryCandidateModel, MemoryExtractionJobModel
from jarvis_worker.shared.domain.models import (
    MemoryCandidate,
    MemoryCandidateStatus,
    MemoryCategory,
    MemoryExtractionJob,
    MemoryExtractionJobStatus,
    MemoryScopeType,
    MemorySensitivity,
)


def _candidate_to_domain(model: MemoryCandidateModel) -> MemoryCandidate:
    return MemoryCandidate(
        id=model.id,
        scope_type=MemoryScopeType(model.scope_type),
        workspace_id=model.workspace_id,
        category=MemoryCategory(model.category),
        suggested_key=model.suggested_key,
        content=model.content,
        status=MemoryCandidateStatus(model.status),
        source_task_id=model.source_task_id,
        source_run_id=model.source_run_id,
        source_message_ids=[UUID(value) for value in model.source_message_ids],
        extraction_input_fingerprint=model.extraction_input_fingerprint,
        confidence=model.confidence,
        importance=model.importance,
        sensitivity=MemorySensitivity(model.sensitivity),
        deduplication_key=model.deduplication_key,
        extraction_policy_version=model.extraction_policy_version,
        extractor_provider=model.extractor_provider,
        extractor_model=model.extractor_model,
        conflict_memory_id=model.conflict_memory_id,
        approved_memory_id=model.approved_memory_id,
        expires_at=model.expires_at,
        resolved_at=model.resolved_at,
        resolution_note=model.resolution_note,
        version=model.version,
        created_at=model.created_at,
        updated_at=model.updated_at,
    )


def _job_to_domain(model: MemoryExtractionJobModel) -> MemoryExtractionJob:
    return MemoryExtractionJob(
        id=model.id,
        source_task_id=model.source_task_id,
        source_run_id=model.source_run_id,
        extraction_policy_version=model.extraction_policy_version,
        status=MemoryExtractionJobStatus(model.status),
        attempts=model.attempts,
        next_retry_at=model.next_retry_at,
        error_code=model.error_code,
        created_at=model.created_at,
        updated_at=model.updated_at,
    )


class PostgresMemoryCandidateRepository(MemoryCandidateRepository):
    def __init__(self, session: AsyncSession):
        self._session = session

    async def create(self, candidate: MemoryCandidate) -> MemoryCandidate:
        model = MemoryCandidateModel(
            id=candidate.id,
            scope_type=candidate.scope_type.value,
            workspace_id=candidate.workspace_id,
            category=candidate.category.value,
            suggested_key=candidate.suggested_key,
            content=candidate.content,
            status=candidate.status.value,
            source_task_id=candidate.source_task_id,
            source_run_id=candidate.source_run_id,
            source_message_ids=[str(value) for value in candidate.source_message_ids],
            extraction_input_fingerprint=candidate.extraction_input_fingerprint,
            confidence=candidate.confidence,
            importance=candidate.importance,
            sensitivity=candidate.sensitivity.value,
            deduplication_key=candidate.deduplication_key,
            extraction_policy_version=candidate.extraction_policy_version,
            extractor_provider=candidate.extractor_provider,
            extractor_model=candidate.extractor_model,
            conflict_memory_id=candidate.conflict_memory_id,
            approved_memory_id=candidate.approved_memory_id,
            expires_at=candidate.expires_at,
            resolved_at=candidate.resolved_at,
            resolution_note=candidate.resolution_note,
            version=candidate.version,
            created_at=candidate.created_at,
            updated_at=candidate.updated_at,
        )
        self._session.add(model)
        await self._session.flush()
        return _candidate_to_domain(model)

    async def get_for_update(self, candidate_id: UUID) -> MemoryCandidate | None:
        result = await self._session.execute(
            select(MemoryCandidateModel)
            .where(MemoryCandidateModel.id == candidate_id)
            .with_for_update()
        )
        model = result.scalar_one_or_none()
        return _candidate_to_domain(model) if model else None

    async def get_pending_by_deduplication_key(
        self, deduplication_key: str
    ) -> MemoryCandidate | None:
        result = await self._session.execute(
            select(MemoryCandidateModel).where(
                MemoryCandidateModel.status == MemoryCandidateStatus.PENDING.value,
                MemoryCandidateModel.deduplication_key == deduplication_key,
            )
        )
        model = result.scalar_one_or_none()
        return _candidate_to_domain(model) if model else None

    async def list_filtered(
        self,
        *,
        status: str | None = None,
        workspace_id: UUID | None = None,
        limit: int = 100,
    ) -> list[MemoryCandidate]:
        stmt = select(MemoryCandidateModel)
        if status:
            stmt = stmt.where(MemoryCandidateModel.status == status)
        if workspace_id:
            stmt = stmt.where(MemoryCandidateModel.workspace_id == workspace_id)
        stmt = stmt.order_by(
            MemoryCandidateModel.created_at.desc(), MemoryCandidateModel.id.desc()
        ).limit(limit)
        result = await self._session.execute(stmt)
        return [_candidate_to_domain(model) for model in result.scalars().all()]

    async def update(self, candidate: MemoryCandidate) -> None:
        model = await self._session.get(MemoryCandidateModel, candidate.id)
        if model is None:
            raise ValueError("MemoryCandidate 不存在")
        model.scope_type = candidate.scope_type.value
        model.workspace_id = candidate.workspace_id
        model.category = candidate.category.value
        model.suggested_key = candidate.suggested_key
        model.content = candidate.content
        model.status = candidate.status.value
        model.importance = candidate.importance
        model.conflict_memory_id = candidate.conflict_memory_id
        model.approved_memory_id = candidate.approved_memory_id
        model.resolved_at = candidate.resolved_at
        model.resolution_note = candidate.resolution_note
        model.version = candidate.version
        model.updated_at = candidate.updated_at
        await self._session.flush()

    async def list_due_for_update(
        self, *, now: datetime, limit: int = 100
    ) -> list[MemoryCandidate]:
        result = await self._session.execute(
            select(MemoryCandidateModel)
            .where(
                MemoryCandidateModel.status == MemoryCandidateStatus.PENDING.value,
                MemoryCandidateModel.expires_at.is_not(None),
                MemoryCandidateModel.expires_at <= now,
            )
            .order_by(
                MemoryCandidateModel.expires_at.asc(),
                MemoryCandidateModel.id.asc(),
            )
            .with_for_update(skip_locked=True)
            .limit(min(max(limit, 1), 100))
        )
        return [_candidate_to_domain(model) for model in result.scalars().all()]


class PostgresMemoryExtractionJobRepository(MemoryExtractionJobRepository):
    def __init__(self, session: AsyncSession):
        self._session = session

    async def create(self, job: MemoryExtractionJob) -> MemoryExtractionJob:
        model = MemoryExtractionJobModel(
            id=job.id,
            source_task_id=job.source_task_id,
            source_run_id=job.source_run_id,
            extraction_policy_version=job.extraction_policy_version,
            status=job.status.value,
            attempts=job.attempts,
            next_retry_at=job.next_retry_at,
            error_code=job.error_code,
            created_at=job.created_at,
            updated_at=job.updated_at,
        )
        self._session.add(model)
        await self._session.flush()
        return _job_to_domain(model)

    async def get_by_run_policy(
        self, source_run_id: UUID, extraction_policy_version: str
    ) -> MemoryExtractionJob | None:
        result = await self._session.execute(
            select(MemoryExtractionJobModel).where(
                MemoryExtractionJobModel.source_run_id == source_run_id,
                MemoryExtractionJobModel.extraction_policy_version == extraction_policy_version,
            )
        )
        model = result.scalar_one_or_none()
        return _job_to_domain(model) if model else None

    async def claim_next(
        self, *, now: datetime, stale_before: datetime
    ) -> MemoryExtractionJob | None:
        due = or_(
            and_(
                MemoryExtractionJobModel.status == "queued",
                or_(
                    MemoryExtractionJobModel.next_retry_at.is_(None),
                    MemoryExtractionJobModel.next_retry_at <= now,
                ),
            ),
            and_(
                MemoryExtractionJobModel.status == "failed",
                MemoryExtractionJobModel.next_retry_at.is_not(None),
                MemoryExtractionJobModel.next_retry_at <= now,
            ),
            and_(
                MemoryExtractionJobModel.status == "running",
                MemoryExtractionJobModel.updated_at <= stale_before,
            ),
        )
        result = await self._session.execute(
            select(MemoryExtractionJobModel)
            .where(due, MemoryExtractionJobModel.attempts < 10)
            .order_by(
                MemoryExtractionJobModel.next_retry_at.asc().nullsfirst(),
                MemoryExtractionJobModel.created_at.asc(),
            )
            .with_for_update(skip_locked=True)
            .limit(1)
        )
        model = result.scalar_one_or_none()
        if model is None:
            return None
        model.status = MemoryExtractionJobStatus.RUNNING.value
        model.attempts += 1
        model.next_retry_at = None
        model.error_code = None
        model.updated_at = now
        await self._session.flush()
        return _job_to_domain(model)

    async def mark_completed(self, job_id: UUID, *, now: datetime) -> None:
        model = await self._locked(job_id)
        model.status = MemoryExtractionJobStatus.COMPLETED.value
        model.next_retry_at = None
        model.error_code = None
        model.updated_at = now
        await self._session.flush()

    async def mark_failed(
        self,
        job_id: UUID,
        *,
        error_code: str,
        next_retry_at: datetime | None,
        now: datetime,
    ) -> None:
        model = await self._locked(job_id)
        model.status = MemoryExtractionJobStatus.FAILED.value
        model.error_code = error_code[:80]
        model.next_retry_at = next_retry_at
        model.updated_at = now
        await self._session.flush()

    async def _locked(self, job_id: UUID) -> MemoryExtractionJobModel:
        result = await self._session.execute(
            select(MemoryExtractionJobModel)
            .where(MemoryExtractionJobModel.id == job_id)
            .with_for_update()
        )
        model = result.scalar_one_or_none()
        if model is None:
            raise ValueError("MemoryExtractionJob 不存在")
        return model
