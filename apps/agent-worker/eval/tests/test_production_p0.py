from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

EVAL_ROOT = Path(__file__).resolve().parents[1]
RUNNERS_ROOT = EVAL_ROOT / "runners"
if str(RUNNERS_ROOT) not in sys.path:
    sys.path.insert(0, str(RUNNERS_ROOT))

import run_production_p0 as runner  # noqa: E402


def test_load_default_p0_suite_has_unique_real_questions():
    suite = runner._load_suite(runner.DEFAULT_SUITE)

    assert suite["suite_id"] == "production-rag-p0-v1"
    assert len(suite["questions"]) == 12
    assert len({question["id"] for question in suite["questions"]}) == 12
    assert all(question["expected_facts"] for question in suite["questions"])


def test_load_suite_rejects_invalid_expected_facts(tmp_path):
    path = tmp_path / "suite.json"
    path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "questions": [{"id": "q1", "question": "question", "expected_facts": []}],
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="expected_facts"):
        runner._load_suite(path)


def test_run_suite_uses_gateway_and_preserves_terminal_failures(monkeypatch):
    suite = {
        "suite_id": "suite",
        "questions": [
            {"id": "ok", "question": "Q1", "expected_facts": ["A1"]},
            {"id": "failed", "question": "Q2", "expected_facts": ["A2"]},
        ],
    }
    created = iter(
        [
            {
                "task": {"id": "task-1"},
                "run": {"id": "run-1"},
                "conversation": {"id": "conversation-1"},
            },
            {
                "task": {"id": "task-2"},
                "run": {"id": "run-2"},
                "conversation": {"id": "conversation-2"},
            },
        ]
    )

    def request_json(url, *, method="GET", body=None):
        if method == "POST":
            assert url == "http://gateway/api/tasks"
            assert body["workspace_id"] == "workspace-1"
            assert "Attention Is All You Need" in body["user_goal"]
            assert "rag.search" not in body["user_goal"]
            return {"data": next(created)}
        assert url.endswith("/api/conversations/conversation-1?limit=50")
        return {
            "data": {
                "messages": [
                    {"role": "user", "content": "Q1"},
                    {"role": "assistant", "content": "A1"},
                ]
            }
        }

    statuses = iter([{"status": "completed"}, {"status": "failed"}])
    monkeypatch.setattr(runner, "_request_json", request_json)
    monkeypatch.setattr(runner, "_wait_for_task", lambda *args, **kwargs: next(statuses))

    report = runner.run_suite(
        suite,
        gateway_url="http://gateway",
        workspace_id="workspace-1",
        timeout_seconds=30,
    )

    assert report["summary"]["attempted"] == 2
    assert report["summary"]["status_counts"] == {"completed": 1, "failed": 1}
    assert report["results"][0]["answer"] == "A1"
    assert report["results"][1]["answer"] == ""
    assert report["results"][1]["run_id"] == "run-2"


def test_score_answer_records_fact_citation_and_refusal_metrics():
    answerable = {
        "expected_answer": {
            "mode": "answerable",
            "min_citations": 2,
            "fact_groups": [["残差", "residual"], ["参数量"]],
        }
    }
    answer = (
        "残差连接降低优化难度，并减少参数量。\n\n引用：\n"
        "- [1] A (`chunk:11111111-1111-1111-1111-111111111111`)\n"
        "- [2] B (`chunk:22222222-2222-2222-2222-222222222222`)"
    )

    metrics = runner._score_answer(answerable, answer, status="completed")

    assert metrics == {
        "answer_correctness": 1.0,
        "fact_coverage": 1.0,
        "citation_completeness": 1.0,
        "citation_count": 2.0,
    }
    refusal = runner._score_answer(
        {"expected_answer": {"mode": "unanswerable", "fact_groups": []}},
        "当前资料未提及该信息，无法确认。",
        status="completed",
    )
    assert refusal["answer_correctness"] == 1.0
    assert refusal["citation_completeness"] == 1.0
    assert runner._normalize("2i/d_{model}") == runner._normalize("2i/dmodel")


def test_load_p4_suite_covers_required_quality_categories():
    suite = runner._load_suite(EVAL_ROOT / "tasks" / "production-rag-p4-v1.json")

    assert {question["category"] for question in suite["questions"]} == {
        "multi_document",
        "table",
        "formula",
        "long_document",
        "unanswerable",
        "conflicting_evidence",
        "same_title",
    }
    assert all("rag.search" not in question["user_goal"] for question in suite["questions"])
