"""Strict parser for one LLM-authored intent candidate."""

from __future__ import annotations

import json
import re
from typing import Any
from uuid import UUID

from jarvis_worker.agent.core.structured_output import (
    StructuredOutputFailureKind,
    normalize_structured_output_text,
    repair_invalid_json_escapes,
)
from jarvis_worker.agent.intents.contracts import (
    DOCUMENT_SCOPES,
    EFFECT_MODES,
    PRIMARY_INTENTS,
    RETRIEVAL_MODES,
    WORKSPACE_ACTION_MODES,
    WORKSPACE_AMBIGUITY_MODES,
    WORKSPACE_EVIDENCE_MODES,
    WORKSPACE_LISTING_ENTRY_TYPES,
    IntentEffects,
    IntentExtraction,
    IntentRuntimeContext,
    IntentWorkspace,
    RetrievalIntent,
)
from jarvis_worker.agent.intents.workspace_listing import (
    explicit_workspace_listing_entry_types,
)


class ParseIntentError(ValueError):
    def __init__(self, message: str, *, failure_kind: StructuredOutputFailureKind):
        super().__init__(message)
        self.failure_kind = failure_kind.value


class _DuplicateField(ValueError):
    pass


class _InvalidConstant(ValueError):
    pass


def parse_intent_extraction(
    raw_text: str,
    *,
    runtime_context: IntentRuntimeContext,
    user_goal: str = "",
) -> IntentExtraction:
    """Validate the candidate and map anonymous document keys to trusted IDs."""
    normalized = normalize_structured_output_text(raw_text)
    try:
        parsed = _decode(normalized)
    except json.JSONDecodeError as exc:
        if exc.msg == "Invalid \\escape":
            try:
                parsed = _decode(repair_invalid_json_escapes(normalized))
            except (json.JSONDecodeError, _DuplicateField, _InvalidConstant):
                pass
            else:
                return _parse_object(parsed, runtime_context, user_goal)
        raise _error("Intent 输出不是合法 JSON", StructuredOutputFailureKind.INVALID_JSON) from None
    except _DuplicateField:
        raise _error(
            "Intent JSON 包含重复字段", StructuredOutputFailureKind.DUPLICATE_FIELD
        ) from None
    except _InvalidConstant:
        raise _error(
            "Intent JSON 包含 NaN 或 Infinity",
            StructuredOutputFailureKind.INVALID_JSON_CONSTANT,
        ) from None
    return _parse_object(parsed, runtime_context, user_goal)


def _decode(value: str) -> Any:
    return json.loads(
        value,
        object_pairs_hook=_strict_pairs,
        parse_constant=_reject_constant,
        strict=False,
    )


def _parse_object(
    parsed: Any,
    context: IntentRuntimeContext,
    user_goal: str,
) -> IntentExtraction:
    if not isinstance(parsed, dict):
        raise _error("Intent 根节点必须是 object", StructuredOutputFailureKind.INVALID_ROOT_TYPE)
    _exact_fields(parsed, {"primary_intent", "retrieval", "effects", "workspace"}, "Intent")

    primary = _enum_string(parsed.get("primary_intent"), PRIMARY_INTENTS, "primary_intent")
    retrieval = parsed.get("retrieval")
    effects = parsed.get("effects")
    workspace = parsed.get("workspace")
    if (
        not isinstance(retrieval, dict)
        or not isinstance(effects, dict)
        or not isinstance(workspace, dict)
    ):
        raise _error(
            "retrieval/effects/workspace 必须是 object",
            StructuredOutputFailureKind.INVALID_FIELD_TYPE,
        )
    _exact_fields(
        retrieval,
        {
            "mode",
            "query",
            "confidence",
            "reason",
            "document_refs",
            "document_scope",
            "document_keys",
        },
        "retrieval",
    )
    _exact_fields(
        effects,
        {
            "knowledge_write",
            "knowledge_provenance",
            "knowledge_title",
            "rag_ingestion",
        },
        "effects",
    )
    _exact_fields(
        workspace,
        {"evidence", "action", "ambiguity", "listing_entry_types", "reason"},
        "workspace",
    )

    mode = _enum_string(retrieval.get("mode"), RETRIEVAL_MODES, "retrieval.mode")
    scope = _enum_string(
        retrieval.get("document_scope"),
        DOCUMENT_SCOPES,
        "retrieval.document_scope",
    )
    query = _bounded_string(
        retrieval.get("query"),
        "retrieval.query",
        2_000,
        allow_empty=mode == "skip",
    )
    reason = _bounded_string(retrieval.get("reason"), "retrieval.reason", 500)
    confidence = retrieval.get("confidence")
    if (
        isinstance(confidence, bool)
        or not isinstance(confidence, (int, float))
        or not 0 <= float(confidence) <= 1
    ):
        raise _error(
            "retrieval.confidence 必须是 0..1 数字",
            StructuredOutputFailureKind.INVALID_FIELD_TYPE,
        )
    refs = _string_list(retrieval.get("document_refs"), "document_refs", 8, 300)
    keys = _string_list(retrieval.get("document_keys"), "document_keys", 20, 40)

    if mode == "skip" and scope != "none":
        raise _schema("retrieval.mode=skip 时 document_scope 必须是 none")
    if mode != "skip" and scope == "none":
        raise _schema("需要检索时 document_scope 不能是 none")
    if scope == "selected" and not keys:
        raise _schema("document_scope=selected 时必须选择 document_keys")
    if scope != "selected" and keys:
        raise _schema("只有 document_scope=selected 可以提交 document_keys")
    if scope == "unresolved" and mode != "required":
        raise _schema("document_scope=unresolved 只适用于 required 文档依赖")
    if scope == "unresolved" and not refs:
        raise _schema("document_scope=unresolved 时必须保留用户的文档指代表达")
    if primary == "document_question" and (
        mode != "required" or scope not in {"selected", "unresolved"}
    ):
        raise _schema("document_question 必须 required 且限定或明确未解析文档")

    by_key = {item.key: item for item in context.documents}
    unknown_keys = [key for key in keys if key not in by_key]
    if unknown_keys:
        raise _schema("document_keys 包含不在当前可信目录中的键")
    ambiguous_identity = _validate_selected_identity(
        scope=scope,
        selected_keys=keys,
        user_goal=user_goal,
        context=context,
    )
    if ambiguous_identity:
        if not refs:
            raise _schema("同名文档无法唯一映射且缺少用户文档指代")
        scope = "unresolved"
        keys = ()
    resolved_ids: list[str] = []
    for key in keys:
        value = by_key[key].document_id
        try:
            resolved = str(UUID(value))
        except ValueError:
            raise _schema("Runtime document catalog 包含无效 ID") from None
        if resolved not in resolved_ids:
            resolved_ids.append(resolved)

    knowledge_write = _enum_string(
        effects.get("knowledge_write"), EFFECT_MODES, "effects.knowledge_write"
    )
    knowledge_provenance = _enum_string(
        effects.get("knowledge_provenance"),
        EFFECT_MODES,
        "effects.knowledge_provenance",
    )
    knowledge_title = _bounded_string(
        effects.get("knowledge_title"),
        "effects.knowledge_title",
        200,
        allow_empty=True,
    )
    rag_ingestion = _enum_string(
        effects.get("rag_ingestion"), EFFECT_MODES, "effects.rag_ingestion"
    )
    if knowledge_write == "skip" and (knowledge_provenance != "skip" or knowledge_title):
        raise _schema(
            "knowledge_write=skip 时 knowledge_provenance 必须为 skip 且 knowledge_title 为空"
        )
    workspace_evidence = _enum_string(
        workspace.get("evidence"),
        WORKSPACE_EVIDENCE_MODES,
        "workspace.evidence",
    )
    workspace_action = _enum_string(
        workspace.get("action"), WORKSPACE_ACTION_MODES, "workspace.action"
    )
    workspace_ambiguity = _enum_string(
        workspace.get("ambiguity"),
        WORKSPACE_AMBIGUITY_MODES,
        "workspace.ambiguity",
    )
    listing_entry_types = _string_list(
        workspace.get("listing_entry_types"),
        "workspace.listing_entry_types",
        len(WORKSPACE_LISTING_ENTRY_TYPES),
        20,
    )
    if any(item not in WORKSPACE_LISTING_ENTRY_TYPES for item in listing_entry_types):
        raise _schema("workspace.listing_entry_types 包含未知条目类型")
    explicit_listing_types = explicit_workspace_listing_entry_types(user_goal)
    if explicit_listing_types and (
        workspace_evidence != "metadata"
        or workspace_action != "read"
        or workspace_ambiguity != "clear"
        or frozenset(listing_entry_types) != frozenset(explicit_listing_types)
    ):
        raise _schema("workspace.listing_entry_types 与用户明确的列举类型不一致")
    workspace_reason = _bounded_string(workspace.get("reason"), "workspace.reason", 500)
    if (
        workspace_ambiguity == "clarification_required"
        and workspace_action in {"write", "destructive"}
        and workspace_evidence in {"metadata", "required"}
    ):
        # 含糊副作用请求可能在澄清后需要读取候选，但当前 Run 必须先收敛为
        # 确定性澄清。丢弃模型混入的读取阶段，不放宽写入/删除或权限边界。
        workspace_evidence = "skip"
    if workspace_action == "read" and workspace_evidence not in {"metadata", "required"}:
        raise _schema("workspace.action=read 必须配 evidence=metadata|required")
    if workspace_evidence in {"metadata", "required"} and workspace_action != "read":
        raise _schema("workspace 读取证据只适用于 action=read")
    if workspace_action == "none" and (
        workspace_evidence != "skip" or workspace_ambiguity != "clear"
    ):
        raise _schema("workspace.action=none 必须配 skip/clear")
    if workspace_ambiguity == "clarification_required" and workspace_action not in {
        "write",
        "destructive",
    }:
        raise _schema("workspace clarification 只适用于写入或破坏性目标")
    if listing_entry_types and (
        workspace_evidence != "metadata"
        or workspace_action != "read"
        or workspace_ambiguity != "clear"
    ):
        raise _schema("workspace listing 类型投影只适用于明确的元数据读取")
    return IntentExtraction(
        primary_intent=primary,
        retrieval=RetrievalIntent(
            mode=mode,
            query=query,
            confidence=float(confidence),
            reason=reason,
            document_refs=refs,
            document_scope=scope,
            resolved_document_ids=tuple(resolved_ids),
        ),
        effects=IntentEffects(
            knowledge_write=knowledge_write,
            knowledge_provenance=knowledge_provenance,
            knowledge_title=knowledge_title,
            rag_ingestion=rag_ingestion,
        ),
        workspace=IntentWorkspace(
            evidence=workspace_evidence,
            action=workspace_action,
            ambiguity=workspace_ambiguity,
            listing_entry_types=listing_entry_types,
            reason=workspace_reason,
        ),
        source="llm",
    )


_ARXIV_IDENTITY_TERM = re.compile(r"(?<!\d)\d{4}\.\d{4,5}(?!\d)")
_QUOTED_IDENTITY_TERM = re.compile(r"《([^》]{2,200})》")
_WORD_IDENTITY_TERM = re.compile(r"[A-Za-z][A-Za-z0-9_.+-]{2,}")
_IDENTITY_STOP_TERMS = frozenset(
    {
        "arxiv",
        "document",
        "documents",
        "paper",
        "papers",
        "pdf",
        "rag",
        "report",
    }
)


def _validate_selected_identity(
    *,
    scope: str,
    selected_keys: tuple[str, ...],
    user_goal: str,
    context: IntentRuntimeContext,
) -> bool:
    """Fail closed when explicit names uniquely identify docs the model did not select."""

    if scope != "selected" or not user_goal.strip():
        return False
    searchable = {
        document.key: f"{document.title}\n{document.identity_excerpt}".casefold()
        for document in context.documents
    }
    required_keys: set[str] = set()
    for raw_term in explicit_document_identity_terms(user_goal):
        term = raw_term.casefold()
        if term in _IDENTITY_STOP_TERMS:
            continue
        matches = [key for key, value in searchable.items() if term in value]
        if len(matches) > 1:
            return True
        if len(matches) == 1:
            required_keys.add(matches[0])
    if not required_keys.issubset(selected_keys):
        raise _schema("document_keys 与用户明确提到的可信文档身份不一致")
    return False


def explicit_document_identity_terms(user_goal: str) -> tuple[str, ...]:
    """Only extract strong document identities, never ordinary query vocabulary."""

    candidates = [*_ARXIV_IDENTITY_TERM.findall(user_goal)]
    candidates.extend(_QUOTED_IDENTITY_TERM.findall(user_goal))
    for token in _WORD_IDENTITY_TERM.findall(user_goal):
        tail = token[1:]
        if token.isupper() or any(character.isupper() for character in tail):
            candidates.append(token)
    result: list[str] = []
    for value in candidates:
        normalized = value.strip()
        if normalized and normalized.casefold() not in {existing.casefold() for existing in result}:
            result.append(normalized)
    return tuple(result)


def _exact_fields(value: dict, expected: set[str], label: str) -> None:
    missing = expected - set(value)
    if missing:
        raise _error(
            f"{label} 缺少字段",
            StructuredOutputFailureKind.MISSING_FIELD,
        )
    if set(value) - expected:
        raise _error(
            f"{label} 包含未知字段",
            StructuredOutputFailureKind.UNEXPECTED_FIELD,
        )


def _enum_string(value: object, allowed: frozenset[str], label: str) -> str:
    if not isinstance(value, str):
        raise _error(
            f"{label} 必须是字符串",
            StructuredOutputFailureKind.INVALID_FIELD_TYPE,
        )
    normalized = value.strip()
    if normalized not in allowed:
        raise _schema(f"{label} 不在允许枚举中")
    return normalized


def _bounded_string(
    value: object,
    label: str,
    maximum: int,
    *,
    allow_empty: bool = False,
) -> str:
    if not isinstance(value, str):
        raise _error(
            f"{label} 必须是字符串",
            StructuredOutputFailureKind.INVALID_FIELD_TYPE,
        )
    normalized = value.strip()
    if (not normalized and not allow_empty) or len(normalized) > maximum:
        raise _error(
            f"{label} 为空或超过长度上限",
            StructuredOutputFailureKind.EMPTY_FIELD,
        )
    return normalized


def _string_list(
    value: object,
    label: str,
    maximum_items: int,
    maximum_length: int,
) -> tuple[str, ...]:
    if not isinstance(value, list) or len(value) > maximum_items:
        raise _error(
            f"{label} 必须是有界数组",
            StructuredOutputFailureKind.INVALID_FIELD_TYPE,
        )
    normalized: list[str] = []
    for item in value:
        if not isinstance(item, str) or not item.strip() or len(item.strip()) > maximum_length:
            raise _error(
                f"{label} 包含无效字符串",
                StructuredOutputFailureKind.INVALID_FIELD_TYPE,
            )
        candidate = item.strip()
        if candidate in normalized:
            raise _schema(f"{label} 不得重复")
        normalized.append(candidate)
    return tuple(normalized)


def _strict_pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise _DuplicateField(key)
        result[key] = value
    return result


def _reject_constant(value: str):
    raise _InvalidConstant(value)


def _schema(message: str) -> ParseIntentError:
    return _error(message, StructuredOutputFailureKind.SCHEMA_VIOLATION)


def _error(message: str, kind: StructuredOutputFailureKind) -> ParseIntentError:
    return ParseIntentError(message, failure_kind=kind)
