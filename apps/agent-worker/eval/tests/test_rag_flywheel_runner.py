from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest

EVAL_ROOT = Path(__file__).resolve().parents[1]
RUNNERS_ROOT = EVAL_ROOT / "runners"
for path in (EVAL_ROOT, RUNNERS_ROOT):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from run_rag_flywheel import (  # noqa: E402
    _baseline_report,
    _load_cohort,
    _request_from_trace,
    _write_snapshot,
)


def test_baseline_accepts_previous_flywheel_snapshot_or_report():
    report = {"aggregate_metrics": {"candidate.recall@5": 0.9}}

    assert _baseline_report(report) is report
    assert _baseline_report({"release_report": report}) is report
    assert _baseline_report(None) is None

    with pytest.raises(ValueError, match="aggregate_metrics"):
        _baseline_report({"schema_version": 1})


def test_snapshot_writer_keeps_review_queue_redacted(tmp_path):
    output = tmp_path / "report"
    snapshot = {
        "release_gate": {
            "status": "insufficient_evidence",
            "checks": [
                {
                    "check_id": "minimum_sample_count",
                    "passed": False,
                    "actual": 0,
                    "required": 10,
                }
            ],
        },
        "observation_report": {"sample_count": 1},
        "release_report": {"sample_count": 0},
        "review_queue": [
            {
                "trace_id": "trace-1",
                "priority": 4,
                "privacy_status": "pending",
                "label_status": None,
                "reasons": ["privacy_review_required"],
            }
        ],
        "feedback_summary": {"total": 2, "by_status": {"pending": 1, "reviewed": 1}, "by_failure_category": {"answer_generation": 1}},
    }

    _write_snapshot(snapshot, output)

    rendered = (output / "flywheel.md").read_text(encoding="utf-8")
    assert "insufficient_evidence" in rendered
    assert "privacy_review_required" in rendered
    assert "private production query" not in rendered
    assert "answer_generation" in rendered


def test_replay_reconstructs_bounded_production_request():
    document_id = uuid4()
    trace = SimpleNamespace(
        query="current pipeline replay",
        request={
            "top_k": 5,
            "candidate_limit": 40,
            "feature_limit": 25,
            "cross_encoder_limit": 12,
            "diversity_limit": 8,
            "token_budget": 3000,
            "document_ids": [str(document_id)],
            "min_score": 0.2,
            "neighbor_window": 2,
            "max_chunks_per_document": 4,
        },
    )

    request = _request_from_trace(trace)

    assert request.query == "current pipeline replay"
    assert request.document_ids == (document_id,)
    assert request.candidate_limit == 40
    assert request.token_budget == 3000


def test_versioned_cohort_loader_rejects_duplicates(tmp_path):
    trace_id = str(uuid4())
    path = tmp_path / "cohort.json"
    path.write_text(
        __import__("json").dumps(
            {
                "schema_version": 1,
                "samples": [
                    {"trace_id": trace_id, "query_hash": "a" * 64},
                    {"trace_id": trace_id, "query_hash": "a" * 64},
                ],
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="重复"):
        _load_cohort(path)
