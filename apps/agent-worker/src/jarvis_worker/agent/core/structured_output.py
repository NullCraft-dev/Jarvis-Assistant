"""结构化输出的安全失败分类与固定纠正策略。"""

from __future__ import annotations

import re
from enum import Enum

_JSON_FENCE = re.compile(
    r"\A```json[ \t]*\n(?P<body>[\s\S]*?)\n```[ \t]*\Z",
    re.IGNORECASE,
)


def normalize_structured_output_text(value: str) -> str:
    """对模型结构化输出执行无歧义的传输层规范化。

    这里只统一换行、去除首尾空白，并解开一层包裹完整响应的 ``json``
    Markdown fence。函数不会补引号、括号或反斜杠，也不会从自然语言中截取
    JSON；规范化后的内容仍必须通过严格 AgentAction parser。
    """

    normalized = value.replace("\r\n", "\n").replace("\r", "\n").strip()
    match = _JSON_FENCE.fullmatch(normalized)
    return match.group("body").strip() if match is not None else normalized


def repair_invalid_json_escapes(value: str) -> str:
    r"""只转义 JSON 字符串中非法的反斜杠序列。

    DeepSeek 在包含 LaTeX 的 final_message 中偶尔会输出 ``\gamma`` 这类
    非法 JSON escape。该修复不补括号、引号或字段，只把无法被 JSON 解释的
    反斜杠改为字面反斜杠；合法 escape 保持不变。
    """

    output: list[str] = []
    index = 0
    while index < len(value):
        char = value[index]
        if char != "\\" or index + 1 >= len(value):
            output.append(char)
            index += 1
            continue
        next_char = value[index + 1]
        if next_char in {'"', "\\", "/", "b", "f", "n", "r", "t"}:
            output.extend((char, next_char))
            index += 2
            continue
        if next_char == "u" and _has_four_hex_digits(value, index + 2):
            output.extend(value[index : index + 6])
            index += 6
            continue
        output.extend(("\\", "\\"))
        index += 1
    return "".join(output)


def _has_four_hex_digits(value: str, start: int) -> bool:
    digits = value[start : start + 4]
    return len(digits) == 4 and all(char in "0123456789abcdefABCDEF" for char in digits)


class StructuredOutputFailureKind(str, Enum):
    SCHEMA_VIOLATION = "schema_violation"
    INVALID_JSON = "invalid_json"
    DUPLICATE_FIELD = "duplicate_field"
    INVALID_JSON_CONSTANT = "invalid_json_constant"
    INVALID_ROOT_TYPE = "invalid_root_type"
    UNEXPECTED_FIELD = "unexpected_field"
    MISSING_FIELD = "missing_field"
    INVALID_FIELD_TYPE = "invalid_field_type"
    EMPTY_FIELD = "empty_field"
    UNSUPPORTED_ACTION = "unsupported_action"
    TOOL_NOT_ALLOWED = "tool_not_allowed"
    FORBIDDEN_ARGUMENT = "forbidden_argument"
    EMPTY_CONTENT = "empty_content"
    RESPONSE_TOO_LARGE = "response_too_large"
    MISSING_FINISH_REASON = "missing_finish_reason"
    TRUNCATED_OUTPUT = "truncated_output"
    UNEXPECTED_FINISH_REASON = "unexpected_finish_reason"
    UNEXPECTED_TOOL_CALLS = "unexpected_tool_calls"


_RETRY_INSTRUCTIONS = {
    StructuredOutputFailureKind.SCHEMA_VIOLATION: "严格沿用系统消息中的 AgentAction JSON 契约重新输出。",
    StructuredOutputFailureKind.INVALID_JSON: (
        "输出必须是单个合法 JSON object，不要使用 Markdown 代码块或附加文字；"
        "字符串中的反斜杠必须按 JSON 规则转义，LaTeX 命令例如 \\gamma 必须写成 \\\\gamma。"
    ),
    StructuredOutputFailureKind.DUPLICATE_FIELD: "每个 JSON object 中的字段名只能出现一次。",
    StructuredOutputFailureKind.INVALID_JSON_CONSTANT: "只能使用标准 JSON 值，不能使用 NaN 或 Infinity。",
    StructuredOutputFailureKind.INVALID_ROOT_TYPE: "最外层必须是 JSON object，不能是数组、字符串、数字或 null。",
    StructuredOutputFailureKind.UNEXPECTED_FIELD: "删除契约未声明的顶层字段，只保留所选 action 允许的字段。",
    StructuredOutputFailureKind.MISSING_FIELD: "补齐所选 action 必需的全部字段，并严格沿用系统消息中的 JSON 示例。",
    StructuredOutputFailureKind.INVALID_FIELD_TYPE: "确保每个字段使用契约要求的 JSON 类型，不要把 object 或 string 相互替代。",
    StructuredOutputFailureKind.EMPTY_FIELD: "必填字符串必须包含实际内容，不能是空字符串或纯空白。",
    StructuredOutputFailureKind.UNSUPPORTED_ACTION: "action_type 只能是 finish 或 call_tool。",
    StructuredOutputFailureKind.TOOL_NOT_ALLOWED: "call_tool 只能选择系统消息中当前允许的工具名称。",
    StructuredOutputFailureKind.FORBIDDEN_ARGUMENT: "删除由 Runtime 管理的可信参数，只提交工具公开参数。",
    StructuredOutputFailureKind.EMPTY_CONTENT: "必须返回非空的单个 JSON object。",
    StructuredOutputFailureKind.RESPONSE_TOO_LARGE: "缩短输出，只保留完成当前 action 所需的字段。",
    StructuredOutputFailureKind.MISSING_FINISH_REASON: "重新生成完整 JSON，确保响应正常结束。",
    StructuredOutputFailureKind.TRUNCATED_OUTPUT: "缩短内容并返回完整 JSON，不能让输出被截断。",
    StructuredOutputFailureKind.UNEXPECTED_FINISH_REASON: "重新生成单个完整 JSON object。",
    StructuredOutputFailureKind.UNEXPECTED_TOOL_CALLS: "不要使用供应商原生 tool_calls；必须按系统消息输出 call_tool JSON object。",
}


def retry_instruction_for(failure_kind: str | None) -> tuple[str, str]:
    """只返回本地枚举中的分类和指令，拒绝动态纠正文本。"""
    try:
        kind = StructuredOutputFailureKind(failure_kind)
    except (TypeError, ValueError):
        kind = StructuredOutputFailureKind.SCHEMA_VIOLATION
    return kind.value, _RETRY_INSTRUCTIONS[kind]
