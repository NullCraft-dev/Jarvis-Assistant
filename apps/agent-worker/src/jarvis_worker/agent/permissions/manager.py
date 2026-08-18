"""PermissionManager — 风险分类与最小用户确认策略。"""

from __future__ import annotations

import logging

from jarvis_worker.agent.tool_gateway.contracts import (
    PermissionCheckResult,
    RiskLevel,
    ToolManifest,
    ToolRequest,
)

log = logging.getLogger("jarvis_worker.permission")


class PermissionManager:
    """最小权限管理器。

    当前策略：
      - L0 白名单只读工具 → auto allow
      - L1 → auto allow（仅在 manifest 明确声明时）
      - L2/L3/L4 → require user approval
      - L5 / 未知 L0 → deny

    不负责：
      - 用户交互/确认（由 Runtime + Web 负责）
      - 持久化 grant 规则（当前真实链只开放 allow_once）
      - 审计日志（由 ToolGateway 负责）
    """

    # L0 工具白名单（即使是 L0，也只允许已知安全工具）
    L0_ALLOWED_TOOLS: set[str] = {
        "workspace.list_files",
        "workspace.get_file_info",
        "workspace.read_file",
        "workspace.read_files",
        "workspace.search_files",
        "workspace.search_text",
        "rag.search",
        "rag.await_ingestion",
    }

    def check(
        self,
        manifest: ToolManifest,
        request: ToolRequest,
    ) -> PermissionCheckResult:
        """检查工具调用权限。

        Args:
            manifest: 工具 manifest（含默认风险等级）
            request: 工具调用请求

        Returns:
            PermissionCheckResult 包含 allow/deny/ask_user 决策
        """
        risk = manifest.risk_level_default

        # 定期任务创建时由用户授予的持久化、最小工具范围。该字段来自可信 RunJob，
        # 模型参数无法设置；当前只允许专用 Vault 的 L2 新建文档工具。
        scope = request.authorization_scope or {}
        if (
            risk == "L2"
            and scope.get("type") == "scheduled_task"
            and isinstance(scope.get("scheduled_task_id"), str)
            and scope.get("scheduled_task_id")
            and manifest.name == "knowledge.create_document"
            and manifest.name in scope.get("authorized_tools", [])
        ):
            return PermissionCheckResult(
                allowed=True, risk_level=risk, decision="allow",
                reason="用户已在创建定期任务时授权写入 Jarvis 专用知识库",
                needs_user_approval=False,
            )

        # L0 read-only + 白名单 → 自动 allow
        if risk == "L0" and manifest.name in self.L0_ALLOWED_TOOLS:
            log.info(
                "权限检查: tool=%s risk=%s → auto allow (L0 read-only)",
                manifest.name,
                risk,
            )
            return PermissionCheckResult(
                allowed=True,
                risk_level=risk,
                decision="allow",
                reason=f"L0 read-only 工具自动放行: {manifest.name}",
                needs_user_approval=False,
            )

        if risk == "L1":
            return PermissionCheckResult(
                allowed=True,
                risk_level=risk,
                decision="allow",
                reason=f"L1 低风险工具自动放行: {manifest.name}",
                needs_user_approval=False,
            )

        if risk in ("L2", "L3", "L4"):
            decisions = [
                value
                for value in manifest.allowed_decisions
                if value in ("allow_once", "deny")
            ]
            if "allow_once" not in decisions:
                decisions.insert(0, "allow_once")
            if "deny" not in decisions:
                decisions.append("deny")
            log.info(
                "权限检查: tool=%s risk=%s → ask user",
                manifest.name,
                risk,
            )
            return PermissionCheckResult(
                allowed=False,
                risk_level=risk,
                decision="ask_user",
                reason=f"工具 {manifest.name} (risk={risk}) 需要用户确认",
                needs_user_approval=True,
                allowed_decisions=decisions,
            )

        # L5 或伪装为 L0 但不在白名单 → deny
        log.warning(
            "权限检查: tool=%s risk=%s → deny",
            manifest.name,
            risk,
        )
        return PermissionCheckResult(
            allowed=False,
            risk_level=risk,
            decision="deny",
            reason=f"工具 {manifest.name} (risk={risk}) 当前策略禁止执行",
            needs_user_approval=False,
        )

    @staticmethod
    def classify_risk(manifest: ToolManifest) -> RiskLevel:
        """从 manifest 获取默认风险等级。

        后续可扩展为结合上下文动态计算风险等级。
        """
        return manifest.risk_level_default
