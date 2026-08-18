from __future__ import annotations

import sys
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest

EVAL_ROOT = Path(__file__).resolve().parents[1]
if str(EVAL_ROOT) not in sys.path:
    sys.path.insert(0, str(EVAL_ROOT))

from framework import (  # noqa: E402
    FlywheelGatePolicy,
    build_review_queue,
    evaluate_regression_gate,
)


def _policy(*, minimum_sample_count: int = 2) -> FlywheelGatePolicy:
    return FlywheelGatePolicy.from_dict(
        {
            "schema_version": 1,
            "gate_id": "test-gate",
            "minimum_sample_count": minimum_sample_count,
            "metrics": {
                "candidate.recall@5": {
                    "minimum": 0.8,
                    "maximum_regression": 0.05,
                },
                "context.evidence_recall": {"minimum": 1.0},
            },
            "maximum_failure_rates": {"candidate_evidence_missed": 0.5},
        }
    )


def _report(*, samples: int, recall: float, failures: int = 0) -> dict:
    return {
        "sample_count": samples,
        "aggregate_metrics": {
            "candidate.recall@5": recall,
            "context.evidence_recall": 1.0,
        },
        "failure_counts": {"candidate_evidence_missed": failures},
        "samples": [],
    }


def test_release_gate_passes_absolute_and_baseline_thresholds():
    result = evaluate_regression_gate(
        _report(samples=2, recall=0.91),
        _policy(),
        baseline=_report(samples=2, recall=0.95),
    )

    assert result["status"] == "passed"
    assert result["failed_checks"] == []


def test_release_gate_never_passes_without_enough_promoted_evidence():
    result = evaluate_regression_gate(_report(samples=0, recall=1.0), _policy())

    assert result["status"] == "insufficient_evidence"
    assert "minimum_sample_count" in result["failed_checks"]


def test_release_gate_blocks_metric_regression_and_failure_rate():
    result = evaluate_regression_gate(
        _report(samples=2, recall=0.84, failures=2),
        _policy(),
        baseline=_report(samples=2, recall=0.95),
    )

    assert result["status"] == "blocked"
    assert "metric:candidate.recall@5" in result["failed_checks"]
    assert "failure_rate:candidate_evidence_missed" in result["failed_checks"]


def test_review_queue_is_prioritized_and_redacted():
    trace_id = uuid4()
    trace = SimpleNamespace(
        id=trace_id,
        query="must not leak",
        query_hash="a" * 64,
        created_at=datetime.now(UTC),
        privacy_status="pending",
        result_count=0,
        context_truncated=False,
        pipeline_versions={},
    )
    report = {
        "samples": [
            {
                "trace_id": str(trace_id),
                "failures": [{"failure_type": "candidate_evidence_missed"}],
            }
        ]
    }

    queue = build_review_queue([(trace, None)], report)

    assert queue[0]["priority"] == 4
    assert queue[0]["reasons"] == [
        "privacy_review_required",
        "empty_context",
        "failure:candidate_evidence_missed",
    ]
    assert "query" not in queue[0]
    assert "must not leak" not in str(queue)


def test_policy_rejects_invalid_ratio():
    value = {
        "schema_version": 1,
        "gate_id": "invalid",
        "minimum_sample_count": 1,
        "metrics": {"candidate.recall@5": {"minimum": 1.1}},
    }

    with pytest.raises(ValueError, match="0..1"):
        FlywheelGatePolicy.from_dict(value)
