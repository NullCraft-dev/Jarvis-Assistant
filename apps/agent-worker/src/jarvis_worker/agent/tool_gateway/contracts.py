"""ToolGateway 核心类型定义。

对齐：
- docs/15-mcp-tool-gateway-design.md § Tool Manifest / Tool Request / Tool Result
- docs/13-interface-contract.md § ToolCallDTO / ToolResultDTO
- shared/src/types.ts ToolCallDTO / ToolResultDTO
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

# -- 工具提供方 --
Provider = Literal["native", "mcp", "system"]

# -- 风险等级 --
RiskLevel = Literal["L0", "L1", "L2", "L3", "L4", "L5"]

# -- 权限决策 --
PermissionDecision = Literal["allow", "deny", "ask_user"]

# -- 工具结果类型 --
ResultKind = Literal["text", "json", "file", "artifact", "empty"]


@dataclass(frozen=True)
class ToolDeliverable:
    """工具已经真实生成、可由 Runtime 投影为 Artifact 的可信描述。"""

    kind: Literal["file"]
    title: str
    path: str
    size_bytes: int
    mime_type: str
    content_hash: str


@dataclass
class ToolManifest:
    """统一工具 manifest — 所有 native/system/MCP 工具的统一描述。

    对齐 docs/15-mcp-tool-gateway-design.md § Tool Manifest。
    """

    name: str                               # 工具名，如 "workspace.list_files"
    provider: Provider = "native"
    description: str = ""
    risk_level_default: RiskLevel = "L0"
    enabled: bool = True
    # 权限范围：工具需要访问的路径前缀
    permission_scope: str = "workspace"
    # 最小 JSON Schema 子集，由 ToolGateway 在执行前统一校验。
    input_schema: dict[str, Any] = field(default_factory=dict)
    # 需要用户确认时允许展示的决策。高风险工具不得包含永久授权。
    allowed_decisions: list[str] = field(
        default_factory=lambda: ["allow_once", "deny"]
    )
    mcp_server_id: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class ToolRequest:
    """AgentRunner 发起的工具调用请求。

    对齐 docs/15-mcp-tool-gateway-design.md § Tool Request。
    """

    task_id: str
    run_id: str
    step_id: str = ""
    tool_name: str = ""
    arguments: dict[str, Any] = field(default_factory=dict)
    reason: str = ""
    requested_by: Literal["agent", "user", "system"] = "agent"
    authorization_scope: dict[str, Any] = field(default_factory=dict)
    # Runtime 生成、模型不可写入的执行上下文，例如受控 Artifact id。
    execution_context: dict[str, Any] = field(default_factory=dict)


@dataclass
class ToolResult:
    """工具执行结果。

    对齐 docs/15-mcp-tool-gateway-design.md § Tool Result。
    """

    ok: bool
    kind: ResultKind = "empty"
    summary: str = ""
    data: Any = None
    artifact_ids: list[str] = field(default_factory=list)
    deliverables: list[ToolDeliverable] = field(default_factory=list)
    error: dict[str, Any] | None = None     # AppError shape
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class PermissionCheckResult:
    """PermissionManager 检查结果。"""

    allowed: bool
    risk_level: RiskLevel = "L0"
    decision: PermissionDecision = "allow"
    reason: str = ""
    # 如果需要用户确认，这里包含权限请求信息
    needs_user_approval: bool = False
    allowed_decisions: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class PermissionApproval:
    """已由持久化 PermissionRequest 验证过的一次性批准。"""

    request_id: str
    decision: Literal["allow_once"]
