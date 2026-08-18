"""Deterministic final-answer integrity checks owned by the Runtime Harness."""

from __future__ import annotations

import re

from jarvis_worker.agent.core.actions import AgentAction
from jarvis_worker.agent.core.conversation_constraints import (
    is_citation_verification_goal,
)
from jarvis_worker.agent.core.final_answer import FinalAnswerValidation
from jarvis_worker.agent.core.state import AgentState

FINAL_MESSAGE_INTEGRITY_VALIDATOR_ID = "final-message-integrity-v1"
CITATION_VERDICT_VALIDATOR_ID = "citation-verdict-consistency-v1"

_INCOMPLETE_ENDING = re.compile(
    r"(?:[，,：:；;（(\[【{]|(?:也|并|而|但|或|以及|而且|同时)不|"
    r"and|or|but|because|therefore|however|including|such\s+as)\s*$",
    re.IGNORECASE,
)
_NEGATIVE_CITATION_VERDICT = re.compile(
    r"(?:不支持|不能支持|并不支持|未能支持|无法支持|引用有误|引用错误|"
    r"张冠李戴|质疑.{0,8}成立|需要纠正|不准确|"
    r"does\s+not\s+support|citation\s+is\s+(?:wrong|incorrect)|mis-?cited)",
    re.IGNORECASE,
)
_POSITIVE_CITATION_VERDICT = re.compile(
    r"(?:确实支持|直接支持|能够支持|可以支持|正是支持|"
    r"(?:该|此|这(?:条|一)?)(?:引用|来源|出处).{0,8}支持|"
    r"这一点.{0,6}(?:成立|正确)|本身成立|"
    r"does\s+support|directly\s+supports|citation\s+is\s+(?:correct|accurate))",
    re.IGNORECASE,
)


class FinalMessageIntegrityValidator:
    """Reject strongly truncated syntax before it reaches persistence or UI."""

    validator_id = FINAL_MESSAGE_INTEGRITY_VALIDATOR_ID

    def requires_buffered_output(self, state: AgentState) -> bool:
        if is_citation_verification_goal(state.user_goal):
            return True
        intent = state.intent if isinstance(state.intent, dict) else {}
        retrieval = intent.get("retrieval")
        mode = retrieval.get("mode") if isinstance(retrieval, dict) else None
        return mode in {"retrieve", "required"}

    def validate(self, *, action: AgentAction, state: AgentState) -> FinalAnswerValidation:
        del state
        message = action.final_message.strip()
        incomplete = bool(
            _INCOMPLETE_ENDING.search(message)
            or message.count("```") % 2
            or _has_unclosed_delimiter(message)
        )
        if not incomplete:
            return FinalAnswerValidation(accepted=True, output=message)
        return FinalAnswerValidation(
            accepted=False,
            output="",
            feedback=(
                "最终回答疑似在句子、列表或定界符中途截断。请只重写 final_message，"
                "保留已有事实、证据边界和引用，不调用工具；确保每个句子与 Markdown 结构完整收口。"
            ),
            reason_code="FINAL_MESSAGE_INCOMPLETE",
        )


class CitationVerdictConsistencyValidator:
    """Require one unambiguous verdict when rechecking a prior citation."""

    validator_id = CITATION_VERDICT_VALIDATOR_ID

    def requires_buffered_output(self, state: AgentState) -> bool:
        return is_citation_verification_goal(state.user_goal)

    def validate(self, *, action: AgentAction, state: AgentState) -> FinalAnswerValidation:
        if not is_citation_verification_goal(state.user_goal):
            return FinalAnswerValidation(accepted=True, output=action.final_message)
        message = action.final_message
        negative = bool(_NEGATIVE_CITATION_VERDICT.search(message))
        # Remove explicit negative verdict spans before looking for positive
        # language. Otherwise the substring ``支持`` inside ``不支持`` would
        # manufacture a contradiction that is not present in the answer.
        positive_message = _NEGATIVE_CITATION_VERDICT.sub("", message)
        positive = bool(_POSITIVE_CITATION_VERDICT.search(positive_message))
        if positive != negative:
            return FinalAnswerValidation(
                accepted=True,
                output=message,
                metadata={"citation_verdict": "supported" if positive else "unsupported"},
            )
        return FinalAnswerValidation(
            accepted=False,
            output="",
            feedback=(
                "引用复核回答必须给出一个一致且明确的判定：支持，或不支持。"
                "不得先称引用有误、随后又称同一引用直接支持；也不得在证据已支持时编造其他纠正。"
                "请只基于当前已检索原文重写 final_message，不调用工具。"
            ),
            reason_code="CITATION_VERDICT_CONTRADICTORY"
            if positive and negative
            else "CITATION_VERDICT_MISSING",
        )


def _has_unclosed_delimiter(message: str) -> bool:
    pairs = (("（", "）"), ("【", "】"), ("[", "]"), ("{", "}"))
    return any(message.count(opening) > message.count(closing) for opening, closing in pairs)
