#!/usr/bin/env python3
"""自动运行 RAG 轨迹采样、质量评估与发布门禁。"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

EVAL_ROOT = Path(__file__).resolve().parents[1]
if str(EVAL_ROOT) not in sys.path:
    sys.path.insert(0, str(EVAL_ROOT))

from framework import (  # noqa: E402
    FlywheelGatePolicy,
    build_review_queue,
    evaluate_labeled_trace,
    evaluate_regression_gate,
    evaluation_sample_from_context,
)
from run_production_trace_eval import _report, build_report  # noqa: E402

from jarvis_worker.agent.rag.embedding.config import RagEmbeddingConfig  # noqa: E402
from jarvis_worker.agent.rag.embedding.openai import (  # noqa: E402
    create_openai_embedding_provider,
)
from jarvis_worker.agent.rag.evaluation.review_service import (  # noqa: E402
    RagEvaluationReviewService,
)
from jarvis_worker.agent.rag.evaluation.gate_service import RagQualityGateService  # noqa: E402
from jarvis_worker.agent.rag.reranking import (  # noqa: E402
    LocalBgeRerankerConfig,
    LocalBgeRerankerProvider,
)
from jarvis_worker.agent.rag.retrieval import RagRetrievalQuery  # noqa: E402
from jarvis_worker.agent.rag.retrieval.service import RagRetrievalService  # noqa: E402
from jarvis_worker.database.engine import (  # noqa: E402
    create_engine,
    dispose_engine,
    get_session_factory,
)
from jarvis_worker.database.unit_of_work import PostgresUnitOfWork  # noqa: E402
from jarvis_worker.shared.config.database import DatabaseConfig  # noqa: E402
from jarvis_worker.shared.config.env_loader import load_default_local_env  # noqa: E402

DEFAULT_POLICY = EVAL_ROOT / "tasks" / "rag-flywheel-gate-v1.json"
DEFAULT_COHORT = EVAL_ROOT / "manifests" / "rag-promoted-p4-v1.json"


async def _load_review_rows(*, limit: int):
    create_engine(DatabaseConfig.from_env())
    try:
        service = RagEvaluationReviewService(get_session_factory)
        return await service.list_traces(privacy_status=None, limit=limit)
    finally:
        await dispose_engine()


async def _load_feedback_summary(*, workspace_ids: set[UUID], limit: int) -> dict:
    create_engine(DatabaseConfig.from_env())
    try:
        counts = Counter()
        categories = Counter()
        factory = get_session_factory()
        async with factory() as session, PostgresUnitOfWork(session).transaction() as tx:
            for workspace_id in workspace_ids:
                values = await tx.rag_evaluation_feedback.list_by_workspace(
                    workspace_id=workspace_id, status=None, limit=min(limit, 100)
                )
                for value in values:
                    counts[value.status] += 1
                    if value.failure_category:
                        categories[value.failure_category] += 1
        return {
            "total": sum(counts.values()),
            "by_status": dict(sorted(counts.items())),
            "by_failure_category": dict(sorted(categories.items())),
            "bounded_per_workspace": min(limit, 100),
        }
    finally:
        await dispose_engine()


async def build_snapshot(
    *,
    limit: int,
    review_limit: int,
    policy: FlywheelGatePolicy,
    baseline: dict | None,
    replay_promoted: bool = False,
    cohort: dict[str, str] | None = None,
) -> dict:
    observation_report = await build_report(
        limit=limit,
        label_statuses=frozenset({"confirmed", "promoted"}),
    )
    release_report = (
        await replay_promoted_report(limit=limit, cohort=cohort or {})
        if replay_promoted
        else await build_report(
            limit=limit,
            label_statuses=frozenset({"promoted"}),
        )
    )
    rows = await _load_review_rows(limit=limit)
    feedback_summary = await _load_feedback_summary(
        workspace_ids={trace.workspace_id for trace, _label in rows}, limit=limit
    )
    gate = evaluate_regression_gate(release_report, policy, baseline=baseline)
    queue = build_review_queue(rows, observation_report, limit=review_limit)
    return {
        "schema_version": 1,
        "generated_at": datetime.now(UTC).isoformat(),
        "source": "production-rag-flywheel",
        "automation": {
            "trace_capture": "automatic",
            "candidate_mining": "automatic",
            "quality_evaluation": "automatic",
            "release_gate": "automatic_promoted_only",
            "promoted_replay": "automatic_current_pipeline" if replay_promoted else "disabled",
            "privacy_review": "human_required",
            "evidence_label_confirmation": "human_required",
            "promotion": "human_required",
        },
        "observation_report": observation_report,
        "release_report": release_report,
        "release_gate": gate,
        "review_queue": queue,
        "review_queue_count": len(queue),
        "feedback_summary": feedback_summary,
        "privacy": {
            "raw_query_included": False,
            "raw_answer_included": False,
            "raw_chunk_content_included": False,
            "embedding_vectors_included": False,
        },
    }


async def replay_promoted_report(*, limit: int, cohort: dict[str, str]) -> dict:
    """用当前生产 RetrievalService 重放 promoted 金标; 不复用历史排序分数。"""

    create_engine(DatabaseConfig.from_env())
    embedding_provider = None
    reranker_provider = None
    try:
        factory = get_session_factory()
        async with factory() as session, PostgresUnitOfWork(session).transaction() as tx:
            labels = await tx.rag_evaluation_labels.list_confirmed(limit=limit)
            promoted_by_trace = {
                str(label.trace_id): label for label in labels if label.status == "promoted"
            }
            missing = [trace_id for trace_id in cohort if trace_id not in promoted_by_trace]
            if missing:
                raise ValueError(f"回归 cohort 包含未晋升或不存在的 trace: {missing}")
            promoted = [promoted_by_trace[trace_id] for trace_id in cohort]
            pairs = []
            for label in promoted:
                trace = await tx.rag_evaluation_traces.get(label.trace_id)
                if trace is not None and trace.privacy_status == "approved":
                    expected_hash = cohort[str(trace.id)]
                    if trace.query_hash != expected_hash:
                        raise ValueError(f"回归 cohort query_hash 不匹配: {trace.id}")
                    pairs.append((trace, label))
            if len(pairs) != len(cohort):
                raise ValueError("回归 cohort 含隐私未批准或缺失的 trace")

        embedding_provider = create_openai_embedding_provider(RagEmbeddingConfig.from_env())
        reranker_config = LocalBgeRerankerConfig.from_env()
        reranker_provider = (
            LocalBgeRerankerProvider(reranker_config) if reranker_config.enabled else None
        )
        service = RagRetrievalService(
            get_session_factory,
            embedding_provider=embedding_provider,
            reranker_provider=reranker_provider,
        )
        outcomes = []
        for trace, label in pairs:
            package = await service.search(
                workspace_id=trace.workspace_id,
                request=_request_from_trace(trace),
            )
            sample = evaluation_sample_from_context(
                package,
                trace_id=str(trace.id),
                query_id=str(label.id),
                positive_chunk_ids=label.positive_chunk_ids,
                hard_negative_chunk_ids=label.hard_negative_chunk_ids,
            )
            outcomes.append(evaluate_labeled_trace(sample))
        report = _report(
            outcomes,
            pairs,
            confirmed_label_count=len(labels),
            eligible_label_count=len(promoted),
            eligible_label_statuses=["promoted"],
        )
        report["source"] = "production-rag-promoted-replay"
        report["replayed_with_current_pipeline"] = True
        report["cohort_trace_count"] = len(cohort)
        return report
    finally:
        if reranker_provider is not None:
            await reranker_provider.aclose()
        if embedding_provider is not None:
            await embedding_provider.aclose()
        await dispose_engine()


def _request_from_trace(trace) -> RagRetrievalQuery:
    request = trace.request
    return RagRetrievalQuery(
        query=trace.query,
        top_k=int(request.get("top_k", 8)),
        candidate_limit=int(request.get("candidate_limit", 50)),
        feature_limit=int(request.get("feature_limit", 30)),
        cross_encoder_limit=int(request.get("cross_encoder_limit", 16)),
        diversity_limit=int(request.get("diversity_limit", 10)),
        token_budget=int(request.get("token_budget", 4_000)),
        document_ids=tuple(UUID(str(value)) for value in request.get("document_ids", [])),
        min_score=float(request.get("min_score", 0.15)),
        neighbor_window=int(request.get("neighbor_window", 1)),
        max_chunks_per_document=int(request.get("max_chunks_per_document", 3)),
    )


def _write_snapshot(snapshot: dict, output_dir: Path) -> None:
    output_dir.mkdir(parents=True, exist_ok=False)
    (output_dir / "flywheel.json").write_text(
        json.dumps(snapshot, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    gate = snapshot["release_gate"]
    observation = snapshot["observation_report"]
    release = snapshot["release_report"]
    queue = snapshot["review_queue"]
    feedback = snapshot.get("feedback_summary", {"total": 0, "by_status": {}, "by_failure_category": {}})
    lines = [
        "# RAG Flywheel Report",
        "",
        f"- Gate: {gate['status']}",
        f"- Observed confirmed/promoted samples: {observation['sample_count']}",
        f"- Release promoted samples: {release['sample_count']}",
        f"- Review queue: {len(queue)}",
        f"- User feedback candidates: {feedback['total']}",
        "- Raw query/answer/chunk/vector: excluded",
        "",
        "## Gate checks",
        "",
        "| Check | Passed | Actual | Required |",
        "|---|---:|---:|---:|",
    ]
    for check in gate["checks"]:
        required = check.get("required", check.get("required_minimum", check.get("maximum")))
        lines.append(
            f"| {check['check_id']} | {str(check['passed']).lower()} | "
            f"{check.get('actual')} | {required} |"
        )
    lines.extend(["", "## Feedback diagnostics", "", f"- Status: {feedback['by_status']}", f"- Failure categories: {feedback['by_failure_category']}"])
    lines.extend(
        [
            "",
            "## Review queue",
            "",
            "| Trace | Priority | Privacy | Label | Reasons |",
            "|---|---:|---|---|---|",
        ]
    )
    for item in queue:
        lines.append(
            f"| {item['trace_id']} | {item['priority']} | {item['privacy_status']} | "
            f"{item['label_status'] or '-'} | {', '.join(item['reasons'])} |"
        )
    (output_dir / "flywheel.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def _load_json(path: Path) -> dict:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"JSON 顶层必须是对象: {path}")
    return value


def _baseline_report(value: dict | None) -> dict | None:
    if value is None:
        return None
    nested = value.get("release_report")
    if isinstance(nested, dict):
        return nested
    if isinstance(value.get("aggregate_metrics"), dict):
        return value
    raise ValueError("baseline 必须是飞轮快照或含 aggregate_metrics 的质量报告")


def _load_cohort(path: Path) -> dict[str, str]:
    value = _load_json(path)
    samples = value.get("samples")
    if value.get("schema_version") != 1 or not isinstance(samples, list) or not samples:
        raise ValueError("回归 cohort 必须是 schema v1 且包含 samples")
    cohort: dict[str, str] = {}
    for sample in samples:
        if not isinstance(sample, dict):
            raise ValueError("回归 cohort sample 必须是对象")
        trace_id = str(UUID(str(sample.get("trace_id", ""))))
        query_hash = str(sample.get("query_hash", ""))
        if len(query_hash) != 64 or any(value not in "0123456789abcdef" for value in query_hash):
            raise ValueError(f"回归 cohort query_hash 无效: {trace_id}")
        if trace_id in cohort:
            raise ValueError(f"回归 cohort trace_id 重复: {trace_id}")
        cohort[trace_id] = query_hash
    if len(cohort) > 500:
        raise ValueError("回归 cohort 不得超过 500 条")
    return cohort


async def _persist_gate_run(
    snapshot: dict, *, revision: str, cohort_id: str, baseline_id: str
) -> None:
    create_engine(DatabaseConfig.from_env())
    try:
        await RagQualityGateService(get_session_factory).record_snapshot(
            snapshot, revision=revision, cohort_id=cohort_id, baseline_id=baseline_id
        )
    finally:
        await dispose_engine()


def main() -> int:
    parser = argparse.ArgumentParser(description="运行 RAG 数据飞轮和 promoted-only 发布门禁")
    parser.add_argument("--policy", type=Path, default=DEFAULT_POLICY)
    parser.add_argument("--baseline", type=Path)
    parser.add_argument("--cohort-manifest", type=Path, default=DEFAULT_COHORT)
    parser.add_argument("--limit", type=int, default=500)
    parser.add_argument("--review-limit", type=int, default=100)
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument(
        "--replay-promoted",
        action="store_true",
        help="用当前生产 RAG Pipeline 重放 promoted 金标后再执行门禁",
    )
    parser.add_argument("--fail-on-blocked", action="store_true")
    parser.add_argument("--revision", required=True)
    args = parser.parse_args()
    if not 1 <= args.limit <= 500 or not 1 <= args.review_limit <= 500:
        parser.error("--limit/--review-limit 必须在 1..500")
    load_default_local_env()
    timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    output_dir = args.output_dir or EVAL_ROOT / "reports" / "flywheel" / timestamp
    try:
        policy = FlywheelGatePolicy.from_dict(_load_json(args.policy))
        baseline_value = _load_json(args.baseline) if args.baseline else None
        baseline = _baseline_report(baseline_value)
        baseline_id = str((baseline_value or {}).get("baseline_id", "no-baseline"))
        cohort_value = _load_json(args.cohort_manifest) if args.replay_promoted else None
        cohort_id = str((cohort_value or {}).get("cohort_id", "no-cohort"))
        cohort = _load_cohort(args.cohort_manifest) if args.replay_promoted else None
        snapshot = asyncio.run(
            build_snapshot(
                limit=args.limit,
                review_limit=args.review_limit,
                policy=policy,
                baseline=baseline,
                replay_promoted=args.replay_promoted,
                cohort=cohort,
            )
        )
        _write_snapshot(snapshot, output_dir)
        asyncio.run(
            _persist_gate_run(
                snapshot,
                revision=args.revision,
                cohort_id=cohort_id,
                baseline_id=baseline_id,
            )
        )
    except (OSError, RuntimeError, ValueError, json.JSONDecodeError) as exc:
        print(json.dumps({"status": "failed", "error": str(exc)}, ensure_ascii=False))
        return 1
    gate_status = snapshot["release_gate"]["status"]
    print(
        json.dumps(
            {"status": "completed", "gate_status": gate_status, "output_dir": str(output_dir)},
            ensure_ascii=False,
        )
    )
    if args.fail_on_blocked and gate_status != "passed":
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
