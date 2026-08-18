#!/usr/bin/env python3
"""Continue a cached preprocessing/chunking run through embedding and answer generation."""

from __future__ import annotations

import argparse
import asyncio
import json
import time
from pathlib import Path
from typing import Any

from pipeline_eval import evaluate_retrieval, rank_chunks
from run_pipeline_eval import (
    EVAL_ROOT,
    STAGE_ORDER,
    OpenAICompatibleAnswerGenerator,
    _block_downstream,
    _block_stage,
    _finish_report,
    _safe_exception_message,
    _write_json,
)

from jarvis_worker.agent.rag.chunking.contracts import ChunkDraft, ChunkModality
from jarvis_worker.agent.rag.embedding.config import RagEmbeddingConfig
from jarvis_worker.agent.rag.embedding.openai import create_openai_embedding_provider
from jarvis_worker.shared.config.env_loader import load_default_local_env


async def continue_run(args: argparse.Namespace) -> tuple[int, dict[str, Any]]:
    run_dir = args.run_dir.resolve()
    reports_root = (EVAL_ROOT / "reports").resolve()
    if not run_dir.is_relative_to(reports_root):
        raise ValueError("run-dir 必须位于 eval/reports 下")
    report = _load(run_dir / "report.json")
    if report["stages"].get("chunking", {}).get("status") != "completed":
        raise ValueError("缓存运行必须已完成 chunking")
    case = _load(EVAL_ROOT / "cases" / report["case_id"] / "case.json")
    annotation = _load(EVAL_ROOT / case["annotation_path"])
    chunk_artifact = _load(run_dir / "04-chunks.json")
    fused = _load(run_dir / "03-preprocessed-fused.json")
    chunks = [_chunk(value) for value in chunk_artifact["chunks"]]
    gold_to_nodes = fused.get("gold_to_output_nodes")
    if not isinstance(gold_to_nodes, dict):
        raise ValueError("缓存运行缺少 gold_to_output_nodes，请先执行 rescore_pipeline_eval.py")

    report["requested_through"] = args.through
    for stage in ("embedding", "retrieval", "generation"):
        report["stages"].pop(stage, None)
    load_default_local_env()
    config = RagEmbeddingConfig.from_env()
    provider = None
    started = time.perf_counter()
    try:
        provider = create_openai_embedding_provider(config)
        chunk_vectors = await provider.embed_documents([chunk.content for chunk in chunks])
        report["stages"]["embedding"] = {
            "status": "completed",
            "elapsed_seconds": _elapsed(started),
            "provider": provider.provider_name,
            "model": provider.model_name,
            "dimensions": provider.dimensions,
            "vector_count": len(chunk_vectors),
        }
        _write_json(
            run_dir / "05-embeddings.json",
            {
                "provider": provider.provider_name,
                "model": provider.model_name,
                "dimensions": provider.dimensions,
                "vectors_omitted": True,
                "chunk_content_hashes": [chunk.content_hash for chunk in chunks],
            },
        )
        if args.through == "embedding":
            _finish_report(report, run_dir)
            return 0, report

        started = time.perf_counter()
        query_vectors = await provider.embed_documents(
            [query["query"] for query in annotation["queries"]]
        )
        rankings = {
            query["query_id"]: rank_chunks(vector, chunk_vectors, limit=args.top_k)
            for query, vector in zip(annotation["queries"], query_vectors, strict=True)
        }
        retrieval = evaluate_retrieval(annotation, chunks, rankings, gold_to_nodes)
        report["stages"]["retrieval"] = {
            "status": "completed-with-eval-adapter",
            "elapsed_seconds": _elapsed(started),
            "top_k": args.top_k,
            "metrics": retrieval,
        }
        _write_json(run_dir / "06-retrieval.json", retrieval)
        if args.through == "retrieval":
            _finish_report(report, run_dir)
            return 0, report

        started = time.perf_counter()
        generator = OpenAICompatibleAnswerGenerator()
        try:
            results = await _generate(annotation, chunks, retrieval, generator)
            report["stages"]["generation"] = {
                "status": "completed-pending-semantic-judgment",
                "elapsed_seconds": _elapsed(started),
                "provider": generator.provider_name,
                "model": generator.model_name,
                "queries": results,
            }
            _write_json(run_dir / "07-generation.json", {"queries": results})
        finally:
            await generator.aclose()
    except Exception as exc:
        stage = next(stage for stage in STAGE_ORDER if stage not in report["stages"])
        _block_stage(
            report,
            stage,
            code=getattr(exc, "code", f"{stage.upper()}_FAILED"),
            message=_safe_exception_message(exc),
            elapsed=time.perf_counter() - started,
        )
        _block_downstream(report, stage, args.through)
    finally:
        if provider is not None:
            await provider.aclose()
    _finish_report(report, run_dir)
    return (0 if report["status"].startswith("completed") else 2), report


async def _generate(
    annotation: dict[str, Any],
    chunks: list[ChunkDraft],
    retrieval: dict[str, Any],
    generator: OpenAICompatibleAnswerGenerator,
) -> list[dict[str, Any]]:
    retrieval_by_id = {item["query_id"]: item for item in retrieval["queries"]}
    results = []
    for query in annotation["queries"]:
        retrieved = retrieval_by_id[query["query_id"]]["retrieved"]
        contexts = [
            {
                "chunk_ordinal": item["chunk_ordinal"],
                "content": chunks[item["chunk_ordinal"]].content,
            }
            for item in retrieved
        ]
        generated = await generator.generate(query["query"], contexts)
        valid = {item["chunk_ordinal"] for item in contexts}
        relevant = set(retrieval_by_id[query["query_id"]]["relevant_chunk_ordinals"])
        citations = set(generated["citations"])
        results.append(
            {
                "query_id": query["query_id"],
                "answerable": query["answerable"],
                **generated,
                "citations_valid": citations.issubset(valid),
                "citation_precision": (
                    round(len(citations & relevant) / len(citations), 6)
                    if citations
                    else (1.0 if not query["answerable"] else 0.0)
                ),
                "expected_answer_facts": query["expected_answer_facts"],
                "forbidden_answer_claims": query["forbidden_answer_claims"],
                "semantic_answer_judgment": "pending",
            }
        )
    return results


def _chunk(value: dict[str, Any]) -> ChunkDraft:
    return ChunkDraft(
        ordinal=value["ordinal"],
        content=value["content"],
        token_count=value["token_count"],
        content_hash=value["content_hash"],
        page_start=value["page_start"],
        page_end=value["page_end"],
        block_start=value["block_start"],
        block_end=value["block_end"],
        heading_path=tuple(value["heading_path"]),
        overlap_tokens=value["overlap_tokens"],
        modality=ChunkModality(value["modality"]),
        node_ids=tuple(value["node_ids"]),
        element_node_ids=tuple(value["element_node_ids"]),
    )


def _load(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"JSON 根节点必须为 object: {path}")
    return value


def _elapsed(started: float) -> float:
    return round(time.perf_counter() - started, 6)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="从缓存 Chunk 继续 RAG 下游评估")
    parser.add_argument("run_dir", type=Path)
    parser.add_argument(
        "--through", choices=("embedding", "retrieval", "generation"), default="generation"
    )
    parser.add_argument("--top-k", type=int, default=5)
    args = parser.parse_args()
    if not 1 <= args.top_k <= 100:
        parser.error("--top-k 必须在 1..100")
    return args


def main() -> int:
    args = parse_args()
    try:
        exit_code, report = asyncio.run(continue_run(args))
    except (OSError, ValueError, KeyError, json.JSONDecodeError) as exc:
        print(json.dumps({"status": "invalid", "error": str(exc)}, ensure_ascii=False))
        return 1
    print(json.dumps({"status": report["status"]}, ensure_ascii=False))
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
