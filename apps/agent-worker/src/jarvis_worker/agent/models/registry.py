"""模型 Provider 注册表与配置归一化。

Provider 标识表达真实供应商/运行后端；底层 API 协议是实现细节。
旧 ``openai_compatible`` 标识仅用于配置迁移，不进入运行时可观察数据。
"""

from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import urlparse


DEEPSEEK_PROVIDER = "deepseek"
CUSTOM_OPENAI_COMPATIBLE_PROVIDER = "custom_openai_compatible"
LEGACY_OPENAI_COMPATIBLE_PROVIDER = "openai_compatible"
OPENAI_CHAT_COMPLETIONS_PROTOCOL = "openai_chat_completions"
DEEPSEEK_DEFAULT_BASE_URL = "https://api.deepseek.com"


@dataclass(frozen=True)
class ModelProviderSpec:
    provider_id: str
    protocol: str
    default_base_url: str = ""
    supports_thinking_mode: bool = False


_PROVIDER_SPECS = {
    DEEPSEEK_PROVIDER: ModelProviderSpec(
        provider_id=DEEPSEEK_PROVIDER,
        protocol=OPENAI_CHAT_COMPLETIONS_PROTOCOL,
        default_base_url=DEEPSEEK_DEFAULT_BASE_URL,
        supports_thinking_mode=True,
    ),
    CUSTOM_OPENAI_COMPATIBLE_PROVIDER: ModelProviderSpec(
        provider_id=CUSTOM_OPENAI_COMPATIBLE_PROVIDER,
        protocol=OPENAI_CHAT_COMPLETIONS_PROTOCOL,
    ),
}


def normalize_provider_id(
    raw: str,
    *,
    base_url: str = "",
    model_name: str = "",
) -> str:
    """返回规范 Provider 标识。

    旧 ``openai_compatible`` 配置按现有 DeepSeek 官方端点迁移为
    ``deepseek``，其他兼容端点迁移为 ``custom_openai_compatible``。
    """
    cleaned = raw.strip().lower()
    if cleaned == LEGACY_OPENAI_COMPATIBLE_PROVIDER:
        return (
            DEEPSEEK_PROVIDER
            if _looks_like_deepseek(base_url, model_name)
            else CUSTOM_OPENAI_COMPATIBLE_PROVIDER
        )
    if cleaned not in _PROVIDER_SPECS:
        raise ValueError(
            f"JARVIS_MODEL_PROVIDER 非法值: {raw!r}，"
            f"允许: {sorted(_PROVIDER_SPECS)}"
        )
    return cleaned


def get_provider_spec(provider_id: str) -> ModelProviderSpec:
    try:
        return _PROVIDER_SPECS[provider_id]
    except KeyError:
        raise ValueError(f"未知 model_provider: {provider_id!r}") from None


def resolve_base_url(provider_id: str, configured_base_url: str) -> str:
    configured = configured_base_url.strip()
    if configured:
        return configured
    return get_provider_spec(provider_id).default_base_url


def _looks_like_deepseek(base_url: str, model_name: str) -> bool:
    # 模型名不能证明真实供应商；代理端点即便代理 DeepSeek 模型，也仍属于 custom。
    del model_name
    try:
        hostname = (urlparse(base_url.strip()).hostname or "").lower()
    except ValueError:
        return False
    return hostname == "api.deepseek.com" or hostname.endswith(".deepseek.com")
