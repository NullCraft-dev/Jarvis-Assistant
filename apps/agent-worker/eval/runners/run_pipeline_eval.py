#!/usr/bin/env python3
"""Run one annotated PDF through the production RAG pipeline and emit diagnostics.

The runner is intentionally fail-closed: the official multimodal path always requires
PaddleOCR-VL when routing selects a page. A missing provider, embedding key, retrieval
owner, or generation adapter is reported as a blocked stage instead of silently
falling back to a weaker pipeline.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx
from pipeline_eval import (
    EVALUATOR_VERSION,
    RecordingStructureProvider,
    align_gold_nodes,
    evaluate_chunking,
    evaluate_preprocessing,
    evaluate_retrieval,
    evaluate_routing,
    rank_chunks,
    serialize_chunk,
    serialize_node,
    serialize_structure_result,
)

from jarvis_worker.agent.rag.chunking import MultimodalChunkRouter

# Import preprocessing before chunking. The production ingestion package currently
# has an order-sensitive import cycle; this ordering matches its working bootstrap.
from jarvis_worker.agent.rag.ingestion.pdf_parser import PyMuPdfNativeParser
from jarvis_worker.agent.rag.preprocessing import MultimodalDocumentPreprocessor
from jarvis_worker.agent.rag.preprocessing.native import native_nodes
from jarvis_worker.agent.rag.preprocessing.policy import PageRoutingPolicy
from jarvis_worker.agent.rag.preprocessing.providers import (
    PaddleOcrVlConfig,
    PaddleOcrVlProvider,
)
from jarvis_worker.agent.rag.worker.config import RagWorkerConfig

EVAL_ROOT = Path(__file__).resolve().parent.parent
STAGE_ORDER = ("preprocessing", "chunking", "embedding", "retrieval", "generation")


class EvalStageError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class OpenAICompatibleAnswerGenerator:
    """Eval-only answer adapter; it is not a substitute for a production RAG owner."""

    def __init__(self) -> None:
        base_url = (
            os.getenv(
                "JARVIS_EVAL_GENERATION_BASE_URL",
                os.getenv("JARVIS_MODEL_BASE_URL", ""),
            )
            .strip()
            .rstrip("/")
        )
        model = os.getenv(
            "JARVIS_EVAL_GENERATION_MODEL", os.getenv("JARVIS_MODEL_NAME", "")
        ).strip()
        key_env = os.getenv(
            "JARVIS_EVAL_GENERATION_API_KEY_ENV",
            os.getenv("JARVIS_MODEL_API_KEY_ENV", "OPENAI_API_KEY"),
        ).strip()
        api_key = os.getenv(key_env, "").strip() if key_env else ""
        if not base_url or not model or not key_env or not api_key:
            raise EvalStageError(
                "GENERATION_NOT_CONFIGURED",
                "需配置 JARVIS_EVAL_GENERATION_*，或提供现有 JARVIS_MODEL_* 配置",
            )
        if not (base_url.startswith("https://") or base_url.startswith("http://127.0.0.1")):
            raise EvalStageError(
                "GENERATION_ENDPOINT_REJECTED",
                "生成服务必须使用 HTTPS 或 127.0.0.1 本地地址",
            )
        self.provider_name = "openai-compatible-eval-adapter"
        self.model_name = model
        self._client = httpx.AsyncClient(
            base_url=base_url,
            timeout=120,
            headers={"Authorization": f"Bearer {api_key}"},
        )

    async def generate(self, query: str, contexts: list[dict[str, Any]]) -> dict[str, Any]:
        evidence = "\n\n".join(
            f"[chunk:{item['chunk_ordinal']}]\n{item['content']}" for item in contexts
        )
        system = (
            "仅依据给定证据回答。证据不足时明确回答无法从文档确定。"
            '返回严格 JSON：{"answer": string, "citations": integer[]}，'
            "citations 只能使用证据中的 chunk 编号。"
        )
        try:
            response = await self._client.post(
                "/chat/completions",
                json={
                    "model": self.model_name,
                    "temperature": 0,
                    "messages": [
                        {"role": "system", "content": system},
                        {"role": "user", "content": f"问题：{query}\n\n证据：\n{evidence}"},
                    ],
                },
            )
        except (httpx.TimeoutException, httpx.NetworkError) as exc:
            raise EvalStageError("GENERATION_UNAVAILABLE", "生成服务暂时不可用") from exc
        if response.status_code >= 400:
            raise EvalStageError(
                "GENERATION_REQUEST_FAILED",
                f"生成请求失败（HTTP {response.status_code}）",
            )
        try:
            content = response.json()["choices"][0]["message"]["content"]
            payload = _parse_json_object(str(content))
            answer = payload["answer"]
            citations = payload["citations"]
            if not isinstance(answer, str) or not answer.strip():
                raise ValueError("answer invalid")
            if not isinstance(citations, list) or not all(
                isinstance(value, int) and value >= 0 for value in citations
            ):
                raise ValueError("citations invalid")
        except (KeyError, IndexError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise EvalStageError("GENERATION_INVALID_RESPONSE", "生成服务返回结构无效") from exc
        return {"answer": answer.strip(), "citations": citations}

    async def aclose(self) -> None:
        await self._client.aclose()


async def run(args: argparse.Namespace) -> tuple[int, dict[str, Any], Path]:
    case_path = EVAL_ROOT / "cases" / args.case_id / "case.json"
    case = _load_json(case_path)
    annotation_path = _resolve_eval_path(case["annotation_path"])
    annotation = _load_json(annotation_path)
    source_path = _resolve_eval_path(case["document"]["relative_path"])
    content = source_path.read_bytes()
    _verify_source(case, source_path, content)

    run_id = args.run_id or datetime.now(timezone.utc).strftime("pipeline-%Y%m%dT%H%M%SZ")
    output_dir = EVAL_ROOT / "reports" / args.case_id / run_id
    output_dir.mkdir(parents=True, exist_ok=False)
    report: dict[str, Any] = {
        "report_schema_version": 2,
        "evaluator_version": EVALUATOR_VERSION,
        "run_id": run_id,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "case_id": args.case_id,
        "annotation_version": annotation["annotation_version"],
        "requested_through": args.through,
        "status": "running",
        "pipeline": {
            "preprocessing": "production",
            "chunking": "production",
            "embedding": "production-provider-direct",
            "retrieval": "evaluation-in-memory-cosine",
            "generation": "evaluation-openai-compatible-adapter",
            "production_retrieval_generation_complete": False,
        },
        "input": {
            "relative_path": case["document"]["relative_path"],
            "sha256": hashlib.sha256(content).hexdigest(),
            "size_bytes": len(content),
            "page_count": case["document"]["page_count"],
        },
        "stages": {},
    }
    _write_json(output_dir / "report.json", report)

    parser = PyMuPdfNativeParser()
    policy = PageRoutingPolicy()
    started = time.perf_counter()
    parsed = parser.parse(content)
    selected_pages = policy.pages_for_structure_model(parsed)
    native = list(native_nodes(parsed))
    routing_metrics = evaluate_routing(annotation, selected_pages)
    _write_json(
        output_dir / "01-native-and-routing.json",
        {
            "parser_version": parsed.parser_version,
            "policy_version": policy.policy_version,
            "pages_requiring_ocr": list(parsed.pages_requiring_ocr),
            "selected_pages": list(selected_pages),
            "routing_metrics": routing_metrics,
            "native_nodes": [serialize_node(node) for node in native],
        },
    )

    config = RagWorkerConfig.from_env()
    recorder = RecordingStructureProvider(
        PaddleOcrVlProvider(
            PaddleOcrVlConfig(
                server_url=config.mlx_vlm_url,
                max_concurrency=1,
                max_pixels=config.paddle_max_pixels,
                max_new_tokens=config.paddle_max_new_tokens,
            )
        )
    )
    preprocessor = MultimodalDocumentPreprocessor(
        structure_provider=recorder,
        routing_policy=policy,
        render_dpi=config.render_dpi,
    )
    try:
        document = await preprocessor.preprocess_pdf(content)
    except Exception as exc:
        _block_stage(
            report,
            "preprocessing",
            code="PADDLEOCR_VL_PIPELINE_FAILED",
            message=_safe_exception_message(exc),
            elapsed=time.perf_counter() - started,
        )
        _block_downstream(report, "preprocessing", args.through)
        _finish_report(report, output_dir)
        return 2, report, output_dir

    _write_json(
        output_dir / "02-structure-model-raw.json",
        {"pages": [serialize_structure_result(result) for result in recorder.results]},
    )
    matches, alignment = align_gold_nodes(annotation, document.nodes)
    preprocessing_metrics = evaluate_preprocessing(annotation, document.nodes, matches, alignment)
    report["stages"]["preprocessing"] = {
        "status": "completed",
        "elapsed_seconds": _elapsed(started),
        "native_parser_version": document.native_parser_version,
        "policy_version": document.preprocessing_policy_version,
        "structure_provider": document.structure_provider,
        "structure_provider_version": document.structure_provider_version,
        "pages_processed": list(document.pages_processed_by_structure_model),
        "routing": routing_metrics,
        "metrics": preprocessing_metrics,
    }
    _write_json(
        output_dir / "03-preprocessed-fused.json",
        {
            "metadata": {
                "page_count": document.page_count,
                "native_parser_version": document.native_parser_version,
                "preprocessing_policy_version": document.preprocessing_policy_version,
                "structure_provider": document.structure_provider,
                "structure_provider_version": document.structure_provider_version,
                "pages_processed_by_structure_model": list(
                    document.pages_processed_by_structure_model
                ),
            },
            "gold_to_output_nodes": matches,
            "nodes": [serialize_node(node) for node in document.nodes],
        },
    )
    if args.through == "preprocessing":
        _finish_report(report, output_dir)
        return 0, report, output_dir

    started = time.perf_counter()
    chunker = MultimodalChunkRouter()
    chunks = chunker.chunk(document)
    chunking_metrics = evaluate_chunking(annotation, chunks, matches)
    report["stages"]["chunking"] = {
        "status": "completed",
        "elapsed_seconds": _elapsed(started),
        "chunker_version": chunker.version,
        "metrics": chunking_metrics,
    }
    _write_json(
        output_dir / "04-chunks.json",
        {"chunker_version": chunker.version, "chunks": [serialize_chunk(c) for c in chunks]},
    )
    if args.through == "chunking":
        _finish_report(report, output_dir)
        return 0, report, output_dir

    started = time.perf_counter()
    embedding_provider = None
    try:
        # Keep this import stage-local. The isolated PaddleOCR client runtime is
        # intentionally smaller than the Agent Worker environment and can still
        # execute preprocessing/chunking evaluations without SQLAlchemy.
        from jarvis_worker.agent.rag.embedding.openai import (
            create_openai_embedding_provider,
        )

        embedding_provider = create_openai_embedding_provider(config.embedding)
        chunk_vectors = await embedding_provider.embed_documents(
            [chunk.content for chunk in chunks]
        )
        report["stages"]["embedding"] = {
            "status": "completed",
            "elapsed_seconds": _elapsed(started),
            "provider": embedding_provider.provider_name,
            "model": embedding_provider.model_name,
            "dimensions": embedding_provider.dimensions,
            "vector_count": len(chunk_vectors),
        }
        _write_json(
            output_dir / "05-embeddings.json",
            {
                "provider": embedding_provider.provider_name,
                "model": embedding_provider.model_name,
                "dimensions": embedding_provider.dimensions,
                "vectors_omitted": True,
                "chunk_content_hashes": [chunk.content_hash for chunk in chunks],
            },
        )
        if args.through == "embedding":
            _finish_report(report, output_dir)
            return 0, report, output_dir

        started = time.perf_counter()
        query_vectors = await embedding_provider.embed_documents(
            [query["query"] for query in annotation["queries"]]
        )
        rankings = {
            query["query_id"]: rank_chunks(vector, chunk_vectors, limit=args.top_k)
            for query, vector in zip(annotation["queries"], query_vectors, strict=True)
        }
        retrieval_metrics = evaluate_retrieval(annotation, chunks, rankings, matches)
        report["stages"]["retrieval"] = {
            "status": "completed-with-eval-adapter",
            "elapsed_seconds": _elapsed(started),
            "top_k": args.top_k,
            "metrics": retrieval_metrics,
        }
        _write_json(output_dir / "06-retrieval.json", retrieval_metrics)
        if args.through == "retrieval":
            _finish_report(report, output_dir)
            return 0, report, output_dir

        started = time.perf_counter()
        generator = OpenAICompatibleAnswerGenerator()
        try:
            generation_results = []
            retrieval_by_id = {item["query_id"]: item for item in retrieval_metrics["queries"]}
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
                valid_ordinals = {item["chunk_ordinal"] for item in contexts}
                relevant = set(retrieval_by_id[query["query_id"]]["relevant_chunk_ordinals"])
                citations = set(generated["citations"])
                generation_results.append(
                    {
                        "query_id": query["query_id"],
                        "answerable": query["answerable"],
                        **generated,
                        "citations_valid": citations.issubset(valid_ordinals),
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
            report["stages"]["generation"] = {
                "status": "completed-pending-semantic-judgment",
                "elapsed_seconds": _elapsed(started),
                "provider": generator.provider_name,
                "model": generator.model_name,
                "queries": generation_results,
            }
            _write_json(output_dir / "07-generation.json", {"queries": generation_results})
        finally:
            await generator.aclose()
    except Exception as exc:
        stage = _first_missing_stage(report)
        code = getattr(exc, "code", f"{stage.upper()}_FAILED")
        _block_stage(
            report,
            stage,
            code=code,
            message=_safe_exception_message(exc),
            elapsed=time.perf_counter() - started,
        )
        _block_downstream(report, stage, args.through)
    finally:
        if embedding_provider is not None:
            await embedding_provider.aclose()

    _finish_report(report, output_dir)
    return (0 if report["status"].startswith("completed") else 2), report, output_dir


def _first_missing_stage(report: dict[str, Any]) -> str:
    return next(stage for stage in STAGE_ORDER if stage not in report["stages"])


def _block_stage(
    report: dict[str, Any], stage: str, *, code: str, message: str, elapsed: float
) -> None:
    report["stages"][stage] = {
        "status": "blocked",
        "elapsed_seconds": round(elapsed, 6),
        "error": {"code": code, "message": message},
    }


def _block_downstream(report: dict[str, Any], failed: str, through: str) -> None:
    failed_index = STAGE_ORDER.index(failed)
    through_index = STAGE_ORDER.index(through)
    for stage in STAGE_ORDER[failed_index + 1 : through_index + 1]:
        report["stages"][stage] = {
            "status": "blocked-by-upstream",
            "upstream_stage": failed,
        }


def _finish_report(report: dict[str, Any], output_dir: Path) -> None:
    requested = STAGE_ORDER[: STAGE_ORDER.index(report["requested_through"]) + 1]
    statuses = [report["stages"].get(stage, {}).get("status") for stage in requested]
    stages_completed = all(
        status
        in {
            "completed",
            "completed-with-eval-adapter",
            "completed-pending-semantic-judgment",
        }
        for status in statuses
    )
    uses_eval_adapters = report["requested_through"] in {"retrieval", "generation"}
    report["status"] = (
        ("completed-with-eval-adapters" if uses_eval_adapters else "completed")
        if stages_completed
        else "incomplete"
    )
    report["end_to_end"] = {
        "status": (
            "not-requested"
            if report["requested_through"] != "generation"
            else (
                "generated-pending-semantic-judgment"
                if report["status"].startswith("completed")
                else "blocked"
            )
        ),
        "production_chain_complete": False,
        "reason": (
            "生产 retrieval/context assembly 与 RAG generation owner 尚未实现；"
            "当前后两阶段使用显式标注的 eval adapter。"
        ),
    }
    _write_json(output_dir / "report.json", report)
    (output_dir / "report.md").write_text(_markdown_report(report), encoding="utf-8")


def _markdown_report(report: dict[str, Any]) -> str:
    lines = [
        f"# RAG Pipeline Evaluation: {report['case_id']}",
        "",
        f"- Run: `{report['run_id']}`",
        f"- Status: `{report['status']}`",
        f"- Requested through: `{report['requested_through']}`",
        "- Official preprocessing path: `PyMuPDF -> routing -> PaddleOCR-VL -> fusion -> chunking`",
        "- Production end-to-end complete: `false`",
        "",
        "## Stage status",
        "",
        "| Stage | Status | Notes |",
        "| --- | --- | --- |",
    ]
    for stage in STAGE_ORDER:
        value = report["stages"].get(stage)
        if value is None:
            continue
        error = value.get("error", {})
        note = error.get("message", "")
        lines.append(f"| {stage} | {value['status']} | {note} |")
    preprocessing = report["stages"].get("preprocessing", {})
    if preprocessing.get("status") == "completed":
        routing = preprocessing["routing"]
        metrics = preprocessing["metrics"]
        lines.extend(
            [
                "",
                "## Preprocessing and routing",
                "",
                f"- Routed pages: `{routing['selected_structure_pages']}`",
                f"- Routing precision / recall: `{routing['precision']}` / `{routing['recall']}`",
                f"- Indexable gold node recall: `{metrics['indexable_gold_node_recall']}`",
                f"- Matched type accuracy: `{metrics['matched_type_accuracy']}`",
            ]
        )
    chunking = report["stages"].get("chunking", {})
    if chunking.get("status") == "completed":
        metrics = chunking["metrics"]
        lines.extend(
            [
                "",
                "## Chunking",
                "",
                f"- Chunk count: `{metrics['chunk_count']}`",
                f"- Must-keep pass rate: `{metrics['must_keep_group_pass_rate']}`",
                f"- Token min / mean / max: `{metrics['token_min']}` / `{metrics['token_mean']}` / `{metrics['token_max']}`",
            ]
        )
    retrieval = report["stages"].get("retrieval", {})
    if retrieval.get("status") == "completed-with-eval-adapter":
        metrics = retrieval["metrics"]
        lines.extend(
            [
                "",
                "## Retrieval (eval adapter)",
                "",
                f"- Mean Recall@K: `{metrics['mean_recall_at_k']}`",
                f"- MRR: `{metrics['mrr']}`",
            ]
        )
    lines.extend(
        [
            "",
            "## Interpretation boundary",
            "",
            report["end_to_end"]["reason"],
            "因此生产 retrieval/generation 落地前，本报告不能宣称已验收部署态完整 RAG。",
            "",
        ]
    )
    return "\n".join(lines)


def _parse_json_object(value: str) -> dict[str, Any]:
    stripped = value.strip()
    if stripped.startswith("```"):
        stripped = stripped.removeprefix("```json").removeprefix("```")
        stripped = stripped.removesuffix("```").strip()
    payload = json.loads(stripped)
    if not isinstance(payload, dict):
        raise ValueError("response is not object")
    return payload


def _load_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"JSON 根节点必须为 object: {path}")
    return value


def _resolve_eval_path(relative: str) -> Path:
    path = (EVAL_ROOT / relative).resolve()
    if not path.is_relative_to(EVAL_ROOT.resolve()):
        raise ValueError(f"路径越出 eval 目录: {relative}")
    return path


def _verify_source(case: dict[str, Any], path: Path, content: bytes) -> None:
    expected = case["document"]
    if hashlib.sha256(content).hexdigest() != expected["sha256"]:
        raise ValueError(f"PDF SHA-256 不匹配: {path}")
    if len(content) != expected["size_bytes"]:
        raise ValueError(f"PDF size_bytes 不匹配: {path}")


def _write_json(path: Path, value: Any) -> None:
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=False) + "\n",
        encoding="utf-8",
    )


def _safe_exception_message(exc: Exception) -> str:
    if isinstance(exc, EvalStageError):
        return str(exc)
    name = type(exc).__name__
    if name == "PaddleOcrVlError":
        return str(exc)
    if isinstance(exc, ValueError) and "未配置" in str(exc):
        return str(exc)
    return f"{name}；详细内部异常未写入评估报告"


def _elapsed(started: float) -> float:
    return round(time.perf_counter() - started, 6)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="运行 Jarvis RAG 完整链路评估")
    parser.add_argument("--case-id", required=True)
    parser.add_argument("--run-id")
    parser.add_argument("--through", choices=STAGE_ORDER, default="generation")
    parser.add_argument("--top-k", type=int, default=5)
    args = parser.parse_args(argv)
    if not 1 <= args.top_k <= 100:
        parser.error("--top-k 必须在 1..100")
    return args


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        exit_code, report, output_dir = asyncio.run(run(args))
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(json.dumps({"status": "invalid", "error": str(exc)}, ensure_ascii=False))
        return 1
    print(
        json.dumps(
            {
                "status": report["status"],
                "run_id": report["run_id"],
                "report": str(output_dir / "report.json"),
            },
            ensure_ascii=False,
        )
    )
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
