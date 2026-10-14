"""文本向量化边界；具体 Provider 通过该目录接入。"""

from jarvis_worker.agent.rag.contracts import EmbeddingProvider
from jarvis_worker.agent.rag.embedding.compatible import EmbeddingProviderError
from jarvis_worker.agent.rag.embedding.config import RagEmbeddingConfig
from jarvis_worker.agent.rag.embedding.dashscope import (
    DEFAULT_DASHSCOPE_EMBEDDING_DIMENSIONS,
    DEFAULT_DASHSCOPE_EMBEDDING_MODEL,
    DashScopeEmbeddingError,
    DashScopeEmbeddingProvider,
)
from jarvis_worker.agent.rag.embedding.factory import create_embedding_provider
from jarvis_worker.agent.rag.embedding.openai import (
    DEFAULT_OPENAI_EMBEDDING_DIMENSIONS,
    DEFAULT_OPENAI_EMBEDDING_MODEL,
    OpenAIEmbeddingError,
    OpenAIEmbeddingProvider,
    create_openai_embedding_provider,
)
from jarvis_worker.agent.rag.embedding.service import (
    RagEmbeddingProcessResult,
    RagEmbeddingService,
)

__all__ = [
    "DEFAULT_DASHSCOPE_EMBEDDING_DIMENSIONS",
    "DEFAULT_DASHSCOPE_EMBEDDING_MODEL",
    "DEFAULT_OPENAI_EMBEDDING_DIMENSIONS",
    "DEFAULT_OPENAI_EMBEDDING_MODEL",
    "DashScopeEmbeddingError",
    "DashScopeEmbeddingProvider",
    "EmbeddingProvider",
    "EmbeddingProviderError",
    "OpenAIEmbeddingError",
    "OpenAIEmbeddingProvider",
    "RagEmbeddingConfig",
    "create_openai_embedding_provider",
    "create_embedding_provider",
    "RagEmbeddingProcessResult",
    "RagEmbeddingService",
]
