"""add RAG ingestion domain persistence

Revision ID: 014_rag_ingestion_foundation
Revises: 013_scheduled_source_reports
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "014_rag_ingestion_foundation"
down_revision = "013_scheduled_source_reports"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "rag_documents",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "workspace_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("workspaces.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "source_artifact_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("artifacts.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("title", sa.String(500), nullable=False),
        sa.Column("mime_type", sa.String(100), nullable=False),
        sa.Column("source_content_hash", sa.String(64), nullable=False),
        sa.Column("ingestion_policy_version", sa.String(80), nullable=False),
        sa.Column("status", sa.String(20), nullable=False, server_default="indexing"),
        sa.Column("parser_version", sa.String(80), nullable=False, server_default=""),
        sa.Column("chunker_version", sa.String(80), nullable=False, server_default=""),
        sa.Column("embedding_provider", sa.String(80), nullable=False, server_default=""),
        sa.Column("embedding_model", sa.String(160), nullable=False, server_default=""),
        sa.Column("embedding_dimensions", sa.Integer(), nullable=True),
        sa.Column("chunk_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("indexed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("disabled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False,
            server_default=sa.func.now(),
        ),
        sa.CheckConstraint(
            "status IN ('indexing','ready','failed','disabled')",
            name="ck_rag_documents_status",
        ),
        sa.CheckConstraint("chunk_count >= 0", name="ck_rag_documents_chunk_count"),
        sa.CheckConstraint(
            "embedding_dimensions IS NULL OR embedding_dimensions > 0",
            name="ck_rag_documents_embedding_dimensions",
        ),
        sa.UniqueConstraint(
            "workspace_id", "source_artifact_id", "source_content_hash",
            name="uq_rag_documents_workspace_source",
        ),
        sa.UniqueConstraint("id", "workspace_id", name="uq_rag_documents_id_workspace"),
    )
    op.create_index(
        "idx_rag_documents_workspace_status",
        "rag_documents", ["workspace_id", "status"],
    )

    op.create_table(
        "rag_ingestion_jobs",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "document_id",
            postgresql.UUID(as_uuid=True),
            nullable=False,
        ),
        sa.Column(
            "workspace_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("workspaces.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("idempotency_key", sa.String(64), nullable=False, unique=True),
        sa.Column("ingestion_policy_version", sa.String(80), nullable=False),
        sa.Column("status", sa.String(20), nullable=False, server_default="queued"),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("max_attempts", sa.Integer(), nullable=False, server_default="3"),
        sa.Column("claimed_by", sa.String(100), nullable=True),
        sa.Column("lease_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("next_retry_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("error_code", sa.String(80), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("failed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("cancelled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False,
            server_default=sa.func.now(),
        ),
        sa.CheckConstraint(
            "status IN ('queued','parsing','chunking','embedding','completed','failed','cancelled')",
            name="ck_rag_ingestion_jobs_status",
        ),
        sa.CheckConstraint(
            "attempts >= 0 AND max_attempts >= 1 AND attempts <= max_attempts",
            name="ck_rag_ingestion_jobs_attempts",
        ),
        sa.UniqueConstraint(
            "document_id", "ingestion_policy_version",
            name="uq_rag_ingestion_jobs_document_policy",
        ),
        sa.UniqueConstraint("id", "workspace_id", name="uq_rag_ingestion_jobs_id_workspace"),
        sa.ForeignKeyConstraint(
            ["document_id", "workspace_id"],
            ["rag_documents.id", "rag_documents.workspace_id"],
            name="fk_rag_ingestion_jobs_document_workspace",
            ondelete="CASCADE",
        ),
    )
    op.create_index(
        "idx_rag_ingestion_jobs_dispatch",
        "rag_ingestion_jobs", ["status", "next_retry_at", "lease_until", "created_at"],
    )
    op.create_index(
        "idx_rag_ingestion_jobs_workspace",
        "rag_ingestion_jobs", ["workspace_id", "created_at"],
    )

    op.create_table(
        "rag_chunks",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "document_id",
            postgresql.UUID(as_uuid=True),
            nullable=False,
        ),
        sa.Column(
            "ingestion_job_id",
            postgresql.UUID(as_uuid=True),
            nullable=False,
        ),
        sa.Column(
            "workspace_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("workspaces.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("ordinal", sa.Integer(), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("content_hash", sa.String(64), nullable=False),
        sa.Column("token_count", sa.Integer(), nullable=False),
        sa.Column(
            "source_locator_json", postgresql.JSONB(), nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column("embedding_key", sa.String(160), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False,
            server_default=sa.func.now(),
        ),
        sa.CheckConstraint("ordinal >= 0", name="ck_rag_chunks_ordinal"),
        sa.CheckConstraint("token_count > 0", name="ck_rag_chunks_token_count"),
        sa.UniqueConstraint(
            "ingestion_job_id", "ordinal", name="uq_rag_chunks_job_ordinal"
        ),
        sa.UniqueConstraint(
            "id", "document_id", "workspace_id",
            name="uq_rag_chunks_id_document_workspace",
        ),
        sa.ForeignKeyConstraint(
            ["document_id", "workspace_id"],
            ["rag_documents.id", "rag_documents.workspace_id"],
            name="fk_rag_chunks_document_workspace",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["ingestion_job_id", "workspace_id"],
            ["rag_ingestion_jobs.id", "rag_ingestion_jobs.workspace_id"],
            name="fk_rag_chunks_job_workspace",
            ondelete="CASCADE",
        ),
    )
    op.create_index(
        "idx_rag_chunks_document_ordinal", "rag_chunks", ["document_id", "ordinal"]
    )
    op.create_index("idx_rag_chunks_workspace", "rag_chunks", ["workspace_id"])

    op.create_table(
        "rag_elements",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("document_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "workspace_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("workspaces.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("element_type", sa.String(20), nullable=False),
        sa.Column("page_number", sa.Integer(), nullable=False),
        sa.Column("bounding_box_json", postgresql.JSONB(), nullable=False),
        sa.Column("page_width", sa.Float(), nullable=False),
        sa.Column("page_height", sa.Float(), nullable=False),
        sa.Column("locator_key", sa.String(64), nullable=False),
        sa.Column("content_hash", sa.String(64), nullable=False),
        sa.Column("extraction_method", sa.String(20), nullable=False),
        sa.Column("extraction_version", sa.String(80), nullable=False),
        sa.Column("confidence", sa.Float(), nullable=False),
        sa.Column("caption_text", sa.Text(), nullable=False, server_default=""),
        sa.Column("ocr_text", sa.Text(), nullable=False, server_default=""),
        sa.Column(
            "structured_data_json", postgresql.JSONB(), nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column("derived_description", sa.Text(), nullable=False, server_default=""),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False,
            server_default=sa.func.now(),
        ),
        sa.CheckConstraint(
            "element_type IN ('image','figure','chart','table','diagram','equation')",
            name="ck_rag_elements_type",
        ),
        sa.CheckConstraint(
            "extraction_method IN ('native','ocr','vision','hybrid')",
            name="ck_rag_elements_extraction_method",
        ),
        sa.CheckConstraint("page_number >= 1", name="ck_rag_elements_page_number"),
        sa.CheckConstraint(
            "page_width > 0 AND page_height > 0", name="ck_rag_elements_page_size"
        ),
        sa.CheckConstraint(
            "jsonb_typeof(bounding_box_json) = 'array' "
            "AND jsonb_array_length(bounding_box_json) = 4",
            name="ck_rag_elements_bounding_box",
        ),
        sa.CheckConstraint("confidence BETWEEN 0 AND 1", name="ck_rag_elements_confidence"),
        sa.UniqueConstraint(
            "document_id", "locator_key", name="uq_rag_elements_document_locator"
        ),
        sa.UniqueConstraint(
            "id", "document_id", "workspace_id",
            name="uq_rag_elements_id_document_workspace",
        ),
        sa.ForeignKeyConstraint(
            ["document_id", "workspace_id"],
            ["rag_documents.id", "rag_documents.workspace_id"],
            name="fk_rag_elements_document_workspace",
            ondelete="CASCADE",
        ),
    )
    op.create_index(
        "idx_rag_elements_document_page",
        "rag_elements", ["document_id", "page_number", "element_type"],
    )
    op.create_index("idx_rag_elements_workspace", "rag_elements", ["workspace_id"])

    op.create_table(
        "rag_assets",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("document_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("element_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "workspace_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("workspaces.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("asset_kind", sa.String(30), nullable=False),
        sa.Column("storage_reference", sa.Text(), nullable=False, unique=True),
        sa.Column("mime_type", sa.String(100), nullable=False),
        sa.Column("content_hash", sa.String(64), nullable=False),
        sa.Column("size_bytes", sa.BigInteger(), nullable=False),
        sa.Column("width", sa.Integer(), nullable=True),
        sa.Column("height", sa.Integer(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False,
            server_default=sa.func.now(),
        ),
        sa.CheckConstraint(
            "asset_kind IN ('crop','embedded_image','page_render')",
            name="ck_rag_assets_kind",
        ),
        sa.CheckConstraint("size_bytes > 0", name="ck_rag_assets_size"),
        sa.CheckConstraint(
            "(width IS NULL AND height IS NULL) OR (width > 0 AND height > 0)",
            name="ck_rag_assets_dimensions",
        ),
        sa.CheckConstraint(
            "storage_reference <> '' AND storage_reference NOT LIKE '/%' "
            "AND storage_reference NOT LIKE '%\\\\%' "
            "AND storage_reference <> '..' AND storage_reference NOT LIKE '../%' "
            "AND storage_reference NOT LIKE '%/../%'",
            name="ck_rag_assets_storage_reference",
        ),
        sa.ForeignKeyConstraint(
            ["document_id", "workspace_id"],
            ["rag_documents.id", "rag_documents.workspace_id"],
            name="fk_rag_assets_document_workspace",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["element_id", "document_id", "workspace_id"],
            ["rag_elements.id", "rag_elements.document_id", "rag_elements.workspace_id"],
            name="fk_rag_assets_element_document_workspace",
            ondelete="CASCADE",
        ),
    )
    op.create_index("idx_rag_assets_element", "rag_assets", ["element_id"])
    op.create_index("idx_rag_assets_workspace", "rag_assets", ["workspace_id"])

    op.create_table(
        "rag_chunk_element_links",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("document_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "workspace_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("workspaces.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("chunk_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("element_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("relation_type", sa.String(20), nullable=False),
        sa.Column("confidence", sa.Float(), nullable=False),
        sa.Column("order_index", sa.Integer(), nullable=False, server_default="0"),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False,
            server_default=sa.func.now(),
        ),
        sa.CheckConstraint(
            "relation_type IN ('contains','references','explains','caption_of','nearby')",
            name="ck_rag_chunk_element_links_relation",
        ),
        sa.CheckConstraint(
            "confidence BETWEEN 0 AND 1", name="ck_rag_chunk_element_links_confidence"
        ),
        sa.CheckConstraint(
            "order_index >= 0", name="ck_rag_chunk_element_links_order"
        ),
        sa.UniqueConstraint(
            "chunk_id", "element_id", "relation_type",
            name="uq_rag_chunk_element_links_relation",
        ),
        sa.ForeignKeyConstraint(
            ["chunk_id", "document_id", "workspace_id"],
            ["rag_chunks.id", "rag_chunks.document_id", "rag_chunks.workspace_id"],
            name="fk_rag_chunk_element_links_chunk_document_workspace",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["element_id", "document_id", "workspace_id"],
            ["rag_elements.id", "rag_elements.document_id", "rag_elements.workspace_id"],
            name="fk_rag_chunk_element_links_element_document_workspace",
            ondelete="CASCADE",
        ),
    )
    op.create_index(
        "idx_rag_chunk_element_links_chunk",
        "rag_chunk_element_links", ["workspace_id", "chunk_id", "order_index"],
    )
    op.create_index(
        "idx_rag_chunk_element_links_element",
        "rag_chunk_element_links", ["workspace_id", "element_id"],
    )


def downgrade() -> None:
    op.drop_index(
        "idx_rag_chunk_element_links_element", table_name="rag_chunk_element_links"
    )
    op.drop_index(
        "idx_rag_chunk_element_links_chunk", table_name="rag_chunk_element_links"
    )
    op.drop_table("rag_chunk_element_links")
    op.drop_index("idx_rag_assets_workspace", table_name="rag_assets")
    op.drop_index("idx_rag_assets_element", table_name="rag_assets")
    op.drop_table("rag_assets")
    op.drop_index("idx_rag_elements_workspace", table_name="rag_elements")
    op.drop_index("idx_rag_elements_document_page", table_name="rag_elements")
    op.drop_table("rag_elements")
    op.drop_index("idx_rag_chunks_workspace", table_name="rag_chunks")
    op.drop_index("idx_rag_chunks_document_ordinal", table_name="rag_chunks")
    op.drop_table("rag_chunks")
    op.drop_index("idx_rag_ingestion_jobs_workspace", table_name="rag_ingestion_jobs")
    op.drop_index("idx_rag_ingestion_jobs_dispatch", table_name="rag_ingestion_jobs")
    op.drop_table("rag_ingestion_jobs")
    op.drop_index("idx_rag_documents_workspace_status", table_name="rag_documents")
    op.drop_table("rag_documents")
