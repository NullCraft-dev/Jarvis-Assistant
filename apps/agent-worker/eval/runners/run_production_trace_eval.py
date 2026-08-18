#!/usr/bin/env python3
"""从真实生产 RAG 轨迹生成分阶段 JSON/Markdown 评估报告。"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from collections import defaultdict
from datetime import UTC, datetime
from pathlib import Path

EVAL_ROOT = Path(__file__).resolve().parents[1]
if str(EVAL_ROOT) not in sys.path:
    sys.path.insert(0, str(EVAL_ROOT))

from framework import evaluate_labeled_trace, evaluation_sample_from_trace  # noqa: E402

from jarvis_worker.database.engine import (  # noqa: E402
    create_engine,
    dispose_engine,
    get_session_factory,
)
from jarvis_worker.database.unit_of_work import PostgresUnitOfWork  # noqa: E402
from jarvis_worker.shared.config.database import DatabaseConfig  # noqa: E402
from jarvis_worker.shared.config.env_loader import load_default_local_env  # noqa: E402


async def build_report(*, limit: int, label_statuses: frozenset[str] | None = None) -> dict:
    create_engine(DatabaseConfig.from_env())
    try:
        factory = get_session_factory()
        async with (
            factory() as session,
            PostgresUnitOfWork(session).transaction() as tx,
        ):
            labels = await tx.rag_evaluation_labels.list_confirmed(limit=limit)
            selected_labels = [
                label
                for label in labels
                if label_statuses is None or label.status in label_statuses
            ]
            pairs = []
            for label in selected_labels:
                trace = await tx.rag_evaluation_traces.get(label.trace_id)
                if trace is not None and trace.privacy_status == "approved":
                    pairs.append((trace, label))
        outcomes = [
            evaluate_labeled_trace(evaluation_sample_from_trace(trace, label))
            for trace, label in pairs
        ]
        return _report(
            outcomes,
            pairs,
            confirmed_label_count=len(labels),
            eligible_label_count=len(selected_labels),
            eligible_label_statuses=sorted(label_statuses or {"confirmed", "promoted"}),
        )
    finally:
        await dispose_engine()


def _report(
    outcomes,
    pairs,
    *,
    confirmed_label_count: int | None = None,
    eligible_label_count: int | None = None,
    eligible_label_statuses: list[str] | None = None,
) -> dict:
    metric_values = defaultdict(list)
    failure_counts = defaultdict(int)
    label_status_counts = defaultdict(int)
    samples = []
    for outcome, (trace, label) in zip(outcomes, pairs, strict=True):
        label_status = getattr(label, "status", "confirmed")
        label_status_counts[label_status] += 1
        for metric in outcome.metrics:
            metric_values[metric.metric_id].append(metric.value)
        for failure in outcome.failures:
            failure_counts[failure.failure_type.value] += 1
        samples.append(
            {
                "trace_id": outcome.sample.trace_id,
                "query_hash": trace.query_hash,
                "pipeline_versions": outcome.sample.pipeline_versions,
                "label_status": label_status,
                "metrics": {metric.metric_id: metric.value for metric in outcome.metrics},
                "failures": [
                    {
                        "candidate_id": failure.candidate_id,
                        "failure_type": failure.failure_type.value,
                        "suspected_stage": failure.suspected_stage.value,
                        "severity": failure.severity.value,
                        "metric_ids": list(failure.metric_ids),
                    }
                    for failure in outcome.failures
                ],
            }
        )
    seen = len(pairs) if confirmed_label_count is None else confirmed_label_count
    eligible = len(pairs) if eligible_label_count is None else eligible_label_count
    return {
        "schema_version": 1,
        "generated_at": datetime.now(UTC).isoformat(),
        "source": "production-rag-traces",
        "sample_count": len(samples),
        "confirmed_label_count": seen,
        "eligible_label_count": eligible,
        "eligible_label_statuses": eligible_label_statuses or ["confirmed", "promoted"],
        "label_status_counts": dict(sorted(label_status_counts.items())),
        "excluded_unapproved_or_missing_trace_count": eligible - len(samples),
        "aggregate_metrics": {
            metric_id: sum(values) / len(values)
            for metric_id, values in sorted(metric_values.items())
        },
        "failure_count": sum(len(sample["failures"]) for sample in samples),
        "failure_counts": dict(sorted(failure_counts.items())),
        "samples": samples,
        "privacy": {
            "raw_query_included": False,
            "raw_chunk_content_included": False,
            "embedding_vectors_included": False,
        },
    }


def _write_report(report: dict, output_dir: Path) -> None:
    output_dir.mkdir(parents=True, exist_ok=False)
    (output_dir / "report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    lines = [
        "# Production RAG Evaluation",
        "",
        f"- Samples: {report['sample_count']}",
        f"- Confirmed labels seen: {report['confirmed_label_count']}",
        "- Excluded by privacy/missing trace: "
        f"{report['excluded_unapproved_or_missing_trace_count']}",
        f"- Failures: {report['failure_count']}",
        "- Raw query/chunk/vector: excluded",
        "",
        "## Aggregate metrics",
        "",
        "| Metric | Value |",
        "|---|---:|",
    ]
    lines.extend(
        f"| {metric_id} | {value:.6f} |" for metric_id, value in report["aggregate_metrics"].items()
    )
    (output_dir / "report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description="评估已确认的真实生产 RAG 轨迹")
    parser.add_argument("--limit", type=int, default=100)
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument(
        "--label-status",
        choices=("all", "confirmed", "promoted"),
        default="all",
        help="发布门禁应使用 promoted; all 用于观察报告",
    )
    args = parser.parse_args()
    if not 1 <= args.limit <= 500:
        parser.error("--limit 必须在 1..500")
    load_default_local_env()
    timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    output_dir = args.output_dir or EVAL_ROOT / "reports" / "production-traces" / timestamp
    label_statuses = None if args.label_status == "all" else frozenset({args.label_status})
    report = asyncio.run(build_report(limit=args.limit, label_statuses=label_statuses))
    _write_report(report, output_dir)
    print(json.dumps({"status": "completed", "output_dir": str(output_dir)}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
