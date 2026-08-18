"""Agent Action Parser — 将模型原始文本输出解析为结构化 AgentAction。

Phase 6B-0：在接入真实 LLM Provider 之前，先补齐"模型输出 → AgentAction"的解析契约。
本轮只做 parser + prompt contract，不接真实 LLM、不引入 LangGraph、不执行工具。

职责：
- 输入模型原始文本输出（预期为 JSON object），输出合法 AgentAction。
- 只接受 JSON object，不从自然语言中猜测；若模型一次返回多个相邻 JSON
  action object，只消费第一个，把其余步骤留给下一轮 Agent Loop 重新决策。
- 支持两种 action：finish / call_tool。
- 对 call_tool 的 tool_name 做白名单校验（不替代 ToolGateway 的最终校验）。
- 校验失败时抛出 ParseAgentActionError，携带可测试的具体失败原因。

不负责：
- 执行工具（由 ToolGateway 负责）
- 权限校验（由 PermissionManager 负责）
- 工具参数深度校验（由 ToolGateway / executor 负责）
- 文件系统、Redis、数据库访问
- 真实 LLM 调用
"""

from __future__ import annotations

import json
from typing import Any

from jarvis_worker.agent.core.actions import AgentAction
from jarvis_worker.agent.core.response_format import normalize_final_message
from jarvis_worker.agent.core.structured_output import (
    StructuredOutputFailureKind,
    normalize_structured_output_text,
    repair_invalid_json_escapes,
)
from jarvis_worker.agent.tools.builtin import builtin_tool_names

# 兼容直接使用 parser 的调用方；集合由 capability manifests 派生，不再手工维护。
_DEFAULT_ALLOWED_TOOLS = builtin_tool_names()
_DEFAULT_ALLOWED_ACTION_TYPES = frozenset({"finish", "call_tool"})

# 禁止模型提供的参数 —— 这些字段属于可信运行时上下文，
# 由 AgentRunner 从 AgentState / RunJobMessage 注入，LLM 不得控制
_FORBIDDEN_ARG_KEYS = frozenset({"workspace_root"})
MAX_FINAL_MESSAGE_CHARS = 32_768


AgentActionFailureKind = StructuredOutputFailureKind


class ParseAgentActionError(Exception):
    """解析 AgentAction 失败。

    用于测试可验证具体失败原因；不包含敏感信息。
    """

    def __init__(
        self,
        message: str,
        *,
        failure_kind: AgentActionFailureKind,
    ) -> None:
        super().__init__(message)
        self.failure_kind = failure_kind.value


class _DuplicateJsonField(ValueError):
    pass


class _InvalidJsonConstant(ValueError):
    pass


def _decode_model_json(raw_text: str) -> Any:
    """Decode one model action without guessing from prose.

    JSON-mode providers occasionally batch several top-level action objects even
    when instructed to emit one.  A pure sequence of adjacent JSON objects is
    unambiguous: the Runtime consumes only the first action and will ask the LLM
    to re-plan after observing its result.  Any prose, damaged trailing value,
    array batch, duplicate field or non-standard constant still fails closed.
    """
    decoder = json.JSONDecoder(
        object_pairs_hook=_strict_object_pairs,
        parse_constant=_reject_json_constant,
        # 只兼容字符串内未转义的控制字符，不修复括号、引号或字段。
        strict=False,
    )
    try:
        return decoder.decode(raw_text)
    except json.JSONDecodeError as original:
        if original.msg != "Extra data":
            raise

        first, end = decoder.raw_decode(raw_text)
        if not isinstance(first, dict):
            raise original
        remaining = raw_text[end:].lstrip()
        batched_count = 0
        while remaining:
            next_value, next_end = decoder.raw_decode(remaining)
            if not isinstance(next_value, dict):
                raise original
            batched_count += 1
            remaining = remaining[next_end:].lstrip()
        if batched_count == 0:
            raise original
        return first


def parse_agent_action(
    raw_text: str,
    allowed_tools: frozenset[str] | None = None,
    allowed_action_types: frozenset[str] | None = None,
) -> AgentAction:
    """将模型原始文本输出解析为 AgentAction。

    Args:
        raw_text: 模型原始输出字符串，预期为 JSON object。
        allowed_tools: 允许的 tool_name 白名单；fallback 从内置 capability
            manifests 读取，生产 Provider 应传入 PromptBuilder 的实际白名单。
        allowed_action_types: 当前模型调用允许的动作类型；默认允许 finish/call_tool。
            Runtime 的 finish-only 与 tool-required 协议必须显式收紧该集合。

    Returns:
        合法的 AgentAction。

    Raises:
        ParseAgentActionError: 解析或校验失败，携带具体失败原因。
    """
    if allowed_tools is None:
        allowed_tools = _DEFAULT_ALLOWED_TOOLS
    if allowed_action_types is None:
        allowed_action_types = _DEFAULT_ALLOWED_ACTION_TYPES
    if not allowed_action_types or not allowed_action_types.issubset(
        _DEFAULT_ALLOWED_ACTION_TYPES
    ):
        raise ValueError("allowed_action_types 必须是 finish/call_tool 的非空子集")

    raw_text = normalize_structured_output_text(raw_text)

    # 1. JSON 解析
    try:
        parsed = _decode_model_json(raw_text)
    except _DuplicateJsonField:
        raise ParseAgentActionError(
            "JSON object 包含重复字段",
            failure_kind=AgentActionFailureKind.DUPLICATE_FIELD,
        ) from None
    except _InvalidJsonConstant as exc:
        raise ParseAgentActionError(
            f"JSON 包含非标准常量: {exc}",
            failure_kind=AgentActionFailureKind.INVALID_JSON_CONSTANT,
        ) from None
    except json.JSONDecodeError as exc:
        if exc.msg == "Invalid \\escape":
            try:
                parsed = _decode_model_json(repair_invalid_json_escapes(raw_text))
            except (json.JSONDecodeError, _DuplicateJsonField, _InvalidJsonConstant):
                pass
            else:
                return _parse_decoded_action(
                    parsed,
                    allowed_tools,
                    allowed_action_types,
                )
        raise ParseAgentActionError(
            f"模型输出不是合法 JSON: {exc}",
            failure_kind=AgentActionFailureKind.INVALID_JSON,
        ) from exc

    return _parse_decoded_action(parsed, allowed_tools, allowed_action_types)


def _parse_decoded_action(
    parsed: Any,
    allowed_tools: frozenset[str],
    allowed_action_types: frozenset[str],
) -> AgentAction:
    """校验已经由严格 JSON decoder 解析出的单个 action。"""

    # 2. 必须是 JSON object
    if not isinstance(parsed, dict):
        raise ParseAgentActionError(
            f"模型输出必须是 JSON object，实际类型: {type(parsed).__name__}",
            failure_kind=AgentActionFailureKind.INVALID_ROOT_TYPE,
        )

    # 3. action_type 必须存在且为字符串
    action_type = parsed.get("action_type")
    if action_type is None:
        raise ParseAgentActionError(
            "action_type 字段缺失",
            failure_kind=AgentActionFailureKind.MISSING_FIELD,
        )
    if not isinstance(action_type, str):
        raise ParseAgentActionError(
            f"action_type 必须是字符串，实际类型: {type(action_type).__name__}",
            failure_kind=AgentActionFailureKind.INVALID_FIELD_TYPE,
        )
    action_type = action_type.strip()
    if not action_type:
        raise ParseAgentActionError(
            "action_type 不能为空字符串",
            failure_kind=AgentActionFailureKind.EMPTY_FIELD,
        )

    if action_type not in _DEFAULT_ALLOWED_ACTION_TYPES:
        raise ParseAgentActionError(
            "未知的 action_type，当前支持: finish, call_tool",
            failure_kind=AgentActionFailureKind.UNSUPPORTED_ACTION,
        )
    if action_type not in allowed_action_types:
        raise ParseAgentActionError(
            "action_type 不符合当前 Runtime 动作模式",
            failure_kind=AgentActionFailureKind.UNSUPPORTED_ACTION,
        )

    # 4. 按 action_type 分发校验
    if action_type == "finish":
        return _parse_finish(parsed)
    elif action_type == "call_tool":
        return _parse_call_tool(parsed, allowed_tools)
    else:
        raise ParseAgentActionError(
            "未知的 action_type，当前支持: finish, call_tool",
            failure_kind=AgentActionFailureKind.UNSUPPORTED_ACTION,
        )


def _parse_finish(parsed: dict[str, Any]) -> AgentAction:
    """校验并构造 finish action。"""
    _reject_unexpected_fields(
        parsed,
        frozenset(
            {
                "action_type",
                "final_message",
                "reason",
                "citations",
                "insufficient_evidence",
            }
        ),
    )
    final_message = parsed.get("final_message")

    if final_message is None:
        raise ParseAgentActionError(
            "finish action 缺少 final_message 字段",
            failure_kind=AgentActionFailureKind.MISSING_FIELD,
        )
    if not isinstance(final_message, str):
        raise ParseAgentActionError(
            f"finish action 的 final_message 必须是字符串，实际类型: {type(final_message).__name__}",
            failure_kind=AgentActionFailureKind.INVALID_FIELD_TYPE,
        )
    if not final_message.strip():
        raise ParseAgentActionError(
            "finish action 的 final_message 不能为空字符串",
            failure_kind=AgentActionFailureKind.EMPTY_FIELD,
        )
    if len(final_message) > MAX_FINAL_MESSAGE_CHARS:
        raise ParseAgentActionError(
            f"finish action 的 final_message 不能超过 {MAX_FINAL_MESSAGE_CHARS} 字符",
            failure_kind=AgentActionFailureKind.RESPONSE_TOO_LARGE,
        )

    citations = parsed.get("citations", [])
    if not isinstance(citations, list) or len(citations) > 12:
        raise ParseAgentActionError(
            "finish action 的 citations 必须是不超过 12 项的数组",
            failure_kind=AgentActionFailureKind.INVALID_FIELD_TYPE,
        )
    normalized: list[dict[str, str]] = []
    for citation in citations:
        if (
            not isinstance(citation, dict)
            or set(citation) != {"chunk_id"}
            or not isinstance(citation.get("chunk_id"), str)
            or not citation["chunk_id"].strip()
        ):
            raise ParseAgentActionError(
                "每个 citation 必须且只能包含非空字符串 chunk_id",
                failure_kind=AgentActionFailureKind.INVALID_FIELD_TYPE,
            )
        normalized.append({"chunk_id": citation["chunk_id"].strip()})
    insufficient = parsed.get("insufficient_evidence", False)
    if not isinstance(insufficient, bool):
        raise ParseAgentActionError(
            "finish action 的 insufficient_evidence 必须是布尔值",
            failure_kind=AgentActionFailureKind.INVALID_FIELD_TYPE,
        )

    normalized_message = normalize_final_message(final_message)
    if not normalized_message:
        raise ParseAgentActionError(
            "finish action 的 final_message 规范化后不能为空字符串",
            failure_kind=AgentActionFailureKind.EMPTY_FIELD,
        )

    return AgentAction.finish(
        final_message=normalized_message,
        citations=tuple(normalized),
        insufficient_evidence=insufficient,
    )


def _parse_call_tool(
    parsed: dict[str, Any],
    allowed_tools: frozenset[str],
) -> AgentAction:
    """校验并构造 call_tool action。"""
    _reject_unexpected_fields(
        parsed,
        frozenset({"action_type", "tool_name", "arguments", "reason"}),
    )
    # 4a. tool_name 必须存在且为字符串
    tool_name = parsed.get("tool_name")
    if tool_name is None:
        raise ParseAgentActionError(
            "call_tool action 缺少 tool_name 字段",
            failure_kind=AgentActionFailureKind.MISSING_FIELD,
        )
    if not isinstance(tool_name, str):
        raise ParseAgentActionError(
            f"call_tool action 的 tool_name 必须是字符串，实际类型: {type(tool_name).__name__}",
            failure_kind=AgentActionFailureKind.INVALID_FIELD_TYPE,
        )
    tool_name = tool_name.strip()
    if not tool_name:
        raise ParseAgentActionError(
            "call_tool action 的 tool_name 不能为空字符串",
            failure_kind=AgentActionFailureKind.EMPTY_FIELD,
        )

    # 4b. tool_name 必须在白名单内
    if tool_name not in allowed_tools:
        raise ParseAgentActionError(
            "call_tool action 的 tool_name 不在允许列表中",
            failure_kind=AgentActionFailureKind.TOOL_NOT_ALLOWED,
        )

    # 4c. arguments 必须存在且为 object
    arguments = parsed.get("arguments")
    if arguments is None:
        raise ParseAgentActionError(
            "call_tool action 缺少 arguments 字段",
            failure_kind=AgentActionFailureKind.MISSING_FIELD,
        )
    if not isinstance(arguments, dict):
        raise ParseAgentActionError(
            f"call_tool action 的 arguments 必须是 JSON object，实际类型: {type(arguments).__name__}",
            failure_kind=AgentActionFailureKind.INVALID_FIELD_TYPE,
        )

    # 4d. 拒绝模型提供可信运行时参数（workspace_root 等）
    for forbidden_key in _FORBIDDEN_ARG_KEYS:
        if forbidden_key in arguments:
            raise ParseAgentActionError(
                f"call_tool action 的 arguments 不能包含 {forbidden_key!r} 字段，"
                f"该字段属于可信运行时上下文，由 AgentRunner 注入，不能由模型提供",
                failure_kind=AgentActionFailureKind.FORBIDDEN_ARGUMENT,
            )

    # 4e. reason 为可选字段
    reason = parsed.get("reason", "")
    if not isinstance(reason, str):
        raise ParseAgentActionError(
            f"call_tool action 的 reason 必须是字符串，实际类型: {type(reason).__name__}",
            failure_kind=AgentActionFailureKind.INVALID_FIELD_TYPE,
        )

    return AgentAction.call_tool(
        tool_name=tool_name,
        arguments=arguments,
        reason=reason.strip(),
    )


def _strict_object_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise _DuplicateJsonField(key)
        result[key] = value
    return result


def _reject_json_constant(value: str) -> Any:
    raise _InvalidJsonConstant(value)


def _reject_unexpected_fields(
    parsed: dict[str, Any],
    allowed_fields: frozenset[str],
) -> None:
    if set(parsed) - allowed_fields:
        raise ParseAgentActionError(
            "action 包含未声明字段",
            failure_kind=AgentActionFailureKind.UNEXPECTED_FIELD,
        )
