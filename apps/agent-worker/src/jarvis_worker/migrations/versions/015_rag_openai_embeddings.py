"""add OpenAI embeddings vector index

Revision ID: 015_rag_openai_embeddings
Revises: 014_rag_ingestion_foundation
"""

import sqlalchemy as sa
from alembic import op

revision = "015_rag_openai_embeddings"
down_revision = "014_rag_ingestion_foundation"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")
    op.add_column(
        "rag_ingestion_jobs",
        sa.Column(
            "embedding_attempts",
            sa.Integer(),
            nullable=False,
            server_default="0",
        ),
    )
    op.add_column(
        "rag_ingestion_jobs",
        sa.Column(
            "embedding_max_attempts",
            sa.Integer(),
            nullable=False,
            server_default="3",
        ),
    )
    op.create_check_constraint(
        "ck_rag_ingestion_jobs_embedding_attempts",
        "rag_ingestion_jobs",
        "embedding_attempts >= 0 AND embedding_max_attempts >= 1 "
        "AND embedding_attempts <= embedding_max_attempts",
    )
    op.execute(
        """
        CREATE TABLE rag_chunk_embeddings (
            chunk_id UUID PRIMARY KEY,
            document_id UUID NOT NULL,
            workspace_id UUID NOT NULL,
            content_hash VARCHAR(64) NOT NULL,
            provider_name VARCHAR(80) NOT NULL,
            model_name VARCHAR(160) NOT NULL,
            dimensions INTEGER NOT NULL,
            embedding vector(1536) NOT NULL,
            updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            CONSTRAINT ck_rag_chunk_embeddings_dimensions CHECK (dimensions = 1536),
            CONSTRAINT fk_rag_chunk_embeddings_chunk_scope
                FOREIGN KEY (chunk_id, document_id, workspace_id)
                REFERENCES rag_chunks (id, document_id, workspace_id)
                ON DELETE CASCADE
        )
        """
    )
    op.create_index(
        "idx_rag_chunk_embeddings_workspace",
        "rag_chunk_embeddings",
        ["workspace_id"],
    )
    op.execute(
        "CREATE INDEX idx_rag_chunk_embeddings_cosine_hnsw "
        "ON rag_chunk_embeddings USING hnsw (embedding vector_cosine_ops)"
    )


def downgrade() -> None:
    op.drop_index(
        "idx_rag_chunk_embeddings_cosine_hnsw",
        table_name="rag_chunk_embeddings",
    )
    op.drop_index(
        "idx_rag_chunk_embeddings_workspace",
        table_name="rag_chunk_embeddings",
    )
    op.drop_table("rag_chunk_embeddings")
    op.drop_constraint(
        "ck_rag_ingestion_jobs_embedding_attempts",
        "rag_ingestion_jobs",
        type_="check",
    )
    op.drop_column("rag_ingestion_jobs", "embedding_max_attempts")
    op.drop_column("rag_ingestion_jobs", "embedding_attempts")
