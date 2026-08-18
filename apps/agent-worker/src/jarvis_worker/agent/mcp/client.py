"""受限的 MCP stdio client。

每次发现或调用都创建短生命周期 session，避免把子进程生命周期泄漏到 Agent
loop。环境变量只传递最小运行环境与 server 显式声明的 key。
"""

from __future__ import annotations

import asyncio
import json
import os
from dataclasses import dataclass
from typing import Any

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import get_default_environment, stdio_client

from jarvis_worker.shared.domain.models import McpServer
from jarvis_worker.shared.errors.application import AppError


@dataclass(frozen=True, slots=True)
class DiscoveredMcpTool:
    name: str
    description: str
    input_schema: dict[str, Any]


class McpClient:
    def __init__(self, *, timeout_seconds: float = 20, max_result_bytes: int = 64 * 1024):
        self._timeout = timeout_seconds
        self._max_result_bytes = max_result_bytes

    async def discover(self, server: McpServer) -> list[DiscoveredMcpTool]:
        async def operation(session: ClientSession):
            result = await session.list_tools()
            return [
                DiscoveredMcpTool(
                    name=item.name,
                    description=item.description or "",
                    input_schema=item.inputSchema if isinstance(item.inputSchema, dict) else {},
                )
                for item in result.tools
            ]

        return await self._with_session(server, operation)

    async def call_tool(self, server: McpServer, tool_name: str, arguments: dict[str, Any]) -> Any:
        async def operation(session: ClientSession):
            result = await session.call_tool(tool_name, arguments)
            if result.isError:
                raise AppError("MCP_TOOL_FAILED", "MCP 工具执行失败", "tool", recoverable=True)
            payload: Any
            if result.structuredContent is not None:
                payload = result.structuredContent
            else:
                payload = [item.model_dump(mode="json") for item in result.content]
            encoded = json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")
            if len(encoded) > self._max_result_bytes:
                raise AppError("MCP_RESULT_TOO_LARGE", "MCP 工具返回结果超过大小限制", "tool")
            return payload

        return await self._with_session(server, operation)

    async def _with_session(self, server: McpServer, operation):
        params = StdioServerParameters(
            command=server.command,
            args=list(server.args),
            env=self._environment(server),
        )
        try:
            # MCP SDK 把 errlog 直接作为子进程文件描述符使用；丢弃不受信任的
            # server stderr，避免其绕过结构化日志并意外泄漏敏感信息。
            with open(os.devnull, "w", encoding="utf-8") as errlog:
                async with asyncio.timeout(self._timeout):
                    async with stdio_client(params, errlog=errlog) as (read, write):
                        async with ClientSession(read, write) as session:
                            await session.initialize()
                            return await operation(session)
        except AppError:
            raise
        except TimeoutError as exc:
            raise AppError("MCP_TIMEOUT", "MCP server 响应超时", "tool", recoverable=True) from exc
        except Exception as exc:
            raise AppError("MCP_SERVER_UNAVAILABLE", "MCP server 不可用", "tool", recoverable=True) from exc

    @staticmethod
    def _environment(server: McpServer) -> dict[str, str]:
        env = get_default_environment()
        for key in server.env_keys:
            value = os.environ.get(key)
            if value is not None:
                env[key] = value
        return env
