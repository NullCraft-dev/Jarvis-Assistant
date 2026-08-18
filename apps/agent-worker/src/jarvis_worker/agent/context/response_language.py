"""Approved response-language preference projection and output enforcement.

Long-term memory remains untrusted background data.  This module is the only
owner allowed to promote the allowlisted ``preference/response.language`` key
into a small, typed Runtime policy.  Raw memory text is never promoted to the
system role.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from enum import Enum
from typing import Any, Sequence

from jarvis_worker.agent.core.actions import AgentAction
from jarvis_worker.agent.core.final_answer import FinalAnswerValidation
from jarvis_worker.agent.core.state import AgentState
from jarvis_worker.agent.models.messages import ModelMessage

RESPONSE_LANGUAGE_POLICY_VERSION = "response-language-v1"
RESPONSE_LANGUAGE_VALIDATOR_ID = "response-language-preference-v1"


class ResponseLanguage(str, Enum):
    ZH = "zh"
    EN = "en"


@dataclass(frozen=True, slots=True)
class ResponseLanguagePolicy:
    """Normalized policy derived from one approved, active memory."""

    default_language: ResponseLanguage
    effective_language: ResponseLanguage
    source_scope: str
    current_turn_override: bool


_MEMORY_PATTERNS: dict[ResponseLanguage, tuple[re.Pattern[str], ...]] = {
    ResponseLanguage.ZH: (
        re.compile(
            r"(?:默认|通常|始终|优先|偏好|希望|请)?\s*(?:使用|用|以)\s*"
            r"中文(?:来)?\s*(?:回答|回复|响应|作答)",
            re.IGNORECASE,
        ),
        re.compile(
            r"(?:默认|通常|始终|优先|偏好|希望|请)?\s*中文\s*"
            r"(?:回答|回复|响应|作答)",
            re.IGNORECASE,
        ),
        re.compile(
            r"\b(?:prefer(?:s|red)?|default(?:s|ly)?|always|please)?\s*"
            r"(?:answer|reply|respond)\s+(?:in\s+)?chinese\b",
            re.IGNORECASE,
        ),
        re.compile(
            r"\bprefers?\s+chinese\s+(?:answers?|responses?|replies)\b",
            re.IGNORECASE,
        ),
    ),
    ResponseLanguage.EN: (
        re.compile(
            r"(?:默认|通常|始终|优先|偏好|希望|请)?\s*(?:使用|用|以)\s*"
            r"英文(?:来)?\s*(?:回答|回复|响应|作答)",
            re.IGNORECASE,
        ),
        re.compile(
            r"(?:默认|通常|始终|优先|偏好|希望|请)?\s*英文\s*"
            r"(?:回答|回复|响应|作答)",
            re.IGNORECASE,
        ),
        re.compile(
            r"\b(?:prefer(?:s|red)?|default(?:s|ly)?|always|please)?\s*"
            r"(?:answer|reply|respond)\s+(?:in\s+)?english\b",
            re.IGNORECASE,
        ),
        re.compile(
            r"\bprefers?\s+english\s+(?:answers?|responses?|replies)\b",
            re.IGNORECASE,
        ),
        re.compile(
            r"\buse\s+english\s+(?:answers?|responses?|replies)\s+by\s+default\b",
            re.IGNORECASE,
        ),
    ),
}

_NEGATION_SUFFIX = re.compile(
    r"(?:不|不要|禁止|避免|无需|不用|别|do\s+not|don't|never)\s*$",
    re.IGNORECASE,
)
_ZH_DIRECTIVE = re.compile(
    r"^\s*(?:请|麻烦|务必|只|请只)?\s*(?:使用|用|以)\s*(中文|英文)"
    r"(?:来)?\s*(?:回答|回复|响应|作答)",
    re.IGNORECASE,
)
_ZH_SHORT_DIRECTIVE = re.compile(
    r"^\s*(?:请|只)?\s*(中文|英文)\s*(?:回答|回复|响应|作答)",
    re.IGNORECASE,
)
_EN_DIRECTIVE = re.compile(
    r"^\s*(?:please\s+)?(?:answer|reply|respond)(?:\s+me)?\s+in\s+"
    r"(chinese|english)\b",
    re.IGNORECASE,
)
_EN_USE_DIRECTIVE = re.compile(
    r"^\s*(?:please\s+)?(?:use|write\s+in)\s+(?:only\s+)?"
    r"(chinese|english)\b",
    re.IGNORECASE,
)
_SENTENCE_SPLIT = re.compile(r"[。！？.!?;；\n]+")
_HAN = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff]")
_LATIN_WORD = re.compile(r"[A-Za-z]+(?:[-'][A-Za-z]+)*")
_FENCED_CODE = re.compile(r"```.*?```", re.DOTALL)
_INLINE_CODE = re.compile(r"`[^`\n]*`")
_URL = re.compile(r"https?://\S+")


def resolve_response_language_policy(
    memory_items: Sequence[dict[str, Any]],
    user_goal: str,
) -> ResponseLanguagePolicy | None:
    """Resolve one allowlisted preference with workspace-over-global precedence.

    Ambiguous conflicting memories at the winning scope fail closed and produce
    no promoted policy.  Merely asking a question in another language is not an
    override; the current turn must contain an explicit language directive.
    """

    by_scope: dict[str, list[ResponseLanguage]] = {"global": [], "workspace": []}
    for item in memory_items:
        if not isinstance(item, dict):
            continue
        if item.get("category") != "preference" or item.get("key") != "response.language":
            continue
        scope = item.get("scope_type")
        content = item.get("content")
        if scope not in by_scope or not isinstance(content, str) or len(content) > 4_000:
            continue
        language = _parse_memory_language(content)
        if language is not None:
            by_scope[scope].append(language)

    selected: ResponseLanguage | None = None
    selected_scope = ""
    for scope in ("workspace", "global"):
        languages = set(by_scope[scope])
        if len(languages) > 1:
            return None
        if len(languages) == 1:
            selected = next(iter(languages))
            selected_scope = scope
            break
    if selected is None:
        return None

    explicit = _explicit_current_turn_language(user_goal)
    effective = explicit or selected
    return ResponseLanguagePolicy(
        default_language=selected,
        effective_language=effective,
        source_scope=selected_scope,
        current_turn_override=explicit is not None and explicit != selected,
    )


def project_response_language_policy(
    system: ModelMessage,
    state: AgentState,
) -> ModelMessage:
    """Append only normalized host-owned policy fields to the system message."""

    policy = resolve_response_language_policy(state.memory_items, state.user_goal)
    if policy is None:
        return system
    payload = {
        "policy_version": RESPONSE_LANGUAGE_POLICY_VERSION,
        "default_language": policy.default_language.value,
        "effective_language": policy.effective_language.value,
        "source": "approved_active_memory",
        "source_scope": policy.source_scope,
        "current_turn_override": policy.current_turn_override,
        "rules": [
            "输入所使用的语言本身不构成偏好覆盖",
            "只有当前用户明确要求另一种回答语言时才覆盖默认偏好",
            "技术名词、代码、路径、引用与专有名词可保留原文",
        ],
    }
    return ModelMessage(
        role=system.role,
        content=(
            system.content
            + "\n\n[Runtime 已批准响应语言策略；原始 Memory 文本未被提升为指令]\n"
            + json.dumps(payload, ensure_ascii=False, sort_keys=True)
        ),
        name=system.name,
        tool_call_id=system.tool_call_id,
    )


class ResponseLanguagePreferenceValidator:
    """Prevent an approved response preference from being silently ignored."""

    validator_id = RESPONSE_LANGUAGE_VALIDATOR_ID

    def requires_buffered_output(self, state: AgentState) -> bool:
        return resolve_response_language_policy(state.memory_items, state.user_goal) is not None

    def validate(
        self,
        *,
        action: AgentAction,
        state: AgentState,
    ) -> FinalAnswerValidation:
        policy = resolve_response_language_policy(state.memory_items, state.user_goal)
        if policy is None:
            return FinalAnswerValidation(accepted=True, output=action.final_message)
        accepted = _matches_effective_language(
            action.final_message,
            policy.effective_language,
        )
        metadata = {
            "policy_version": RESPONSE_LANGUAGE_POLICY_VERSION,
            "default_language": policy.default_language.value,
            "effective_language": policy.effective_language.value,
            "source_scope": policy.source_scope,
            "current_turn_override": policy.current_turn_override,
        }
        if accepted:
            return FinalAnswerValidation(
                accepted=True,
                output=action.final_message,
                metadata=metadata,
            )
        if policy.effective_language is ResponseLanguage.ZH:
            feedback = (
                "已批准的 response.language 偏好要求本轮使用中文回答，且当前目标没有明确覆盖。"
                "请只重写 final_message，用中文完成自然语言说明；技术名词、代码、路径、引用和专有名词"
                "可保留原文。不得调用工具。"
            )
        else:
            feedback = (
                "本轮有效的 response.language 策略要求使用英文回答。请只重写 final_message，"
                "用英文完成自然语言说明；代码、路径、引用和专有名词可保留原文。不得调用工具。"
            )
        return FinalAnswerValidation(
            accepted=False,
            output="",
            feedback=feedback,
            metadata=metadata,
            reason_code="RESPONSE_LANGUAGE_MISMATCH",
        )


def _parse_memory_language(content: str) -> ResponseLanguage | None:
    matches: set[ResponseLanguage] = set()
    for language, patterns in _MEMORY_PATTERNS.items():
        for pattern in patterns:
            for match in pattern.finditer(content):
                prefix = content[max(0, match.start() - 24) : match.start()]
                if not _NEGATION_SUFFIX.search(prefix):
                    matches.add(language)
                    break
    return next(iter(matches)) if len(matches) == 1 else None


def _explicit_current_turn_language(user_goal: str) -> ResponseLanguage | None:
    if not isinstance(user_goal, str) or not user_goal.strip():
        return None
    fragments = [part.strip() for part in _SENTENCE_SPLIT.split(user_goal) if part.strip()]
    # Directives conventionally live at the start or in a final sentence.  This
    # avoids promoting quoted examples in the middle of an analysis request.
    candidates = fragments[:1]
    if len(fragments) > 1:
        candidates.append(fragments[-1])
    matches: set[ResponseLanguage] = set()
    for fragment in candidates:
        for pattern in (
            _ZH_DIRECTIVE,
            _ZH_SHORT_DIRECTIVE,
            _EN_DIRECTIVE,
            _EN_USE_DIRECTIVE,
        ):
            match = pattern.search(fragment)
            if match is None:
                continue
            value = match.group(1).lower()
            matches.add(
                ResponseLanguage.ZH if value in {"中文", "chinese"} else ResponseLanguage.EN
            )
    return next(iter(matches)) if len(matches) == 1 else None


def _matches_effective_language(output: str, language: ResponseLanguage) -> bool:
    prose = _URL.sub(" ", _INLINE_CODE.sub(" ", _FENCED_CODE.sub(" ", output))).strip()
    if not prose:
        return True
    try:
        json.loads(prose)
    except (TypeError, ValueError, json.JSONDecodeError):
        pass
    else:
        return True
    han_count = len(_HAN.findall(prose))
    latin_words = _LATIN_WORD.findall(prose)
    latin_letters = sum(len(word) for word in latin_words)
    # Very short labels such as "OK" are language-neutral.  Natural-language
    # sentences must contain meaningful evidence of the effective language.
    if language is ResponseLanguage.ZH:
        if len(latin_words) < 3 and han_count == 0:
            return True
        return han_count >= 1 and (len(latin_words) < 6 or han_count >= 4)
    if han_count < 2 and len(latin_words) < 3:
        return True
    return len(latin_words) >= 3 and (han_count <= 2 or latin_letters >= han_count * 4)
