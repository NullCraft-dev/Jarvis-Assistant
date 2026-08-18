"""供应商 LangChain ChatModel 的唯一装配入口。"""

from __future__ import annotations

from typing import Any

from langchain_deepseek import ChatDeepSeek
from langchain_openai import ChatOpenAI

from jarvis_worker.agent.models.langchain_provider import LangChainModelProvider
from jarvis_worker.agent.models.provider import ModelProvider
from jarvis_worker.agent.models.provider_config import (
    check_api_key_exists,
    read_api_key,
    validate_provider_config,
)
from jarvis_worker.agent.models.registry import (
    CUSTOM_OPENAI_COMPATIBLE_PROVIDER,
    DEEPSEEK_PROVIDER,
)
from jarvis_worker.agent.prompts.builder import PromptBuilder
from jarvis_worker.shared.config.settings import WorkerConfig


class OpenAiCompatibleChatModel(ChatOpenAI):
    """保留项目既有兼容端点的 ``max_tokens`` 请求契约。

    ChatOpenAI 面向官方 OpenAI API，会把旧参数自动改名为
    ``max_completion_tokens``；自定义兼容端点不一定支持该新名称。
    版本锁定后在最窄供应商 adapter 处恢复原字段，避免改变既有协议。
    """

    def _get_request_payload(
        self,
        input_: Any,
        *,
        stop: list[str] | None = None,
        **kwargs: Any,
    ) -> dict[str, Any]:
        payload = super()._get_request_payload(input_, stop=stop, **kwargs)
        if "max_completion_tokens" in payload:
            payload["max_tokens"] = payload.pop("max_completion_tokens")
        return payload


def create_langchain_model_provider(
    cfg: WorkerConfig,
    prompt_builder: PromptBuilder,
    base_url: str,
    provider_type: str,
) -> ModelProvider:
    """创建实现项目 ModelProvider 的供应商专用 LangChain adapter。"""
    validate_provider_config(
        base_url.strip().rstrip("/"),
        cfg.model_name.strip(),
        cfg.model_api_key_env.strip(),
        cfg.model_max_retries,
    )
    check_api_key_exists(cfg.model_api_key_env.strip())
    api_key = read_api_key(cfg.model_api_key_env.strip())

    if provider_type == DEEPSEEK_PROVIDER:
        extra_body = (
            {"thinking": {"type": "disabled"}}
            if cfg.model_thinking_mode == "disabled"
            else None
        )
        chat_model = ChatDeepSeek(
            model=cfg.model_name.strip(),
            base_url=base_url.strip().rstrip("/"),
            api_key=api_key,
            timeout=cfg.model_timeout_seconds,
            max_retries=0,
            max_tokens=cfg.model_max_tokens,
            model_kwargs={"response_format": {"type": "json_object"}},
            extra_body=extra_body,
            stream_usage=False,
        )
        retry_structured_output_once = True
    elif provider_type == CUSTOM_OPENAI_COMPATIBLE_PROVIDER:
        chat_model = OpenAiCompatibleChatModel(
            model=cfg.model_name.strip(),
            base_url=base_url.strip().rstrip("/"),
            api_key=api_key,
            timeout=cfg.model_timeout_seconds,
            max_retries=0,
            max_tokens=cfg.model_max_tokens,
            stream_usage=False,
            use_responses_api=False,
        )
        retry_structured_output_once = False
    else:
        raise ValueError(f"不支持的 LangChain model provider: {provider_type!r}")

    return LangChainModelProvider(
        chat_model=chat_model,
        provider_name=provider_type,
        model=cfg.model_name.strip(),
        prompt_builder=prompt_builder,
        max_retries=cfg.model_max_retries,
        max_tokens=cfg.model_max_tokens,
        context_window_tokens=cfg.model_context_window_tokens,
        retry_structured_output_once=retry_structured_output_once,
    )
