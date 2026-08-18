#!/usr/bin/env python3
"""开发者内部 RAG 生产轨迹审核 CLI。"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from uuid import UUID

EVAL_ROOT = Path(__file__).resolve().parents[1]
if str(EVAL_ROOT) not in sys.path:
    sys.path.insert(0, str(EVAL_ROOT))

from run_production_trace_eval import _write_report, build_report  # noqa: E402
from jarvis_worker.agent.rag.evaluation.review_service import (  # noqa: E402
    RagEvaluationReview,
    RagEvaluationReviewService,
)
from jarvis_worker.database.engine import (  # noqa: E402
    create_engine,
    dispose_engine,
    get_session_factory,
)
from jarvis_worker.shared.config.database import DatabaseConfig  # noqa: E402
from jarvis_worker.shared.config.env_loader import load_default_local_env  # noqa: E402


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="审核真实生产 RAG 轨迹")
    subcommands = parser.add_subparsers(dest="command", required=True)

    list_parser = subcommands.add_parser("list", help="列出待审核或指定隐私状态的轨迹")
    list_parser.add_argument(
        "--privacy", choices=("pending", "approved", "rejected", "all"), default="pending"
    )
    list_parser.add_argument("--limit", type=int, default=50)

    inspect_parser = subcommands.add_parser("inspect", help="查看 query、排序和有界 Chunk 预览")
    inspect_parser.add_argument("trace_id", type=UUID)
    inspect_parser.add_argument("--preview-chars", type=int, default=600)
    inspect_parser.add_argument("--redact-query", action="store_true")

    documents_parser = subcommands.add_parser(
        "documents", help="列出该轨迹 Workspace 中可供核对的 RAG 文档"
    )
    documents_parser.add_argument("trace_id", type=UUID)
    documents_parser.add_argument("--limit", type=int, default=100)

    chunks_parser = subcommands.add_parser(
        "chunks", help="浏览某文档的 Chunk，以寻找未被召回的正确证据"
    )
    chunks_parser.add_argument("trace_id", type=UUID)
    chunks_parser.add_argument("document_id", type=UUID)
    chunks_parser.add_argument("--limit", type=int, default=200)
    chunks_parser.add_argument("--preview-chars", type=int, default=600)

    for name in ("approve", "reject"):
        review_parser = subcommands.add_parser(name, help=f"{name} 隐私复核")
        review_parser.add_argument("trace_id", type=UUID)

    label_parser = subcommands.add_parser("label", help="设置人工正例和难负例标签")
    label_parser.add_argument("trace_id", type=UUID)
    label_parser.add_argument("--positive", action="append", required=True)
    label_parser.add_argument("--hard-negative", action="append", default=[])
    label_parser.add_argument("--notes", default="")
    label_parser.add_argument("--status", choices=("draft", "confirmed"), default="confirmed")

    promote_parser = subcommands.add_parser("promote", help="晋升并导出本地回归候选")
    promote_parser.add_argument("trace_id", type=UUID)
    promote_parser.add_argument("--output", type=Path)

    evaluate_parser = subcommands.add_parser("evaluate", help="生成生产轨迹评估报告")
    evaluate_parser.add_argument("--limit", type=int, default=100)
    evaluate_parser.add_argument("--output-dir", type=Path)
    return parser


async def _run(args) -> dict:
    if args.command == "evaluate":
        report = await build_report(limit=_limit(args.limit, maximum=500))
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        output_dir = args.output_dir or (
            EVAL_ROOT / "reports" / "production-traces" / timestamp
        )
        _write_report(report, output_dir)
        return {
            "status": "completed",
            "sample_count": report["sample_count"],
            "output_dir": str(output_dir),
        }

    create_engine(DatabaseConfig.from_env())
    try:
        service = RagEvaluationReviewService(get_session_factory)
        if args.command == "list":
            privacy = None if args.privacy == "all" else args.privacy
            values = await service.list_traces(
                privacy_status=privacy,
                limit=_limit(args.limit, maximum=500),
            )
            return {
                "count": len(values),
                "traces": [_trace_summary(trace, label) for trace, label in values],
            }
        if args.command == "inspect":
            preview_chars = _limit(args.preview_chars, minimum=80, maximum=4000)
            review = await service.inspect(args.trace_id)
            return _review_data(
                review,
                preview_chars=preview_chars,
                redact_query=args.redact_query,
            )
        if args.command == "documents":
            documents = await service.list_documents(
                args.trace_id,
                limit=_limit(args.limit, maximum=100),
            )
            return {
                "trace_id": str(args.trace_id),
                "count": len(documents),
                "documents": [
                    {
                        "document_id": str(document.id),
                        "title": document.title,
                        "status": document.status.value,
                        "chunk_count": document.chunk_count,
                        "source_content_hash": document.source_content_hash,
                    }
                    for document in documents
                ],
            }
        if args.command == "chunks":
            chunks = await service.list_document_chunks(
                args.trace_id,
                args.document_id,
                limit=_limit(args.limit, maximum=1000),
            )
            preview_chars = _limit(args.preview_chars, minimum=80, maximum=4000)
            return {
                "trace_id": str(args.trace_id),
                "document_id": str(args.document_id),
                "count": len(chunks),
                "chunks": [_chunk_data(chunk, preview_chars) for chunk in chunks],
            }
        if args.command in {"approve", "reject"}:
            await service.review_privacy(
                args.trace_id,
                approved=args.command == "approve",
            )
            return {
                "trace_id": str(args.trace_id),
                "privacy_status": "approved" if args.command == "approve" else "rejected",
            }
        if args.command == "label":
            label = await service.set_label(
                trace_id=args.trace_id,
                positive_chunk_ids=_uuid_values(args.positive, "positive"),
                hard_negative_chunk_ids=_uuid_values(
                    args.hard_negative, "hard-negative"
                ),
                notes=_notes(args.notes),
                status=args.status,
            )
            return _label_data(label)
        if args.command == "promote":
            output = args.output or (
                EVAL_ROOT
                / "reports"
                / "promotion-candidates"
                / f"{args.trace_id}.json"
            )
            if output.exists():
                raise ValueError(f"晋升候选文件已存在: {output}")
            review = await service.promote(args.trace_id)
            _write_promotion_candidate(review, output)
            return {
                "trace_id": str(args.trace_id),
                "label_status": "promoted",
                "output": str(output),
            }
        raise ValueError(f"未知命令: {args.command}")
    finally:
        await dispose_engine()


def _trace_summary(trace, label) -> dict:
    return {
        "trace_id": str(trace.id),
        "task_id": str(trace.task_id),
        "run_id": str(trace.run_id),
        "created_at": trace.created_at.isoformat(),
        "workspace_id": str(trace.workspace_id),
        "query_hash": trace.query_hash,
        "privacy_status": trace.privacy_status,
        "label_status": label.status if label else None,
        "candidate_count": len(trace.candidate_ranking),
        "reranked_count": len(trace.reranked_ranking),
        "context_chunk_count": len(trace.context_chunk_ids),
        "context_truncated": trace.context_truncated,
        "pipeline_versions": trace.pipeline_versions,
    }


def _review_data(
    review: RagEvaluationReview, *, preview_chars: int, redact_query: bool
) -> dict:
    trace = review.trace
    chunks = {str(chunk.id): chunk for chunk in review.chunks}
    positive = set(review.label.positive_chunk_ids) if review.label else set()
    hard_negative = set(review.label.hard_negative_chunk_ids) if review.label else set()

    def ranking(values) -> list[dict]:
        result = []
        for value in values:
            chunk = chunks.get(str(value["chunk_id"]))
            item = dict(value)
            item["in_context"] = UUID(str(value["chunk_id"])) in trace.context_chunk_ids
            item["label"] = _chunk_label(UUID(str(value["chunk_id"])), positive, hard_negative)
            item["chunk"] = _chunk_data(chunk, preview_chars) if chunk else None
            result.append(item)
        return result

    return {
        "trace": _trace_summary(trace, review.label),
        "query": "<redacted>" if redact_query else trace.query,
        "request": trace.request,
        "candidate_ranking": ranking(trace.candidate_ranking),
        "reranked_ranking": ranking(trace.reranked_ranking),
        "context_chunk_ids": [str(value) for value in trace.context_chunk_ids],
        "label": _label_data(review.label) if review.label else None,
    }


def _chunk_data(chunk, preview_chars: int) -> dict:
    content = chunk.content
    return {
        "chunk_id": str(chunk.id),
        "document_id": str(chunk.document_id),
        "ordinal": chunk.ordinal,
        "token_count": chunk.token_count,
        "content_hash": chunk.content_hash,
        "source_locator": chunk.source_locator,
        "content_preview": content[:preview_chars]
        + ("…" if len(content) > preview_chars else ""),
    }


def _label_data(label) -> dict:
    return {
        "label_id": str(label.id),
        "trace_id": str(label.trace_id),
        "status": label.status,
        "source": label.source,
        "positive_chunk_ids": [str(value) for value in label.positive_chunk_ids],
        "hard_negative_chunk_ids": [
            str(value) for value in label.hard_negative_chunk_ids
        ],
        "notes": label.notes,
        "updated_at": label.updated_at.isoformat(),
    }


def _write_promotion_candidate(review: RagEvaluationReview, output: Path) -> None:
    if output.exists():
        raise ValueError(f"晋升候选文件已存在: {output}")
    output.parent.mkdir(parents=True, exist_ok=True)
    chunks = {
        str(chunk.id): {
            "document_id": str(chunk.document_id),
            "ordinal": chunk.ordinal,
            "content_hash": chunk.content_hash,
            "token_count": chunk.token_count,
            "source_locator": chunk.source_locator,
        }
        for chunk in review.chunks
    }
    payload = {
        "schema_version": 1,
        "kind": "rag-regression-candidate",
        "trace_id": str(review.trace.id),
        "workspace_id": str(review.trace.workspace_id),
        "query": review.trace.query,
        "query_hash": review.trace.query_hash,
        "privacy_status": review.trace.privacy_status,
        "pipeline_versions": review.trace.pipeline_versions,
        "label": _label_data(review.label),
        "chunks": chunks,
        "raw_chunk_content_included": False,
    }
    output.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def _uuid_values(values: list[str], field: str) -> tuple[UUID, ...]:
    result = []
    for raw in values:
        for value in raw.split(","):
            try:
                result.append(UUID(value.strip()))
            except ValueError:
                raise ValueError(f"{field} 包含无效 UUID") from None
    if field == "positive" and not result:
        raise ValueError("positive 至少需要一个 chunk id")
    if len(set(result)) != len(result):
        raise ValueError(f"{field} 不得包含重复 chunk id")
    return tuple(result)


def _notes(value: str) -> str:
    value = value.strip()
    if len(value) > 2000:
        raise ValueError("notes 不得超过 2000 字符")
    return value


def _limit(value: int, *, minimum: int = 1, maximum: int) -> int:
    if not minimum <= value <= maximum:
        raise ValueError(f"数值必须在 {minimum}..{maximum}")
    return value


def _chunk_label(chunk_id: UUID, positive: set[UUID], hard_negative: set[UUID]) -> str | None:
    if chunk_id in positive:
        return "positive"
    if chunk_id in hard_negative:
        return "hard_negative"
    return None


def main() -> int:
    args = build_parser().parse_args()
    load_default_local_env()
    try:
        result = asyncio.run(_run(args))
    except (OSError, RuntimeError, ValueError) as exc:
        print(json.dumps({"status": "failed", "error": str(exc)}, ensure_ascii=False))
        return 1
    print(json.dumps({"status": "completed", **result}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
