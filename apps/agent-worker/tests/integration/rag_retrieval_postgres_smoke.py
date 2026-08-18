#!/usr/bin/env python3
"""在回滚事务内验证混合检索的 Workspace/ready/provider 约束。"""

from __future__ import annotations

import argparse
import asyncio
from datetime import datetime, timezone
from uuid import uuid4

from jarvis_worker.agent.rag.embedding import (
    RagEmbeddingConfig,
    create_openai_embedding_provider,
)
from jarvis_worker.agent.rag.retrieval.postgres import (
    PostgresRagRetrievalRepository,
)
from jarvis_worker.database.engine import (
    create_engine,
    dispose_engine,
    get_session_factory,
)
from jarvis_worker.database.models import (
    AgentRunModel,
    ArtifactModel,
    ConversationModel,
    RagChunkEmbeddingModel,
    RagChunkModel,
    RagDocumentModel,
    RagIngestionJobModel,
    TaskModel,
    WorkspaceModel,
)
from jarvis_worker.shared.config.env_loader import load_default_local_env


async def _run(*, real_embedding: bool) -> None:
    create_engine()
    factory = get_session_factory()
    now = datetime.now(timezone.utc)
    workspace_id, other_workspace_id = uuid4(), uuid4()
    conversation_id, task_id, run_id, artifact_id = uuid4(), uuid4(), uuid4(), uuid4()
    target_document_id, indexing_document_id, foreign_document_id = (
        uuid4(),
        uuid4(),
        uuid4(),
    )
    if real_embedding:
        provider = create_openai_embedding_provider(RagEmbeddingConfig.from_env())
        try:
            query_vector = await provider.embed_query(
                "Jarvis retrieval workspace safety smoke test"
            )
        finally:
            await provider.aclose()
    else:
        query_vector = [0.0] * 1_536
        query_vector[0] = 1.0

    async with factory() as session:
        transaction = await session.begin()
        try:
            session.add_all(
                [
                    WorkspaceModel(
                        id=workspace_id,
                        name="RAG retrieval smoke",
                        root_path=f"/private/tmp/{workspace_id}",
                        canonical_path=f"/private/tmp/{workspace_id}",
                        status="active",
                        source="user_picker",
                    ),
                    WorkspaceModel(
                        id=other_workspace_id,
                        name="Other workspace",
                        root_path=f"/private/tmp/{other_workspace_id}",
                        canonical_path=f"/private/tmp/{other_workspace_id}",
                        status="active",
                        source="user_picker",
                    ),
                    ConversationModel(id=conversation_id, title="RAG smoke"),
                ]
            )
            await session.flush()
            session.add(
                TaskModel(
                    id=task_id,
                    conversation_id=conversation_id,
                    title="RAG smoke",
                    user_goal="verify retrieval filters",
                    status="completed",
                    workspace_id=workspace_id,
                )
            )
            await session.flush()
            session.add(
                AgentRunModel(
                    id=run_id,
                    task_id=task_id,
                    status="completed",
                )
            )
            await session.flush()
            session.add(
                ArtifactModel(
                    id=artifact_id,
                    task_id=task_id,
                    run_id=run_id,
                    kind="file",
                    title="smoke.pdf",
                    purpose="deliverable",
                    producer_type="runtime",
                    mime_type="application/pdf",
                    content_hash="f" * 64,
                )
            )
            await session.flush()
            documents = [
                (target_document_id, workspace_id, "ready", "a" * 64),
                (indexing_document_id, workspace_id, "indexing", "b" * 64),
                (foreign_document_id, other_workspace_id, "ready", "c" * 64),
            ]
            for ordinal, (document_id, owner, status, digest) in enumerate(documents):
                job_id, chunk_id = uuid4(), uuid4()
                session.add(
                    RagDocumentModel(
                        id=document_id,
                        workspace_id=owner,
                        source_artifact_id=artifact_id,
                        title=f"Smoke {ordinal}",
                        mime_type="application/pdf",
                        source_content_hash=digest,
                        ingestion_policy_version=f"smoke-{ordinal}",
                        status=status,
                        parser_version="smoke-parser",
                        chunker_version="smoke-chunker",
                        embedding_provider="openai" if status == "ready" else "",
                        embedding_model=("text-embedding-3-small" if status == "ready" else ""),
                        embedding_dimensions=1_536 if status == "ready" else None,
                        chunk_count=1,
                        indexed_at=now if status == "ready" else None,
                    )
                )
                await session.flush()
                session.add(
                    RagIngestionJobModel(
                        id=job_id,
                        document_id=document_id,
                        workspace_id=owner,
                        idempotency_key=f"{ordinal + 1:064x}",
                        ingestion_policy_version=f"smoke-{ordinal}",
                        status="completed" if status == "ready" else "embedding",
                    )
                )
                await session.flush()
                session.add(
                    RagChunkModel(
                        id=chunk_id,
                        document_id=document_id,
                        ingestion_job_id=job_id,
                        workspace_id=owner,
                        ordinal=0,
                        content=f"Smoke evidence {ordinal}",
                        content_hash=f"{ordinal + 10:064x}",
                        token_count=8,
                        source_locator_json={"page_start": 1},
                        embedding_key="openai:text-embedding-3-small:smoke",
                    )
                )
                await session.flush()
                session.add(
                    RagChunkEmbeddingModel(
                        chunk_id=chunk_id,
                        document_id=document_id,
                        workspace_id=owner,
                        content_hash=f"{ordinal + 10:064x}",
                        provider_name="openai",
                        model_name="text-embedding-3-small",
                        dimensions=1_536,
                        embedding=query_vector,
                    )
                )
                await session.flush()
            repository = PostgresRagRetrievalRepository(session)
            matches = await repository.search_candidates(
                workspace_id=workspace_id,
                query_vector=query_vector,
                provider_name="openai",
                model_name="text-embedding-3-small",
                document_ids=(),
                limit=10,
            )
            wrong_model = await repository.search_candidates(
                workspace_id=workspace_id,
                query_vector=query_vector,
                provider_name="openai",
                model_name="different-model",
                document_ids=(),
                limit=10,
            )
            keyword_matches = await repository.search_keyword_candidates(
                workspace_id=workspace_id,
                query_terms=("smoke evidence",),
                document_ids=(),
                limit=10,
            )
            keyword_document_scope = await repository.search_keyword_candidates(
                workspace_id=workspace_id,
                query_terms=("smoke evidence",),
                document_ids=(indexing_document_id,),
                limit=10,
            )
            assert [match.document_id for match in matches] == [target_document_id]
            assert matches[0].score == 1.0
            assert wrong_model == []
            assert [match.document_id for match in keyword_matches] == [
                target_document_id
            ]
            assert keyword_matches[0].trace.sources == ("keyword",)
            assert keyword_document_scope == []
            mode = "openai" if real_embedding else "deterministic"
            print(
                "rag_retrieval_postgres_smoke=passed "
                f"mode={mode} semantic_matches=1 keyword_matches=1"
            )
        finally:
            await transaction.rollback()
    await dispose_engine()


def main() -> None:
    load_default_local_env()
    parser = argparse.ArgumentParser()
    parser.add_argument("--real-embedding", action="store_true")
    args = parser.parse_args()
    asyncio.run(_run(real_embedding=args.real_embedding))


if __name__ == "__main__":
    main()
