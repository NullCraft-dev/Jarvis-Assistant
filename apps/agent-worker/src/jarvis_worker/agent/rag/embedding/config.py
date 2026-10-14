"""离线索引与在线检索共享的 Embedding 配置。"""

from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class RagEmbeddingConfig:
    rag_embedding_provider: str = "openai"
    rag_embedding_model: str = "text-embedding-3-small"
    rag_embedding_api_key_env: str = "OPENAI_API_KEY"
    rag_embedding_base_url: str = ""
    rag_embedding_dimensions: int = 1_536
    rag_embedding_batch_size: int = 128
    rag_embedding_timeout_seconds: int = 60

    @classmethod
    def from_env(cls) -> "RagEmbeddingConfig":
        provider = os.getenv("JARVIS_RAG_EMBEDDING_PROVIDER", "openai").strip().lower()
        if provider not in {"openai", "dashscope"}:
            raise ValueError("JARVIS_RAG_EMBEDDING_PROVIDER 仅支持 openai 或 dashscope")
        defaults = {
            "openai": ("text-embedding-3-small", "OPENAI_API_KEY", 128),
            "dashscope": ("qwen3.7-text-embedding", "DASHSCOPE_API_KEY", 20),
        }
        default_model, default_key_env, default_batch_size = defaults[provider]
        model = os.getenv("JARVIS_RAG_EMBEDDING_MODEL", default_model).strip()
        api_key_env = os.getenv("JARVIS_RAG_EMBEDDING_API_KEY_ENV", default_key_env).strip()
        if not model or not api_key_env:
            raise ValueError("RAG Embedding model/api_key_env 不能为空")
        return cls(
            rag_embedding_provider=provider,
            rag_embedding_model=model,
            rag_embedding_api_key_env=api_key_env,
            rag_embedding_base_url=os.getenv("JARVIS_RAG_EMBEDDING_BASE_URL", "").strip(),
            rag_embedding_dimensions=_parse_int(
                "JARVIS_RAG_EMBEDDING_DIMENSIONS",
                1_536,
                minimum=1_536,
                maximum=1_536,
            ),
            rag_embedding_batch_size=_parse_int(
                "JARVIS_RAG_EMBEDDING_BATCH_SIZE",
                default_batch_size,
                minimum=1,
                maximum=20 if provider == "dashscope" else 2_048,
            ),
            rag_embedding_timeout_seconds=_parse_int(
                "JARVIS_RAG_EMBEDDING_TIMEOUT_SECONDS",
                60,
                minimum=1,
                maximum=300,
            ),
        )


def _parse_int(name: str, default: int, *, minimum: int, maximum: int) -> int:
    raw = os.getenv(name)
    if raw is None:
        return default
    try:
        value = int(raw)
    except ValueError:
        raise ValueError(f"{name} 必须是整数，当前: {raw!r}") from None
    if value < minimum or value > maximum:
        raise ValueError(f"{name} 必须在 {minimum}..{maximum}，当前: {value}")
    return value
