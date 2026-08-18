"""DeepSeek 专用 ModelProvider。

DeepSeek 当前复用 OpenAI Chat Completions 协议，但供应商身份、默认端点和
``thinking`` 扩展由本类拥有，避免泄漏到通用兼容 Provider。
"""

from __future__ import annotations

from typing import Any

from jarvis_worker.agent.models.errors import ModelProviderError, model_config_error
from jarvis_worker.agent.models.messages import ModelMessage
from jarvis_worker.agent.models.openai_compatible_adapter import build_request_body
from jarvis_worker.agent.models.openai_compatible_provider import (
    OpenAiCompatibleModelProvider,
    _append_structured_output_retry,
    _append_tool_required_retry,
)
from jarvis_worker.agent.models.registry import DEEPSEEK_PROVIDER


class DeepSeekModelProvider(OpenAiCompatibleModelProvider):
    """通过 DeepSeek API 执行模型调用。"""

    def __init__(self, *, thinking_mode: str = "", **kwargs: Any):
        normalized_thinking = thinking_mode.strip().lower()
        if normalized_thinking not in {"", "disabled"}:
            raise model_config_error(
                f"JARVIS_MODEL_THINKING_MODE 非法: {thinking_mode!r}，"
                "DeepSeek 允许: ['', 'disabled']"
            )
        self._thinking_mode = normalized_thinking
        super().__init__(provider_name=DEEPSEEK_PROVIDER, **kwargs)

    def _build_request_body(
        self,
        messages: list[ModelMessage],
        *,
        stream: bool,
    ) -> dict[str, Any]:
        body = build_request_body(
            messages,
            model=self._model,
            max_tokens=self._max_tokens,
            stream=stream,
        )
        # DeepSeek 官方 JSON Output 能力。该字段不是 OpenAI-compatible
        # 协议的普遍保证，必须由 DeepSeek Provider 独立拥有。
        body["response_format"] = {"type": "json_object"}
        if self._thinking_mode == "disabled":
            body["thinking"] = {"type": "disabled"}
        return body

    def _should_retry_error(
        self,
        error: ModelProviderError,
        attempt: int,
    ) -> bool:
        """对 DeepSeek 偶发 JSON Output 失败做一次安全重试。

        官方文档注明 JSON Output 偶尔可能返回空内容。这里只在尚未向用户
        发布安全文本时由基类执行重试，并且结构化输出错误最多额外尝试一次；
        HTTP/超时仍沿用通用 recoverable 与 max_retries 策略。
        """
        if super()._should_retry_error(error, attempt):
            return True
        return error.code == "MODEL_OUTPUT_INVALID" and attempt == 0

    def _prepare_retry_messages(
        self,
        messages: list[ModelMessage],
        error: ModelProviderError,
    ) -> list[ModelMessage]:
        """结构化输出失败时注入固定纠正反馈，不回灌模型原文。"""
        if error.code != "MODEL_OUTPUT_INVALID":
            return super()._prepare_retry_messages(messages, error)
        if any(
            message.role == "system" and "工具补证模式" in message.content
            for message in messages
        ):
            return _append_tool_required_retry(messages)
        return _append_structured_output_retry(
            messages,
            failure_kind=error.output_failure_kind or "schema_violation",
        )
