"""ToolGateway — 工具调用统一入口。

职责：
- 校验工具名是否存在且启用。
- 请求 PermissionManager 判断。
- 调用已注册的 capability executor。
- 返回结构化 ToolResult。

不负责：
- 决定 Agent 下一步要做什么。
- 绕过权限直接执行本地动作。
- MCP server 管理（后续迭代）。
- 保存 AuditLog（本轮由 RuntimeEvent 覆盖）。

调用链：
AgentRunner
-> ToolGateway.execute(request)
-> ToolRegistry.get_manifest / get_executor
-> PermissionManager.check
-> Capability Executor
-> ToolResult
-> AgentRunner observe

对齐 docs/15-mcp-tool-gateway-design.md § ToolGateway 职责。
"""

from __future__ import annotations

import logging
import time

from jarvis_worker.agent.permissions.manager import PermissionManager
from jarvis_worker.agent.tool_gateway.contracts import (
    PermissionApproval,
    PermissionCheckResult,
    ToolManifest,
    ToolRequest,
    ToolResult,
)
from jarvis_worker.agent.tool_gateway.effect_boundary import (
    ToolEffectBoundary,
    ToolEffectBoundaryError,
)
from jarvis_worker.agent.tool_gateway.registry import ToolRegistry

log = logging.getLogger("jarvis_worker.agent.tool_gateway")


class ToolGateway:
    """工具调用统一入口。

    用法：
        registry = ToolRegistry()
        registry.register(manifest, executor)

        perm_mgr = PermissionManager()
        gateway = ToolGateway(registry, perm_mgr)

        result = gateway.execute(request)
    """

    def __init__(
        self,
        registry: ToolRegistry,
        permission_manager: PermissionManager | None = None,
        *,
        effect_boundary: ToolEffectBoundary | None = None,
    ):
        self._registry = registry
        self._permission = permission_manager or PermissionManager()
        self._effect_boundary = effect_boundary

    @property
    def registry(self) -> ToolRegistry:
        return self._registry

    @property
    def permission_manager(self) -> PermissionManager:
        return self._permission

    def assess(
        self, request: ToolRequest
    ) -> tuple[ToolManifest | None, PermissionCheckResult | None, ToolResult | None]:
        """校验工具、参数并返回权限判断；不执行任何本地动作。"""
        manifest = self._registry.get_manifest(request.tool_name)
        if manifest is None:
            return (
                None,
                None,
                ToolResult(
                    ok=False,
                    kind="empty",
                    summary=f"未知工具: {request.tool_name}",
                    error={
                        "code": "TOOL_NOT_FOUND",
                        "message": f"工具 {request.tool_name} 未注册",
                        "category": "tool",
                        "recoverable": False,
                    },
                ),
            )
        if not manifest.enabled:
            return (
                manifest,
                None,
                ToolResult(
                    ok=False,
                    kind="empty",
                    summary=f"工具已禁用: {request.tool_name}",
                    error={
                        "code": "TOOL_DISABLED",
                        "message": f"工具 {request.tool_name} 已被禁用",
                        "category": "tool",
                        "recoverable": False,
                    },
                ),
            )
        schema_error = _validate_arguments(manifest.input_schema, request.arguments)
        if schema_error is not None:
            return (
                manifest,
                None,
                ToolResult(
                    ok=False,
                    kind="empty",
                    summary="工具参数校验失败",
                    error={
                        "code": "TOOL_ARGUMENTS_INVALID",
                        "message": schema_error,
                        "category": "validation",
                        "recoverable": True,
                    },
                ),
            )
        return manifest, self._permission.check(manifest, request), None

    def execute(
        self,
        request: ToolRequest,
        approval: PermissionApproval | None = None,
    ) -> ToolResult:
        """执行一个工具调用。

        流程：
        1. 校验工具名是否存在
        2. 校验工具是否启用
        3. 获取 manifest 和风险等级
        4. PermissionManager.check
        5. 执行工具
        6. 返回结构化 ToolResult

        Args:
            request: 工具调用请求

        Returns:
            ToolResult（ok=True 成功，ok=False 失败含 error）
        """
        manifest, perm_result, assessment_error = self.assess(request)
        if assessment_error is not None:
            return assessment_error
        assert manifest is not None and perm_result is not None

        if approval is not None and (
            approval.decision != "allow_once" or not approval.request_id.strip()
        ):
            return ToolResult(
                ok=False,
                kind="empty",
                summary="无效的权限批准",
                error={
                    "code": "PERMISSION_APPROVAL_INVALID",
                    "message": "权限批准缺少有效的一次性请求标识",
                    "category": "permission",
                    "recoverable": False,
                },
            )

        if perm_result.needs_user_approval and approval is None:
            return ToolResult(
                ok=False,
                kind="empty",
                summary=perm_result.reason,
                error={
                    "code": "PERMISSION_REQUIRED",
                    "message": perm_result.reason,
                    "category": "permission",
                    "recoverable": True,
                },
                metadata={
                    "risk_level": perm_result.risk_level,
                    "allowed_decisions": perm_result.allowed_decisions,
                },
            )

        if not perm_result.allowed and approval is None:
            log.warning(
                "工具执行被拒绝: tool=%s risk=%s reason=%s",
                request.tool_name,
                perm_result.risk_level,
                perm_result.reason,
            )
            return ToolResult(
                ok=False,
                kind="empty",
                summary=f"权限拒绝: {perm_result.reason}",
                error={
                    "code": "PERMISSION_DENIED",
                    "message": perm_result.reason,
                    "category": "permission",
                    "recoverable": False,
                },
            )

        if approval is not None and not perm_result.needs_user_approval:
            return ToolResult(
                ok=False,
                kind="empty",
                summary="无效的权限批准",
                error={
                    "code": "PERMISSION_APPROVAL_INVALID",
                    "message": "该工具调用不需要用户批准",
                    "category": "permission",
                    "recoverable": False,
                },
            )

        # 4. 经统一入口调用已注册的 capability executor
        executor = self._registry.get_executor(request.tool_name)
        if executor is None:
            # 不应发生：manifest 存在但 executor 不存在
            return ToolResult(
                ok=False,
                kind="empty",
                summary=f"工具执行器缺失: {request.tool_name}",
                error={
                    "code": "EXECUTOR_NOT_FOUND",
                    "message": f"工具 {request.tool_name} 的执行器未注册",
                    "category": "internal",
                    "recoverable": False,
                },
            )

        # 只有经过一次性持久化批准的 effect 才进入该测试端口。生产默认没有
        # effect_boundary；模型参数也无法自行开启或控制这个窗口。
        if approval is not None and self._effect_boundary is not None:
            try:
                self._effect_boundary.before_effect(
                    request=request,
                    manifest=manifest,
                    approval=approval,
                )
            except ToolEffectBoundaryError as exc:
                log.error(
                    "工具副作用边界拒绝执行: tool=%s code=%s",
                    request.tool_name,
                    exc.code,
                    extra={
                        "task_id": request.task_id,
                        "run_id": request.run_id,
                        "step_id": request.step_id,
                    },
                )
                return ToolResult(
                    ok=False,
                    kind="empty",
                    summary="测试副作用屏障未安全释放",
                    error={
                        "code": exc.code,
                        "message": str(exc),
                        "category": "runtime",
                        "recoverable": False,
                    },
                )
            except Exception:
                log.exception(
                    "工具副作用边界发生未分类异常: tool=%s",
                    request.tool_name,
                    extra={
                        "task_id": request.task_id,
                        "run_id": request.run_id,
                        "step_id": request.step_id,
                    },
                )
                return ToolResult(
                    ok=False,
                    kind="empty",
                    summary="测试副作用屏障不可用",
                    error={
                        "code": "TOOL_EFFECT_BOUNDARY_ERROR",
                        "message": "测试副作用屏障发生内部错误",
                        "category": "runtime",
                        "recoverable": False,
                    },
                )

        log.info(
            "工具执行开始: tool=%s risk=%s argument_names=%s",
            request.tool_name,
            perm_result.risk_level,
            sorted(key for key in request.arguments if key != "workspace_root"),
            extra={
                "task_id": request.task_id,
                "run_id": request.run_id,
                "step_id": request.step_id,
            },
        )

        started_at = time.monotonic()
        try:
            result = executor(request)
        except Exception as e:
            log.error(
                "工具执行异常: tool=%s duration_ms=%d error_type=%s",
                request.tool_name,
                int((time.monotonic() - started_at) * 1000),
                type(e).__name__,
                extra={
                    "task_id": request.task_id,
                    "run_id": request.run_id,
                    "step_id": request.step_id,
                },
            )
            return ToolResult(
                ok=False,
                kind="empty",
                summary=f"工具执行异常: {request.tool_name}",
                error={
                    "code": "TOOL_EXECUTION_ERROR",
                    "message": "工具执行时发生意外错误",
                    "category": "tool",
                    "recoverable": True,
                },
            )

        log.info(
            "工具执行结束: tool=%s ok=%s duration_ms=%d result_kind=%s",
            request.tool_name,
            result.ok,
            int((time.monotonic() - started_at) * 1000),
            result.kind,
            extra={
                "task_id": request.task_id,
                "run_id": request.run_id,
                "step_id": request.step_id,
            },
        )
        return result


def _validate_arguments(schema: dict, arguments: dict) -> str | None:
    """校验项目当前使用的最小 JSON Schema 子集。"""
    if not schema:
        return None
    if schema.get("type") == "object" and not isinstance(arguments, dict):
        return "工具参数必须是 object"
    properties = schema.get("properties") or {}
    for name in schema.get("required") or []:
        if name not in arguments:
            return f"缺少必要参数: {name}"
    if schema.get("additionalProperties") is False:
        unknown = sorted(set(arguments) - set(properties))
        if unknown:
            return f"包含未知参数: {', '.join(unknown)}"
    type_map = {
        "string": str,
        "integer": int,
        "number": (int, float),
        "boolean": bool,
        "object": dict,
        "array": list,
    }
    for name, value in arguments.items():
        expected = (properties.get(name) or {}).get("type")
        python_type = type_map.get(expected)
        if python_type is not None and not isinstance(value, python_type):
            return f"参数 {name} 类型必须是 {expected}"
    return None
