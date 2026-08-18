"""Dependency container — 组装 Worker 运行时组件。"""

from __future__ import annotations

import logging
import os as _os
import sys
from dataclasses import dataclass
from pathlib import Path

from jarvis_worker.agent.artifacts.file_store import LocalArtifactFileStore
from jarvis_worker.agent.context.context_builder import ConversationContextBuilder
from jarvis_worker.agent.context.manager import ContextManager
from jarvis_worker.agent.core.runner import AgentRunner
from jarvis_worker.agent.intents import LlmIntentExtractor, PostgresIntentContextProvider
from jarvis_worker.agent.knowledge.service import KnowledgeApplicationService
from jarvis_worker.agent.literature.source_index import ScheduledSourceIndex
from jarvis_worker.agent.mcp.adapter import create_mcp_capability_modules
from jarvis_worker.agent.mcp.client import McpClient
from jarvis_worker.agent.mcp.service import McpApplicationService
from jarvis_worker.agent.memory.candidate_service import (
    MemoryCandidateApplicationService,
    MemoryCandidateMaintenanceWorker,
)
from jarvis_worker.agent.memory.deepseek_extractor import DeepSeekMemoryExtractor
from jarvis_worker.agent.memory.extraction_service import (
    MemoryExtractionApplicationService,
    MemoryExtractionBackgroundWorker,
)
from jarvis_worker.agent.memory.service import MemoryApplicationService
from jarvis_worker.agent.models.registry import get_provider_spec
from jarvis_worker.agent.permissions.manager import PermissionManager
from jarvis_worker.agent.prompts.builder import PromptBuilder
from jarvis_worker.agent.rag.answer import RagCitationValidator
from jarvis_worker.agent.rag.embedding import (
    RagEmbeddingConfig,
    create_openai_embedding_provider,
)
from jarvis_worker.agent.rag.evaluation.service import RagEvaluationTraceService
from jarvis_worker.agent.rag.ingestion import RagIngestionCommandService
from jarvis_worker.agent.rag.query import RagIngestionMonitorService
from jarvis_worker.agent.rag.reranking import (
    LocalBgeRerankerConfig,
    LocalBgeRerankerProvider,
)
from jarvis_worker.agent.rag.retrieval import RagRetrievalService
from jarvis_worker.agent.skills.layer import SkillLayer
from jarvis_worker.agent.skills.loader import SkillLoader
from jarvis_worker.agent.skills.script_module import (
    create_skill_script_capability_modules,
)
from jarvis_worker.agent.tool_gateway.effect_boundary import FileToolEffectBarrier
from jarvis_worker.agent.tool_gateway.gateway import ToolGateway
from jarvis_worker.agent.tools.knowledge.create_document import KnowledgeDocumentToolExecutor
from jarvis_worker.agent.tools.literature.download_arxiv_pdf import (
    ArxivPdfDownloadExecutor,
)
from jarvis_worker.agent.tools.literature.search_arxiv import ArxivSearchExecutor
from jarvis_worker.agent.tools.rag.await_ingestion import RagAwaitIngestionToolExecutor
from jarvis_worker.agent.tools.rag.ingest_artifact import RagIngestArtifactToolExecutor
from jarvis_worker.agent.tools.rag.search import RagSearchToolExecutor
from jarvis_worker.bootstrap.model_factory import create_model_provider
from jarvis_worker.bootstrap.tool_registry import create_tool_registry
from jarvis_worker.database.engine import create_engine, get_session_factory
from jarvis_worker.runtime.async_bridge import AsyncServiceBridge
from jarvis_worker.runtime.conversations.service import ConversationApplicationService
from jarvis_worker.runtime.permissions.service import PermissionApplicationService
from jarvis_worker.runtime.run_executor import AgentRunExecutor
from jarvis_worker.runtime.runs.service import RunApplicationService
from jarvis_worker.runtime.service import RuntimeApplicationService
from jarvis_worker.runtime.worker import AgentWorker
from jarvis_worker.runtime_bus import (
    RedisClientProtocol,
    create_redis_client,
    ensure_consumer_group,
)
from jarvis_worker.runtime_bus.command_consumer import WorkerCommandConsumer
from jarvis_worker.runtime_bus.consumer import RunQueueConsumer
from jarvis_worker.runtime_bus.heartbeat import HeartbeatProducer
from jarvis_worker.runtime_bus.messages import STREAM_RUN_QUEUE, STREAM_WORKER_COMMAND
from jarvis_worker.runtime_bus.producer import RuntimeEventProducer
from jarvis_worker.shared.config.database import DatabaseConfig
from jarvis_worker.shared.config.settings import WorkerConfig

log = logging.getLogger("jarvis_worker.bootstrap")


@dataclass
class WorkerRuntimeComponents:
    client: RedisClientProtocol
    worker: AgentWorker


def create_worker_runtime(cfg: WorkerConfig) -> WorkerRuntimeComponents:
    # 1. PostgreSQL engine
    db_cfg = DatabaseConfig.from_env()
    create_engine(db_cfg)

    # 2. Redis
    try:
        client = create_redis_client(
            cfg.redis_addr,
            password=cfg.redis_password,
            db=cfg.redis_db,
        )
        client.ping()
        log.info("Redis 连接成功: %s", cfg.redis_addr)
    except Exception as e:
        log.fatal("Redis 连接失败: %s — %s", cfg.redis_addr, e)
        sys.exit(1)

    _ensure_groups(client, cfg)

    # 3. Redis 通信组件
    consumer = RunQueueConsumer(
        client,
        cfg.worker_consumer,
        group=cfg.worker_group,
        reclaim_idle_ms=cfg.run_queue_reclaim_idle_ms,
        max_deliveries=cfg.run_queue_max_deliveries,
    )
    producer = RuntimeEventProducer(client)

    # 4. ToolGateway（同步 Agent loop 与 async Application Service 共用固定 bridge）
    service_bridge = AsyncServiceBridge()
    knowledge_service = KnowledgeApplicationService(get_session_factory)
    knowledge_executor = KnowledgeDocumentToolExecutor(knowledge_service, service_bridge)
    artifact_root = (
        Path(cfg.artifact_root)
        if cfg.artifact_root
        else Path(cfg.workspace_root or ".") / ".local" / "artifacts"
    )
    artifact_store = LocalArtifactFileStore(
        artifact_root,
        max_bytes=cfg.artifact_max_file_bytes,
        max_run_bytes=cfg.artifact_max_run_bytes,
        max_workspace_bytes=cfg.artifact_max_workspace_bytes,
        max_total_bytes=cfg.artifact_max_total_bytes,
    )
    literature_executor = ArxivPdfDownloadExecutor(artifact_store)
    literature_search_executor = ArxivSearchExecutor(
        ScheduledSourceIndex(get_session_factory),
        service_bridge,
    )
    rag_embedding_provider = create_openai_embedding_provider(RagEmbeddingConfig.from_env())
    rag_reranker_config = LocalBgeRerankerConfig.from_env()
    rag_reranker_provider = (
        LocalBgeRerankerProvider(rag_reranker_config) if rag_reranker_config.enabled else None
    )
    rag_search_executor = RagSearchToolExecutor(
        RagRetrievalService(
            get_session_factory,
            embedding_provider=rag_embedding_provider,
            reranker_provider=rag_reranker_provider,
        ),
        service_bridge,
        trace_service=RagEvaluationTraceService(get_session_factory),
    )
    rag_ingestion_executor = RagIngestArtifactToolExecutor(
        RagIngestionCommandService(
            get_session_factory,
            artifact_file_store=artifact_store,
        ),
        service_bridge,
    )
    rag_await_ingestion_executor = RagAwaitIngestionToolExecutor(
        RagIngestionMonitorService(get_session_factory),
        service_bridge,
    )
    mcp_client = McpClient()
    mcp_service = McpApplicationService(get_session_factory)
    mcp_discoveries = service_bridge.run(mcp_service.refresh_enabled(mcp_client), timeout=60)
    mcp_modules = create_mcp_capability_modules(mcp_discoveries, mcp_client, service_bridge)
    skills_root = (
        Path(cfg.skills_root) if cfg.skills_root else Path(__file__).resolve().parents[5] / "skills"
    )
    skill_adapters_root = (
        Path(cfg.skill_adapters_root) if cfg.skill_adapters_root else skills_root / ".jarvis"
    )
    skill_loader = SkillLoader(skills_root, skill_adapters_root)
    skill_definitions = skill_loader.load_all()
    skill_script_modules = create_skill_script_capability_modules(skill_definitions)
    tool_registry = create_tool_registry(
        knowledge_executor=knowledge_executor,
        literature_executor=literature_executor,
        literature_search_executor=literature_search_executor,
        rag_search_executor=rag_search_executor,
        rag_ingestion_executor=rag_ingestion_executor,
        rag_await_ingestion_executor=rag_await_ingestion_executor,
        additional_modules=(*mcp_modules, *skill_script_modules),
    )
    perm_mgr = PermissionManager()
    effect_boundary = (
        FileToolEffectBarrier(
            Path(cfg.test_tool_effect_barrier_root),
            timeout_seconds=cfg.test_tool_effect_barrier_timeout_seconds,
        )
        if cfg.test_fault_injection_enabled
        else None
    )
    tool_gateway = ToolGateway(
        tool_registry,
        perm_mgr,
        effect_boundary=effect_boundary,
    )

    # 5. AgentRunner
    skill_layer = SkillLayer(
        skill_loader,
        tool_registry,
        definitions=skill_definitions,
    )
    prompt_builder = PromptBuilder.from_registry(tool_registry)
    model_provider = create_model_provider(cfg, prompt_builder=prompt_builder)
    agent_runner = AgentRunner(
        model_provider=model_provider,
        tool_gateway=tool_gateway,
        context_manager=ContextManager(prompt_builder),
        intent_extractor=LlmIntentExtractor(model_provider),
        intent_context_provider=PostgresIntentContextProvider(
            get_session_factory,
            service_bridge,
        ),
        skill_layer=skill_layer,
        final_answer_validators=(RagCitationValidator(),),
        worker_id=cfg.worker_id,
        max_iterations=cfg.agent_max_iterations,
        max_run_seconds=cfg.agent_max_run_seconds,
    )

    # 6. RunExecutor
    runner = AgentRunExecutor(
        agent_runner=agent_runner,
        worker_id=cfg.worker_id,
        default_workspace_root=cfg.workspace_root,
    )

    # 7. Heartbeat
    _api_key_ok = bool(cfg.model_api_key_env and _os.environ.get(cfg.model_api_key_env, "").strip())
    _model_status = {
        "provider": cfg.model_provider,
        "protocol": get_provider_spec(cfg.model_provider).protocol,
        "model_name": cfg.model_name,
        "api_key_configured": _api_key_ok,
        "thinking_mode": cfg.model_thinking_mode,
        "context_window_tokens": cfg.model_context_window_tokens,
        "status": "configured"
        if (_api_key_ok and cfg.model_name and cfg.model_base_url)
        else "not_configured",
        "last_error_code": None,
    }
    heartbeat = HeartbeatProducer(
        client,
        worker_id=cfg.worker_id,
        interval_ms=cfg.heartbeat_interval_ms,
        model_status=_model_status,
    )

    # 8. CommandConsumer
    cmd_consumer = WorkerCommandConsumer(
        client,
        consumer_name=cfg.worker_consumer,
        group=cfg.worker_group,
        reclaim_idle_ms=cfg.command_reclaim_idle_ms,
    )

    # 9. Application Services（生产 Worker 必须具备 PostgreSQL 持久化）
    run_svc = RunApplicationService(get_session_factory)
    memory_extraction_supported = cfg.memory_extraction_enabled and cfg.model_provider == "deepseek"
    runtime_svc = RuntimeApplicationService(
        get_session_factory,
        artifact_file_store=artifact_store,
        artifact_inline_max_bytes=cfg.artifact_inline_max_bytes,
        memory_extraction_enabled=memory_extraction_supported,
    )
    perm_svc = PermissionApplicationService(get_session_factory)

    # 9b. ConversationContextBuilder（多轮对话 MVP）
    # 通过 ConversationApplicationService 注入，Context Builder 不直接依赖 PostgreSQL
    conv_svc = ConversationApplicationService(get_session_factory)
    context_builder = ConversationContextBuilder(conv_svc)
    memory_svc = MemoryApplicationService(get_session_factory)
    memory_candidate_maintenance_worker = MemoryCandidateMaintenanceWorker(
        MemoryCandidateApplicationService(get_session_factory),
        poll_interval=cfg.memory_candidate_expiry_poll_interval_ms / 1000,
    )
    memory_extraction_worker = None
    if memory_extraction_supported:
        memory_extractor = DeepSeekMemoryExtractor(
            base_url=cfg.model_base_url,
            model=cfg.model_name,
            api_key_env=cfg.model_api_key_env,
            timeout=cfg.model_timeout_seconds,
            thinking_mode=cfg.model_thinking_mode,
        )
        memory_extraction_worker = MemoryExtractionBackgroundWorker(
            MemoryExtractionApplicationService(
                get_session_factory,
                memory_extractor,
                max_attempts=cfg.memory_extraction_max_attempts,
                stale_after_seconds=cfg.memory_extraction_stale_seconds,
            ),
            poll_interval=cfg.memory_extraction_poll_interval_ms / 1000,
        )
    elif cfg.memory_extraction_enabled:
        log.warning(
            "MemoryExtractor 当前仅支持 deepseek；provider=%s 时不启动提取执行器",
            cfg.model_provider,
        )

    # 10. AgentWorker
    worker = AgentWorker(
        client,
        consumer,
        producer,
        runner,
        heartbeat=heartbeat,
        cmd_consumer=cmd_consumer,
        run_service=run_svc,
        event_service=runtime_svc,
        perm_service=perm_svc,
        context_builder=context_builder,
        memory_service=memory_svc,
        memory_extraction_worker=memory_extraction_worker,
        memory_candidate_maintenance_worker=memory_candidate_maintenance_worker,
        mcp_service=mcp_service,
        mcp_client=mcp_client,
        service_bridge=service_bridge,
        async_closeables=tuple(
            value for value in (rag_embedding_provider, rag_reranker_provider) if value is not None
        ),
        run_queue_reclaim_interval_ms=cfg.run_queue_reclaim_interval_ms,
        command_reclaim_interval_ms=cfg.command_reclaim_interval_ms,
    )
    heartbeat.set_runtime_bus_metrics_provider(lambda: worker.run_queue_metrics)

    if not worker.verify_persistence():
        raise RuntimeError("PostgreSQL 不可用，Worker 拒绝启动")
    log.info("PostgreSQL 持久化链路就绪")

    return WorkerRuntimeComponents(client=client, worker=worker)


def _ensure_groups(client: RedisClientProtocol, cfg: WorkerConfig) -> None:
    for stream_key in (STREAM_RUN_QUEUE, STREAM_WORKER_COMMAND):
        try:
            ensure_consumer_group(client, stream_key, cfg.worker_group, start_id="0")
            log.info("Consumer group 就绪: stream=%s group=%s", stream_key, cfg.worker_group)
        except Exception as e:
            log.fatal("创建 consumer group 失败: stream=%s error=%s", stream_key, e)
            sys.exit(1)
