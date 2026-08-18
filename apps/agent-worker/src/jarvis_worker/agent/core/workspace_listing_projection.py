"""Workspace 目录列举的模型可见结果投影与最终回答范围校验。"""

from __future__ import annotations

import re
from typing import Any

from jarvis_worker.agent.core.actions import AgentAction
from jarvis_worker.agent.core.final_answer import FinalAnswerValidation
from jarvis_worker.agent.core.state import AgentState

WORKSPACE_LISTING_PROJECTION_VALIDATOR_ID = "workspace-listing-projection-v1"
_ENTRY_TYPES = frozenset({"file", "dir", "symlink", "other"})


def workspace_listing_entry_types(intent: object) -> frozenset[str]:
    """只从已校验 Intent 读取允许类型；损坏或旧 Intent 不启用投影。"""
    if not isinstance(intent, dict):
        return frozenset()
    workspace = intent.get("workspace")
    if not isinstance(workspace, dict):
        return frozenset()
    values = workspace.get("listing_entry_types")
    if (
        not isinstance(values, list)
        or not values
        or len(values) > len(_ENTRY_TYPES)
        or any(not isinstance(value, str) or value not in _ENTRY_TYPES for value in values)
        or len(set(values)) != len(values)
        or workspace.get("evidence") != "metadata"
        or workspace.get("action") != "read"
        or workspace.get("ambiguity") != "clear"
    ):
        return frozenset()
    return frozenset(values)


def project_workspace_listing_observations(
    observations: list[dict[str, Any]],
    intent: object,
) -> list[dict[str, Any]]:
    """为模型过滤 list_files entries；原始 state/ToolResult 保持不变。"""
    allowed = workspace_listing_entry_types(intent)
    if not allowed:
        return observations
    projected: list[dict[str, Any]] = []
    for observation in observations:
        if not (
            isinstance(observation, dict)
            and observation.get("tool_name") == "workspace.list_files"
            and observation.get("ok") is True
            and isinstance(observation.get("data"), dict)
        ):
            projected.append(observation)
            continue
        data = observation["data"]
        entries = data.get("entries")
        if not isinstance(entries, list):
            projected.append(observation)
            continue
        filtered = [
            dict(entry)
            for entry in entries
            if isinstance(entry, dict) and entry.get("type") in allowed
        ]
        projected_data = dict(data)
        projected_data["entries"] = filtered
        projected_observation = dict(observation)
        projected_observation["data"] = projected_data
        projected.append(projected_observation)
    return projected


class WorkspaceListingProjectionValidator:
    """阻止最终回答重新提及 Intent 已排除的根目录条目。"""

    validator_id = WORKSPACE_LISTING_PROJECTION_VALIDATOR_ID

    def requires_buffered_output(self, state: AgentState) -> bool:
        return bool(workspace_listing_entry_types(state.intent))

    def validate(
        self,
        *,
        action: AgentAction,
        state: AgentState,
    ) -> FinalAnswerValidation:
        allowed = workspace_listing_entry_types(state.intent)
        if not allowed:
            return FinalAnswerValidation(accepted=True, output=action.final_message)
        excluded_names = _excluded_listing_names(state.observations, allowed)
        if not any(_mentions_entry(action.final_message, name) for name in excluded_names):
            return FinalAnswerValidation(accepted=True, output=action.final_message)
        return FinalAnswerValidation(
            accepted=False,
            output="",
            feedback=(
                "Workspace 列举回答超出了用户明确要求的条目类型范围。请只重写 final_message，"
                "仅使用 Runtime Intent 允许的 listing_entry_types 和当前成功 ToolResult；不得提及"
                "被排除的文件、目录、符号链接或其他条目，也不得再次调用工具。"
            ),
            reason_code="WORKSPACE_LISTING_OUTPUT_SCOPE",
        )


def _excluded_listing_names(
    observations: list[dict[str, Any]],
    allowed: frozenset[str],
) -> tuple[str, ...]:
    allowed_names: set[str] = set()
    excluded_names: list[str] = []
    for observation in observations:
        if not (
            isinstance(observation, dict)
            and observation.get("tool_name") == "workspace.list_files"
            and observation.get("ok") is True
            and isinstance(observation.get("data"), dict)
        ):
            continue
        entries = observation["data"].get("entries")
        if not isinstance(entries, list):
            continue
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            name = entry.get("name")
            if not isinstance(name, str) or not name:
                continue
            if entry.get("type") in allowed:
                allowed_names.add(name)
            elif name not in excluded_names:
                excluded_names.append(name)
    return tuple(name for name in excluded_names if name not in allowed_names)


def _mentions_entry(output: str, name: str) -> bool:
    return (
        re.search(
            rf"(?<![\w.-]){re.escape(name)}(?![\w.-])",
            output,
            flags=re.IGNORECASE,
        )
        is not None
    )
