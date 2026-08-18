"""add persisted MCP server and discovered tool registry

Revision ID: 012_mcp_foundation
Revises: 011_scheduled_knowledge_tasks
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "012_mcp_foundation"
down_revision = "011_scheduled_knowledge_tasks"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "mcp_servers",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("slug", sa.String(80), nullable=False, unique=True),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("transport", sa.String(20), nullable=False),
        sa.Column("command", sa.Text(), nullable=False),
        sa.Column("args_json", postgresql.JSONB(), nullable=False, server_default=sa.text("'[]'::jsonb")),
        sa.Column("env_keys_json", postgresql.JSONB(), nullable=False, server_default=sa.text("'[]'::jsonb")),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("status", sa.String(20), nullable=False, server_default="disconnected"),
        sa.Column("last_error_code", sa.String(80), nullable=True),
        sa.Column("last_connected_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("transport IN ('stdio')", name="ck_mcp_servers_transport"),
        sa.CheckConstraint("status IN ('disconnected','connected','error')", name="ck_mcp_servers_status"),
    )
    op.create_index("idx_mcp_servers_enabled", "mcp_servers", ["enabled"])
    op.create_table(
        "mcp_tools",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("server_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("mcp_servers.id", ondelete="CASCADE"), nullable=False),
        sa.Column("original_name", sa.String(200), nullable=False),
        sa.Column("internal_name", sa.String(300), nullable=False, unique=True),
        sa.Column("description", sa.String(500), nullable=False, server_default=""),
        sa.Column("input_schema_json", postgresql.JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("risk_level", sa.String(5), nullable=False, server_default="L3"),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("discovered_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("risk_level IN ('L0','L1','L2','L3','L4','L5')", name="ck_mcp_tools_risk"),
        sa.UniqueConstraint("server_id", "original_name", name="uq_mcp_tools_server_name"),
    )
    op.create_index("idx_mcp_tools_server_enabled", "mcp_tools", ["server_id", "enabled"])


def downgrade() -> None:
    op.drop_table("mcp_tools")
    op.drop_table("mcp_servers")
