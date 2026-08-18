"""ModelFactory — 根据配置创建模型提供者。

生产运行支持独立 DeepSeek provider 与自定义 OpenAI-compatible provider。
测试直接注入确定性测试替身，不通过生产配置选择。
缺失或非法配置在启动阶段安全失败。
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from pathlib import Path

from jarvis_worker.agent.models.deepseek_provider import DeepSeekModelProvider
from jarvis_worker.agent.models.errors import model_config_error
from jarvis_worker.agent.models.fault_injection import (
    OneShotRecoverableModelFailureProvider,
)
from jarvis_worker.agent.models.langchain_factory import create_langchain_model_provider
from jarvis_worker.agent.models.openai_compatible_provider import OpenAiCompatibleModelProvider
from jarvis_worker.agent.models.provider import ModelProvider
from jarvis_worker.agent.models.registry import (
    CUSTOM_OPENAI_COMPATIBLE_PROVIDER,
    DEEPSEEK_PROVIDER,
    normalize_provider_id,
    resolve_base_url,
)
from jarvis_worker.agent.prompts.builder import PromptBuilder
from jarvis_worker.agent.tool_gateway.registry import ToolRegistry
from jarvis_worker.shared.config.settings import WorkerConfig

log = logging.getLogger("jarvis_worker.model_factory")

ProviderFactory = Callable[[WorkerConfig, PromptBuilder, str], ModelProvider]


def _create_deepseek_provider(
    cfg: WorkerConfig,
    prompt_builder: PromptBuilder,
    base_url: str,
) -> ModelProvider:
    return DeepSeekModelProvider(
        base_url=base_url,
        model=cfg.model_name,
        api_key_env=cfg.model_api_key_env,
        prompt_builder=prompt_builder,
        timeout=cfg.model_timeout_seconds,
        max_retries=cfg.model_max_retries,
        max_tokens=cfg.model_max_tokens,
        context_window_tokens=cfg.model_context_window_tokens,
        thinking_mode=cfg.model_thinking_mode,
    )


def _create_custom_openai_compatible_provider(
    cfg: WorkerConfig,
    prompt_builder: PromptBuilder,
    base_url: str,
) -> ModelProvider:
    if cfg.model_thinking_mode:
        raise model_config_error(
            "JARVIS_MODEL_THINKING_MODE 当前仅支持 deepseek provider"
        )
    return OpenAiCompatibleModelProvider(
        base_url=base_url,
        model=cfg.model_name,
        api_key_env=cfg.model_api_key_env,
        prompt_builder=prompt_builder,
        timeout=cfg.model_timeout_seconds,
        max_retries=cfg.model_max_retries,
        max_tokens=cfg.model_max_tokens,
        context_window_tokens=cfg.model_context_window_tokens,
    )


_PROVIDER_FACTORIES: dict[str, ProviderFactory] = {
    DEEPSEEK_PROVIDER: _create_deepseek_provider,
    CUSTOM_OPENAI_COMPATIBLE_PROVIDER: _create_custom_openai_compatible_provider,
}


def create_model_provider(
    cfg: WorkerConfig,
    prompt_builder: PromptBuilder | None = None,
    tool_registry: ToolRegistry | None = None,
) -> ModelProvider:
    """根据配置创建模型提供者。

    Args:
        cfg: Worker 配置（含 model_provider / model_base_url 等字段）。
        prompt_builder: PromptBuilder 实例（生产 Provider 必填）。
        tool_registry: ToolRegistry 实例，为 PromptBuilder 提供唯一工具真源。

    Returns:
        ModelProvider 实例。

    Raises:
        ModelProviderError: 配置非法（如缺少模型名或 API key）。
    """
    try:
        provider_type = normalize_provider_id(
            cfg.model_provider,
            base_url=cfg.model_base_url,
            model_name=cfg.model_name,
        )
    except ValueError:
        raise model_config_error(
            f"未知 model_provider: {cfg.model_provider!r}"
        ) from None
    base_url = resolve_base_url(provider_type, cfg.model_base_url)
    if prompt_builder is None:
        prompt_builder = (
            PromptBuilder.from_registry(tool_registry)
            if tool_registry is not None
            else PromptBuilder()
        )
    if cfg.model_adapter == "langchain":
        provider = create_langchain_model_provider(
            cfg,
            prompt_builder,
            base_url,
            provider_type,
        )
    elif cfg.model_adapter == "direct":
        factory = _PROVIDER_FACTORIES.get(provider_type)
        if factory is None:
            raise model_config_error(
                f"未知 model_provider: {provider_type!r}"
            )
        provider = factory(cfg, prompt_builder, base_url)
    else:
        raise model_config_error(
            f"未知 model_adapter: {cfg.model_adapter!r}"
        )
    if cfg.test_fault_injection_enabled:
        provider = OneShotRecoverableModelFailureProvider(
            provider,
            Path(cfg.test_tool_effect_barrier_root),
        )
        log.warning("已启用隔离验收专用一次性模型故障注入")
    log.info(
        "ModelProvider: %s/%s (%s, model=%s, timeout=%ds, retries=%d, thinking=%r)",
        cfg.model_adapter,
        provider_type,
        base_url,
        cfg.model_name,
        cfg.model_timeout_seconds,
        cfg.model_max_retries,
        cfg.model_thinking_mode,
    )
    return provider
