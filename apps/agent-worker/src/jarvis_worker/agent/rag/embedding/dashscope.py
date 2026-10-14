"""Alibaba Cloud Model Studio / Qwen text Embedding Provider."""

from __future__ import annotations

import httpx

from jarvis_worker.agent.rag.embedding.compatible import (
    EmbeddingProviderError,
    OpenAICompatibleEmbeddingProvider,
)

DEFAULT_DASHSCOPE_EMBEDDING_MODEL = "qwen3.7-text-embedding"
DEFAULT_DASHSCOPE_EMBEDDING_DIMENSIONS = 1_536
DEFAULT_DASHSCOPE_EMBEDDING_BASE_URL = "https://dashscope.aliyuncs.com/compatible-mode/v1"
MAX_DASHSCOPE_EMBEDDING_BATCH_SIZE = 20


class DashScopeEmbeddingError(EmbeddingProviderError):
    pass


class DashScopeEmbeddingProvider(OpenAICompatibleEmbeddingProvider):
    provider_name = "dashscope"
    service_label = "DashScope"
    error_prefix = "DASHSCOPE"
    error_type = DashScopeEmbeddingError

    def __init__(
        self,
        *,
        api_key: str,
        model_name: str = DEFAULT_DASHSCOPE_EMBEDDING_MODEL,
        dimensions: int = DEFAULT_DASHSCOPE_EMBEDDING_DIMENSIONS,
        base_url: str = DEFAULT_DASHSCOPE_EMBEDDING_BASE_URL,
        timeout_seconds: float = 60.0,
        batch_size: int = MAX_DASHSCOPE_EMBEDDING_BATCH_SIZE,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        if model_name != DEFAULT_DASHSCOPE_EMBEDDING_MODEL:
            raise ValueError("DashScope Provider 当前只允许 qwen3.7-text-embedding")
        super().__init__(
            api_key=api_key,
            model_name=model_name,
            dimensions=dimensions,
            base_url=base_url,
            timeout_seconds=timeout_seconds,
            batch_size=batch_size,
            max_batch_size=MAX_DASHSCOPE_EMBEDDING_BATCH_SIZE,
            client=client,
        )
