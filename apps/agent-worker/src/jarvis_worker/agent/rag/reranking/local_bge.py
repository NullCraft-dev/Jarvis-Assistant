"""本地 BGE Cross-Encoder sidecar 的 Provider adapter。"""

from __future__ import annotations

import os
from dataclasses import dataclass
from urllib.parse import urlparse

import httpx

from jarvis_worker.agent.rag.reranking.contracts import (
    RerankDocument,
    RerankerProvider,
    RerankerProviderError,
    RerankScore,
)


@dataclass(frozen=True, slots=True)
class LocalBgeRerankerConfig:
    enabled: bool = False
    base_url: str = "http://127.0.0.1:8121"
    model_name: str = "BAAI/bge-reranker-v2-m3"
    timeout_seconds: float = 30.0

    def __post_init__(self) -> None:
        parsed = urlparse(self.base_url)
        if parsed.scheme != "http" or parsed.hostname not in {"127.0.0.1", "localhost", "::1"}:
            raise ValueError("本地 Reranker URL 必须是无凭据 localhost HTTP 地址")
        if parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError("本地 Reranker URL 不得包含凭据、query 或 fragment")
        if not self.model_name.strip() or len(self.model_name) > 200:
            raise ValueError("Reranker model_name 必须是 1..200 字符")
        if not 1 <= self.timeout_seconds <= 120:
            raise ValueError("Reranker timeout_seconds 必须在 1..120")

    @classmethod
    def from_env(cls) -> "LocalBgeRerankerConfig":
        return cls(
            enabled=_parse_bool("JARVIS_RAG_RERANKER_ENABLED", False),
            base_url=os.getenv("JARVIS_RAG_RERANKER_URL", "http://127.0.0.1:8121").strip(),
            model_name=os.getenv("JARVIS_RAG_RERANKER_MODEL", "BAAI/bge-reranker-v2-m3").strip(),
            timeout_seconds=_parse_float(
                "JARVIS_RAG_RERANKER_TIMEOUT_SECONDS", 30.0, minimum=1, maximum=120
            ),
        )


class LocalBgeRerankerProvider(RerankerProvider):
    provider_name = "local-bge-cross-encoder"

    def __init__(
        self,
        config: LocalBgeRerankerConfig,
        *,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._config = config
        self.model_name = config.model_name
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(
            base_url=config.base_url.rstrip("/"),
            timeout=httpx.Timeout(config.timeout_seconds),
            trust_env=False,
        )

    async def score(
        self, *, query: str, documents: tuple[RerankDocument, ...]
    ) -> tuple[RerankScore, ...]:
        try:
            response = await self._client.post(
                "/v1/rerank",
                json={
                    "model": self.model_name,
                    "query": query,
                    "documents": [
                        {
                            "chunk_id": str(document.chunk_id),
                            "title": document.document_title,
                            "heading_path": list(document.heading_path),
                            "content": document.content,
                        }
                        for document in documents
                    ],
                },
            )
            response.raise_for_status()
            payload = response.json()
            if payload.get("model") != self.model_name or not isinstance(
                payload.get("scores"), list
            ):
                raise ValueError("响应契约无效")
            return tuple(
                RerankScore(
                    chunk_id=_uuid(item["chunk_id"]),
                    score=float(item["score"]),
                )
                for item in payload["scores"]
            )
        except httpx.TimeoutException as exc:
            raise RerankerProviderError("RERANKER_TIMEOUT") from exc
        except httpx.HTTPError as exc:
            raise RerankerProviderError("RERANKER_UNAVAILABLE") from exc
        except (KeyError, TypeError, ValueError) as exc:
            raise RerankerProviderError("RERANKER_RESPONSE_INVALID") from exc

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()


def _uuid(value):
    from uuid import UUID

    return UUID(str(value))


def _parse_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    normalized = raw.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"{name} 必须是 true 或 false")


def _parse_float(name: str, default: float, *, minimum: float, maximum: float) -> float:
    raw = os.getenv(name)
    if raw is None:
        return default
    try:
        value = float(raw)
    except ValueError:
        raise ValueError(f"{name} 必须是数值") from None
    if not minimum <= value <= maximum:
        raise ValueError(f"{name} 必须在 {minimum}..{maximum}")
    return value
