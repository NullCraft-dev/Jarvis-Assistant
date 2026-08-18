"""PostgreSQL ArtifactRepository。"""

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from jarvis_worker.shared.domain.models import Artifact
from jarvis_worker.database.models import ArtifactModel
from jarvis_worker.database.repositories.interfaces import ArtifactRepository


class PostgresArtifactRepository(ArtifactRepository):
    """PostgreSQL Artifact 持久化 adapter。"""

    def __init__(self, session: AsyncSession):
        self._session = session

    async def get(self, artifact_id: UUID) -> Artifact | None:
        result = await self._session.execute(
            select(ArtifactModel).where(ArtifactModel.id == artifact_id)
        )
        model = result.scalar_one_or_none()
        return self._to_domain(model) if model is not None else None

    async def create(self, artifact: Artifact) -> Artifact:
        model = ArtifactModel(
            id=artifact.id,
            task_id=artifact.task_id,
            run_id=artifact.run_id,
            step_id=artifact.step_id,
            kind=artifact.kind,
            title=artifact.title,
            purpose=artifact.purpose,
            producer_type=artifact.producer_type,
            source_tool_call_id=artifact.source_tool_call_id,
            content=artifact.content,
            file_path=artifact.file_path,
            file_size_bytes=artifact.file_size_bytes,
            mime_type=artifact.mime_type,
            content_hash=artifact.content_hash,
            metadata_json=artifact.metadata,
            created_at=artifact.created_at,
        )
        self._session.add(model)
        return artifact

    async def list_by_task(self, task_id: UUID) -> list[Artifact]:
        result = await self._session.execute(
            select(ArtifactModel)
            .where(ArtifactModel.task_id == task_id)
            .order_by(ArtifactModel.created_at.desc())
        )
        return [self._to_domain(m) for m in result.scalars().all()]

    async def list_by_run(self, run_id: UUID) -> list[Artifact]:
        result = await self._session.execute(
            select(ArtifactModel)
            .where(ArtifactModel.run_id == run_id)
            .order_by(ArtifactModel.created_at.asc())
        )
        return [self._to_domain(m) for m in result.scalars().all()]

    @staticmethod
    def _to_domain(m: ArtifactModel) -> Artifact:
        return Artifact(
            id=m.id,
            task_id=m.task_id,
            run_id=m.run_id,
            step_id=m.step_id,
            kind=m.kind,
            title=m.title,
            purpose=m.purpose,
            producer_type=m.producer_type,
            source_tool_call_id=m.source_tool_call_id,
            content=m.content,
            file_path=m.file_path,
            file_size_bytes=m.file_size_bytes,
            mime_type=m.mime_type,
            content_hash=m.content_hash,
            metadata=m.metadata_json,
            created_at=m.created_at,
        )
