"""OpenAI Embeddings Provider。密钥只驻留内存，不进入领域对象或日志。"""

from __future__ import annotations

import os

import httpx

from jarvis_worker.agent.rag.embedding.compatible import (
    EmbeddingProviderError,
    OpenAICompatibleEmbeddingProvider,
)

DEFAULT_OPENAI_EMBEDDING_MODEL = "text-embedding-3-small"
DEFAULT_OPENAI_EMBEDDING_DIMENSIONS = 1_536
DEFAULT_OPENAI_EMBEDDING_BASE_URL = "https://api.openai.com/v1"


class OpenAIEmbeddingError(EmbeddingProviderError):
    pass


class OpenAIEmbeddingProvider(OpenAICompatibleEmbeddingProvider):
    provider_name = "openai"
    service_label = "OpenAI"
    error_prefix = "OPENAI"
    error_type = OpenAIEmbeddingError

    def __init__(
        self,
        *,
        api_key: str,
        model_name: str = DEFAULT_OPENAI_EMBEDDING_MODEL,
        dimensions: int = DEFAULT_OPENAI_EMBEDDING_DIMENSIONS,
        base_url: str = DEFAULT_OPENAI_EMBEDDING_BASE_URL,
        timeout_seconds: float = 60.0,
        batch_size: int = 128,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        if not model_name.startswith("text-embedding-3-"):
            raise ValueError("OpenAI Provider 只允许 text-embedding-3 系列模型")
        super().__init__(
            api_key=api_key,
            model_name=model_name,
            dimensions=dimensions,
            base_url=base_url,
            timeout_seconds=timeout_seconds,
            batch_size=batch_size,
            max_batch_size=2_048,
            client=client,
        )


def create_openai_embedding_provider(config) -> OpenAIEmbeddingProvider:
    """兼容旧调用点；新代码应通过 create_embedding_provider 装配。"""

    api_key_env = config.rag_embedding_api_key_env.strip()
    if not api_key_env:
        raise ValueError("JARVIS_RAG_EMBEDDING_API_KEY_ENV 不能为空")
    api_key = os.getenv(api_key_env, "").strip()
    if not api_key:
        raise ValueError(f"{api_key_env} 未配置")
    return OpenAIEmbeddingProvider(
        api_key=api_key,
        model_name=config.rag_embedding_model,
        dimensions=config.rag_embedding_dimensions,
        base_url=config.rag_embedding_base_url or DEFAULT_OPENAI_EMBEDDING_BASE_URL,
        timeout_seconds=config.rag_embedding_timeout_seconds,
        batch_size=config.rag_embedding_batch_size,
    )
