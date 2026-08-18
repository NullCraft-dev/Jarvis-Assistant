#!/usr/bin/env python3
"""把人工核验过的生产答案引用接回现有 RAG trace/label 数据飞轮。"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from statistics import median
from uuid import UUID

EVAL_ROOT = Path(__file__).resolve().parents[1]
if str(EVAL_ROOT) not in sys.path:
    sys.path.insert(0, str(EVAL_ROOT))

from framework import evaluate_labeled_trace, evaluation_sample_from_trace  # noqa: E402

from jarvis_worker.agent.rag.evaluation.review_service import (  # noqa: E402
    RagEvaluationReviewService,
)
from jarvis_worker.database.engine import (  # noqa: E402
    create_engine,
    dispose_engine,
    get_session_factory,
)
from jarvis_worker.shared.config.database import DatabaseConfig  # noqa: E402
from jarvis_worker.shared.config.env_loader import load_default_local_env  # noqa: E402

_CITATION_PATTERN = re.compile(r"`chunk:([0-9a-fA-F-]{36})`")


async def build_baseline(report_paths: list[Path], *, confirm: bool) -> dict:
    reports = [_load_report(path) for path in report_paths]
    results = [result for report in reports for result in report["results"]]
    create_engine(DatabaseConfig.from_env())
    try:
        service = RagEvaluationReviewService(get_session_factory)
        traces = [trace for trace, _ in await service.list_traces(privacy_status=None, limit=500)]
        by_run: dict[str, list] = defaultdict(list)
        for trace in traces:
            by_run[str(trace.run_id)].append(trace)
        reviewed = []
        skipped = []
        for result in results:
            citations = tuple(dict.fromkeys(_CITATION_PATTERN.findall(result.get("answer", ""))))
            if not citations:
                skipped.append({"question_id": result["question_id"], "reason": "no_citations"})
                continue
            trace = _select_trace(by_run.get(result["run_id"], []), citations)
            if trace is None:
                skipped.append({"question_id": result["question_id"], "reason": "trace_not_found"})
                continue
            review = await service.inspect(trace.id)
            scoped = {str(chunk.id) for chunk in review.chunks}
            if not set(citations).issubset(scoped):
                skipped.append(
                    {"question_id": result["question_id"], "reason": "citation_not_in_trace"}
                )
                continue
            if not confirm:
                raise ValueError("写入人工标签必须显式传入 --confirm-reviewed-citations")
            if trace.privacy_status != "approved":
                await service.review_privacy(trace.id, approved=True)
            label = await service.set_label(
                trace_id=trace.id,
                positive_chunk_ids=tuple(UUID(value) for value in citations),
                notes="P4-2 人工逐条核验最终答案事实与引用后确认",
            )
            refreshed = await service.inspect(trace.id)
            outcome = evaluate_labeled_trace(evaluation_sample_from_trace(refreshed.trace, label))
            document_by_chunk = {
                str(chunk.id): str(chunk.document_id) for chunk in refreshed.chunks
            }
            context_documents = {
                document_by_chunk[str(chunk_id)]
                for chunk_id in refreshed.trace.context_chunk_ids
                if str(chunk_id) in document_by_chunk
            }
            reviewed.append((result, refreshed.trace, outcome, context_documents))
        return _report(reports, results, reviewed, skipped)
    finally:
        await dispose_engine()


def _select_trace(traces: list, citations: tuple[str, ...]):
    wanted = set(citations)
    scored = []
    for trace in traces:
        available = {
            str(value["chunk_id"]) for value in (*trace.candidate_ranking, *trace.reranked_ranking)
        } | {str(value) for value in trace.context_chunk_ids}
        scored.append((len(wanted & available), trace.created_at, trace))
    if not scored:
        return None
    return max(scored, key=lambda item: (item[0], item[1]))[2]


def _report(reports, results, reviewed, skipped) -> dict:
    metrics: dict[str, list[float]] = defaultdict(list)
    failures: dict[str, int] = defaultdict(int)
    coverage_values = []
    samples = []
    for result, trace, outcome, context_documents in reviewed:
        for metric in outcome.metrics:
            metrics[metric.metric_id].append(metric.value)
        for failure in outcome.failures:
            failures[failure.failure_type.value] += 1
        requested = set(trace.request.get("document_ids", []))
        if requested:
            coverage_values.append(float(requested.issubset(context_documents)))
        samples.append(
            {
                "question_id": result["question_id"],
                "trace_id": str(trace.id),
                "run_id": str(trace.run_id),
                "pipeline_versions": trace.pipeline_versions,
            }
        )
    durations = sorted(
        int(result.get("duration_ms", 0)) for result in results if result.get("duration_ms")
    )
    end_to_end = defaultdict(list)
    for result in results:
        for metric_id in ("answer_correctness", "fact_coverage", "citation_completeness"):
            value = result.get("metrics", {}).get(metric_id)
            if isinstance(value, (int, float)):
                end_to_end[metric_id].append(float(value))
    return {
        "schema_version": 1,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "suite_ids": [report["suite_id"] for report in reports],
        "sample_count": len(results),
        "reviewed_trace_count": len(reviewed),
        "skipped_trace_count": len(skipped),
        "aggregate_metrics": {
            **{key: sum(values) / len(values) for key, values in sorted(metrics.items())},
            "coverage.completeness": (
                sum(coverage_values) / len(coverage_values) if coverage_values else 0.0
            ),
            **{
                f"end_to_end.{key}": sum(values) / len(values)
                for key, values in sorted(end_to_end.items())
            },
        },
        "latency_ms": {
            "min": durations[0] if durations else 0,
            "median": median(durations) if durations else 0,
            "p95": _percentile(durations, 0.95),
            "max": durations[-1] if durations else 0,
        },
        "failure_counts": dict(sorted(failures.items())),
        "skipped": skipped,
        "samples": samples,
        "review_policy": "human-reviewed-final-answer-citations",
        "privacy": {
            "raw_query_included": False,
            "raw_answer_included": False,
            "raw_chunk_content_included": False,
        },
    }


def _percentile(values: list[int], ratio: float) -> int:
    if not values:
        return 0
    return values[min(round((len(values) - 1) * ratio), len(values) - 1)]


def _load_report(path: Path) -> dict:
    value = json.loads(path.read_text(encoding="utf-8"))
    if value.get("schema_version") != 2 or not isinstance(value.get("results"), list):
        raise ValueError(f"生产运行报告无效: {path}")
    return value


def main() -> int:
    parser = argparse.ArgumentParser(description="生成 P4 当前版本端到端 + 分阶段质量基线")
    parser.add_argument("--run-report", action="append", type=Path, required=True)
    parser.add_argument("--confirm-reviewed-citations", action="store_true")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    load_default_local_env()
    try:
        report = asyncio.run(
            build_baseline(args.run_report, confirm=args.confirm_reviewed_citations)
        )
        args.output.parent.mkdir(parents=True, exist_ok=True)
        if args.output.exists():
            raise ValueError(f"输出已存在: {args.output}")
        args.output.write_text(
            json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
    except (OSError, RuntimeError, ValueError) as exc:
        print(json.dumps({"status": "failed", "error": str(exc)}, ensure_ascii=False))
        return 1
    print(json.dumps({"status": "completed", "output": str(args.output)}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
