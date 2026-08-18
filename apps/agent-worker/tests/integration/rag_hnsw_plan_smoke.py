#!/usr/bin/env python3
"""只读验证 pgvector HNSW 索引存在且可进入 KNN 执行计划。"""

from __future__ import annotations

import asyncio
import json

from sqlalchemy import text

from jarvis_worker.database.engine import (
    create_engine,
    dispose_engine,
    get_session_factory,
)
from jarvis_worker.shared.config.env_loader import load_default_local_env


_HNSW_INDEX = "idx_rag_chunk_embeddings_cosine_hnsw"


async def _run() -> None:
    create_engine()
    factory = get_session_factory()
    try:
        async with factory() as session:
            row = (
                (
                    await session.execute(
                        text(
                            "SELECT embedding::text AS embedding, "
                            "(SELECT count(*) FROM rag_chunk_embeddings) AS row_count "
                            "FROM rag_chunk_embeddings ORDER BY chunk_id LIMIT 1"
                        )
                    )
                )
                .mappings()
                .one_or_none()
            )
            if row is None:
                raise RuntimeError("rag_chunk_embeddings 没有可用于验收的真实向量")

            index_definition = await session.scalar(
                text(
                    "SELECT indexdef FROM pg_indexes "
                    "WHERE tablename = 'rag_chunk_embeddings' AND indexname = :index_name"
                ),
                {"index_name": _HNSW_INDEX},
            )
            if not index_definition or "vector_cosine_ops" not in index_definition:
                raise RuntimeError("cosine HNSW 索引缺失或操作类错误")

            transaction = await session.begin_nested()
            try:
                await session.execute(text("SET LOCAL enable_seqscan = off"))
                plan_value = await session.scalar(
                    text(
                        "EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) "
                        "SELECT chunk_id FROM rag_chunk_embeddings "
                        "ORDER BY embedding <=> CAST(:query_vector AS vector) LIMIT 20"
                    ),
                    {"query_vector": row["embedding"]},
                )
            finally:
                await transaction.rollback()

            plan = json.loads(plan_value) if isinstance(plan_value, str) else plan_value
            indexes = _collect_indexes(plan)
            if _HNSW_INDEX not in indexes:
                raise RuntimeError(f"HNSW 未进入强制 KNN 执行计划: {sorted(indexes)}")
            print(f"rag_hnsw_plan_smoke=passed rows={row['row_count']} index={_HNSW_INDEX}")
    finally:
        await dispose_engine()


def _collect_indexes(value) -> set[str]:
    indexes: set[str] = set()
    if isinstance(value, dict):
        index_name = value.get("Index Name")
        if isinstance(index_name, str):
            indexes.add(index_name)
        for child in value.values():
            indexes.update(_collect_indexes(child))
    elif isinstance(value, list):
        for child in value:
            indexes.update(_collect_indexes(child))
    return indexes


def main() -> None:
    load_default_local_env()
    asyncio.run(_run())


if __name__ == "__main__":
    main()
