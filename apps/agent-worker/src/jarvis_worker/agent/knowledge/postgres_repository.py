from datetime import datetime
from uuid import UUID

from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from jarvis_worker.database.models import KnowledgeDocumentModel, KnowledgeVaultModel
from jarvis_worker.shared.domain.models import (
    KnowledgeDocument,
    KnowledgeDocumentKind,
    KnowledgeVault,
    KnowledgeVaultSource,
    KnowledgeVaultStatus,
)


def _vault(m: KnowledgeVaultModel) -> KnowledgeVault:
    return KnowledgeVault(
        id=m.id, name=m.name, root_path=m.root_path, canonical_path=m.canonical_path,
        status=KnowledgeVaultStatus(m.status), source=KnowledgeVaultSource(m.source),
        created_at=m.created_at, updated_at=m.updated_at, revoked_at=m.revoked_at,
    )


def _document(m: KnowledgeDocumentModel) -> KnowledgeDocument:
    return KnowledgeDocument(
        id=m.id, vault_id=m.vault_id, title=m.title, kind=KnowledgeDocumentKind(m.kind),
        relative_path=m.relative_path, content_hash=m.content_hash, size_bytes=m.size_bytes,
        tags=list(m.tags_json or []), source_urls=list(m.source_urls_json or []), source_task_id=m.source_task_id,
        source_run_id=m.source_run_id, created_at=m.created_at, updated_at=m.updated_at,
    )


class PostgresKnowledgeVaultRepository:
    def __init__(self, session: AsyncSession): self._session = session

    async def insert_if_absent(self, vault: KnowledgeVault) -> bool:
        result = await self._session.execute(
            insert(KnowledgeVaultModel).values(
                id=vault.id, name=vault.name, root_path=vault.root_path,
                canonical_path=vault.canonical_path, status=vault.status.value,
                source=vault.source.value, created_at=vault.created_at,
                updated_at=vault.updated_at, revoked_at=vault.revoked_at,
            ).on_conflict_do_nothing(index_elements=[KnowledgeVaultModel.canonical_path])
            .returning(KnowledgeVaultModel.id)
        )
        return result.scalar_one_or_none() is not None

    async def get(self, vault_id: UUID) -> KnowledgeVault | None:
        model = await self._session.get(KnowledgeVaultModel, vault_id)
        return _vault(model) if model else None

    async def get_by_canonical_path(self, path: str) -> KnowledgeVault | None:
        result = await self._session.execute(
            select(KnowledgeVaultModel).where(KnowledgeVaultModel.canonical_path == path)
        )
        model = result.scalar_one_or_none()
        return _vault(model) if model else None

    async def list_active(self) -> list[KnowledgeVault]:
        result = await self._session.execute(
            select(KnowledgeVaultModel).where(KnowledgeVaultModel.status == "active")
            .order_by(KnowledgeVaultModel.created_at)
        )
        return [_vault(item) for item in result.scalars().all()]

    async def activate_exclusive(self, vault_id: UUID, at: datetime) -> None:
        """Atomically make one Vault active without deleting prior Vault data."""
        await self._session.execute(
            update(KnowledgeVaultModel)
            .where(
                KnowledgeVaultModel.status == "active",
                KnowledgeVaultModel.id != vault_id,
            )
            .values(status="revoked", revoked_at=at, updated_at=at)
        )
        await self._session.execute(
            update(KnowledgeVaultModel)
            .where(KnowledgeVaultModel.id == vault_id)
            .values(status="active", revoked_at=None, updated_at=at)
        )


class PostgresKnowledgeDocumentRepository:
    def __init__(self, session: AsyncSession): self._session = session

    async def create(self, document: KnowledgeDocument) -> KnowledgeDocument:
        model = KnowledgeDocumentModel(
            id=document.id, vault_id=document.vault_id, title=document.title,
            kind=document.kind.value, relative_path=document.relative_path,
            content_hash=document.content_hash, size_bytes=document.size_bytes,
            tags_json=document.tags, source_urls_json=document.source_urls, source_task_id=document.source_task_id,
            source_run_id=document.source_run_id, created_at=document.created_at,
            updated_at=document.updated_at,
        )
        self._session.add(model)
        await self._session.flush()
        return _document(model)

    async def list_by_vault(self, vault_id: UUID, limit: int = 100) -> list[KnowledgeDocument]:
        result = await self._session.execute(
            select(KnowledgeDocumentModel).where(KnowledgeDocumentModel.vault_id == vault_id)
            .order_by(KnowledgeDocumentModel.created_at.desc()).limit(limit)
        )
        return [_document(item) for item in result.scalars().all()]
