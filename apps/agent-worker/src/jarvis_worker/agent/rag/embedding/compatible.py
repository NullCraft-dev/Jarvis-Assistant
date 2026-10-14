"""OpenAI-compatible Embedding transport shared by concrete providers."""

from __future__ import annotations

import math
from typing import Sequence, Type

import httpx

from jarvis_worker.agent.rag.contracts import EmbeddingProvider


class EmbeddingProviderError(RuntimeError):
    """Provider-safe error; upstream response bodies and credentials are never exposed."""

    def __init__(self, code: str, message: str, *, recoverable: bool):
        super().__init__(message)
        self.code = code
        self.recoverable = recoverable


class OpenAICompatibleEmbeddingProvider(EmbeddingProvider):
    provider_name = ""
    service_label = "Embedding"
    error_prefix = "RAG"
    error_type: Type[EmbeddingProviderError] = EmbeddingProviderError

    def __init__(
        self,
        *,
        api_key: str,
        model_name: str,
        dimensions: int,
        base_url: str,
        timeout_seconds: float,
        batch_size: int,
        max_batch_size: int,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        if not api_key.strip():
            raise ValueError(f"{self.service_label} API key 未配置")
        if dimensions != 1_536:
            raise ValueError("当前 RAG 向量索引固定为 1536 维")
        if not base_url.startswith("https://"):
            raise ValueError(f"{self.service_label} Embedding base_url 必须使用 HTTPS")
        if timeout_seconds <= 0 or not 1 <= batch_size <= max_batch_size:
            raise ValueError(
                f"{self.service_label} Embedding timeout/batch_size 无效，"
                f"batch_size 必须在 1..{max_batch_size}"
            )
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
        inputs = list(texts)
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
            raise self.error_type(
                f"{self.error_prefix}_EMBEDDING_UNAVAILABLE",
                f"{self.service_label} Embedding 服务暂时不可用",
                recoverable=True,
            ) from exc
        if response.status_code >= 400:
            recoverable = response.status_code == 429 or response.status_code >= 500
            suffix = "UNAVAILABLE" if recoverable else "REQUEST_REJECTED"
            raise self.error_type(
                f"{self.error_prefix}_EMBEDDING_{suffix}",
                f"{self.service_label} Embedding 请求失败（HTTP {response.status_code}）",
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
            raise self.error_type(
                f"{self.error_prefix}_EMBEDDING_INVALID_RESPONSE",
                f"{self.service_label} Embedding 返回了无效结构",
                recoverable=True,
            ) from exc
