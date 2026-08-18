"""可持久化的 Loop 完成、进展与停止契约。"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass
from pathlib import PurePosixPath
from typing import Any, Literal

COMPLETION_CONTRACT_VERSION = "completion-contract-v2"
LEGACY_COMPLETION_CONTRACT_VERSIONS = frozenset({"completion-contract-v1"})
LOOP_PROGRESS_VERSION = "loop-progress-v1"
LOOP_STOP_DECISION_VERSION = "loop-stop-v1"

_WORKSPACE_EVIDENCE = frozenset({"skip", "metadata", "required"})
_WORKSPACE_ACTIONS = frozenset({"none", "read", "write", "destructive"})
_WORKSPACE_EFFECT_PRECONDITIONS = frozenset({"none", "target_absent"})
_STOP_DISPOSITIONS = frozenset({"continue", "complete", "clarify", "fail"})
_REASON_CODE_RE = re.compile(r"^[A-Z][A-Z0-9_]{2,79}$")
_MAX_REQUIREMENTS = 24
_MAX_ACTION_FINGERPRINTS = 40


def _bounded_strings(
    value: object,
    *,
    maximum_items: int,
    maximum_length: int,
) -> tuple[str, ...]:
    if (
        not isinstance(value, list)
        or len(value) > maximum_items
        or any(
            not isinstance(item, str) or not item or len(item) > maximum_length for item in value
        )
        or len(set(value)) != len(value)
    ):
        raise ValueError("Loop 字符串列表无效")
    return tuple(value)


@dataclass(frozen=True, slots=True)
class CompletionContract:
    """由 Runtime 从已校验 Intent 冻结的完成要求。"""

    required_tool_names: tuple[str, ...] = ()
    requires_rag_evidence: bool = False
    workspace_evidence: str = "skip"
    workspace_action: str = "none"
    workspace_effect_precondition: str = "none"
    workspace_effect_target: str = ""
    clarification_required: bool = False
    version: str = COMPLETION_CONTRACT_VERSION

    def to_state_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["required_tool_names"] = list(self.required_tool_names)
        return value

    @classmethod
    def from_state_dict(cls, value: object) -> "CompletionContract":
        legacy_expected = {
            "required_tool_names",
            "requires_rag_evidence",
            "workspace_evidence",
            "workspace_action",
            "clarification_required",
            "version",
        }
        expected = legacy_expected | {
            "workspace_effect_precondition",
            "workspace_effect_target",
        }
        if not isinstance(value, dict):
            raise ValueError("completion contract 结构无效")
        version = value.get("version")
        if version in LEGACY_COMPLETION_CONTRACT_VERSIONS:
            if set(value) != legacy_expected:
                raise ValueError("completion contract 结构无效")
            effect_precondition = "none"
            effect_target = ""
        elif version == COMPLETION_CONTRACT_VERSION:
            if set(value) != expected:
                raise ValueError("completion contract 结构无效")
            effect_precondition = value["workspace_effect_precondition"]
            effect_target = value["workspace_effect_target"]
        else:
            raise ValueError("completion contract 结构无效")
        required = _bounded_strings(
            value["required_tool_names"],
            maximum_items=_MAX_REQUIREMENTS,
            maximum_length=200,
        )
        if (
            value["workspace_evidence"] not in _WORKSPACE_EVIDENCE
            or value["workspace_action"] not in _WORKSPACE_ACTIONS
            or effect_precondition not in _WORKSPACE_EFFECT_PRECONDITIONS
            or not _valid_workspace_effect_target(effect_target)
            or (effect_precondition == "none" and effect_target)
            or (
                effect_precondition == "target_absent"
                and (
                    not effect_target
                    or (
                        value["workspace_action"] != "write"
                        and "workspace.create_file" not in required
                    )
                )
            )
            or not isinstance(value["requires_rag_evidence"], bool)
            or not isinstance(value["clarification_required"], bool)
        ):
            raise ValueError("completion contract 字段无效")
        return cls(
            required_tool_names=required,
            requires_rag_evidence=value["requires_rag_evidence"],
            workspace_evidence=value["workspace_evidence"],
            workspace_action=value["workspace_action"],
            workspace_effect_precondition=effect_precondition,
            workspace_effect_target=effect_target,
            clarification_required=value["clarification_required"],
        )


def _valid_workspace_effect_target(value: object) -> bool:
    if not isinstance(value, str) or len(value) > 500:
        return False
    if not value:
        return True
    if "\\" in value or "\x00" in value or value.startswith("/"):
        return False
    path = PurePosixPath(value)
    return bool(path.suffix) and not path.is_absolute() and all(
        part not in {"", ".", ".."} for part in path.parts
    )


@dataclass(frozen=True, slots=True)
class LoopProgressSnapshot:
    """只由可信 ToolResult observation 推导的通用进展快照。"""

    tool_calls_used: int = 0
    successful_tool_names: tuple[str, ...] = ()
    failed_tool_names: tuple[str, ...] = ()
    successful_action_fingerprints: tuple[str, ...] = ()
    failed_action_fingerprints: tuple[str, ...] = ()
    no_progress_streak: int = 0
    last_observation_advanced: bool = False
    version: str = LOOP_PROGRESS_VERSION

    def to_state_dict(self) -> dict[str, Any]:
        value = asdict(self)
        for field_name in (
            "successful_tool_names",
            "failed_tool_names",
            "successful_action_fingerprints",
            "failed_action_fingerprints",
        ):
            value[field_name] = list(value[field_name])
        return value

    @classmethod
    def from_state_dict(cls, value: object) -> "LoopProgressSnapshot":
        expected = {
            "tool_calls_used",
            "successful_tool_names",
            "failed_tool_names",
            "successful_action_fingerprints",
            "failed_action_fingerprints",
            "no_progress_streak",
            "last_observation_advanced",
            "version",
        }
        if not isinstance(value, dict) or set(value) != expected:
            raise ValueError("loop progress 结构无效")
        tool_calls_used = value["tool_calls_used"]
        no_progress_streak = value["no_progress_streak"]
        if (
            value["version"] != LOOP_PROGRESS_VERSION
            or not isinstance(tool_calls_used, int)
            or isinstance(tool_calls_used, bool)
            or not 0 <= tool_calls_used <= _MAX_ACTION_FINGERPRINTS
            or not isinstance(no_progress_streak, int)
            or isinstance(no_progress_streak, bool)
            or not 0 <= no_progress_streak <= _MAX_ACTION_FINGERPRINTS
            or not isinstance(value["last_observation_advanced"], bool)
        ):
            raise ValueError("loop progress 字段无效")
        return cls(
            tool_calls_used=tool_calls_used,
            successful_tool_names=_bounded_strings(
                value["successful_tool_names"],
                maximum_items=_MAX_REQUIREMENTS,
                maximum_length=200,
            ),
            failed_tool_names=_bounded_strings(
                value["failed_tool_names"],
                maximum_items=_MAX_REQUIREMENTS,
                maximum_length=200,
            ),
            successful_action_fingerprints=_bounded_strings(
                value["successful_action_fingerprints"],
                maximum_items=_MAX_ACTION_FINGERPRINTS,
                maximum_length=64,
            ),
            failed_action_fingerprints=_bounded_strings(
                value["failed_action_fingerprints"],
                maximum_items=_MAX_ACTION_FINGERPRINTS,
                maximum_length=64,
            ),
            no_progress_streak=no_progress_streak,
            last_observation_advanced=value["last_observation_advanced"],
        )


StopDisposition = Literal["continue", "complete", "clarify", "fail"]


@dataclass(frozen=True, slots=True)
class StopDecision:
    """Loop 对继续、完成、澄清或失败的结构化决定。"""

    disposition: StopDisposition
    reason_code: str
    missing_requirements: tuple[str, ...] = ()
    version: str = LOOP_STOP_DECISION_VERSION

    def to_state_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["missing_requirements"] = list(self.missing_requirements)
        return value

    @classmethod
    def from_state_dict(cls, value: object) -> "StopDecision":
        expected = {"disposition", "reason_code", "missing_requirements", "version"}
        if not isinstance(value, dict) or set(value) != expected:
            raise ValueError("loop stop decision 结构无效")
        reason_code = value["reason_code"]
        if (
            value["version"] != LOOP_STOP_DECISION_VERSION
            or value["disposition"] not in _STOP_DISPOSITIONS
            or not isinstance(reason_code, str)
            or _REASON_CODE_RE.fullmatch(reason_code) is None
        ):
            raise ValueError("loop stop decision 字段无效")
        return cls(
            disposition=value["disposition"],
            reason_code=reason_code,
            missing_requirements=_bounded_strings(
                value["missing_requirements"],
                maximum_items=_MAX_REQUIREMENTS,
                maximum_length=200,
            ),
        )
