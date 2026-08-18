#!/usr/bin/env python3
"""通过真实 Gateway/Agent/RAG 链路执行 P0 问题集。"""

from __future__ import annotations

import argparse
import json
import re
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

EVAL_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_SUITE = EVAL_ROOT / "tasks" / "production-rag-p0-v1.json"


def run_suite(
    suite: dict,
    *,
    gateway_url: str,
    workspace_id: str,
    timeout_seconds: int,
    skip_ids: frozenset[str] = frozenset(),
) -> dict:
    results = []
    for question in suite["questions"]:
        if question["id"] in skip_ids:
            continue
        goal = question.get("user_goal") or (
            "请根据知识库中的《Attention Is All You Need》（arXiv 1706.03762）回答："
            f"{question['question']}"
        )
        started_at = time.monotonic()
        created = _request_json(
            f"{gateway_url}/api/tasks",
            method="POST",
            body={"user_goal": goal, "workspace_id": workspace_id},
        )["data"]
        task_id = created["task"]["id"]
        conversation_id = created["conversation"]["id"]
        task = _wait_for_task(
            gateway_url,
            task_id,
            timeout_seconds=timeout_seconds,
        )
        answer = ""
        if task["status"] == "completed":
            conversation = _request_json(
                f"{gateway_url}/api/conversations/{conversation_id}?limit=50"
            )["data"]
            assistants = [
                value for value in conversation["messages"] if value.get("role") == "assistant"
            ]
            answer = assistants[-1]["content"] if assistants else ""
        metrics = _score_answer(question, answer, status=task["status"])
        results.append(
            {
                "question_id": question["id"],
                "question": question["question"],
                "expected_facts": question.get("expected_facts", []),
                "expected_answer": question.get("expected_answer"),
                "category": question.get("category", "single_fact"),
                "task_id": task_id,
                "run_id": created["run"]["id"],
                "conversation_id": conversation_id,
                "status": task["status"],
                "answer": answer,
                "duration_ms": round((time.monotonic() - started_at) * 1000),
                "metrics": metrics,
                "failure_type": _failure_type(task["status"], metrics),
            }
        )
    return _build_report(suite, results, workspace_id=workspace_id, gateway_url=gateway_url)


def recover_suite(
    suite: dict,
    *,
    gateway_url: str,
    workspace_id: str,
) -> dict:
    """从 Gateway 业务真源恢复最近一次同目标运行，不重新创建任务。"""
    tasks = _request_json(f"{gateway_url}/api/tasks?limit=100")["data"]["tasks"]
    results = []
    for question in suite["questions"]:
        goal = question.get("user_goal") or (
            "请根据知识库中的《Attention Is All You Need》（arXiv 1706.03762）回答："
            f"{question['question']}"
        )
        task = next(
            (
                value
                for value in tasks
                if value.get("workspace_id") == workspace_id
                and value.get("user_goal") == goal
                and value.get("status") in {"completed", "failed", "cancelled"}
            ),
            None,
        )
        if task is None:
            raise ValueError(f"没有可恢复的任务: {question['id']}")
        conversation = _request_json(
            f"{gateway_url}/api/conversations/{task['conversation_id']}?limit=50"
        )["data"]
        assistants = [
            value for value in conversation["messages"] if value.get("role") == "assistant"
        ]
        answer = assistants[-1]["content"] if assistants else ""
        metrics = _score_answer(question, answer, status=task["status"])
        results.append(
            {
                "question_id": question["id"],
                "question": question["question"],
                "expected_facts": question.get("expected_facts", []),
                "expected_answer": question.get("expected_answer"),
                "category": question.get("category", "single_fact"),
                "task_id": task["id"],
                "run_id": task.get("active_run_id", ""),
                "conversation_id": task["conversation_id"],
                "status": task["status"],
                "answer": answer,
                "duration_ms": _duration_ms(task.get("created_at"), task.get("updated_at")),
                "metrics": metrics,
                "failure_type": _failure_type(task["status"], metrics),
            }
        )
    return _build_report(suite, results, workspace_id=workspace_id, gateway_url=gateway_url)


def _build_report(suite: dict, results: list[dict], *, workspace_id: str, gateway_url: str) -> dict:
    status_counts: dict[str, int] = {}
    for result in results:
        status = result["status"]
        status_counts[status] = status_counts.get(status, 0) + 1
    metric_values: dict[str, list[float]] = {}
    for result in results:
        for metric_id, value in result["metrics"].items():
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                metric_values.setdefault(metric_id, []).append(float(value))
    return {
        "schema_version": 2,
        "suite_id": suite["suite_id"],
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "workspace_id": workspace_id,
        "gateway_url": gateway_url,
        "summary": {
            "attempted": len(results),
            "status_counts": status_counts,
            "metrics": {
                metric_id: sum(values) / len(values)
                for metric_id, values in sorted(metric_values.items())
            },
            "failure_counts": _count_failures(results),
        },
        "results": results,
    }


def _duration_ms(created_at: str | None, updated_at: str | None) -> int:
    if not created_at or not updated_at:
        return 0
    return max(
        round(
            (
                datetime.fromisoformat(updated_at)
                - datetime.fromisoformat(created_at)
            ).total_seconds()
            * 1000
        ),
        0,
    )


def _wait_for_task(gateway_url: str, task_id: str, *, timeout_seconds: int) -> dict:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        task = _request_json(f"{gateway_url}/api/tasks/{task_id}")["data"]["task"]
        if task["status"] in {"completed", "failed", "cancelled"}:
            return task
        time.sleep(1)
    raise TimeoutError(f"任务超时: {task_id}")


def _request_json(url: str, *, method: str = "GET", body: dict | None = None) -> dict:
    data = json.dumps(body, ensure_ascii=False).encode("utf-8") if body else None
    request = urllib.request.Request(
        url,
        data=data,
        method=method,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            value = json.loads(response.read().decode("utf-8"))
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"Gateway 请求失败: {url}: {exc}") from exc
    if not value.get("ok"):
        raise RuntimeError(f"Gateway 返回失败: {url}")
    return value


def _load_suite(path: Path) -> dict:
    value = json.loads(path.read_text(encoding="utf-8"))
    if value.get("schema_version") not in {1, 2} or not value.get("questions"):
        raise ValueError("production RAG suite schema 无效")
    ids = [question.get("id") for question in value["questions"]]
    if any(not value for value in ids) or len(set(ids)) != len(ids):
        raise ValueError("P0 question id 必须非空且唯一")
    for question in value["questions"]:
        if not isinstance(question.get("question"), str) or not question["question"].strip():
            raise ValueError("P0 question 必须是非空字符串")
        _validate_expected_answer(question)
    return value


_CITATION_PATTERN = re.compile(r"`chunk:([0-9a-fA-F-]{36})`")
_REFUSAL_MARKERS = (
    "无法确认",
    "无法依据",
    "无法基于",
    "无法直接定位",
    "证据不足",
    "没有提供",
    "没有提到",
    "未提及",
    "不能回答",
    "请明确",
    "请确认",
    "请指定",
)


def _score_answer(question: dict, answer: str, *, status: str) -> dict[str, float]:
    expected = question.get("expected_answer", {})
    answer_mode = expected.get("mode", "answerable")
    normalized = _normalize(answer)
    citations = set(_CITATION_PATTERN.findall(answer))
    fact_groups = expected.get("fact_groups")
    if fact_groups is None:
        fact_groups = [[value] for value in question.get("expected_facts", [])]
    matched = sum(
        1
        for alternatives in fact_groups
        if any(_normalize(value) in normalized for value in alternatives)
    )
    fact_coverage = matched / len(fact_groups) if fact_groups else 1.0
    refusal = any(marker in answer for marker in _REFUSAL_MARKERS)
    if answer_mode == "answerable":
        answer_correctness = float(
            status == "completed" and fact_coverage == 1.0 and bool(citations)
        )
        citation_completeness = min(
            len(citations) / max(int(expected.get("min_citations", 1)), 1), 1.0
        )
    else:
        answer_correctness = float(
            status == "completed" and not citations and refusal
        )
        citation_completeness = float(not citations)
    return {
        "answer_correctness": answer_correctness,
        "fact_coverage": fact_coverage,
        "citation_completeness": citation_completeness,
        "citation_count": float(len(citations)),
    }


def _normalize(value: str) -> str:
    return re.sub(r"[\s`*_{}\\·（）()，,。:：=\-]", "", value).casefold()


def _validate_expected_answer(question: dict) -> None:
    expected = question.get("expected_answer")
    if expected is None:
        facts = question.get("expected_facts")
        if (
            not isinstance(facts, list)
            or not facts
            or any(not isinstance(fact, str) or not fact.strip() for fact in facts)
        ):
            raise ValueError("expected_facts 必须是非空字符串数组")
        return
    if not isinstance(expected, dict) or expected.get("mode") not in {
        "answerable",
        "unanswerable",
        "clarification",
    }:
        raise ValueError("expected_answer.mode 无效")
    groups = expected.get("fact_groups", [])
    if expected["mode"] == "answerable" and (
        not isinstance(groups, list)
        or not groups
        or any(
            not isinstance(group, list)
            or not group
            or any(not isinstance(value, str) or not value.strip() for value in group)
            for group in groups
        )
    ):
        raise ValueError("answerable expected_answer.fact_groups 无效")


def _failure_type(status: str, metrics: dict[str, float]) -> str | None:
    if status != "completed":
        return "runtime_or_tool_failure"
    if metrics["citation_completeness"] < 1:
        return "citation_incomplete"
    if metrics["fact_coverage"] < 1:
        return "answer_fact_missing"
    if metrics["answer_correctness"] < 1:
        return "answer_behavior_mismatch"
    return None


def _count_failures(results: list[dict]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for result in results:
        failure = result["failure_type"]
        if failure:
            counts[failure] = counts.get(failure, 0) + 1
    return counts


def main() -> int:
    parser = argparse.ArgumentParser(description="执行真实生产 RAG P0 问题集")
    parser.add_argument("--suite", type=Path, default=DEFAULT_SUITE)
    parser.add_argument("--gateway-url", default="http://127.0.0.1:8080")
    parser.add_argument("--workspace-id", required=True)
    parser.add_argument("--timeout-seconds", type=int, default=120)
    parser.add_argument("--skip-id", action="append", default=[])
    parser.add_argument(
        "--reuse-existing",
        action="store_true",
        help="按自然用户目标从 Gateway 恢复最近的终态任务，不创建新任务",
    )
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if not 10 <= args.timeout_seconds <= 600:
        parser.error("--timeout-seconds 必须在 10..600")
    suite = _load_suite(args.suite)
    try:
        if args.reuse_existing:
            report = recover_suite(
                suite,
                gateway_url=args.gateway_url.rstrip("/"),
                workspace_id=args.workspace_id,
            )
        else:
            report = run_suite(
                suite,
                gateway_url=args.gateway_url.rstrip("/"),
                workspace_id=args.workspace_id,
                timeout_seconds=args.timeout_seconds,
                skip_ids=frozenset(args.skip_id),
            )
    except (OSError, RuntimeError, TimeoutError, ValueError) as exc:
        print(json.dumps({"status": "failed", "error": str(exc)}, ensure_ascii=False))
        return 1
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    output = args.output or EVAL_ROOT / "reports" / "production-p0" / timestamp / "run.json"
    output.parent.mkdir(parents=True, exist_ok=True)
    if output.exists():
        print(json.dumps({"status": "failed", "error": f"输出已存在: {output}"}, ensure_ascii=False))
        return 1
    output.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(
        json.dumps(
            {"status": "completed", "count": len(report["results"]), "output": str(output)},
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
