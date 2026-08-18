"""MCP server 与已发现工具的 PostgreSQL repositories。"""

from __future__ import annotations

from uuid import UUID

from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from jarvis_worker.database.models import McpServerModel, McpToolModel
from jarvis_worker.shared.domain.models import McpServer, McpServerStatus, McpTool, McpTransport


def _server(model: McpServerModel) -> McpServer:
    return McpServer(
        id=model.id, slug=model.slug, name=model.name, transport=McpTransport(model.transport),
        command=model.command, args=list(model.args_json or []), env_keys=list(model.env_keys_json or []),
        enabled=model.enabled, status=McpServerStatus(model.status), last_error_code=model.last_error_code,
        last_connected_at=model.last_connected_at, version=model.version,
        created_at=model.created_at, updated_at=model.updated_at,
    )


def _tool(model: McpToolModel) -> McpTool:
    return McpTool(
        id=model.id, server_id=model.server_id, original_name=model.original_name,
        internal_name=model.internal_name, description=model.description,
        input_schema=dict(model.input_schema_json or {}), risk_level=model.risk_level,
        enabled=model.enabled, discovered_at=model.discovered_at, updated_at=model.updated_at,
    )


class PostgresMcpServerRepository:
    def __init__(self, session: AsyncSession): self._session = session

    async def create(self, server: McpServer) -> McpServer:
        model = McpServerModel(
            id=server.id, slug=server.slug, name=server.name, transport=server.transport.value,
            command=server.command, args_json=server.args, env_keys_json=server.env_keys,
            enabled=server.enabled, status=server.status.value, last_error_code=server.last_error_code,
            last_connected_at=server.last_connected_at, version=server.version,
            created_at=server.created_at, updated_at=server.updated_at,
        )
        self._session.add(model)
        await self._session.flush()
        return _server(model)

    async def get(self, server_id: UUID) -> McpServer | None:
        model = await self._session.get(McpServerModel, server_id)
        return _server(model) if model else None

    async def list(self, *, enabled_only: bool = False) -> list[McpServer]:
        query = select(McpServerModel)
        if enabled_only:
            query = query.where(McpServerModel.enabled.is_(True))
        result = await self._session.execute(query.order_by(McpServerModel.created_at))
        return [_server(item) for item in result.scalars().all()]

    async def set_status(self, server_id: UUID, *, status: str, error_code: str | None, connected_at, updated_at) -> None:
        await self._session.execute(update(McpServerModel).where(McpServerModel.id == server_id).values(
            status=status, last_error_code=error_code, last_connected_at=connected_at, updated_at=updated_at,
        ))

    async def set_enabled(self, server_id: UUID, *, enabled: bool, expected_version: int, updated_at) -> McpServer | None:
        result = await self._session.execute(
            update(McpServerModel)
            .where(McpServerModel.id == server_id, McpServerModel.version == expected_version)
            .values(enabled=enabled, status="disconnected", last_error_code=None,
                    version=McpServerModel.version + 1, updated_at=updated_at)
            .returning(McpServerModel)
        )
        model = result.scalar_one_or_none()
        return _server(model) if model else None


class PostgresMcpToolRepository:
    def __init__(self, session: AsyncSession): self._session = session

    async def replace_discovery(self, server_id: UUID, tools: list[McpTool]) -> None:
        await self._session.execute(update(McpToolModel).where(McpToolModel.server_id == server_id).values(enabled=False))
        for tool in tools:
            await self._session.execute(insert(McpToolModel).values(
                id=tool.id, server_id=tool.server_id, original_name=tool.original_name,
                internal_name=tool.internal_name, description=tool.description,
                input_schema_json=tool.input_schema, risk_level=tool.risk_level,
                enabled=True, discovered_at=tool.discovered_at, updated_at=tool.updated_at,
            ).on_conflict_do_update(
                constraint="uq_mcp_tools_server_name",
                set_={"internal_name": tool.internal_name, "description": tool.description,
                      "input_schema_json": tool.input_schema, "enabled": True,
                      "discovered_at": tool.discovered_at, "updated_at": tool.updated_at},
            ))

    async def list_by_server(self, server_id: UUID, *, enabled_only: bool = False) -> list[McpTool]:
        query = select(McpToolModel).where(McpToolModel.server_id == server_id)
        if enabled_only:
            query = query.where(McpToolModel.enabled.is_(True))
        result = await self._session.execute(query.order_by(McpToolModel.internal_name))
        return [_tool(item) for item in result.scalars().all()]
