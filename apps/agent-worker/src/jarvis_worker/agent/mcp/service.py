"""MCP 配置、发现与审计的 application service。"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from uuid import UUID

from jarvis_worker.agent.mcp.client import McpClient
from jarvis_worker.database.unit_of_work import PostgresUnitOfWork
from jarvis_worker.shared.domain.models import AuditLog, McpServer, McpServerStatus, McpTool, McpTransport, new_id, utcnow
from jarvis_worker.shared.errors.application import AppError

_SLUG = re.compile(r"^[a-z][a-z0-9_-]{0,63}$")
_ENV_KEY = re.compile(r"^[A-Z_][A-Z0-9_]*$")


@dataclass(frozen=True, slots=True)
class CreateMcpServerInput:
    slug: str
    name: str
    command: str
    args: list[str]
    env_keys: list[str]


class McpApplicationService:
    def __init__(self, uow_factory): self._uow_factory = uow_factory

    async def list_servers(self) -> list[McpServer]:
        async with self._uow_factory()() as session:
            return await PostgresUnitOfWork(session).mcp_servers.list()

    async def list_tools(self, server_id: UUID, *, enabled_only: bool = False) -> list[McpTool]:
        async with self._uow_factory()() as session:
            return await PostgresUnitOfWork(session).mcp_tools.list_by_server(server_id, enabled_only=enabled_only)

    async def create_server(self, value: CreateMcpServerInput) -> McpServer:
        command = self._validate(value)
        now = utcnow()
        server = McpServer(
            id=new_id(), slug=value.slug, name=value.name.strip(), transport=McpTransport.STDIO,
            command=command, args=list(value.args), env_keys=list(dict.fromkeys(value.env_keys)),
            created_at=now, updated_at=now,
        )
        async with self._uow_factory()() as session:
            uow = PostgresUnitOfWork(session)
            async with uow.transaction() as tx:
                server = await tx.mcp_servers.create(server)
                await tx.audits.create(AuditLog(
                    id=new_id(), event_type="mcp.server.created", actor="user", risk_level="L4",
                    permission_decision="user_explicit", action_summary=f"注册 MCP server：{server.name}",
                    details={"server_id": str(server.id), "slug": server.slug, "transport": "stdio",
                             "command": server.command, "argument_count": len(server.args), "env_keys": server.env_keys},
                    result_summary="MCP server 配置已保存，尚未发现工具",
                ))
                await tx.commit()
        return server

    async def set_enabled(self, server_id: UUID, enabled: bool, expected_version: int) -> McpServer:
        now = utcnow()
        async with self._uow_factory()() as session:
            uow = PostgresUnitOfWork(session)
            async with uow.transaction() as tx:
                current = await tx.mcp_servers.get(server_id)
                if current is None:
                    raise AppError("MCP_SERVER_NOT_FOUND", "MCP server 不存在", "not_found")
                updated = await tx.mcp_servers.set_enabled(
                    server_id, enabled=enabled, expected_version=expected_version, updated_at=now,
                )
                if updated is None:
                    raise AppError("VERSION_CONFLICT", "MCP server 配置已变化，请刷新后重试", "conflict", True)
                await tx.audits.create(AuditLog(
                    id=new_id(), event_type="mcp.server.enabled" if enabled else "mcp.server.disabled",
                    actor="user", risk_level="L4", permission_decision="user_explicit",
                    action_summary=f"{'启用' if enabled else '停用'} MCP server：{current.name}",
                    details={"server_id": str(server_id)}, result_summary="配置已更新；Worker 重启后生效",
                ))
                await tx.commit()
                return updated

    async def refresh_enabled(self, client: McpClient) -> list[tuple[McpServer, list[McpTool]]]:
        async with self._uow_factory()() as session:
            servers = await PostgresUnitOfWork(session).mcp_servers.list(enabled_only=True)
        bindings: list[tuple[McpServer, list[McpTool]]] = []
        for server in servers:
            try:
                discovered = await client.discover(server)
                now = utcnow()
                tools = [McpTool(
                    id=new_id(), server_id=server.id, original_name=item.name,
                    internal_name=_internal_name(server.slug, item.name),
                    description=_description(item.description), input_schema=_schema(item.input_schema),
                    discovered_at=now, updated_at=now,
                ) for item in discovered]
                internal_names = [item.internal_name for item in tools]
                if len(internal_names) != len(set(internal_names)):
                    raise AppError("MCP_TOOL_NAME_COLLISION", "MCP 工具名称规范化后发生冲突", "tool")
                async with self._uow_factory()() as session:
                    uow = PostgresUnitOfWork(session)
                    async with uow.transaction() as tx:
                        await tx.mcp_tools.replace_discovery(server.id, tools)
                        await tx.mcp_servers.set_status(server.id, status="connected", error_code=None, connected_at=now, updated_at=now)
                        await tx.audits.create(AuditLog(
                            id=new_id(), event_type="mcp.discovery.completed", actor="system", risk_level="L0",
                            permission_decision="system", action_summary=f"发现 MCP 工具：{server.name}",
                            details={"server_id": str(server.id), "tool_count": len(tools)}, result_summary="MCP 工具发现完成",
                        ))
                        await tx.commit()
                server.status = McpServerStatus.CONNECTED
                server.last_connected_at = now
                bindings.append((server, tools))
            except AppError as error:
                now = utcnow()
                async with self._uow_factory()() as session:
                    uow = PostgresUnitOfWork(session)
                    async with uow.transaction() as tx:
                        await tx.mcp_servers.set_status(server.id, status="error", error_code=error.code, connected_at=server.last_connected_at, updated_at=now)
                        await tx.audits.create(AuditLog(
                            id=new_id(), event_type="mcp.discovery.failed", actor="system", risk_level="L0",
                            permission_decision="system", action_summary=f"发现 MCP 工具失败：{server.name}",
                            details={"server_id": str(server.id)}, result_summary="MCP server 不可用",
                            error={"code": error.code, "message": error.message, "category": error.category,
                                   "recoverable": error.recoverable},
                        ))
                        await tx.commit()
        return bindings

    @staticmethod
    def _validate(value: CreateMcpServerInput) -> str:
        if not _SLUG.fullmatch(value.slug):
            raise AppError("VALIDATION_ERROR", "MCP slug 格式无效", "validation")
        if not value.name.strip() or len(value.name.strip()) > 200:
            raise AppError("VALIDATION_ERROR", "MCP 名称格式无效", "validation")
        path = Path(value.command)
        if not path.is_absolute() or path.is_symlink() or not path.is_file() or not os.access(path, os.X_OK):
            raise AppError("MCP_COMMAND_INVALID", "MCP command 必须是已存在且可执行的绝对文件路径", "validation")
        if len(value.args) > 50 or any(not isinstance(arg, str) or len(arg) > 500 or "\0" in arg for arg in value.args):
            raise AppError("VALIDATION_ERROR", "MCP arguments 格式无效", "validation")
        if len(value.env_keys) > 30 or any(not isinstance(key, str) or not _ENV_KEY.fullmatch(key) for key in value.env_keys):
            raise AppError("VALIDATION_ERROR", "MCP env_keys 格式无效", "validation")
        return str(path.resolve(strict=True))


def _internal_name(slug: str, original: str) -> str:
    normalized = re.sub(r"[^a-zA-Z0-9_-]+", "_", original).strip("_").lower()
    if not normalized:
        raise AppError("MCP_TOOL_NAME_INVALID", "MCP 工具名称无法规范化", "tool")
    return f"mcp.{slug}.{normalized[:120]}"


def _description(value: str) -> str:
    return " ".join(value.replace("\0", "").split())[:500]


def _schema(value: dict) -> dict:
    if not isinstance(value, dict) or len(json.dumps(value, ensure_ascii=False).encode()) > 32 * 1024:
        raise AppError("MCP_SCHEMA_INVALID", "MCP 工具参数 schema 无效或过大", "tool")
    schema = dict(value)
    if schema.get("type") not in (None, "object"):
        raise AppError("MCP_SCHEMA_INVALID", "MCP 工具参数 schema 必须是 object", "tool")
    schema.setdefault("type", "object")
    schema.setdefault("properties", {})
    return schema
