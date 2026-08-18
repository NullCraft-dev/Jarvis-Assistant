"""Deterministic user-authored answer constraints owned by the Runtime Harness."""

from __future__ import annotations

import re

from jarvis_worker.agent.core.actions import AgentAction
from jarvis_worker.agent.core.final_answer import FinalAnswerValidation
from jarvis_worker.agent.core.state import AgentState

ANSWER_CONSTRAINT_VALIDATOR_ID = "explicit-answer-constraints-v1"

_MAXIMUM_LENGTH = re.compile(
    r"(?:不超过|最多|控制在)\s*(\d{1,5})\s*(?:个)?(?:字|字符)|"
    r"(?:no\s+more\s+than|at\s+most|under)\s+(\d{1,5})\s+(?:characters?|words?)",
    re.IGNORECASE,
)
_EXHAUSTIVE_RETRIEVAL = re.compile(
    r"(?:所有|全部|每一|逐一|穷尽).{0,24}(?:章节|位置|段落|提到|涉及|出现|引用|条目)|"
    r"(?:all|every|exhaustive).{0,30}(?:sections?|mentions?|occurrences?|references?)",
    re.IGNORECASE,
)
_BOUNDED_RECALL_DISCLOSURE = re.compile(
    r"(?:基于|限于).{0,18}(?:本次|当前).{0,12}(?:有界|检索|召回|结果)|"
    r"(?:不能|无法|不).{0,10}(?:保证|声称).{0,12}(?:穷尽|覆盖全文|无遗漏)|"
    r"(?:可能|仍可能).{0,12}(?:遗漏|未召回)|"
    r"(?:bounded\s+retrieval|cannot\s+guarantee).{0,40}(?:exhaustive|complete|all)",
    re.IGNORECASE,
)
_FACT_INFERENCE_REQUEST = re.compile(
    r"(?:区分|分别标明|分开).{0,20}(?:原文|文中|事实).{0,20}(?:判断|推断|观点)|"
    r"(?:distinguish|separate).{0,30}(?:fact|source).{0,30}(?:inference|judg(?:e)?ment)",
    re.IGNORECASE,
)
_FACT_LABEL = re.compile(
    r"(?:原文事实|文中事实|论文明确|原文明示|事实\s*[：:]|source\s+fact)",
    re.IGNORECASE,
)
_INFERENCE_LABEL = re.compile(
    r"(?:我的判断|分析判断|推断\s*[：:]|我的推断|基于此推断|inference\s*[：:]|my\s+judg(?:e)?ment)",
    re.IGNORECASE,
)
_MODEL_AUTHORED_CITATION_SECTION = re.compile(
    r"(?:\A|\n)\s*(?:#{1,6}\s*)?"
    r"(?:引用|参考资料|参考文献|sources?|references?)\s*[：:]\s*\n",
    re.IGNORECASE,
)
_BOUNDED_DISCLOSURE = "范围说明：以上仅限本次有界检索命中，仍可能遗漏未召回章节。"


class ExplicitAnswerConstraintValidator:
    """Enforce explicit size and epistemic constraints before persistence."""

    validator_id = ANSWER_CONSTRAINT_VALIDATOR_ID

    def requires_buffered_output(self, state: AgentState) -> bool:
        goal = state.user_goal
        return bool(
            _maximum_chars(goal) is not None
            or _EXHAUSTIVE_RETRIEVAL.search(goal)
            or _FACT_INFERENCE_REQUEST.search(goal)
        )

    def validate(self, *, action: AgentAction, state: AgentState) -> FinalAnswerValidation:
        message = action.final_message.strip()
        if _has_successful_rag_search(state):
            message = _without_model_authored_citation_section(message)
        maximum = _maximum_chars(state.user_goal)
        host_normalized = False
        if maximum is not None:
            observed = _visible_character_count(message)
            if observed > maximum:
                if state.answer_guard_rejections < 1:
                    return FinalAnswerValidation(
                        accepted=False,
                        output="",
                        feedback=(
                            f"用户明确要求回答不超过 {maximum} 字符；当前 final_message 为 "
                            f"{observed} 个可见字符，至少需要减少 {observed - maximum} 个。"
                            "请保留结论、证据边界和必要章节，不自行添加引用列表；不得调用新工具。"
                        ),
                        reason_code="ANSWER_LENGTH_LIMIT_EXCEEDED",
                    )
                message = _normalize_length_overflow(
                    message,
                    maximum=maximum,
                    require_bounded_disclosure=bool(
                        _EXHAUSTIVE_RETRIEVAL.search(state.user_goal)
                        and _has_successful_rag_search(state)
                    ),
                )
                host_normalized = True
        if (
            _EXHAUSTIVE_RETRIEVAL.search(state.user_goal)
            and _has_successful_rag_search(state)
            and not _BOUNDED_RECALL_DISCLOSURE.search(message)
        ):
            return FinalAnswerValidation(
                accepted=False,
                output="",
                feedback=(
                    "当前证据来自有界 RAG 检索，不能证明已经穷尽整份长文档。"
                    "请明确说明结论限于本次检索命中的章节、仍可能存在未召回内容；"
                    "不得声称无遗漏，也不得调用新工具。"
                ),
                reason_code="BOUNDED_RETRIEVAL_DISCLOSURE_MISSING",
            )
        if _FACT_INFERENCE_REQUEST.search(state.user_goal) and not (
            _FACT_LABEL.search(message) and _INFERENCE_LABEL.search(message)
        ):
            return FinalAnswerValidation(
                accepted=False,
                output="",
                feedback=(
                    "用户要求明确区分来源事实与分析判断。请在 final_message 中分别使用"
                    "“原文事实”和“我的判断/推断”标识，且不要把推断写成文献结论。"
                ),
                reason_code="FACT_INFERENCE_BOUNDARY_MISSING",
            )
        return FinalAnswerValidation(
            accepted=True,
            output=message,
            metadata=(
                {
                    "explicit_max_chars": maximum,
                    "visible_characters": _visible_character_count(message),
                    "host_normalized": host_normalized,
                }
                if maximum is not None
                else {}
            ),
        )


def _maximum_chars(goal: str) -> int | None:
    match = _MAXIMUM_LENGTH.search(goal[:10_000])
    if match is None:
        return None
    raw = next((value for value in match.groups() if value is not None), "")
    value = int(raw)
    return value if 20 <= value <= 10_000 else None


def _visible_character_count(message: str) -> int:
    return len(re.sub(r"\s+", "", message))


def _without_model_authored_citation_section(message: str) -> str:
    match = _MODEL_AUTHORED_CITATION_SECTION.search(message)
    return message[: match.start()].rstrip() if match is not None else message


def _normalize_length_overflow(
    message: str,
    *,
    maximum: int,
    require_bounded_disclosure: bool,
) -> str:
    suffix = f"\n\n{_BOUNDED_DISCLOSURE}" if require_bounded_disclosure else ""
    budget = maximum - _visible_character_count(suffix)
    if budget <= 0:
        return _truncate_visible_characters(suffix.strip(), maximum)
    core = _truncate_visible_characters(message, budget)
    return f"{core.rstrip()}{suffix}".strip()


def _truncate_visible_characters(message: str, maximum: int) -> str:
    visible = 0
    cutoff = len(message)
    for index, character in enumerate(message):
        if not character.isspace():
            visible += 1
        if visible > maximum:
            cutoff = index
            break
    candidate = message[:cutoff].rstrip()
    if cutoff < len(message):
        floor = max(0, int(len(candidate) * 0.7))
        boundary = max(
            candidate.rfind("。", floor),
            candidate.rfind("！", floor),
            candidate.rfind("？", floor),
            candidate.rfind("\n", floor),
        )
        if boundary >= floor:
            candidate = candidate[: boundary + 1].rstrip()
    return candidate


def _has_successful_rag_search(state: AgentState) -> bool:
    return any(
        isinstance(item, dict) and item.get("tool_name") == "rag.search" and item.get("ok") is True
        for item in state.observations
    )
