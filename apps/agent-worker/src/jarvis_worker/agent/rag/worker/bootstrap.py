"""独立 RAG Worker 依赖装配。"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path

from jarvis_worker.agent.artifacts.file_store import LocalArtifactFileStore
from jarvis_worker.agent.rag.embedding.openai import create_openai_embedding_provider
from jarvis_worker.agent.rag.embedding.service import RagEmbeddingService
from jarvis_worker.agent.rag.ingestion.asset_store import LocalRagAssetFileStore
from jarvis_worker.agent.rag.ingestion.service import RagIngestionService
from jarvis_worker.agent.rag.preprocessing import (
    LocalStructureResultCache,
    MultimodalDocumentPreprocessor,
)
from jarvis_worker.agent.rag.preprocessing.providers import (
    PaddleOcrVlConfig,
    PaddleOcrVlProvider,
)
from jarvis_worker.agent.rag.worker.config import RagWorkerConfig
from jarvis_worker.agent.rag.worker.runtime import RagWorker
from jarvis_worker.database.engine import (
    check_connection,
    create_engine,
    dispose_engine,
    get_session_factory,
)
from jarvis_worker.runtime_bus import RedisClientProtocol, create_redis_client
from jarvis_worker.runtime_bus.heartbeat import HeartbeatProducer
from jarvis_worker.shared.config.database import DatabaseConfig

log = logging.getLogger("jarvis_worker.rag_worker.bootstrap")


@dataclass(slots=True)
class RagWorkerRuntime:
    worker: RagWorker
    embedding_provider: object
    redis_client: RedisClientProtocol
    heartbeat: HeartbeatProducer

    def start(self) -> None:
        self.heartbeat.set_status("starting")
        self.heartbeat.publish_now()
        self.heartbeat.start()

    async def close(self) -> None:
        self.heartbeat.set_status("draining")
        self.heartbeat.publish_now()
        self.heartbeat.stop()
        self.heartbeat.set_status("stopped")
        self.heartbeat.publish_now()
        self.redis_client.close()
        close = getattr(self.embedding_provider, "aclose", None)
        if close is not None:
            await close()
        await dispose_engine()


async def create_rag_worker_runtime(config: RagWorkerConfig) -> RagWorkerRuntime:
    create_engine(DatabaseConfig.from_env())
    embedding_provider = None
    redis_client = None
    try:
        if not await check_connection():
            raise RuntimeError("PostgreSQL 不可用，RAG Worker 拒绝启动")

        workspace_root = Path(config.workspace_root or ".").expanduser().resolve()
        artifact_root = (
            Path(config.artifact_root).expanduser().resolve()
            if config.artifact_root
            else workspace_root / ".local" / "artifacts"
        )
        asset_root = (
            Path(config.asset_root).expanduser().resolve()
            if config.asset_root
            else workspace_root / ".local" / "rag-assets"
        )
        structure_cache_root = (
            Path(config.structure_cache_root).expanduser().resolve()
            if config.structure_cache_root
            else workspace_root / ".local" / "rag-cache" / "structure"
        )
        structure_provider = None
        if config.structure_provider == "paddleocr-vl":
            structure_provider = PaddleOcrVlProvider(
                PaddleOcrVlConfig(
                    server_url=config.mlx_vlm_url,
                    max_concurrency=1,
                    max_pixels=config.paddle_max_pixels,
                    max_new_tokens=config.paddle_max_new_tokens,
                    client_site_packages=config.paddleocr_site_packages,
                )
            )
        else:
            log.warning("RAG Worker 使用 native-only；扫描页与复杂视觉元素不会被增强解析")

        embedding_provider = create_openai_embedding_provider(config.embedding)
        redis_client = create_redis_client(
            config.redis_addr,
            password=config.redis_password,
            db=config.redis_db,
        )
        redis_client.ping()
        heartbeat = HeartbeatProducer(
            redis_client,
            worker_id=config.worker_id,
            interval_ms=config.heartbeat_interval_ms,
            worker_kind="rag",
        )
        session_factory = get_session_factory
        ingestion_service = RagIngestionService(
            session_factory,
            artifact_file_store=LocalArtifactFileStore(
                artifact_root, max_bytes=config.artifact_max_file_bytes
            ),
            asset_file_store=LocalRagAssetFileStore(
                asset_root,
                max_bytes=config.asset_max_file_bytes,
                max_total_bytes=config.asset_max_total_bytes,
            ),
            preprocessor=MultimodalDocumentPreprocessor(
                structure_provider=structure_provider,
                structure_cache=(
                    LocalStructureResultCache(structure_cache_root)
                    if structure_provider is not None
                    else None
                ),
                render_dpi=config.render_dpi,
            ),
            lease_duration=timedelta(seconds=config.job_lease_seconds),
        )
        embedding_service = RagEmbeddingService(
            session_factory,
            provider=embedding_provider,
            lease_duration=timedelta(seconds=config.job_lease_seconds),
        )
        return RagWorkerRuntime(
            worker=RagWorker(
                worker_id=config.worker_id,
                ingestion_service=ingestion_service,
                embedding_service=embedding_service,
                poll_interval=config.poll_interval_ms / 1_000,
                error_backoff=config.error_backoff_ms / 1_000,
                status_callback=heartbeat.set_status,
            ),
            embedding_provider=embedding_provider,
            redis_client=redis_client,
            heartbeat=heartbeat,
        )
    except BaseException:
        if embedding_provider is not None:
            await embedding_provider.aclose()
        if redis_client is not None:
            redis_client.close()
        await dispose_engine()
        raise
