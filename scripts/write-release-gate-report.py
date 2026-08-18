#!/usr/bin/env python3
"""把 release-gate 的安全步骤索引转换为机器可读 JSON 报告。"""

from __future__ import annotations

import argparse
import csv
import json
import tempfile
from pathlib import Path

SCHEMA_VERSION = "1.0"
VALID_GATE_STATUSES = {"passed", "failed"}
VALID_STEP_STATUSES = {"passed", "failed"}


class ReportValidationError(ValueError):
    """发布门禁报告输入不满足约束。"""


def _read_summary(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        if not raw_line or "=" not in raw_line:
            continue
        key, value = raw_line.split("=", 1)
        values[key] = value
    required = {
        "mode",
        "revision",
        "worktree_clean",
        "started_at",
        "finished_at",
        "status",
        "passed_steps",
    }
    missing = sorted(required - values.keys())
    if missing:
        raise ReportValidationError(f"summary 缺少字段: {', '.join(missing)}")
    if values["status"] not in VALID_GATE_STATUSES:
        raise ReportValidationError(f"非法门禁状态: {values['status']}")
    if values["worktree_clean"] not in {"true", "false"}:
        raise ReportValidationError("worktree_clean 必须是 true 或 false")
    return values


def _read_steps(path: Path) -> list[dict[str, object]]:
    steps: list[dict[str, object]] = []
    with path.open(encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(
            handle,
            delimiter="\t",
            fieldnames=("name", "status", "exit_code", "duration_seconds", "log"),
        )
        for row in reader:
            if not row["name"]:
                continue
            if row["status"] not in VALID_STEP_STATUSES:
                raise ReportValidationError(f"非法步骤状态: {row['status']}")
            steps.append(
                {
                    "name": row["name"],
                    "status": row["status"],
                    "exit_code": int(row["exit_code"]),
                    "duration_seconds": int(row["duration_seconds"]),
                    "log": row["log"],
                }
            )
    return steps


def build_report(summary_path: Path, steps_path: Path) -> dict[str, object]:
    summary = _read_summary(summary_path)
    steps = _read_steps(steps_path)
    passed_steps = sum(step["status"] == "passed" for step in steps)
    failed_steps = [str(step["name"]) for step in steps if step["status"] == "failed"]
    if passed_steps != int(summary["passed_steps"]):
        raise ReportValidationError("summary 与 steps 的 passed_steps 不一致")
    if summary["status"] == "passed" and failed_steps:
        raise ReportValidationError("通过报告不能包含失败步骤")
    if summary["status"] == "failed" and not failed_steps:
        failed_step = summary.get("failed_step")
        if failed_step:
            failed_steps = [failed_step]

    return {
        "schema_version": SCHEMA_VERSION,
        "gate_id": "jarvis-release-gate",
        "mode": summary["mode"],
        "status": summary["status"],
        "revision": summary["revision"],
        "worktree_clean": summary["worktree_clean"] == "true",
        "release_candidate_eligible": (
            summary["mode"] in {"rc1", "rc2"}
            and summary["status"] == "passed"
            and summary["worktree_clean"] == "true"
        ),
        "started_at": summary["started_at"],
        "finished_at": summary["finished_at"],
        "passed_steps": passed_steps,
        "failed_steps": failed_steps,
        "steps": steps,
    }


def write_report(report: dict[str, object], output: Path) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_suffix(f"{output.suffix}.tmp")
    temporary.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    temporary.replace(output)


def self_test() -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        summary = root / "summary.txt"
        steps = root / "steps.tsv"
        output = root / "report.json"
        summary.write_text(
            "mode=rc2\nrevision=abc123\nworktree_clean=true\n"
            "started_at=2026-08-03T00:00:00Z\n"
            "finished_at=2026-08-03T00:00:02Z\nstatus=passed\npassed_steps=2\n",
            encoding="utf-8",
        )
        steps.write_text(
            "shared_typecheck\tpassed\t0\t1\tshared_typecheck.log\n"
            "gateway_test\tpassed\t0\t1\tgateway_test.log\n",
            encoding="utf-8",
        )
        report = build_report(summary, steps)
        write_report(report, output)
        loaded = json.loads(output.read_text(encoding="utf-8"))
        if (
            loaded["status"] != "passed"
            or loaded["passed_steps"] != 2
            or loaded["release_candidate_eligible"] is not True
        ):
            raise ReportValidationError("report self-test 结果不正确")

        summary.write_text(
            "mode=rc2\nrevision=abc123\nworktree_clean=true\n"
            "started_at=2026-08-03T00:00:00Z\n"
            "finished_at=2026-08-03T00:00:02Z\nstatus=failed\n"
            "passed_steps=1\nfailed_step=gateway_test\nexit_code=7\n",
            encoding="utf-8",
        )
        steps.write_text(
            "shared_typecheck\tpassed\t0\t1\tshared_typecheck.log\n"
            "gateway_test\tfailed\t7\t1\tgateway_test.log\n",
            encoding="utf-8",
        )
        failed = build_report(summary, steps)
        if (
            failed["status"] != "failed"
            or failed["failed_steps"] != ["gateway_test"]
            or failed["release_candidate_eligible"] is not False
        ):
            raise ReportValidationError("failed report self-test 结果不正确")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--summary", type=Path)
    parser.add_argument("--steps", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--self-test", action="store_true")
    args = parser.parse_args()
    if args.self_test:
        self_test()
        print("release gate report self-test passed")
        return 0
    if not args.summary or not args.steps or not args.output:
        parser.error("--summary、--steps 和 --output 必须同时提供")
    write_report(build_report(args.summary, args.steps), args.output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
