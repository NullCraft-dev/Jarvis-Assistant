from __future__ import annotations

import asyncio
import sys
from pathlib import Path

from jarvis_worker.agent.mcp.adapter import create_mcp_capability_modules
from jarvis_worker.agent.mcp.client import McpClient
from jarvis_worker.agent.permissions.manager import PermissionManager
from jarvis_worker.agent.tool_gateway.catalog import install_capability_modules
from jarvis_worker.agent.tool_gateway.contracts import PermissionApproval, ToolRequest
from jarvis_worker.agent.tool_gateway.gateway import ToolGateway
from jarvis_worker.agent.tool_gateway.registry import ToolRegistry
from jarvis_worker.runtime.async_bridge import AsyncServiceBridge
from jarvis_worker.runtime.worker import AgentWorker
from jarvis_worker.runtime_bus.messages import McpDiscoveryRefreshCommand
from jarvis_worker.shared.domain.models import McpServer, McpTool, McpTransport, new_id


def _server() -> McpServer:
    return McpServer(
        id=new_id(), slug="local_test", name="Local test", transport=McpTransport.STDIO,
        command=sys.executable,
        args=[str(Path(__file__).parent / "fixtures" / "mcp_echo_server.py")],
    )


def test_stdio_client_discovers_and_calls_real_mcp_server():
    client = McpClient(timeout_seconds=10)
    server = _server()

    tools = asyncio.run(client.discover(server))
    assert [item.name for item in tools] == ["echo"]
    assert tools[0].input_schema["required"] == ["text"]

    result = asyncio.run(client.call_tool(server, "echo", {"text": "Jarvis"}))
    assert result == {"echo": "Jarvis"}


def test_mcp_adapter_cannot_bypass_tool_gateway_permission():
    client = McpClient(timeout_seconds=10)
    server = _server()
    discovered = asyncio.run(client.discover(server))[0]
    tool = McpTool(
        id=new_id(), server_id=server.id, original_name=discovered.name,
        internal_name="mcp.local_test.echo", description=discovered.description,
        input_schema=discovered.input_schema,
    )
    bridge = AsyncServiceBridge()
    try:
        modules = create_mcp_capability_modules([(server, [tool])], client, bridge)
        registry = ToolRegistry()
        install_capability_modules(registry, modules)
        gateway = ToolGateway(registry, PermissionManager())
        request = ToolRequest(
            task_id=str(new_id()), run_id=str(new_id()), tool_name=tool.internal_name,
            arguments={"text": "approved"}, requested_by="agent",
        )

        pending = gateway.execute(request)
        assert pending.error and pending.error["code"] == "PERMISSION_REQUIRED"

        result = gateway.execute(
            request, approval=PermissionApproval(request_id=str(new_id()), decision="allow_once")
        )
        assert result.ok is True
        assert result.data == {"echo": "approved"}
        assert result.metadata["mcp_server_id"] == str(server.id)
    finally:
        bridge.close()


def test_idle_worker_executes_and_acks_mcp_discovery_command():
    class McpService:
        calls = 0

        async def refresh_enabled(self, client):
            self.calls += 1
            return []

    class CommandConsumer:
        acked: list[str] = []

        def ack(self, message_id):
            self.acked.append(message_id)
            return True

    class Runner:
        worker_id = "mcp-worker"

    bridge = AsyncServiceBridge()
    service = McpService()
    consumer = CommandConsumer()
    try:
        worker = AgentWorker(
            object(), object(), object(), Runner(), cmd_consumer=consumer,
            mcp_service=service, mcp_client=object(), service_bridge=bridge,
        )
        worker._handle_claimed_command(McpDiscoveryRefreshCommand(
            command_id="mcp-command-1", trace_id="trace-1",
            requested_at="2026-07-26T12:00:00Z",
        ), "redis-message-1")
        assert service.calls == 1
        assert consumer.acked == ["redis-message-1"]
    finally:
        bridge.close()


def test_busy_worker_defers_mcp_discovery_without_ack():
    class CommandConsumer:
        acked: list[str] = []

        def ack(self, message_id):
            self.acked.append(message_id)
            return True

    class Runner:
        worker_id = "mcp-worker"

    consumer = CommandConsumer()
    worker = AgentWorker(
        object(), object(), object(), Runner(), cmd_consumer=consumer,
        mcp_service=object(), mcp_client=object(),
    )
    worker._set_active_run_id("active-run")
    worker._handle_claimed_command(McpDiscoveryRefreshCommand(
        command_id="mcp-command-2", trace_id="trace-2",
        requested_at="2026-07-26T12:00:00Z",
    ), "redis-message-2")
    assert consumer.acked == []
