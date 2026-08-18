"""跨层源码调用链 finish 前的确定性覆盖校验。"""

from __future__ import annotations

import re

from jarvis_worker.agent.core.actions import AgentAction
from jarvis_worker.agent.core.evidence_navigation import (
    build_workspace_source_chain_coverage,
    is_workspace_source_chain_goal,
    workspace_source_chain_missing_summary,
)
from jarvis_worker.agent.core.final_answer import FinalAnswerValidation
from jarvis_worker.agent.core.state import AgentState

_UNCONFIRMED_CHAIN_CLAUSE_RE = re.compile(
    r"(?:"
    r"(?:未|尚未|无法|不能|未能).{0,20}(?:确认|验证|闭合|证明|核对|读取)|"
    r"证据不足|(?:没有|缺少).{0,8}(?:直接)?证据|不知道|"
    r"存在.{0,12}(?:缺口|断链)|(?:缺口|断链)(?:处|仍|是|为|存在)|"
    r"\b(?:unconfirmed|not\s+(?:confirmed|verified)|cannot\s+(?:confirm|verify)|"
    r"insufficient\s+evidence|missing\s+(?:edge|evidence)|evidence\s+gap)\b"
    r")",
    re.IGNORECASE | re.DOTALL,
)
_NO_UNCONFIRMED_CHAIN_RE = re.compile(
    r"(?:没有|不存在|无).{0,8}(?:未确认|缺口|断链)|"
    r"(?:未确认|缺口|断链).{0,8}(?:为零|没有|不存在|无)",
    re.IGNORECASE,
)
_GLOBAL_CHAIN_SCOPE_RE = re.compile(
    r"(?:"
    r"(?:整条|整个|整体|完整|全部|所有|端到端).{0,16}(?:调用链|链路|执行路径|数据流)|"
    r"(?:调用链|链路|执行路径|数据流).{0,16}(?:整体|完整|全部|所有|端到端)|"
    r"\b(?:entire|whole|overall|complete|end[- ]to[- ]end)\b.{0,24}"
    r"\b(?:call\s+chain|execution\s+path|data\s+flow)\b|"
    r"\b(?:call\s+chain|execution\s+path|data\s+flow)\b.{0,24}"
    r"\b(?:entire|whole|overall|complete|end[- ]to[- ]end)\b"
    r")",
    re.IGNORECASE,
)

SOURCE_CHAIN_VALIDATOR_ID = "workspace-source-chain-coverage-v4"


class WorkspaceSourceChainCoverageValidator:
    """阻止用户明确点名的跨运行端源码链路只完成其中一半。"""

    validator_id = SOURCE_CHAIN_VALIDATOR_ID

    def requires_buffered_output(self, state: AgentState) -> bool:
        return is_workspace_source_chain_goal(state.user_goal)

    def validate(self, *, action: AgentAction, state: AgentState) -> FinalAnswerValidation:
        coverage = build_workspace_source_chain_coverage(
            state.user_goal,
            state.observations,
        )
        if coverage is None:
            return FinalAnswerValidation(accepted=True, output=action.final_message)
        if coverage["complete"] is not True:
            return FinalAnswerValidation(
                accepted=False,
                output="",
                feedback=(
                    "跨层源码证据覆盖校验失败：用户明确点名的运行端只覆盖 "
                    f"{coverage['covered_endpoint_count']}/"
                    f"{coverage['required_endpoint_count']}，入口/传输/执行阶段只覆盖 "
                    f"{coverage['covered_stage_count']}/"
                    f"{coverage['required_stage_count']}。尚缺必需证据类别："
                    f"{workspace_source_chain_missing_summary(coverage)}。下一步必须优先搜索并读取尚未覆盖"
                    "的用户端点或中间阶段源码；不得重复加深已覆盖组件，也不得用相邻定义"
                    "或推断替代直接调用边。"
                ),
                metadata={"coverage": _metadata(coverage)},
                reason_code="SOURCE_CHAIN_EVIDENCE_INCOMPLETE",
                diagnostics={"coverage": _metadata(coverage)},
            )
        uncertainty_count = _answer_uncertainty_clause_count(action.final_message)
        if _answer_denies_global_chain_coverage(action.final_message):
            return FinalAnswerValidation(
                accepted=False,
                output="",
                feedback=(
                    "跨层源码回答一致性校验失败：Runtime 已确认所有固定证据槽，但最终回答否定了整条"
                    "调用链已经闭合。请只重写最终回答：保留具体、局部的未知项和证据限制，不得把局部"
                    "不确定性扩大为整条链未确认，也不得再请求工具。"
                ),
                metadata={
                    "coverage": _metadata(coverage),
                    "answer_denied_global_coverage": True,
                },
                reason_code="SOURCE_CHAIN_GLOBAL_CONTRADICTION",
                diagnostics={
                    "coverage": _metadata(coverage),
                    "answer_denied_global_coverage": True,
                    "uncertainty_clause_count": uncertainty_count,
                },
            )
        return FinalAnswerValidation(
            accepted=True,
            output=action.final_message,
            metadata={
                "coverage": _metadata(coverage),
                "scoped_uncertainty_count": uncertainty_count,
            },
        )


def _metadata(coverage: dict[str, object]) -> dict[str, object]:
    return {
        "schema": coverage["schema"],
        "required_endpoint_count": coverage["required_endpoint_count"],
        "covered_endpoint_count": coverage["covered_endpoint_count"],
        "required_stage_count": coverage["required_stage_count"],
        "covered_stage_count": coverage["covered_stage_count"],
        "required_evidence_slot_count": coverage["required_evidence_slot_count"],
        "covered_evidence_slot_count": coverage["covered_evidence_slot_count"],
        "unique_source_paths": coverage["unique_source_paths"],
        "complete": coverage["complete"],
    }


def _answer_uncertainty_clause_count(final_message: str) -> int:
    count = 0
    for clause in re.split(r"[\n。；;]+", final_message):
        normalized = clause.strip()
        if not normalized or _NO_UNCONFIRMED_CHAIN_RE.search(normalized):
            continue
        if _UNCONFIRMED_CHAIN_CLAUSE_RE.search(normalized):
            count += 1
    return count


def _answer_denies_global_chain_coverage(final_message: str) -> bool:
    """只拒绝对整条链覆盖状态的全局否定，允许诚实的局部未知项。"""
    for clause in re.split(r"[\n。；;]+", final_message):
        normalized = clause.strip()
        if not normalized or _NO_UNCONFIRMED_CHAIN_RE.search(normalized):
            continue
        if (
            _UNCONFIRMED_CHAIN_CLAUSE_RE.search(normalized)
            and _GLOBAL_CHAIN_SCOPE_RE.search(normalized)
        ):
            return True
    return False
