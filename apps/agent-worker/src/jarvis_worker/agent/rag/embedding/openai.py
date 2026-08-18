"""OpenAI Embeddings Provider。密钥只驻留内存，不进入领域对象或日志。"""

from __future__ import annotations

import math
import os
from typing import Sequence

import httpx

from jarvis_worker.agent.rag.contracts import EmbeddingProvider

DEFAULT_OPENAI_EMBEDDING_MODEL = "text-embedding-3-small"
DEFAULT_OPENAI_EMBEDDING_DIMENSIONS = 1536


class OpenAIEmbeddingError(RuntimeError):
    def __init__(self, code: str, message: str, *, recoverable: bool):
        super().__init__(message)
        self.code = code
        self.recoverable = recoverable


class OpenAIEmbeddingProvider(EmbeddingProvider):
    provider_name = "openai"

    def __init__(
        self,
        *,
        api_key: str,
        model_name: str = DEFAULT_OPENAI_EMBEDDING_MODEL,
        dimensions: int = DEFAULT_OPENAI_EMBEDDING_DIMENSIONS,
        base_url: str = "https://api.openai.com/v1",
        timeout_seconds: float = 60.0,
        batch_size: int = 128,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        if not api_key.strip():
            raise ValueError("OPENAI_API_KEY 未配置")
        if not model_name.startswith("text-embedding-3-"):
            raise ValueError("第一版只允许 text-embedding-3 系列模型")
        if dimensions != DEFAULT_OPENAI_EMBEDDING_DIMENSIONS:
            raise ValueError("当前 RAG 向量索引固定为 1536 维")
        if not base_url.startswith("https://"):
            raise ValueError("OpenAI Embedding base_url 必须使用 HTTPS")
        if timeout_seconds <= 0 or not 1 <= batch_size <= 2048:
            raise ValueError("OpenAI Embedding timeout/batch_size 无效")
        self.model_name = model_name
        self.dimensions = dimensions
        self._batch_size = batch_size
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(
            base_url=base_url.rstrip("/"),
            timeout=timeout_seconds,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
        )

    async def embed_documents(self, texts: Sequence[str]) -> list[list[float]]:
        inputs = [text for text in texts]
        if not inputs:
            return []
        if any(not isinstance(text, str) or not text.strip() for text in inputs):
            raise ValueError("Embedding 输入不得为空")
        vectors: list[list[float]] = []
        for offset in range(0, len(inputs), self._batch_size):
            vectors.extend(await self._request(inputs[offset : offset + self._batch_size]))
        return vectors

    async def embed_query(self, text: str) -> list[float]:
        vectors = await self.embed_documents([text])
        return vectors[0]

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def _request(self, texts: list[str]) -> list[list[float]]:
        try:
            response = await self._client.post(
                "/embeddings",
                json={
                    "model": self.model_name,
                    "input": texts,
                    "dimensions": self.dimensions,
                    "encoding_format": "float",
                },
            )
        except (httpx.TimeoutException, httpx.NetworkError) as exc:
            raise OpenAIEmbeddingError(
                "OPENAI_EMBEDDING_UNAVAILABLE",
                "OpenAI Embedding 服务暂时不可用",
                recoverable=True,
            ) from exc
        if response.status_code >= 400:
            recoverable = response.status_code == 429 or response.status_code >= 500
            code = (
                "OPENAI_EMBEDDING_UNAVAILABLE"
                if recoverable
                else "OPENAI_EMBEDDING_REQUEST_REJECTED"
            )
            raise OpenAIEmbeddingError(
                code,
                f"OpenAI Embedding 请求失败（HTTP {response.status_code}）",
                recoverable=recoverable,
            )
        try:
            payload = response.json()
            items = payload["data"]
            if not isinstance(items, list) or len(items) != len(texts):
                raise ValueError("data 数量不匹配")
            ordered: list[list[float] | None] = [None] * len(texts)
            for item in items:
                index = int(item["index"])
                if index < 0 or index >= len(texts) or ordered[index] is not None:
                    raise ValueError("index 非法或重复")
                vector = [float(value) for value in item["embedding"]]
                if len(vector) != self.dimensions:
                    raise ValueError("维度不匹配")
                if not all(math.isfinite(value) for value in vector):
                    raise ValueError("包含非有限数值")
                ordered[index] = vector
            if any(vector is None for vector in ordered):
                raise ValueError("响应缺失")
            return [vector for vector in ordered if vector is not None]
        except (KeyError, TypeError, ValueError) as exc:
            raise OpenAIEmbeddingError(
                "OPENAI_EMBEDDING_INVALID_RESPONSE",
                "OpenAI Embedding 返回了无效结构",
                recoverable=True,
            ) from exc


def create_openai_embedding_provider(config) -> OpenAIEmbeddingProvider:
    """在 bootstrap 边界解析密钥；配置对象本身只保存环境变量名。"""

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
        timeout_seconds=config.rag_embedding_timeout_seconds,
        batch_size=config.rag_embedding_batch_size,
    )
