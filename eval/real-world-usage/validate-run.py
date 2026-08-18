#!/usr/bin/env python3
"""Validate typed real-world execution contracts, fixtures, and run evidence."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import re
from datetime import datetime
from pathlib import Path
from typing import Any

EVAL_ROOT = Path(__file__).resolve().parent
REPO_ROOT = EVAL_ROOT.parents[1]
REVISION_RE = re.compile(r"^[0-9a-f]{40}$")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
ALLOWED_STATUSES = {"passed", "partial", "failed", "blocked_by_upstream", "not_run"}
SCORED_STATUSES = {"passed", "partial", "failed"}
RESULT_COLUMNS = (
    "evaluation_id",
    "revision",
    "environment",
    "case_id",
    "attempt",
    "status",
    "task_id",
    "run_id",
    "started_at",
    "finished_at",
    "duration_seconds",
    "human_interventions",
    "goal_completion",
    "result_correctness",
    "tool_planning",
    "permission_safety",
    "observability",
    "recovery_idempotency",
    "experience_efficiency",
    "blocking_behavior",
    "defect_ids",
    "owner_tags",
    "evidence_refs",
    "reviewer_notes",
)
SCORE_FIELDS = (
    "goal_completion",
    "result_correctness",
    "tool_planning",
    "permission_safety",
    "observability",
    "recovery_idempotency",
    "experience_efficiency",
)
PLACEHOLDER_EVIDENCE = {"PostgreSQL + UI + filesystem", "UI", "DOM"}


class ValidationError(ValueError):
    pass


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def load_contracts(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if (
        value.get("schema_version") != 1
        or not isinstance(value.get("profiles"), dict)
        or not isinstance(value.get("fixtures"), dict)
        or not isinstance(value.get("bindings"), dict)
    ):
        raise ValidationError("execution contracts 结构无效")
    return value


def load_cases(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8", newline="") as handle:
        return list(csv.DictReader(handle))


def _binding_profiles(case_id: str, contracts: dict[str, Any]) -> list[dict[str, Any]]:
    binding = contracts["bindings"].get(case_id, {})
    return [contracts["profiles"][name] for name in binding.get("profiles", [])]


def validate_contracts(cases_path: Path, contracts_path: Path) -> dict[str, Any]:
    cases = load_cases(cases_path)
    indexed = {case["case_id"]: case for case in cases}
    document = load_contracts(contracts_path)
    profiles = document["profiles"]
    fixtures = document["fixtures"]
    bindings = document["bindings"]
    errors: list[str] = []

    if document.get("case_set") != cases_path.name:
        errors.append("case_set 与输入案例文件名不一致")
    for profile_name, profile in profiles.items():
        if not isinstance(profile, dict):
            errors.append(f"profile {profile_name}: 定义必须是对象")
            continue
        invariants = profile.get("pass_invariants")
        if not isinstance(profile.get("kind"), str) or not profile["kind"].strip():
            errors.append(f"profile {profile_name}: kind 缺失")
        if profile.get("runtime_ids") not in {"required", "optional"}:
            errors.append(f"profile {profile_name}: runtime_ids 非法")
        if (
            not isinstance(invariants, list)
            or not invariants
            or any(
                not isinstance(value, str) or not value.strip() for value in invariants
            )
        ):
            errors.append(f"profile {profile_name}: pass_invariants 必须是非空文本列表")
        for flag in (
            "requires_fixture",
            "requires_task_input",
            "allow_shared_run_lineage",
        ):
            if flag in profile and not isinstance(profile[flag], bool):
                errors.append(f"profile {profile_name}: {flag} 必须是布尔值")

    required_bindings = {
        case["case_id"]
        for case in cases
        if case["priority"] == "P0" and case["entry"] == "ui"
    }
    missing = sorted(required_bindings - bindings.keys())
    if missing:
        errors.append(f"P0 UI case 缺少类型绑定: {', '.join(missing)}")

    hashes_by_group: dict[str, dict[str, str]] = {}
    for fixture_name, fixture in fixtures.items():
        if not isinstance(fixture, dict):
            errors.append(f"fixture {fixture_name}: 定义必须是对象")
            continue
        relative_path = Path(str(fixture.get("path", "")))
        path = (REPO_ROOT / relative_path).resolve()
        expected_hash = str(fixture.get("sha256", ""))
        try:
            path.relative_to(REPO_ROOT.resolve())
            path_within_repo = True
        except ValueError:
            path_within_repo = False
        if relative_path.is_absolute() or not path_within_repo:
            errors.append(f"fixture {fixture_name}: path 必须位于仓库内")
            continue
        if not path.is_file() or path.is_symlink():
            errors.append(f"fixture {fixture_name}: 不存在或不是普通文件")
            continue
        if SHA256_RE.fullmatch(expected_hash) is None or sha256(path) != expected_hash:
            errors.append(f"fixture {fixture_name}: SHA-256 与合同不一致")
        if path.stat().st_size != fixture.get("size_bytes"):
            errors.append(f"fixture {fixture_name}: size_bytes 与合同不一致")
        if fixture.get("filename") != path.name:
            errors.append(f"fixture {fixture_name}: filename 与 path 不一致")
        if not isinstance(fixture.get("must_be_new_in_workspace"), bool):
            errors.append(
                f"fixture {fixture_name}: must_be_new_in_workspace 必须是布尔值"
            )
        group = fixture.get("uniqueness_group")
        if group:
            previous = hashes_by_group.setdefault(str(group), {}).get(expected_hash)
            if previous:
                errors.append(
                    f"fixture {fixture_name}: 与 {previous} 在唯一性组 {group} 复用内容"
                )
            hashes_by_group[str(group)][expected_hash] = fixture_name

    for case_id, binding in bindings.items():
        case = indexed.get(case_id)
        if case is None:
            errors.append(f"{case_id}: 绑定引用未知 case")
            continue
        if not isinstance(binding, dict):
            errors.append(f"{case_id}: binding 必须是对象")
            continue
        profile_names = binding.get("profiles")
        if not isinstance(profile_names, list) or not profile_names:
            errors.append(f"{case_id}: profiles 必须是非空列表")
            continue
        if len(profile_names) != len(set(profile_names)):
            errors.append(f"{case_id}: profiles 不得重复")
        unknown_profiles = [name for name in profile_names if name not in profiles]
        if unknown_profiles:
            errors.append(f"{case_id}: 未知 profile {', '.join(unknown_profiles)}")
            continue
        bound_fixtures = binding.get("fixtures", [])
        if not isinstance(bound_fixtures, list):
            errors.append(f"{case_id}: fixtures 必须是列表")
            continue
        if len(bound_fixtures) != len(set(bound_fixtures)):
            errors.append(f"{case_id}: fixtures 不得重复")
        unknown_fixtures = [name for name in bound_fixtures if name not in fixtures]
        if unknown_fixtures:
            errors.append(f"{case_id}: 未知 fixture {', '.join(unknown_fixtures)}")
        selected_profiles = [profiles[name] for name in profile_names]
        if (
            any(profile.get("requires_fixture") for profile in selected_profiles)
            and not bound_fixtures
        ):
            errors.append(f"{case_id}: profile 要求 fixture 绑定")
        if any(
            profile.get("requires_task_input") for profile in selected_profiles
        ) and binding.get("task_input") != case.get("user_task"):
            errors.append(f"{case_id}: task_input 与版本化 case 不一致")

    referenced_profiles = {
        name
        for binding in bindings.values()
        if isinstance(binding, dict)
        for name in binding.get("profiles", [])
    }
    referenced_fixtures = {
        name
        for binding in bindings.values()
        if isinstance(binding, dict)
        for name in binding.get("fixtures", [])
    }
    unused_profiles = sorted(profiles.keys() - referenced_profiles)
    unused_fixtures = sorted(fixtures.keys() - referenced_fixtures)
    if unused_profiles:
        errors.append(f"未使用 profile: {', '.join(unused_profiles)}")
    if unused_fixtures:
        errors.append(f"未使用 fixture: {', '.join(unused_fixtures)}")

    if errors:
        raise ValidationError("; ".join(errors))
    return {
        "cases": len(cases),
        "p0_cases": sum(case["priority"] == "P0" for case in cases),
        "profiles": len(profiles),
        "fixtures": len(fixtures),
        "bindings": len(bindings),
    }


def validate_fixture_freshness(
    case_id: str,
    contracts_path: Path,
    known_content_hashes: set[str],
) -> dict[str, Any]:
    """Generic preflight: reject fixtures whose policy requires unseen content."""
    document = load_contracts(contracts_path)
    binding = document["bindings"].get(case_id)
    if binding is None:
        raise ValidationError(f"{case_id}: 没有 execution binding")
    checked: list[str] = []
    for name in binding.get("fixtures", []):
        fixture = document["fixtures"][name]
        digest = fixture["sha256"]
        if fixture.get("must_be_new_in_workspace") and digest in known_content_hashes:
            raise ValidationError(f"{case_id}: fixture {name} 已存在于目标 Workspace")
        checked.append(name)
    return {"case_id": case_id, "fixtures_checked": checked}


def _parse_iso(value: str, label: str) -> datetime:
    if not value:
        raise ValidationError(f"{label} 缺失")
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValidationError(f"{label} 不是 ISO-8601") from exc


def _parse_score(row: dict[str, str], field: str) -> int:
    try:
        score = int(row.get(field, ""))
    except ValueError as exc:
        raise ValidationError(f"{row.get('case_id')}: {field} 不是整数") from exc
    if score not in {0, 1, 2}:
        raise ValidationError(f"{row.get('case_id')}: {field} 必须为 0..2")
    return score


def _require_metadata(row: dict[str, str], field: str) -> str:
    value = row.get(field, "").strip()
    if not value or (value.startswith("<") and value.endswith(">")):
        raise ValidationError(f"{row.get('case_id') or '<unknown>'}: {field} 缺失或仍是模板占位")
    return value


def _parse_attempt(row: dict[str, str]) -> int:
    try:
        attempt = int(_require_metadata(row, "attempt"))
    except ValueError as exc:
        raise ValidationError(f"{row.get('case_id')}: attempt 不是正整数") from exc
    if attempt < 1:
        raise ValidationError(f"{row.get('case_id')}: attempt 必须大于等于 1")
    return attempt


def _may_share_lineage(
    first_id: str,
    second_id: str,
    cases: dict[str, dict[str, str]],
    contracts: dict[str, Any],
) -> bool:
    first = cases[first_id]
    second = cases[second_id]
    if not first.get("chain_id") or first["chain_id"] != second.get("chain_id"):
        return False
    profiles = _binding_profiles(first_id, contracts) + _binding_profiles(
        second_id, contracts
    )
    return bool(profiles) and all(
        profile.get("allow_shared_run_lineage") for profile in profiles
    )


def validate_results(
    results_path: Path,
    cases_path: Path,
    expected_revision: str,
    contracts_path: Path = EVAL_ROOT / "p0-execution-contracts-v1.json",
) -> dict[str, Any]:
    if not REVISION_RE.fullmatch(expected_revision):
        raise ValidationError("expected revision 必须是 40 位 Git SHA")
    all_cases = load_cases(cases_path)
    cases = {case["case_id"]: case for case in all_cases}
    expected_ids = {case["case_id"] for case in all_cases if case["priority"] == "P0"}
    contracts = load_contracts(contracts_path)
    with results_path.open(encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        if tuple(reader.fieldnames or ()) != RESULT_COLUMNS:
            raise ValidationError("results CSV header 必须与 run-record-template.csv 完全一致")
        rows = list(reader)
    ids = [row.get("case_id", "") for row in rows]
    if (
        len(rows) != len(expected_ids)
        or set(ids) != expected_ids
        or len(ids) != len(set(ids))
    ):
        raise ValidationError("results 必须恰好包含全部且唯一的 P0 case")

    seen_task_ids: dict[str, str] = {}
    seen_run_ids: dict[str, str] = {}
    counts = {status: 0 for status in ALLOWED_STATUSES}
    evaluation_ids: set[str] = set()
    for row in rows:
        case_id = row["case_id"]
        evaluation_ids.add(_require_metadata(row, "evaluation_id"))
        _require_metadata(row, "environment")
        _parse_attempt(row)
        status = row.get("status", "")
        if status not in ALLOWED_STATUSES:
            raise ValidationError(f"{case_id}: status 非法")
        counts[status] += 1
        if row.get("revision") != expected_revision:
            raise ValidationError(f"{case_id}: revision 与候选不一致")
        if status not in SCORED_STATUSES:
            continue
        started = _parse_iso(row.get("started_at", ""), f"{case_id}.started_at")
        finished = _parse_iso(row.get("finished_at", ""), f"{case_id}.finished_at")
        if finished <= started:
            raise ValidationError(f"{case_id}: finished_at 必须晚于 started_at")
        try:
            duration = float(row.get("duration_seconds", ""))
        except ValueError as exc:
            raise ValidationError(f"{case_id}: duration_seconds 非法") from exc
        if duration <= 0:
            raise ValidationError(f"{case_id}: duration_seconds 必须大于 0")
        scores = [_parse_score(row, field) for field in SCORE_FIELDS]
        if status == "passed" and (sum(scores) < 12 or 0 in scores):
            raise ValidationError(f"{case_id}: passed 分数不满足 12/14 且无 0 的合同")
        evidence = row.get("evidence_refs", "").strip()
        if not evidence or evidence in PLACEHOLDER_EVIDENCE:
            raise ValidationError(f"{case_id}: evidence_refs 必须引用具体证据")

        profiles = _binding_profiles(case_id, contracts)
        runtime_ids_optional = bool(profiles) and all(
            profile.get("runtime_ids") == "optional" for profile in profiles
        )
        if not runtime_ids_optional and (
            not row.get("task_id", "").strip() or not row.get("run_id", "").strip()
        ):
            raise ValidationError(f"{case_id}: 缺少 Task/Run ID")
        for field, seen in (("task_id", seen_task_ids), ("run_id", seen_run_ids)):
            for identifier in filter(None, row.get(field, "").split(";")):
                previous = seen.get(identifier)
                if previous and not _may_share_lineage(
                    previous, case_id, cases, contracts
                ):
                    raise ValidationError(f"{case_id}: {field} 与 {previous} 重复")
                seen[identifier] = case_id
    if len(evaluation_ids) != 1:
        raise ValidationError("results 必须只对应一个 evaluation_id")

    status_counts = {key: value for key, value in counts.items() if value}
    release_candidate_eligible = counts["passed"] == len(expected_ids)
    return {
        "evaluation_id": next(iter(evaluation_ids)),
        "rows": len(rows),
        "status_counts": status_counts,
        "release_candidate_eligible": release_candidate_eligible,
        "verdict": "all_p0_passed" if release_candidate_eligible else "not_eligible",
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--cases", type=Path, default=EVAL_ROOT / "cases-v1.csv")
    parser.add_argument(
        "--contracts", type=Path, default=EVAL_ROOT / "p0-execution-contracts-v1.json"
    )
    parser.add_argument("--results", type=Path)
    parser.add_argument("--expected-revision", default="")
    parser.add_argument(
        "--require-all-passed",
        action="store_true",
        help="将任何 partial/failed/blocked_by_upstream/not_run 视为非零退出的发布阻断",
    )
    parser.add_argument("--contracts-only", action="store_true")
    parser.add_argument("--preflight-case")
    parser.add_argument(
        "--known-content-hashes",
        type=Path,
        help="目标 Workspace 已知 Artifact SHA-256，一行一个",
    )
    args = parser.parse_args()
    summary: dict[str, Any] = {
        "contracts": validate_contracts(args.cases, args.contracts)
    }
    if args.preflight_case:
        if args.known_content_hashes is None:
            raise SystemExit("--preflight-case 需要 --known-content-hashes")
        known_hashes = {
            value.strip().lower()
            for value in args.known_content_hashes.read_text(
                encoding="utf-8"
            ).splitlines()
            if value.strip()
        }
        invalid = sorted(
            value for value in known_hashes if SHA256_RE.fullmatch(value) is None
        )
        if invalid:
            raise ValidationError("known-content-hashes 包含非法 SHA-256")
        summary["preflight"] = validate_fixture_freshness(
            args.preflight_case, args.contracts, known_hashes
        )
    if not args.contracts_only:
        if args.results is None or not args.expected_revision:
            raise SystemExit("--results 与 --expected-revision 必须同时提供")
        summary["results"] = validate_results(
            args.results, args.cases, args.expected_revision, args.contracts
        )
        if args.require_all_passed and not summary["results"]["release_candidate_eligible"]:
            raise SystemExit("P0 结果结构有效，但不具备全量通过的发布资格")
    print(
        json.dumps(
            {
                "validation_status": "passed",
                "release_candidate_eligible": summary.get("results", {}).get(
                    "release_candidate_eligible"
                ),
                **summary,
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    try:
        main()
    except (OSError, json.JSONDecodeError, ValidationError) as exc:
        raise SystemExit(f"validation failed: {exc}") from None
