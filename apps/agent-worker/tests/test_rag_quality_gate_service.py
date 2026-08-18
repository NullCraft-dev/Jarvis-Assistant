from dataclasses import replace
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from jarvis_worker.agent.rag.evaluation.contracts import RagQualityGateRun, RagQualityIssue
from jarvis_worker.agent.rag.evaluation.gate_service import (
    _sanitize_failure_targets,
    _sanitize_check,
    build_quality_gate_insights,
    apply_quality_issue_update,
    _sync_quality_issues,
)


def test_quality_issue_requires_explicit_processing_before_regression_verification():
    now = datetime.now(UTC)
    issue = RagQualityIssue(
        id=uuid4(), candidate_id="a" * 64, trace_id=uuid4(), gate_id="gate-v1",
        cohort_id="cohort-v1", failure_type="candidate_evidence_missed",
        owner="candidate_recall", status="open", occurrence_count=1,
        first_seen_run_id=uuid4(), last_seen_run_id=uuid4(), created_at=now, updated_at=now,
    )
    processing = apply_quality_issue_update(issue, owner="candidate_recall", status="in_progress", resolution_note="", now=now)
    resolved = apply_quality_issue_update(processing, owner="candidate_recall", status="resolved", resolution_note="修复候选召回过滤", now=now)
    assert resolved.status == "resolved"
    assert resolved.version == 3
    with pytest.raises(ValueError, match="不能从 open"):
        apply_quality_issue_update(issue, owner="candidate_recall", status="resolved", resolution_note="跳过处理", now=now)
    with pytest.raises(ValueError, match="必须填写"):
        apply_quality_issue_update(processing, owner="candidate_recall", status="resolved", resolution_note="", now=now)


@pytest.mark.asyncio
async def test_next_comparable_gate_verifies_absent_resolved_issue():
    now = datetime.now(UTC)
    issue = RagQualityIssue(
        id=uuid4(), candidate_id="a" * 64, trace_id=uuid4(), gate_id="gate-v1",
        cohort_id="cohort-v1", failure_type="candidate_evidence_missed",
        owner="candidate_recall", status="resolved", occurrence_count=1,
        first_seen_run_id=uuid4(), last_seen_run_id=uuid4(), resolution_note="fixed",
        version=2, created_at=now, updated_at=now,
    )
    repository = _FakeIssueRepository(issue)
    run = _run(candidate_recall=1.0, cohort_id="cohort-v1")
    run = replace(run, gate_id="gate-v1", generated_at=now + timedelta(microseconds=1))

    await _sync_quality_issues(type("Tx", (), {"rag_quality_issues": repository})(), run)

    assert repository.issue.status == "verified"
    assert repository.issue.verified_run_id == run.id


class _FakeIssueRepository:
    def __init__(self, issue): self.issue = issue
    async def get_by_candidate_id(self, candidate_id): return self.issue if self.issue.candidate_id == candidate_id else None
    async def create(self, issue): self.issue = issue; return issue
    async def list_resolved(self, *, gate_id, cohort_id): return [self.issue] if self.issue.status == "resolved" and self.issue.gate_id == gate_id and self.issue.cohort_id == cohort_id else []
    async def save(self, issue, *, expected_version): self.issue = issue; return issue


def test_quality_gate_failure_target_projection_is_redacted_and_bounded():
    values = _sanitize_failure_targets([{
        "trace_id": "11111111-1111-4111-8111-111111111111",
        "query_hash": "a" * 64,
        "query": "private query",
        "failures": [{
            "candidate_id": "b" * 64,
            "failure_type": "candidate_evidence_missed",
            "suspected_stage": "candidate_recall",
            "severity": "high",
            "metric_ids": ["candidate.recall@5"],
            "chunk_content": "private evidence",
        }],
    }])

    assert values == ({
        "candidate_id": "b" * 64,
        "trace_id": "11111111-1111-4111-8111-111111111111",
        "query_hash": "a" * 64,
        "failure_type": "candidate_evidence_missed",
        "suspected_stage": "candidate_recall",
        "severity": "high",
        "metric_ids": ["candidate.recall@5"],
    },)
    assert "query" not in values[0]
    assert "chunk_content" not in values[0]


def test_quality_gate_check_projection_drops_unknown_and_sensitive_fields():
    value = _sanitize_check(
        {
            "check_id": "metric:candidate.recall@5",
            "passed": True,
            "actual": 0.9,
            "required_minimum": 0.85,
            "query": "private query",
            "report_path": "/private/report.json",
        }
    )

    assert value == {
        "check_id": "metric:candidate.recall@5",
        "passed": True,
        "actual": 0.9,
        "required_minimum": 0.85,
    }


@pytest.mark.parametrize("value", [{}, {"check_id": "x", "passed": "yes"}])
def test_quality_gate_check_projection_rejects_invalid_shape(value):
    with pytest.raises(ValueError, match="check"):
        _sanitize_check(value)


def test_quality_gate_insights_require_compatible_history_and_prioritize_failures():
    latest = _run(
        candidate_recall=0.9,
        candidate_failure_rate=0.2,
        candidate_failure_count=2,
    )

    insights = build_quality_gate_insights((latest,))

    assert insights.comparison_state == "insufficient_history"
    assert insights.metric_trends == ()
    assert insights.alerts == ()
    assert len(insights.failure_clusters) == 1
    assert insights.failure_clusters[0].failure_type == "candidate_evidence_missed"
    assert insights.failure_clusters[0].priority == "medium"


def test_quality_gate_insights_detect_metric_regression_and_increasing_failure_cluster():
    latest = _run(
        candidate_recall=0.85,
        candidate_failure_rate=0.3,
        candidate_failure_count=3,
    )
    previous = _run(
        candidate_recall=0.9,
        candidate_failure_rate=0.2,
        candidate_failure_count=2,
    )

    insights = build_quality_gate_insights((latest, previous))

    assert insights.comparison_state == "ready"
    assert insights.previous_run_id == previous.id
    assert insights.metric_trends[0].direction == "regressed"
    assert insights.metric_trends[0].delta == pytest.approx(-0.05)
    assert [(value.code, value.severity) for value in insights.alerts] == [
        ("metric_regressed", "warning")
    ]
    assert insights.failure_clusters[0].priority == "high"
    assert insights.failure_clusters[0].rate_delta == pytest.approx(0.1)
    assert insights.failure_clusters[0].occurrence_count == 2


def test_quality_gate_insights_do_not_compare_different_cohorts():
    latest = _run(candidate_recall=0.8, cohort_id="cohort-v2")
    previous = _run(candidate_recall=1.0, cohort_id="cohort-v1")

    insights = build_quality_gate_insights((latest, previous))

    assert insights.comparison_state == "insufficient_history"
    assert insights.compatible_history_count == 1
    assert insights.metric_trends == ()


def _run(
    *,
    candidate_recall: float,
    candidate_failure_rate: float = 0,
    candidate_failure_count: int = 0,
    cohort_id: str = "cohort-v1",
) -> RagQualityGateRun:
    return RagQualityGateRun(
        id=uuid4(),
        gate_id="rag-release-v1",
        cohort_id=cohort_id,
        baseline_id="baseline-v1",
        revision="a" * 40,
        status="passed",
        sample_count=10,
        metrics={"candidate.recall@5": candidate_recall},
        checks=(
            {
                "check_id": "failure_rate:candidate_evidence_missed",
                "passed": True,
                "actual": candidate_failure_rate,
                "failure_count": candidate_failure_count,
                "maximum": 0.35,
            },
        ),
        generated_at=datetime.now(UTC),
    )
