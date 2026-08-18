"""独立 RAG Worker 配置，不依赖 Agent 模型配置。"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field

from jarvis_worker.agent.rag.embedding.config import RagEmbeddingConfig
from jarvis_worker.shared.config.redis import redis_db_from_env, redis_password_from_env


@dataclass(frozen=True, slots=True)
class RagWorkerConfig:
    worker_id: str = "rag-worker-01"
    redis_addr: str = "127.0.0.1:6379"
    redis_password: str = field(default="", repr=False)
    redis_db: int = 0
    heartbeat_interval_ms: int = 3_000
    poll_interval_ms: int = 1_000
    error_backoff_ms: int = 5_000
    job_lease_seconds: int = 300
    workspace_root: str = ""
    artifact_root: str = ""
    asset_root: str = ""
    structure_cache_root: str = ""
    artifact_max_file_bytes: int = 50 * 1024 * 1024
    asset_max_file_bytes: int = 16 * 1024 * 1024
    asset_max_total_bytes: int = 20 * 1024 * 1024 * 1024
    structure_provider: str = "paddleocr-vl"
    mlx_vlm_url: str = "http://127.0.0.1:8111/"
    render_dpi: int = 144
    paddle_max_pixels: int = 4_000_000
    paddle_max_new_tokens: int = 4_096
    paddleocr_site_packages: str = ""
    embedding: RagEmbeddingConfig = field(default_factory=RagEmbeddingConfig)

    @classmethod
    def from_env(cls) -> "RagWorkerConfig":
        worker_id = os.getenv("JARVIS_RAG_WORKER_ID", "rag-worker-01").strip()
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}", worker_id):
            raise ValueError("JARVIS_RAG_WORKER_ID 必须是安全的 1..64 字符标识")
        structure_provider = (
            os.getenv("JARVIS_RAG_STRUCTURE_PROVIDER", "paddleocr-vl").strip().lower()
        )
        if structure_provider not in {"paddleocr-vl", "native-only"}:
            raise ValueError("JARVIS_RAG_STRUCTURE_PROVIDER 仅支持 paddleocr-vl 或 native-only")
        asset_max_file_bytes = _parse_int(
            "JARVIS_RAG_ASSET_MAX_FILE_BYTES",
            16 * 1024 * 1024,
            minimum=1_024,
            maximum=100 * 1024 * 1024,
        )
        asset_max_total_bytes = _parse_int(
            "JARVIS_RAG_ASSET_MAX_TOTAL_BYTES",
            20 * 1024 * 1024 * 1024,
            minimum=1_024,
            maximum=500 * 1024 * 1024 * 1024,
        )
        if asset_max_total_bytes < asset_max_file_bytes:
            raise ValueError(
                "RAG Asset 容量必须满足 file <= total"
            )
        return cls(
            worker_id=worker_id,
            redis_addr=os.getenv("JARVIS_REDIS_ADDR", "127.0.0.1:6379").strip(),
            redis_password=redis_password_from_env(),
            redis_db=redis_db_from_env(),
            heartbeat_interval_ms=_parse_int(
                "JARVIS_HEARTBEAT_INTERVAL_MS", 3_000, minimum=100, maximum=60_000
            ),
            poll_interval_ms=_parse_int(
                "JARVIS_RAG_WORKER_POLL_INTERVAL_MS", 1_000, minimum=100, maximum=60_000
            ),
            error_backoff_ms=_parse_int(
                "JARVIS_RAG_WORKER_ERROR_BACKOFF_MS", 5_000, minimum=1_000, maximum=60_000
            ),
            job_lease_seconds=_parse_int(
                "JARVIS_RAG_JOB_LEASE_SECONDS", 300, minimum=5, maximum=1_800
            ),
            workspace_root=os.getenv("JARVIS_WORKSPACE_ROOT", ""),
            artifact_root=os.getenv("JARVIS_ARTIFACT_ROOT", ""),
            asset_root=os.getenv("JARVIS_RAG_ASSET_ROOT", ""),
            structure_cache_root=os.getenv("JARVIS_RAG_STRUCTURE_CACHE_ROOT", ""),
            artifact_max_file_bytes=_parse_int(
                "JARVIS_ARTIFACT_MAX_FILE_BYTES",
                50 * 1024 * 1024,
                minimum=1_024,
                maximum=100 * 1024 * 1024,
            ),
            asset_max_file_bytes=asset_max_file_bytes,
            asset_max_total_bytes=asset_max_total_bytes,
            structure_provider=structure_provider,
            mlx_vlm_url=os.getenv("JARVIS_RAG_MLX_VLM_URL", "http://127.0.0.1:8111/").strip(),
            render_dpi=_parse_int("JARVIS_RAG_RENDER_DPI", 144, minimum=72, maximum=240),
            paddle_max_pixels=_parse_int(
                "JARVIS_RAG_PADDLE_MAX_PIXELS",
                4_000_000,
                minimum=100_000,
                maximum=20_000_000,
            ),
            paddle_max_new_tokens=_parse_int(
                "JARVIS_RAG_PADDLE_MAX_NEW_TOKENS",
                4_096,
                minimum=256,
                maximum=16_384,
            ),
            paddleocr_site_packages=os.getenv(
                "JARVIS_RAG_PADDLEOCR_SITE_PACKAGES", ""
            ).strip(),
            embedding=RagEmbeddingConfig.from_env(),
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
