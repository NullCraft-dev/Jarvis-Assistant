"""add explicit Artifact v2 purpose and producer contract

Revision ID: 006_artifact_v2_contract
Revises: 005_run_pause_resume
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "006_artifact_v2_contract"
down_revision = "005_run_pause_resume"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "artifacts",
        sa.Column(
            "purpose",
            sa.String(30),
            nullable=False,
            server_default="final_response",
        ),
    )
    op.add_column(
        "artifacts",
        sa.Column(
            "producer_type",
            sa.String(20),
            nullable=False,
            server_default="runtime",
        ),
    )
    op.add_column(
        "artifacts",
        sa.Column(
            "source_tool_call_id",
            postgresql.UUID(as_uuid=True),
            nullable=True,
        ),
    )
    op.create_foreign_key(
        "fk_artifacts_source_tool_call_id",
        "artifacts",
        "tool_calls",
        ["source_tool_call_id"],
        ["id"],
    )
    op.create_check_constraint(
        "ck_artifacts_purpose",
        "artifacts",
        "purpose IN ('final_response','deliverable')",
    )
    op.create_check_constraint(
        "ck_artifacts_producer_type",
        "artifacts",
        "producer_type IN ('runtime','tool')",
    )
    op.create_check_constraint(
        "ck_artifacts_producer_source",
        "artifacts",
        "(producer_type = 'runtime' AND source_tool_call_id IS NULL) OR "
        "(producer_type = 'tool' AND source_tool_call_id IS NOT NULL)",
    )
    op.create_index("idx_artifacts_purpose", "artifacts", ["purpose"])
    op.create_index(
        "idx_artifacts_source_tool_call_id",
        "artifacts",
        ["source_tool_call_id"],
    )
    op.alter_column("artifacts", "purpose", server_default=None)
    op.alter_column("artifacts", "producer_type", server_default=None)


def downgrade() -> None:
    op.drop_index("idx_artifacts_source_tool_call_id", table_name="artifacts")
    op.drop_index("idx_artifacts_purpose", table_name="artifacts")
    op.drop_constraint(
        "ck_artifacts_producer_source", "artifacts", type_="check"
    )
    op.drop_constraint(
        "ck_artifacts_producer_type", "artifacts", type_="check"
    )
    op.drop_constraint("ck_artifacts_purpose", "artifacts", type_="check")
    op.drop_constraint(
        "fk_artifacts_source_tool_call_id", "artifacts", type_="foreignkey"
    )
    op.drop_column("artifacts", "source_tool_call_id")
    op.drop_column("artifacts", "producer_type")
    op.drop_column("artifacts", "purpose")
