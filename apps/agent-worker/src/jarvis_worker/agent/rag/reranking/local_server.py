"""独立本地 BGE Cross-Encoder 服务；重模型不进入 Agent Worker 进程。"""

from __future__ import annotations

import argparse
import os
from contextlib import asynccontextmanager
from dataclasses import dataclass

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, ConfigDict, Field


class DocumentInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    chunk_id: str = Field(min_length=36, max_length=36)
    title: str = Field(min_length=1, max_length=500)
    heading_path: list[str] = Field(default_factory=list, max_length=16)
    content: str = Field(min_length=1, max_length=6_000)


class RerankInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    model: str = Field(min_length=1, max_length=200)
    query: str = Field(min_length=1, max_length=2_000)
    documents: list[DocumentInput] = Field(min_length=1, max_length=30)


def create_app(backend=None) -> FastAPI:
    state = {"backend": backend}

    @asynccontextmanager
    async def lifespan(_app):
        if state["backend"] is None:
            state["backend"] = TransformersCrossEncoder.from_env()
        warmup = getattr(state["backend"], "warmup", None)
        if callable(warmup):
            warmup()
        yield

    app = FastAPI(title="Jarvis Local BGE Reranker", lifespan=lifespan)

    @app.get("/health")
    async def health():
        active = state["backend"]
        return {"status": "ok", "model": active.model_name if active else "loading"}

    @app.post("/v1/rerank")
    async def rerank(payload: RerankInput):
        active = state["backend"]
        if active is None or payload.model != active.model_name:
            raise HTTPException(status_code=409, detail="reranker model mismatch")
        passages = [
            "\n".join(
                part
                for part in (
                    document.title,
                    " > ".join(document.heading_path),
                    document.content,
                )
                if part
            )
            for document in payload.documents
        ]
        scores = active.score(payload.query, passages)
        return {
            "model": active.model_name,
            "scores": [
                {"chunk_id": document.chunk_id, "score": float(score)}
                for document, score in zip(payload.documents, scores, strict=True)
            ],
        }

    return app


class TransformersCrossEncoder:
    def __init__(
        self,
        model_name,
        tokenizer,
        model,
        torch_module,
        device,
        *,
        max_batch_size: int,
        max_batch_tokens: int,
        max_length: int,
        warmup_enabled: bool,
    ) -> None:
        self.model_name = model_name
        self._tokenizer = tokenizer
        self._model = model
        self._torch = torch_module
        self._device = device
        self._max_batch_size = max_batch_size
        self._max_batch_tokens = max_batch_tokens
        self._max_length = max_length
        self._warmup_enabled = warmup_enabled

    @classmethod
    def from_env(cls):
        try:
            import torch
            from huggingface_hub import snapshot_download
            from transformers import AutoModelForSequenceClassification, AutoTokenizer
        except ImportError as exc:
            raise RuntimeError(
                "本地 Reranker 缺少 torch/transformers；请先安装 reranker runtime"
            ) from exc
        config = CrossEncoderRuntimeConfig.from_env()
        model_path = snapshot_download(repo_id=config.model_name, local_files_only=True)
        device = (
            "mps"
            if torch.backends.mps.is_available()
            else ("cuda" if torch.cuda.is_available() else "cpu")
        )
        tokenizer = AutoTokenizer.from_pretrained(model_path, local_files_only=True)
        model = AutoModelForSequenceClassification.from_pretrained(
            model_path, local_files_only=True
        )
        model.to(device)
        model.eval()
        return cls(
            config.model_name,
            tokenizer,
            model,
            torch,
            device,
            max_batch_size=config.max_batch_size,
            max_batch_tokens=config.max_batch_tokens,
            max_length=config.max_length,
            warmup_enabled=config.warmup_enabled,
        )

    def score(self, query: str, passages: list[str]) -> list[float]:
        if not passages:
            return []
        encoded = self._tokenizer(
            [query] * len(passages),
            passages,
            padding=False,
            truncation=True,
            max_length=self._max_length,
        )
        lengths = [len(value) for value in encoded["input_ids"]]
        scores = [0.0] * len(passages)
        for indices in build_dynamic_batches(
            lengths,
            max_batch_size=self._max_batch_size,
            max_batch_tokens=self._max_batch_tokens,
        ):
            batch = self._tokenizer.pad(
                {key: [values[index] for index in indices] for key, values in encoded.items()},
                padding=True,
                return_tensors="pt",
            ).to(self._device)
            with self._torch.inference_mode():
                logits = self._model(**batch, return_dict=True).logits.view(-1)
            for index, score in zip(
                indices,
                logits.detach().float().cpu().tolist(),
                strict=True,
            ):
                scores[index] = score
        return scores

    def warmup(self) -> None:
        if self._warmup_enabled:
            self.score(
                "Jarvis 本地重排预热",
                ["本地 Cross-Encoder 已加载，后续请求可以直接执行推理。"],
            )


@dataclass(frozen=True, slots=True)
class CrossEncoderRuntimeConfig:
    model_name: str = "BAAI/bge-reranker-v2-m3"
    max_batch_size: int = 8
    max_batch_tokens: int = 4_096
    max_length: int = 640
    warmup_enabled: bool = True

    def __post_init__(self) -> None:
        if not self.model_name.strip() or len(self.model_name) > 200:
            raise ValueError("Reranker model_name 必须是 1..200 字符")
        if not 1 <= self.max_batch_size <= 32:
            raise ValueError("Reranker max_batch_size 必须在 1..32")
        if not 256 <= self.max_batch_tokens <= 32_768:
            raise ValueError("Reranker max_batch_tokens 必须在 256..32768")
        if not 128 <= self.max_length <= 1_024:
            raise ValueError("Reranker max_length 必须在 128..1024")

    @classmethod
    def from_env(cls) -> "CrossEncoderRuntimeConfig":
        return cls(
            model_name=os.getenv("JARVIS_RAG_RERANKER_MODEL", "BAAI/bge-reranker-v2-m3").strip(),
            max_batch_size=_env_int("JARVIS_RAG_RERANKER_BATCH_SIZE", 8),
            max_batch_tokens=_env_int("JARVIS_RAG_RERANKER_MAX_BATCH_TOKENS", 4_096),
            max_length=_env_int("JARVIS_RAG_RERANKER_MAX_LENGTH", 640),
            warmup_enabled=_env_bool("JARVIS_RAG_RERANKER_WARMUP", True),
        )


def build_dynamic_batches(
    lengths: list[int],
    *,
    max_batch_size: int,
    max_batch_tokens: int,
) -> list[list[int]]:
    """按真实序列长度组批，减少 padding，同时保持输出可恢复到原顺序。"""

    ordered = sorted(range(len(lengths)), key=lambda index: (lengths[index], index))
    batches: list[list[int]] = []
    current: list[int] = []
    current_max = 0
    for index in ordered:
        length = max(1, lengths[index])
        next_max = max(current_max, length)
        exceeds_size = len(current) >= max_batch_size
        exceeds_tokens = bool(current and next_max * (len(current) + 1) > max_batch_tokens)
        if exceeds_size or exceeds_tokens:
            batches.append(current)
            current = []
            current_max = 0
        current.append(index)
        current_max = max(current_max, length)
    if current:
        batches.append(current)
    return batches


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None:
        return default
    try:
        return int(raw)
    except ValueError:
        raise ValueError(f"{name} 必须是整数") from None


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    normalized = raw.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"{name} 必须是 true 或 false")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8121)
    args = parser.parse_args()
    if args.host not in {"127.0.0.1", "localhost", "::1"}:
        raise SystemExit("本地 Reranker 只允许监听 loopback")
    import uvicorn

    uvicorn.run(create_app(), host=args.host, port=args.port)


if __name__ == "__main__":
    main()
