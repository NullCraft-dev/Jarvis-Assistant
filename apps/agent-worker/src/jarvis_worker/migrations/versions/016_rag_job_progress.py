"""add persistent RAG job progress snapshot

Revision ID: 016_rag_job_progress
Revises: 015_rag_openai_embeddings
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "016_rag_job_progress"
down_revision = "015_rag_openai_embeddings"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "rag_ingestion_jobs",
        sa.Column(
            "progress_json",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
    )


def downgrade() -> None:
    op.drop_column("rag_ingestion_jobs", "progress_json")
