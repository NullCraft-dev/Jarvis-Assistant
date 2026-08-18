"""Agent finish 前的可插拔确定性校验端口。"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Protocol

from jarvis_worker.agent.core.actions import AgentAction
from jarvis_worker.agent.core.state import AgentState


@dataclass(frozen=True, slots=True)
class FinalAnswerValidation:
    accepted: bool
    output: str
    feedback: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)
    reason_code: str = "FINAL_ANSWER_REJECTED"
    diagnostics: dict[str, Any] = field(default_factory=dict)


class FinalAnswerValidator(Protocol):
    validator_id: str

    def requires_buffered_output(self, state: AgentState) -> bool: ...

    def validate(self, *, action: AgentAction, state: AgentState) -> FinalAnswerValidation: ...


def sanitize_final_answer_validation_details(value: object) -> dict[str, Any] | None:
    """在持久化边界再次约束最终回答校验诊断的安全形状。"""
    if not isinstance(value, dict):
        return None
    validator_id = value.get("validator_id")
    reason_code = value.get("reason_code")
    rejection_count = value.get("rejection_count")
    max_rewrites = value.get("max_rewrites")
    rewrite_available = value.get("rewrite_available")
    recovery_mode = value.get("recovery_mode")
    if (
        not isinstance(validator_id, str)
        or not re.fullmatch(r"[a-z0-9][a-z0-9._-]{0,99}", validator_id)
        or not isinstance(reason_code, str)
        or not re.fullmatch(r"[A-Z][A-Z0-9_]{2,79}", reason_code)
        or not isinstance(rejection_count, int)
        or isinstance(rejection_count, bool)
        or not 1 <= rejection_count <= 20
        or not isinstance(max_rewrites, int)
        or isinstance(max_rewrites, bool)
        or not 0 <= max_rewrites <= 20
        or not isinstance(rewrite_available, bool)
        or recovery_mode not in {"answer_rewrite", "tool_planning", "none"}
    ):
        return None
    sanitized: dict[str, Any] = {
        "validator_id": validator_id,
        "reason_code": reason_code,
        "rejection_count": rejection_count,
        "max_rewrites": max_rewrites,
        "rewrite_available": rewrite_available,
        "recovery_mode": recovery_mode,
    }
    coverage = value.get("coverage")
    if isinstance(coverage, dict):
        safe_coverage: dict[str, Any] = {}
        schema = coverage.get("schema")
        if (
            isinstance(schema, str)
            and re.fullmatch(r"[a-z0-9][a-z0-9._-]{0,99}", schema)
        ):
            safe_coverage["schema"] = schema
        for key in (
            "required_endpoint_count",
            "covered_endpoint_count",
            "required_stage_count",
            "covered_stage_count",
            "required_evidence_slot_count",
            "covered_evidence_slot_count",
            "unique_source_paths",
        ):
            item = coverage.get(key)
            if isinstance(item, int) and not isinstance(item, bool) and 0 <= item <= 10_000:
                safe_coverage[key] = item
        if isinstance(coverage.get("complete"), bool):
            safe_coverage["complete"] = coverage["complete"]
        if safe_coverage:
            sanitized["coverage"] = safe_coverage
    if isinstance(value.get("answer_denied_global_coverage"), bool):
        sanitized["answer_denied_global_coverage"] = value[
            "answer_denied_global_coverage"
        ]
    uncertainty_count = value.get("uncertainty_clause_count")
    if (
        isinstance(uncertainty_count, int)
        and not isinstance(uncertainty_count, bool)
        and 0 <= uncertainty_count <= 100
    ):
        sanitized["uncertainty_clause_count"] = uncertainty_count
    return sanitized
