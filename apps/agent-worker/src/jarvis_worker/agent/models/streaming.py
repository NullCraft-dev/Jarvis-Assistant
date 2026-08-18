"""OpenAI-compatible JSON 输出的安全流式文本提取。

模型协议仍要求一个完整 AgentAction JSON。本模块只在确认 action_type 为
finish 后，从 top-level final_message 字段增量解码用户可见文本；绝不转发原始
JSON、工具调用参数或上下文。
"""

from __future__ import annotations

import re

_FINISH_ACTION_RE = re.compile(r'"action_type"\s*:\s*"finish"')
_FINAL_MESSAGE_START_RE = re.compile(r'"final_message"\s*:\s*"')


class FinalMessageStreamExtractor:
    """从分片 JSON 中提取并解码 ``finish.final_message``。

    该提取器只处理由受信 Provider 收到的 OpenAI content 分片。完整响应仍会
    交给既有 AgentAction parser 做最终严格校验；若模型输出不合法，调用方会以
    model.call.failed 收口，而不会把原始响应暴露给 UI。
    """

    def __init__(self) -> None:
        self._raw = ""
        self._finish_confirmed = False
        self._collecting = False
        self._finished = False
        self._escaped = False
        self._unicode_digits: str | None = None
        self._pending_high_surrogate: int | None = None

    def feed(self, fragment: str) -> list[str]:
        """输入一个模型 content 分片，返回安全可展示的文本增量。"""
        if not fragment or self._finished:
            return []

        self._raw += fragment
        if not self._finish_confirmed:
            self._finish_confirmed = _FINISH_ACTION_RE.search(self._raw) is not None
            if not self._finish_confirmed:
                return []

        if not self._collecting:
            match = _FINAL_MESSAGE_START_RE.search(self._raw)
            if match is None:
                return []
            self._collecting = True
            return self._consume(self._raw[match.end():])

        return self._consume(fragment)

    def _consume(self, text: str) -> list[str]:
        output: list[str] = []
        for char in text:
            if self._unicode_digits is not None:
                if char.lower() not in "0123456789abcdef":
                    # 无效 JSON escape 会由完整 Action parser 拒绝；不展示半截内容。
                    self._unicode_digits = None
                    self._escaped = False
                    continue
                self._unicode_digits += char
                if len(self._unicode_digits) == 4:
                    self._append_code_unit(int(self._unicode_digits, 16), output)
                    self._unicode_digits = None
                    self._escaped = False
                continue

            if self._escaped:
                escapes = {
                    '"': '"', '\\': '\\', '/': '/', 'b': '\b', 'f': '\f',
                    'n': '\n', 'r': '\r', 't': '\t',
                }
                if char == "u":
                    self._unicode_digits = ""
                    continue
                if char in escapes:
                    self._flush_pending_surrogate(output)
                    output.append(escapes[char])
                self._escaped = False
                continue

            if char == "\\":
                self._escaped = True
            elif char == '"':
                self._flush_pending_surrogate(output)
                self._finished = True
                break
            else:
                self._flush_pending_surrogate(output)
                output.append(char)

        return ["".join(output)] if output else []

    def _append_code_unit(self, value: int, output: list[str]) -> None:
        if 0xD800 <= value <= 0xDBFF:
            self._flush_pending_surrogate(output)
            self._pending_high_surrogate = value
            return
        if 0xDC00 <= value <= 0xDFFF and self._pending_high_surrogate is not None:
            high = self._pending_high_surrogate
            self._pending_high_surrogate = None
            output.append(chr(0x10000 + ((high - 0xD800) << 10) + (value - 0xDC00)))
            return
        self._flush_pending_surrogate(output)
        output.append(chr(value))

    def _flush_pending_surrogate(self, output: list[str]) -> None:
        if self._pending_high_surrogate is not None:
            output.append("\ufffd")
            self._pending_high_surrogate = None
