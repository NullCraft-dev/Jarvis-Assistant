"""Embedding Provider bootstrap factory; secret values are resolved only here."""

from __future__ import annotations

import os

from jarvis_worker.agent.rag.embedding.dashscope import (
    DEFAULT_DASHSCOPE_EMBEDDING_BASE_URL,
    DashScopeEmbeddingProvider,
)
from jarvis_worker.agent.rag.embedding.openai import (
    DEFAULT_OPENAI_EMBEDDING_BASE_URL,
    OpenAIEmbeddingProvider,
)


def create_embedding_provider(config):
    api_key_env = config.rag_embedding_api_key_env.strip()
    if not api_key_env:
        raise ValueError("JARVIS_RAG_EMBEDDING_API_KEY_ENV 不能为空")
    api_key = os.getenv(api_key_env, "").strip()
    if not api_key:
        raise ValueError(f"{api_key_env} 未配置")

    common = {
        "api_key": api_key,
        "model_name": config.rag_embedding_model,
        "dimensions": config.rag_embedding_dimensions,
        "timeout_seconds": config.rag_embedding_timeout_seconds,
        "batch_size": config.rag_embedding_batch_size,
    }
    if config.rag_embedding_provider == "openai":
        return OpenAIEmbeddingProvider(
            base_url=config.rag_embedding_base_url or DEFAULT_OPENAI_EMBEDDING_BASE_URL,
            **common,
        )
    if config.rag_embedding_provider == "dashscope":
        return DashScopeEmbeddingProvider(
            base_url=config.rag_embedding_base_url or DEFAULT_DASHSCOPE_EMBEDDING_BASE_URL,
            **common,
        )
    raise ValueError(f"不支持的 RAG Embedding Provider: {config.rag_embedding_provider}")
