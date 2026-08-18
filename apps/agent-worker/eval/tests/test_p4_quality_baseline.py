from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

EVAL_ROOT = Path(__file__).resolve().parents[1]
RUNNERS_ROOT = EVAL_ROOT / "runners"
if str(RUNNERS_ROOT) not in sys.path:
    sys.path.insert(0, str(RUNNERS_ROOT))

import run_p4_quality_baseline as baseline  # noqa: E402


def _trace(chunk_id: str, *, created_at: datetime):
    return SimpleNamespace(
        candidate_ranking=({"chunk_id": chunk_id},),
        reranked_ranking=(),
        context_chunk_ids=(),
        created_at=created_at,
    )


def test_select_trace_prefers_citation_overlap_then_latest():
    now = datetime.now(timezone.utc)
    older_match = _trace("11111111-1111-1111-1111-111111111111", created_at=now)
    latest_miss = _trace("22222222-2222-2222-2222-222222222222", created_at=now)

    selected = baseline._select_trace(
        [latest_miss, older_match],
        ("11111111-1111-1111-1111-111111111111",),
    )

    assert selected is older_match
    assert baseline._select_trace([], ()) is None


def test_load_report_requires_schema_v2(tmp_path):
    valid = tmp_path / "valid.json"
    valid.write_text(json.dumps({"schema_version": 2, "results": []}), encoding="utf-8")

    assert baseline._load_report(valid)["results"] == []
    assert baseline._percentile([10, 20, 30], 0.95) == 30
