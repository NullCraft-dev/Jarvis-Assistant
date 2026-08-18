"""离线 RAG 发布门禁结果的持久化与只读查询。"""

from __future__ import annotations

import re
from dataclasses import replace
from datetime import datetime, timezone
from uuid import UUID, uuid4

from jarvis_worker.database.unit_of_work import PostgresUnitOfWork
from jarvis_worker.shared.domain.models import AuditLog

from .contracts import (
    RagQualityAlert,
    RagQualityFailureCluster,
    RagQualityFailureTarget,
    RagQualityGateInsights,
    RagQualityGateRun,
    RagQualityMetricTrend,
    RagQualityIssue,
    RagQualityIssueLedgerItem,
)

_SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$")
_REVISION = re.compile(r"^[0-9a-f]{40}$")
_ALLOWED_STATUSES = {"passed", "blocked", "insufficient_evidence"}
_ALLOWED_METRICS = {
    "candidate.recall@1",
    "candidate.recall@5",
    "candidate.mrr",
    "reranker.recall@1",
    "reranker.recall@5",
    "reranker.mrr",
    "context.evidence_recall",
    "context.truncated_rate",
    "context.truncated",
    "embedding.positive_negative_margin",
    "chunk.must_keep_pass_rate",
}
_ALLOWED_CHECK_FIELDS = {
    "check_id", "passed", "actual", "required", "required_minimum",
    "absolute_minimum", "baseline", "maximum_regression", "failure_count", "maximum",
}
_HEX64 = re.compile(r"^[0-9a-f]{64}$")
_ALLOWED_FAILURE_TYPES = {
    "chunk_semantic_split", "embedding_margin_low", "candidate_evidence_missed",
    "reranker_evidence_dropped", "context_evidence_dropped", "context_truncated",
}
_ALLOWED_STAGES = {
    "preprocessing", "chunking", "embedding", "candidate_recall", "reranker",
    "context_assembly", "generation", "citation", "end_to_end",
}
_ALLOWED_SEVERITIES = {"low", "medium", "high", "critical"}
_ALLOWED_ISSUE_OWNERS = {"data_quality", "candidate_recall", "reranker", "context_assembly"}
_ISSUE_TRANSITIONS = {
    "open": {"open", "in_progress", "dismissed"},
    "in_progress": {"in_progress", "open", "resolved", "dismissed"},
    "resolved": {"resolved", "in_progress"},
    "verified": {"verified", "in_progress"},
    "dismissed": {"dismissed", "open"},
}


class RagQualityGateService:
    def __init__(self, uow_factory) -> None:
        self._uow_factory = uow_factory

    async def record_snapshot(
        self, snapshot: dict, *, revision: str, cohort_id: str, baseline_id: str
    ) -> RagQualityGateRun:
        gate = snapshot.get("release_gate")
        report = snapshot.get("release_report")
        privacy = snapshot.get("privacy")
        if not isinstance(gate, dict) or not isinstance(report, dict):
            raise ValueError("质量门禁快照缺少 release_gate/release_report")
        if privacy != {
            "raw_query_included": False,
            "raw_answer_included": False,
            "raw_chunk_content_included": False,
            "embedding_vectors_included": False,
        }:
            raise ValueError("质量门禁快照未通过隐私投影检查")
        gate_id = str(gate.get("gate_id", ""))
        status = str(gate.get("status", ""))
        if not _SAFE_ID.fullmatch(gate_id) or not _SAFE_ID.fullmatch(cohort_id) or not _SAFE_ID.fullmatch(baseline_id):
            raise ValueError("质量门禁标识无效")
        if not _REVISION.fullmatch(revision) or status not in _ALLOWED_STATUSES:
            raise ValueError("质量门禁 revision/status 无效")
        generated_at = datetime.fromisoformat(str(snapshot.get("generated_at", "")))
        if generated_at.tzinfo is None:
            raise ValueError("质量门禁 generated_at 必须含时区")
        raw_metrics = report.get("aggregate_metrics", {})
        if not isinstance(raw_metrics, dict):
            raise ValueError("质量门禁 metrics 无效")
        metrics = {
            key: float(value)
            for key, value in raw_metrics.items()
            if key in _ALLOWED_METRICS and isinstance(value, int | float)
        }
        checks = tuple(_sanitize_check(value) for value in gate.get("checks", []))
        failure_targets = _sanitize_failure_targets(report.get("samples", []))
        run = RagQualityGateRun(
            id=uuid4(), gate_id=gate_id, cohort_id=cohort_id, baseline_id=baseline_id,
            revision=revision, status=status, sample_count=int(gate.get("sample_count", 0)),
            metrics=metrics, checks=checks, generated_at=generated_at,
            failure_targets=failure_targets,
        )
        session_factory = self._uow_factory()
        async with session_factory() as session:
            async with PostgresUnitOfWork(session).transaction() as tx:
                await tx.rag_quality_gate_runs.create(run)
                await _sync_quality_issues(tx, run)
                await tx.commit()
        return run

    async def list_runs(self, *, limit: int = 20) -> tuple[RagQualityGateRun, ...]:
        session_factory = self._uow_factory()
        async with session_factory() as session:
            async with PostgresUnitOfWork(session).transaction() as tx:
                return tuple(await tx.rag_quality_gate_runs.list_latest(limit=limit))

    async def get_overview(
        self, *, limit: int = 20
    ) -> tuple[tuple[RagQualityGateRun, ...], RagQualityGateInsights]:
        runs = await self.list_runs(limit=limit)
        return runs, build_quality_gate_insights(runs)

    async def list_failure_targets(
        self, *, run_id: UUID, failure_type: str, limit: int = 50
    ) -> tuple[RagQualityFailureTarget, ...]:
        if failure_type not in _ALLOWED_FAILURE_TYPES:
            raise ValueError("失败类型无效")
        session_factory = self._uow_factory()
        values: list[RagQualityFailureTarget] = []
        async with session_factory() as session:
            async with PostgresUnitOfWork(session).transaction() as tx:
                run = await tx.rag_quality_gate_runs.get(run_id)
                if run is None:
                    raise LookupError("质量门禁运行不存在")
                for target in run.failure_targets:
                    if target["failure_type"] != failure_type:
                        continue
                    trace = await tx.rag_evaluation_traces.get(UUID(target["trace_id"]))
                    if trace is None or trace.query_hash != target["query_hash"]:
                        continue
                    label = await tx.rag_evaluation_labels.get_for_trace(trace.id)
                    issue = await tx.rag_quality_issues.get_by_candidate_id(target["candidate_id"])
                    label_status = label.status if label else None
                    values.append(RagQualityFailureTarget(
                        candidate_id=target["candidate_id"], trace_id=trace.id,
                        workspace_id=trace.workspace_id, query_hash=trace.query_hash,
                        failure_type=target["failure_type"], suspected_stage=target["suspected_stage"],
                        severity=target["severity"], metric_ids=tuple(target["metric_ids"]),
                        privacy_status=trace.privacy_status, label_status=label_status,
                        label_source=label.source if label else None,
                        review_state=_review_state(trace.privacy_status, label_status),
                        issue=issue,
                    ))
                    if len(values) >= min(max(limit, 1), 100):
                        break
        return tuple(values)

    async def update_issue(
        self, issue_id: UUID, *, expected_version: int, owner: str, status: str,
        resolution_note: str = "",
    ) -> RagQualityIssue:
        if owner not in _ALLOWED_ISSUE_OWNERS or status not in _ISSUE_TRANSITIONS:
            raise ValueError("质量问题 owner/status 无效")
        note = resolution_note.strip()
        if len(note) > 500 or (status in {"resolved", "dismissed"} and not note):
            raise ValueError("待验证或忽略质量问题时必须填写不超过 500 字的说明")
        session_factory = self._uow_factory()
        async with session_factory() as session:
            async with PostgresUnitOfWork(session).transaction() as tx:
                current = await tx.rag_quality_issues.get(issue_id)
                if current is None:
                    raise LookupError("质量问题不存在")
                if current.version != expected_version:
                    raise RuntimeError("质量问题已被其他操作更新，请刷新后重试")
                updated = apply_quality_issue_update(
                    current, owner=owner, status=status, resolution_note=note,
                    now=datetime.now(timezone.utc),
                )
                saved = await tx.rag_quality_issues.save(updated, expected_version=expected_version)
                if saved is None:
                    raise RuntimeError("质量问题已被其他操作更新，请刷新后重试")
                await tx.audits.create(AuditLog(
                    id=uuid4(), event_type="rag.quality.issue_updated", actor="user",
                    risk_level="L2", permission_decision="user_explicit",
                    action_summary="更新 RAG 质量问题治理状态",
                    details={"issue_id": str(saved.id), "candidate_id": saved.candidate_id,
                             "owner": saved.owner, "status": saved.status, "version": saved.version},
                    result_summary=f"质量问题已更新为 {saved.status}",
                ))
                await tx.commit()
                return saved

    async def list_issues(
        self, *, status: str = "all", owner: str = "all",
        failure_type: str = "all", limit: int = 50,
    ) -> tuple[tuple[RagQualityIssueLedgerItem, ...], dict[str, int]]:
        if status not in {*_ISSUE_TRANSITIONS, "all"}:
            raise ValueError("质量问题 status 无效")
        if owner not in {*_ALLOWED_ISSUE_OWNERS, "all"}:
            raise ValueError("质量问题 owner 无效")
        if failure_type not in {*_ALLOWED_FAILURE_TYPES, "all"}:
            raise ValueError("质量问题 failure_type 无效")
        if limit < 1 or limit > 100:
            raise ValueError("质量问题 limit 无效")
        session_factory = self._uow_factory()
        values: list[RagQualityIssueLedgerItem] = []
        async with session_factory() as session:
            async with PostgresUnitOfWork(session).transaction() as tx:
                issues = await tx.rag_quality_issues.list_filtered(
                    status=None if status == "all" else status,
                    owner=None if owner == "all" else owner,
                    failure_type=None if failure_type == "all" else failure_type,
                    limit=limit,
                )
                counts = await tx.rag_quality_issues.count_by_status()
                for issue in issues:
                    trace = await tx.rag_evaluation_traces.get(issue.trace_id)
                    first_run = await tx.rag_quality_gate_runs.get(issue.first_seen_run_id)
                    last_run = await tx.rag_quality_gate_runs.get(issue.last_seen_run_id)
                    if trace is None or first_run is None or last_run is None:
                        continue
                    label = await tx.rag_evaluation_labels.get_for_trace(trace.id)
                    verified_run = (
                        await tx.rag_quality_gate_runs.get(issue.verified_run_id)
                        if issue.verified_run_id else None
                    )
                    label_status = label.status if label else None
                    values.append(RagQualityIssueLedgerItem(
                        issue=issue, workspace_id=trace.workspace_id,
                        query_hash=trace.query_hash, privacy_status=trace.privacy_status,
                        label_status=label_status,
                        review_state=_review_state(trace.privacy_status, label_status),
                        first_seen_revision=first_run.revision,
                        last_seen_revision=last_run.revision,
                        verified_revision=verified_run.revision if verified_run else None,
                    ))
        summary = {key: counts.get(key, 0) for key in _ISSUE_TRANSITIONS}
        summary["total"] = sum(summary.values())
        return tuple(values), summary


def apply_quality_issue_update(
    current: RagQualityIssue, *, owner: str, status: str, resolution_note: str,
    now: datetime,
) -> RagQualityIssue:
    if owner not in _ALLOWED_ISSUE_OWNERS or status not in _ISSUE_TRANSITIONS:
        raise ValueError("质量问题 owner/status 无效")
    if status not in _ISSUE_TRANSITIONS[current.status]:
        raise ValueError(f"质量问题不能从 {current.status} 更新为 {status}")
    note = resolution_note.strip()
    if len(note) > 500 or (status in {"resolved", "dismissed"} and not note):
        raise ValueError("待验证或忽略质量问题时必须填写不超过 500 字的说明")
    return replace(
        current, owner=owner, status=status, resolution_note=note,
        verified_run_id=None if status != "verified" else current.verified_run_id,
        version=current.version + 1, updated_at=now,
    )


async def _sync_quality_issues(tx, run: RagQualityGateRun) -> None:
    current_ids = {target["candidate_id"] for target in run.failure_targets}
    now = run.generated_at
    for target in run.failure_targets:
        existing = await tx.rag_quality_issues.get_by_candidate_id(target["candidate_id"])
        if existing is None:
            await tx.rag_quality_issues.create(RagQualityIssue(
                id=uuid4(), candidate_id=target["candidate_id"], trace_id=UUID(target["trace_id"]),
                gate_id=run.gate_id, cohort_id=run.cohort_id, failure_type=target["failure_type"],
                owner=_default_issue_owner(target["suspected_stage"]), status="open",
                occurrence_count=1, first_seen_run_id=run.id, last_seen_run_id=run.id,
                created_at=now, updated_at=now,
            ))
        elif existing.last_seen_run_id != run.id:
            status = "open" if existing.status in {"resolved", "verified"} else existing.status
            await tx.rag_quality_issues.save(replace(
                existing, status=status, occurrence_count=existing.occurrence_count + 1,
                last_seen_run_id=run.id, verified_run_id=None, version=existing.version + 1,
                updated_at=now,
            ), expected_version=existing.version)
    for issue in await tx.rag_quality_issues.list_resolved(gate_id=run.gate_id, cohort_id=run.cohort_id):
        if issue.candidate_id not in current_ids and issue.updated_at < run.generated_at:
            await tx.rag_quality_issues.save(replace(
                issue, status="verified", verified_run_id=run.id,
                version=issue.version + 1, updated_at=now,
            ), expected_version=issue.version)


def _default_issue_owner(stage: str) -> str:
    return stage if stage in {"candidate_recall", "reranker", "context_assembly"} else "data_quality"


def _sanitize_failure_targets(samples: object) -> tuple[dict, ...]:
    if not isinstance(samples, list):
        raise ValueError("质量门禁 samples 无效")
    values: list[dict] = []
    for sample in samples[:1000]:
        if not isinstance(sample, dict):
            raise ValueError("质量门禁 sample 无效")
        trace_id, query_hash = str(sample.get("trace_id", "")), str(sample.get("query_hash", ""))
        try:
            UUID(trace_id)
        except ValueError:
            raise ValueError("质量门禁 trace_id 无效") from None
        if not _HEX64.fullmatch(query_hash):
            raise ValueError("质量门禁 query_hash 无效")
        failures = sample.get("failures", [])
        if not isinstance(failures, list):
            raise ValueError("质量门禁 failures 无效")
        for failure in failures[:20]:
            if not isinstance(failure, dict):
                raise ValueError("质量门禁 failure 无效")
            candidate_id = str(failure.get("candidate_id", ""))
            failure_type = str(failure.get("failure_type", ""))
            stage = str(failure.get("suspected_stage", ""))
            severity = str(failure.get("severity", ""))
            metric_ids = failure.get("metric_ids", [])
            if (not _HEX64.fullmatch(candidate_id) or failure_type not in _ALLOWED_FAILURE_TYPES
                    or stage not in _ALLOWED_STAGES or severity not in _ALLOWED_SEVERITIES
                    or not isinstance(metric_ids, list) or len(metric_ids) > 10
                    or any(not isinstance(v, str) or v not in _ALLOWED_METRICS for v in metric_ids)):
                raise ValueError("质量门禁 failure 投影无效")
            values.append({
                "candidate_id": candidate_id, "trace_id": trace_id, "query_hash": query_hash,
                "failure_type": failure_type, "suspected_stage": stage, "severity": severity,
                "metric_ids": metric_ids,
            })
    return tuple(values)


def _review_state(privacy_status: str, label_status: str | None) -> str:
    if privacy_status == "pending": return "privacy_required"
    if privacy_status == "rejected": return "privacy_rejected"
    if label_status in {None, "draft", "rejected"}: return "label_review_required"
    if label_status == "confirmed": return "promotion_ready"
    return "fixed_regression_sample"


def build_quality_gate_insights(
    runs: tuple[RagQualityGateRun, ...], *, stable_epsilon: float = 0.005
) -> RagQualityGateInsights:
    if not runs:
        return RagQualityGateInsights(
            comparison_state="insufficient_history",
            compatible_history_count=0,
            previous_run_id=None,
            metric_trends=(),
            alerts=(),
            failure_clusters=(),
        )
    latest = runs[0]
    compatible = tuple(
        value
        for value in runs
        if value.gate_id == latest.gate_id and value.cohort_id == latest.cohort_id
    )
    previous = compatible[1] if len(compatible) > 1 else None
    trends = _metric_trends(latest, previous, stable_epsilon=stable_epsilon)
    alerts = _quality_alerts(latest, previous, trends)
    clusters = _failure_clusters(
        latest, previous, compatible, stable_epsilon=stable_epsilon
    )
    return RagQualityGateInsights(
        comparison_state="ready" if previous else "insufficient_history",
        compatible_history_count=len(compatible),
        previous_run_id=previous.id if previous else None,
        metric_trends=trends,
        alerts=alerts,
        failure_clusters=clusters,
    )


def _metric_trends(
    latest: RagQualityGateRun,
    previous: RagQualityGateRun | None,
    *,
    stable_epsilon: float,
) -> tuple[RagQualityMetricTrend, ...]:
    if previous is None:
        return ()
    values = []
    for metric_id in sorted(set(latest.metrics) & set(previous.metrics)):
        current = latest.metrics[metric_id]
        before = previous.metrics[metric_id]
        delta = current - before
        direction = (
            "improved"
            if delta > stable_epsilon
            else "regressed"
            if delta < -stable_epsilon
            else "stable"
        )
        values.append(
            RagQualityMetricTrend(metric_id, current, before, delta, direction)
        )
    return tuple(values)


def _quality_alerts(
    latest: RagQualityGateRun,
    previous: RagQualityGateRun | None,
    trends: tuple[RagQualityMetricTrend, ...],
) -> tuple[RagQualityAlert, ...]:
    values: list[RagQualityAlert] = []
    if previous is not None and previous.status == "passed" and latest.status != "passed":
        values.append(
            RagQualityAlert(
                code="status_regressed",
                severity="critical",
                subject_id=latest.gate_id,
            )
        )
    for check in latest.checks:
        if check.get("passed") is False:
            values.append(
                RagQualityAlert(
                    code="check_failed",
                    severity="critical",
                    subject_id=str(check.get("check_id", "unknown")),
                    current=_number(check.get("actual")),
                )
            )
    for trend in trends:
        if trend.direction == "regressed":
            values.append(
                RagQualityAlert(
                    code="metric_regressed",
                    severity="warning",
                    subject_id=trend.metric_id,
                    current=trend.current,
                    previous=trend.previous,
                    delta=trend.delta,
                )
            )
    return tuple(values)


def _failure_clusters(
    latest: RagQualityGateRun,
    previous: RagQualityGateRun | None,
    compatible: tuple[RagQualityGateRun, ...],
    *,
    stable_epsilon: float,
) -> tuple[RagQualityFailureCluster, ...]:
    latest_checks = _failure_checks(latest)
    previous_checks = _failure_checks(previous) if previous else {}
    values = []
    for failure_type, check in latest_checks.items():
        latest_rate = _number(check.get("actual")) or 0.0
        latest_count = int(_number(check.get("failure_count")) or 0)
        threshold = _number(check.get("maximum")) or 0.0
        previous_check = previous_checks.get(failure_type)
        previous_rate = _number(previous_check.get("actual")) if previous_check else None
        rate_delta = latest_rate - previous_rate if previous_rate is not None else None
        occurrences = sum(
            1
            for run in compatible
            if (_number(_failure_checks(run).get(failure_type, {}).get("actual")) or 0) > 0
        )
        if latest_count == 0 and occurrences == 0:
            continue
        passed = check.get("passed") is True
        priority = (
            "critical"
            if not passed
            else "high"
            if rate_delta is not None and rate_delta > stable_epsilon
            else "medium"
            if latest_count > 0
            else "low"
        )
        values.append(
            RagQualityFailureCluster(
                failure_type=failure_type,
                priority=priority,
                latest_rate=latest_rate,
                latest_count=latest_count,
                previous_rate=previous_rate,
                rate_delta=rate_delta,
                occurrence_count=occurrences,
                threshold=threshold,
                check_passed=passed,
            )
        )
    values.sort(
        key=lambda value: (
            {"critical": 0, "high": 1, "medium": 2, "low": 3}[value.priority],
            -value.latest_rate,
            value.failure_type,
        )
    )
    return tuple(values)


def _failure_checks(run: RagQualityGateRun | None) -> dict[str, dict]:
    if run is None:
        return {}
    return {
        str(check["check_id"]).removeprefix("failure_rate:"): check
        for check in run.checks
        if str(check.get("check_id", "")).startswith("failure_rate:")
    }


def _number(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return float(value)


def _sanitize_check(value: object) -> dict:
    if not isinstance(value, dict) or not isinstance(value.get("check_id"), str) or not isinstance(value.get("passed"), bool):
        raise ValueError("质量门禁 check 无效")
    return {
        key: item
        for key, item in value.items()
        if key in _ALLOWED_CHECK_FIELDS
        and (item is None or isinstance(item, (str, int, float, bool)))
    }
