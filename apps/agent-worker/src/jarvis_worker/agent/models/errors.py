"""ModelProvider 错误类型 — 结构化、安全的模型调用错误。

Phase 6B-1：支持 OpenAI-compatible 真实 Provider 的错误分类。
所有错误不包含原始响应 body、Authorization header 或密钥。
"""

from __future__ import annotations


class ModelProviderError(Exception):
    """模型提供者错误基类。

    Attributes:
        code: 错误码（如 MODEL_TIMEOUT）。
        message: 面向用户/日志的安全消息（不含密钥/原始body）。
        recoverable: 是否可重试。
    """

    def __init__(
        self,
        code: str,
        message: str,
        recoverable: bool = False,
        *,
        output_failure_kind: str | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.recoverable = recoverable
        self.output_failure_kind = output_failure_kind
        self.attempt_count = 1


def model_config_error(message: str) -> ModelProviderError:
    """模型配置错误（不可重试）。"""
    return ModelProviderError("MODEL_CONFIG_ERROR", message, recoverable=False)


def model_timeout_error(message: str) -> ModelProviderError:
    """请求超时（可重试）。"""
    return ModelProviderError("MODEL_TIMEOUT", message, recoverable=True)


def model_http_error(status: int, message: str) -> ModelProviderError:
    """HTTP 错误。4xx 不可重试，5xx 可重试。"""
    recoverable = status >= 500 or status == 429
    return ModelProviderError("MODEL_HTTP_ERROR", message, recoverable=recoverable)


def model_response_invalid(message: str) -> ModelProviderError:
    """响应格式无效（不可重试 —— 模型返回了非预期结构）。"""
    return ModelProviderError("MODEL_RESPONSE_INVALID", message, recoverable=False)


def model_output_invalid(
    message: str,
    *,
    failure_kind: str = "schema_violation",
) -> ModelProviderError:
    """模型输出无效（不可重试 —— 如非法 JSON/未知 action）。"""
    return ModelProviderError(
        "MODEL_OUTPUT_INVALID",
        message,
        recoverable=False,
        output_failure_kind=failure_kind,
    )


def context_budget_exceeded(message: str) -> ModelProviderError:
    """必需上下文无法在不破坏原子语义的情况下装入输入预算。"""
    return ModelProviderError(
        "CONTEXT_BUDGET_EXCEEDED", message, recoverable=False
    )
