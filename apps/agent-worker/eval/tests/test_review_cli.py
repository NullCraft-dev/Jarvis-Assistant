from __future__ import annotations

import json
import sys
from pathlib import Path
from uuid import uuid4

EVAL_ROOT = Path(__file__).resolve().parents[1]
for path in (EVAL_ROOT, EVAL_ROOT / "runners"):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from review_production_traces import (  # noqa: E402
    _review_data,
    _uuid_values,
    _write_promotion_candidate,
    build_parser,
)
from jarvis_worker.agent.rag.contracts import RagChunk  # noqa: E402
from jarvis_worker.agent.rag.evaluation.contracts import (  # noqa: E402
    RagEvaluationLabel,
    RagEvaluationTrace,
)
from jarvis_worker.agent.rag.evaluation.review_service import (  # noqa: E402
    RagEvaluationReview,
)


def _review() -> RagEvaluationReview:
    workspace_id, document_id, chunk_id = uuid4(), uuid4(), uuid4()
    trace = RagEvaluationTrace(
        id=uuid4(),
        workspace_id=workspace_id,
        task_id=uuid4(),
        run_id=uuid4(),
        step_id=uuid4(),
        query="private production query",
        query_hash="a" * 64,
        request={"top_k": 5},
        pipeline_versions={"retriever": "hybrid-v1"},
        candidate_ranking=(
            {
                "chunk_id": str(chunk_id),
                "document_id": str(document_id),
                "rank": 1,
                "score": 0.9,
                "content_hash": "b" * 64,
                "sources": ["semantic"],
            },
        ),
        reranked_ranking=(),
        context_chunk_ids=(chunk_id,),
        context_truncated=False,
        result_count=1,
        privacy_status="approved",
    )
    label = RagEvaluationLabel(
        id=uuid4(),
        trace_id=trace.id,
        positive_chunk_ids=(chunk_id,),
        status="promoted",
    )
    chunk = RagChunk(
        id=chunk_id,
        document_id=document_id,
        ingestion_job_id=uuid4(),
        workspace_id=workspace_id,
        ordinal=2,
        content="secret chunk body",
        content_hash="b" * 64,
        token_count=4,
        source_locator={"page_start": 3},
    )
    return RagEvaluationReview(trace, label, (chunk,))


def test_review_cli_parser_and_uuid_values():
    trace_id, positive = uuid4(), uuid4()
    args = build_parser().parse_args(
        ["label", str(trace_id), "--positive", str(positive), "--status", "draft"]
    )

    assert args.command == "label"
    assert _uuid_values(args.positive, "positive") == (positive,)

    chunks = build_parser().parse_args(
        ["chunks", str(trace_id), str(uuid4()), "--limit", "50"]
    )
    assert chunks.command == "chunks"
    assert chunks.limit == 50


def test_inspect_marks_context_and_label_with_bounded_preview():
    data = _review_data(_review(), preview_chars=6, redact_query=True)
    candidate = data["candidate_ranking"][0]

    assert data["query"] == "<redacted>"
    assert candidate["in_context"] is True
    assert candidate["label"] == "positive"
    assert candidate["chunk"]["content_preview"] == "secret…"


def test_promotion_candidate_excludes_raw_chunk_content(tmp_path):
    output = tmp_path / "candidate.json"
    _write_promotion_candidate(_review(), output)
    value = json.loads(output.read_text(encoding="utf-8"))

    assert value["query"] == "private production query"
    assert value["raw_chunk_content_included"] is False
    assert "secret chunk body" not in output.read_text(encoding="utf-8")
