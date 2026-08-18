#!/usr/bin/env python3
"""校验 Jarvis MVP RC1 真实用户旅程证据。"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

JOURNEY_IDS = (
    "conversation_no_tool",
    "workspace_read",
    "workspace_create_allow_deny",
    "rag_ingest_retrieve",
    "rag_to_knowledge",
    "pause_resume_cancel_retry",
    "service_restart_recovery",
    "redis_state_loss_recovery",
)

EVIDENCE_CATEGORIES = (
    "user_visible_result",
    "task_run",
    "steps",
    "tools",
    "permissions",
    "audit_logs",
    "events",
    "artifacts",
)

REQUIRED_VERIFIED: dict[str, frozenset[str]] = {
    "conversation_no_tool": frozenset({"user_visible_result", "task_run", "steps", "events"}),
    "workspace_read": frozenset(
        {"user_visible_result", "task_run", "steps", "tools", "audit_logs", "events"}
    ),
    "workspace_create_allow_deny": frozenset(EVIDENCE_CATEGORIES),
    "rag_ingest_retrieve": frozenset(EVIDENCE_CATEGORIES),
    "rag_to_knowledge": frozenset(EVIDENCE_CATEGORIES),
    "pause_resume_cancel_retry": frozenset(
        {"user_visible_result", "task_run", "steps", "audit_logs", "events"}
    ),
    "service_restart_recovery": frozenset(
        {"user_visible_result", "task_run", "steps", "audit_logs", "events"}
    ),
    "redis_state_loss_recovery": frozenset(
        {"user_visible_result", "task_run", "steps", "audit_logs", "events"}
    ),
}


class EvidenceValidationError(ValueError):
    """RC1 evidence 不满足发布门禁。"""


def _non_empty_strings(value: Any, field: str) -> list[str]:
    if not isinstance(value, list) or any(
        not isinstance(item, str) or not item.strip() for item in value
    ):
        raise EvidenceValidationError(f"{field} 必须是非空字符串数组（数组本身可为空）")
    return value


def _validate_timestamp(value: Any) -> None:
    if not isinstance(value, str) or not value.strip():
        raise EvidenceValidationError("executed_at 必须是 ISO-8601 字符串")
    try:
        datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise EvidenceValidationError("executed_at 不是合法 ISO-8601 时间") from exc


def validate_report(
    report: Any,
    *,
    allow_pending: bool = False,
    expected_revision: str | None = None,
) -> None:
    if not isinstance(report, dict):
        raise EvidenceValidationError("证据根节点必须是 object")
    if report.get("schema_version") != 1:
        raise EvidenceValidationError("schema_version 必须为 1")
    for field in ("release_candidate", "revision", "environment"):
        if not isinstance(report.get(field), str) or not report[field].strip():
            raise EvidenceValidationError(f"{field} 必须是非空字符串")
    if expected_revision is not None and report["revision"] != expected_revision:
        raise EvidenceValidationError(
            f"revision 与当前候选不一致: evidence={report['revision']} expected={expected_revision}"
        )
    _validate_timestamp(report.get("executed_at"))

    journeys = report.get("journeys")
    if not isinstance(journeys, list):
        raise EvidenceValidationError("journeys 必须是数组")
    by_id: dict[str, dict[str, Any]] = {}
    for index, journey in enumerate(journeys):
        if not isinstance(journey, dict):
            raise EvidenceValidationError(f"journeys[{index}] 必须是 object")
        journey_id = journey.get("id")
        if journey_id not in JOURNEY_IDS:
            raise EvidenceValidationError(f"journeys[{index}].id 未知: {journey_id!r}")
        if journey_id in by_id:
            raise EvidenceValidationError(f"journey id 重复: {journey_id}")
        by_id[journey_id] = journey

    missing = [journey_id for journey_id in JOURNEY_IDS if journey_id not in by_id]
    if missing:
        raise EvidenceValidationError(f"缺少 RC1 journey: {', '.join(missing)}")
    if len(by_id) != len(JOURNEY_IDS):
        raise EvidenceValidationError("journeys 只能包含固定的八条 RC1 用户旅程")

    for journey_id in JOURNEY_IDS:
        journey = by_id[journey_id]
        allowed_statuses = {"passed", "pending"} if allow_pending else {"passed"}
        if journey.get("status") not in allowed_statuses:
            expected = "passed 或 pending" if allow_pending else "passed"
            raise EvidenceValidationError(f"{journey_id}.status 必须为 {expected}")
        task_ids = _non_empty_strings(journey.get("task_ids"), f"{journey_id}.task_ids")
        run_ids = _non_empty_strings(journey.get("run_ids"), f"{journey_id}.run_ids")
        if journey.get("status") == "passed" and (not task_ids or not run_ids):
            raise EvidenceValidationError(f"{journey_id} 通过时必须记录 task_ids 和 run_ids")
        if journey_id == "workspace_create_allow_deny" and journey.get("status") == "passed":
            if len(task_ids) < 2 or len(run_ids) < 2:
                raise EvidenceValidationError(
                    "workspace_create_allow_deny 必须至少记录批准和拒绝两个 Task/Run"
                )

        evidence = journey.get("evidence")
        if not isinstance(evidence, dict) or set(evidence) != set(EVIDENCE_CATEGORIES):
            raise EvidenceValidationError(
                f"{journey_id}.evidence 必须精确包含: {', '.join(EVIDENCE_CATEGORIES)}"
            )
        for category in EVIDENCE_CATEGORIES:
            item = evidence[category]
            if not isinstance(item, dict):
                raise EvidenceValidationError(f"{journey_id}.{category} 必须是 object")
            status = item.get("status")
            if status not in {"verified", "not_applicable", "pending"}:
                raise EvidenceValidationError(
                    f"{journey_id}.{category}.status 非法: {status!r}"
                )
            if status == "pending" and not allow_pending:
                raise EvidenceValidationError(f"{journey_id}.{category} 仍为 pending")
            refs = _non_empty_strings(item.get("refs"), f"{journey_id}.{category}.refs")
            note = item.get("note")
            if not isinstance(note, str):
                raise EvidenceValidationError(f"{journey_id}.{category}.note 必须是字符串")
            if status == "verified" and not refs:
                raise EvidenceValidationError(f"{journey_id}.{category} verified 时 refs 不得为空")
            if status == "not_applicable" and (refs or not note.strip()):
                raise EvidenceValidationError(
                    f"{journey_id}.{category} not_applicable 时 refs 必须为空且 note 必须解释原因"
                )
            if category in REQUIRED_VERIFIED[journey_id] and journey.get("status") == "passed":
                if status != "verified":
                    raise EvidenceValidationError(
                        f"{journey_id}.{category} 是该旅程的必需证据，必须 verified"
                    )


def build_template() -> dict[str, Any]:
    def evidence_item() -> dict[str, Any]:
        return {"status": "pending", "refs": [], "note": ""}

    return {
        "schema_version": 1,
        "release_candidate": "RC1",
        "revision": "REPLACE_WITH_GIT_REVISION",
        "executed_at": "2026-07-30T00:00:00+08:00",
        "environment": "local-production-like",
        "journeys": [
            {
                "id": journey_id,
                "status": "pending",
                "task_ids": [],
                "run_ids": [],
                "evidence": {
                    category: evidence_item() for category in EVIDENCE_CATEGORIES
                },
                "notes": "",
            }
            for journey_id in JOURNEY_IDS
        ],
    }


def _self_test() -> None:
    template = build_template()
    validate_report(template, allow_pending=True)
    try:
        validate_report(template)
    except EvidenceValidationError:
        pass
    else:
        raise AssertionError("pending template 不得通过正式 RC1 gate")

    complete = build_template()
    for journey in complete["journeys"]:
        journey["status"] = "passed"
        journey["task_ids"] = [f"task-{journey['id']}"]
        journey["run_ids"] = [f"run-{journey['id']}"]
        if journey["id"] == "workspace_create_allow_deny":
            journey["task_ids"].append("task-deny")
            journey["run_ids"].append("run-deny")
        for category, item in journey["evidence"].items():
            if category in REQUIRED_VERIFIED[journey["id"]]:
                item.update(status="verified", refs=[f"{category}-ref"], note="")
            else:
                item.update(status="not_applicable", refs=[], note="该旅程不产生此类记录")
    validate_report(complete)
    try:
        validate_report(complete, expected_revision="different-revision")
    except EvidenceValidationError:
        pass
    else:
        raise AssertionError("revision 不一致不得通过正式 RC1 gate")


def main() -> int:
    parser = argparse.ArgumentParser(description="校验 Jarvis MVP RC1 真实旅程证据")
    parser.add_argument("evidence", nargs="?", type=Path)
    parser.add_argument("--allow-pending", action="store_true")
    parser.add_argument("--expected-revision")
    parser.add_argument("--write-template", type=Path)
    parser.add_argument("--self-test", action="store_true")
    args = parser.parse_args()

    try:
        if args.self_test:
            _self_test()
        if args.write_template:
            args.write_template.parent.mkdir(parents=True, exist_ok=True)
            args.write_template.write_text(
                json.dumps(build_template(), ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
        if args.evidence:
            report = json.loads(args.evidence.read_text(encoding="utf-8"))
            validate_report(
                report,
                allow_pending=args.allow_pending,
                expected_revision=args.expected_revision,
            )
        if not (args.self_test or args.write_template or args.evidence):
            parser.error("必须提供 evidence 文件、--write-template 或 --self-test")
    except (OSError, json.JSONDecodeError, EvidenceValidationError, AssertionError) as exc:
        print(f"RC1 evidence gate failed: {exc}", file=sys.stderr)
        return 1
    print("RC1 evidence gate passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
