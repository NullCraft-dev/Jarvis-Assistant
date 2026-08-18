from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

EVAL_ROOT = Path(__file__).resolve().parents[1]
for path in (EVAL_ROOT, EVAL_ROOT / "runners"):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from framework import (  # noqa: E402
    RagEvaluationSample,
    RankedChunk,
    evaluate_labeled_trace,
)
from run_production_trace_eval import _report, _write_report  # noqa: E402


def test_production_report_excludes_raw_query_and_vectors(tmp_path):
    sample = RagEvaluationSample(
        trace_id="trace-1",
        query_id="label-1",
        query="private user question",
        positive_chunk_ids=frozenset({"positive"}),
        candidate_ranking=(RankedChunk("positive", 1, 0.9),),
        reranked_ranking=(RankedChunk("positive", 1, 0.95),),
        context_chunk_ids=("positive",),
    )
    outcome = evaluate_labeled_trace(sample, cutoffs=(1,))
    trace = SimpleNamespace(query_hash="a" * 64)

    report = _report([outcome], [(trace, SimpleNamespace())])
    output_dir = tmp_path / "report"
    _write_report(report, output_dir)

    serialized = (output_dir / "report.json").read_text(encoding="utf-8")
    assert report["sample_count"] == 1
    assert report["aggregate_metrics"]["candidate.recall@1"] == 1.0
    assert "private user question" not in serialized
    assert '"raw_query_included": false' in serialized
    assert (output_dir / "report.md").exists()
