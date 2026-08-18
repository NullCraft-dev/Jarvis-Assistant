"""离线索引与在线检索共享的 Embedding 配置。"""

from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class RagEmbeddingConfig:
    rag_embedding_model: str = "text-embedding-3-small"
    rag_embedding_api_key_env: str = "OPENAI_API_KEY"
    rag_embedding_dimensions: int = 1_536
    rag_embedding_batch_size: int = 128
    rag_embedding_timeout_seconds: int = 60

    @classmethod
    def from_env(cls) -> "RagEmbeddingConfig":
        model = os.getenv("JARVIS_RAG_EMBEDDING_MODEL", "text-embedding-3-small").strip()
        api_key_env = os.getenv("JARVIS_RAG_EMBEDDING_API_KEY_ENV", "OPENAI_API_KEY").strip()
        if not model or not api_key_env:
            raise ValueError("RAG Embedding model/api_key_env 不能为空")
        return cls(
            rag_embedding_model=model,
            rag_embedding_api_key_env=api_key_env,
            rag_embedding_dimensions=_parse_int(
                "JARVIS_RAG_EMBEDDING_DIMENSIONS",
                1_536,
                minimum=1_536,
                maximum=1_536,
            ),
            rag_embedding_batch_size=_parse_int(
                "JARVIS_RAG_EMBEDDING_BATCH_SIZE",
                128,
                minimum=1,
                maximum=2_048,
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
