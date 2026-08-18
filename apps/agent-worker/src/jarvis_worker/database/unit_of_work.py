"""PostgresUnitOfWork — 同一 AsyncSession 下的 Repository 集合。

Application Service 通过 UnitOfWork.transaction() 管理事务边界。
Repository 不允许自行 commit，所有写操作在同一个事务中提交或回滚。
"""

from types import TracebackType
from typing import Optional, Type

from sqlalchemy.ext.asyncio import AsyncSession

from jarvis_worker.agent.artifacts.postgres_repository import PostgresArtifactRepository
from jarvis_worker.agent.knowledge.postgres_repository import (
    PostgresKnowledgeDocumentRepository,
    PostgresKnowledgeVaultRepository,
)
from jarvis_worker.agent.mcp.postgres_repository import (
    PostgresMcpServerRepository,
    PostgresMcpToolRepository,
)
from jarvis_worker.agent.memory.candidate_postgres_repository import (
    PostgresMemoryCandidateRepository,
    PostgresMemoryExtractionJobRepository,
)
from jarvis_worker.agent.memory.candidate_repository import (
    MemoryCandidateRepository,
    MemoryExtractionJobRepository,
)
from jarvis_worker.agent.memory.postgres_repository import PostgresMemoryRepository
from jarvis_worker.agent.memory.repository import MemoryRepository
from jarvis_worker.agent.rag.evaluation.postgres import (
    PostgresRagEvaluationFeedbackRepository,
    PostgresRagEvaluationLabelRepository,
    PostgresRagEvaluationTraceRepository,
    PostgresRagQualityGateRunRepository,
    PostgresRagQualityIssueRepository,
)
from jarvis_worker.agent.rag.evaluation.repository import (
    RagEvaluationFeedbackRepository,
    RagEvaluationLabelRepository,
    RagEvaluationTraceRepository,
    RagQualityGateRunRepository,
    RagQualityIssueRepository,
)
from jarvis_worker.agent.rag.indexing.postgres import PostgresPgVectorIndex
from jarvis_worker.agent.rag.postgres_repository import (
    PostgresRagAssetRepository,
    PostgresRagChunkElementLinkRepository,
    PostgresRagChunkRepository,
    PostgresRagDocumentRepository,
    PostgresRagElementRepository,
    PostgresRagIngestionJobRepository,
)
from jarvis_worker.agent.rag.repository import (
    RagAssetRepository,
    RagChunkElementLinkRepository,
    RagChunkRepository,
    RagDocumentRepository,
    RagElementRepository,
    RagIngestionJobRepository,
)
from jarvis_worker.agent.rag.retrieval.postgres import PostgresRagRetrievalRepository
from jarvis_worker.agent.rag.retrieval.repository import RagRetrievalRepository
from jarvis_worker.database.outbox.inbox_repository import PostgresInboxRepository
from jarvis_worker.database.outbox.repository import PostgresOutboxRepository
from jarvis_worker.database.repositories.interfaces import (
    ArtifactRepository,
    AuditRepository,
    ConversationRepository,
    EventRepository,
    InboxRepository,
    MessageRepository,
    OutboxRepository,
    PermissionRepository,
    RunRepository,
    StepRepository,
    TaskRepository,
    ToolCallRepository,
    WorkspaceRepository,
)
from jarvis_worker.runtime.audit.postgres_repository import PostgresAuditRepository
from jarvis_worker.runtime.conversations.postgres_repository import (
    PostgresConversationRepository,
    PostgresMessageRepository,
)
from jarvis_worker.runtime.event_repository import PostgresEventRepository
from jarvis_worker.runtime.permissions.postgres_repository import PostgresPermissionRepository
from jarvis_worker.runtime.runs.postgres_repository import PostgresRunRepository
from jarvis_worker.runtime.runs.step_repository import PostgresStepRepository
from jarvis_worker.runtime.schedules.postgres_repository import (
    PostgresScheduledExecutionRepository,
    PostgresScheduledTaskRepository,
)
from jarvis_worker.runtime.tasks.postgres_repository import PostgresTaskRepository
from jarvis_worker.runtime.tool_calls.postgres_repository import PostgresToolCallRepository
from jarvis_worker.runtime.workspaces.postgres_repository import PostgresWorkspaceRepository


class PostgresUnitOfWork:
    """PostgreSQL UnitOfWork — 所有 Repository 共享同一个 AsyncSession。

    使用 async with uow.transaction() as tx 管理事务：
        async with uow.transaction() as tx:
            task = await tx.tasks.create(task)
            run = await tx.runs.create(run)
            await tx.outbox.create([event])
            await tx.commit()  # 在同一事务中提交
    """

    def __init__(self, session: AsyncSession):
        self._session = session
        self._committed = False

        # 创建所有 Repository（共享同一 session）
        self.tasks: TaskRepository = PostgresTaskRepository(session)
        self.runs: RunRepository = PostgresRunRepository(session)
        self.steps: StepRepository = PostgresStepRepository(session)
        self.events: EventRepository = PostgresEventRepository(session)
        self.messages: MessageRepository = PostgresMessageRepository(session)
        self.memories: MemoryRepository = PostgresMemoryRepository(session)
        self.memory_candidates: MemoryCandidateRepository = PostgresMemoryCandidateRepository(session)
        self.memory_extraction_jobs: MemoryExtractionJobRepository = PostgresMemoryExtractionJobRepository(session)
        self.conversations: ConversationRepository = PostgresConversationRepository(session)
        self.tool_calls: ToolCallRepository = PostgresToolCallRepository(session)
        self.permissions: PermissionRepository = PostgresPermissionRepository(session)
        self.audits: AuditRepository = PostgresAuditRepository(session)
        self.artifacts: ArtifactRepository = PostgresArtifactRepository(session)
        self.outbox: OutboxRepository = PostgresOutboxRepository(session)
        self.inbox: InboxRepository = PostgresInboxRepository(session)
        self.workspaces: WorkspaceRepository = PostgresWorkspaceRepository(session)
        self.knowledge_vaults = PostgresKnowledgeVaultRepository(session)
        self.knowledge_documents = PostgresKnowledgeDocumentRepository(session)
        self.rag_documents: RagDocumentRepository = PostgresRagDocumentRepository(session)
        self.rag_ingestion_jobs: RagIngestionJobRepository = (
            PostgresRagIngestionJobRepository(session)
        )
        self.rag_chunks: RagChunkRepository = PostgresRagChunkRepository(session)
        self.rag_elements: RagElementRepository = PostgresRagElementRepository(session)
        self.rag_assets: RagAssetRepository = PostgresRagAssetRepository(session)
        self.rag_chunk_element_links: RagChunkElementLinkRepository = (
            PostgresRagChunkElementLinkRepository(session)
        )
        self.rag_vector_index = PostgresPgVectorIndex(session)
        self.rag_evaluation_traces: RagEvaluationTraceRepository = (
            PostgresRagEvaluationTraceRepository(session)
        )
        self.rag_evaluation_labels: RagEvaluationLabelRepository = (
            PostgresRagEvaluationLabelRepository(session)
        )
        self.rag_evaluation_feedback: RagEvaluationFeedbackRepository = (
            PostgresRagEvaluationFeedbackRepository(session)
        )
        self.rag_quality_gate_runs: RagQualityGateRunRepository = (
            PostgresRagQualityGateRunRepository(session)
        )
        self.rag_quality_issues: RagQualityIssueRepository = PostgresRagQualityIssueRepository(session)
        self.rag_retrieval: RagRetrievalRepository = PostgresRagRetrievalRepository(
            session
        )
        self.scheduled_tasks = PostgresScheduledTaskRepository(session)
        self.scheduled_executions = PostgresScheduledExecutionRepository(session)
        self.mcp_servers = PostgresMcpServerRepository(session)
        self.mcp_tools = PostgresMcpToolRepository(session)

    async def __aenter__(self) -> "PostgresUnitOfWork":
        return self

    async def __aexit__(
        self,
        exc_type: Optional[Type[BaseException]],
        exc_val: Optional[BaseException],
        exc_tb: Optional[TracebackType],
    ) -> None:
        if exc_type is not None:
            await self.rollback()
        elif not self._committed:
            await self.rollback()

    async def commit(self) -> None:
        """提交事务。"""
        await self._session.commit()
        self._committed = True

    async def flush(self) -> None:
        """把当前 UnitOfWork 的待写对象按显式事务阶段发送到数据库。"""
        await self._session.flush()

    async def rollback(self) -> None:
        """回滚事务。"""
        await self._session.rollback()

    def transaction(self) -> "PostgresUnitOfWork":
        """返回 self，支持 async with uow.transaction() as tx 语义。"""
        return self
