"""把已发现 MCP tools 适配到运行时唯一 ToolRegistry。"""

from __future__ import annotations

from jarvis_worker.agent.mcp.client import McpClient
from jarvis_worker.agent.tool_gateway.contracts import ToolManifest, ToolRequest, ToolResult
from jarvis_worker.agent.tool_gateway.modules import CapabilityModule, ToolBinding
from jarvis_worker.runtime.async_bridge import AsyncServiceBridge
from jarvis_worker.shared.domain.models import McpServer, McpTool
from jarvis_worker.shared.errors.application import AppError


def create_mcp_capability_modules(
    discoveries: list[tuple[McpServer, list[McpTool]]],
    client: McpClient,
    bridge: AsyncServiceBridge,
) -> tuple[CapabilityModule, ...]:
    modules: list[CapabilityModule] = []
    for server, tools in discoveries:
        bindings = tuple(_binding(server, tool, client, bridge) for tool in tools if tool.enabled)
        if bindings:
            modules.append(CapabilityModule(
                capability_id=f"mcp.{server.slug}", version="1", tool_bindings=bindings,
            ))
    return tuple(modules)


def _binding(server: McpServer, tool: McpTool, client: McpClient, bridge: AsyncServiceBridge) -> ToolBinding:
    manifest = ToolManifest(
        name=tool.internal_name, provider="mcp", description=tool.description,
        risk_level_default="L3", permission_scope="mcp_server",
        input_schema=tool.input_schema, allowed_decisions=["allow_once", "deny"],
        mcp_server_id=str(server.id), metadata={"original_tool_name": tool.original_name},
    )

    def execute(request: ToolRequest) -> ToolResult:
        try:
            data = bridge.run(client.call_tool(server, tool.original_name, request.arguments), timeout=25)
            return ToolResult(
                ok=True, kind="json", summary=f"MCP 工具 {tool.original_name} 执行完成", data=data,
                metadata={"provider": "mcp", "mcp_server_id": str(server.id), "original_tool_name": tool.original_name},
            )
        except AppError as error:
            return ToolResult(
                ok=False, kind="empty", summary="MCP 工具执行失败",
                error={"code": error.code, "message": error.message, "category": error.category,
                       "recoverable": error.recoverable},
                metadata={"provider": "mcp", "mcp_server_id": str(server.id)},
            )

    return ToolBinding(manifest=manifest, executor=execute)
