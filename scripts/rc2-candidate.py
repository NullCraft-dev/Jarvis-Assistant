#!/usr/bin/env python3
"""Validate same-revision RC2 evidence and write a redacted candidate record."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import tarfile
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

SCHEMA_VERSION = "1.0"
REVISION_RE = re.compile(r"^[0-9a-f]{40}$")
MAX_JSON_BYTES = 5 * 1024 * 1024
SUPPORT_MEMBERS = {
    "environment.json",
    "health.json",
    "log-summary.json",
    "manifest.json",
    "operations-summary.json",
    "report.json",
}
ALLOWED_SUPPORT_WARNINGS = {"runtime.dead_letters"}
REQUIRED_GATE_STEPS = {
    "release_report_self_test",
    "dev_preflight_self_test",
    "data_lifecycle_self_test",
    "runtime_support_self_test",
    "rc2_candidate_self_test",
    "evidence_validator_self_test",
    "git_diff_check",
    "shared_typecheck",
    "gateway_test",
    "gateway_vet",
    "web_test",
    "web_build",
    "worker_test",
    "worker_quality",
    "worker_compile",
    "runtime_smoke",
    "rag_flywheel",
}
REQUIRED_FAULT_CHECKS = {
    "baseline_runtime",
    "gateway_restart_sse_recovery",
    "agent_worker_restart",
    "rag_worker_restart_nonterminal_ingestion",
    "redis_restart_permission_resume",
    "redis_restart",
    "run_queue_retry_exhaustion",
    "poison_message_atomic_dlq_ack",
    "runtime_terminal_reconciliation",
}


class CandidateError(ValueError):
    pass


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def iso(value: datetime) -> str:
    return value.isoformat().replace("+00:00", "Z")


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def load_json(path: Path) -> dict[str, Any]:
    if not path.is_file() or path.is_symlink():
        raise CandidateError("证据文件不存在或不是普通文件")
    if path.stat().st_size > MAX_JSON_BYTES:
        raise CandidateError("证据 JSON 超过容量上限")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise CandidateError("证据 JSON 无法读取") from exc
    if not isinstance(value, dict):
        raise CandidateError("证据 JSON 根节点必须是 object")
    return value


def require_revision(value: Any, expected: str, label: str) -> None:
    if value != expected:
        raise CandidateError(f"{label} revision 与候选不一致")


def validate_gate(value: dict[str, Any], expected: str) -> dict[str, Any]:
    require_revision(value.get("revision"), expected, "RC2 gate")
    if (
        value.get("mode") != "rc2"
        or value.get("status") != "passed"
        or value.get("worktree_clean") is not True
        or value.get("release_candidate_eligible") is not True
        or value.get("failed_steps") != []
    ):
        raise CandidateError("RC2 gate 未满足干净候选放行条件")
    steps = value.get("steps")
    if not isinstance(steps, list) or any(
        not isinstance(item, dict) or item.get("status") != "passed" for item in steps
    ):
        raise CandidateError("RC2 gate steps 不完整或包含失败")
    names = {str(item.get("name", "")) for item in steps}
    missing = REQUIRED_GATE_STEPS - names
    if missing:
        raise CandidateError("RC2 gate 缺少必要步骤")
    if int(value.get("passed_steps", 0)) != len(steps):
        raise CandidateError("RC2 gate passed_steps 与 steps 不一致")
    return {"status": "passed", "passed_steps": len(steps)}


def validate_rag(value: dict[str, Any]) -> dict[str, Any]:
    gate = value.get("release_gate")
    if not isinstance(gate, dict):
        raise CandidateError("RAG 门禁报告缺少 release_gate")
    if gate.get("status") != "passed" or gate.get("failed_checks") != []:
        raise CandidateError("promoted RAG 门禁未通过")
    release = value.get("release_report")
    automation = value.get("automation")
    privacy = value.get("privacy")
    if (
        not isinstance(release, dict)
        or release.get("source") != "production-rag-promoted-replay"
        or release.get("eligible_label_statuses") != ["promoted"]
        or not isinstance(automation, dict)
        or automation.get("promoted_replay") != "automatic_current_pipeline"
        or not isinstance(privacy, dict)
        or any(privacy.values())
    ):
        raise CandidateError("RAG 证据不是当前生产 Pipeline 的脱敏 promoted-only 重放")
    sample_count = int(gate.get("sample_count", 0))
    if sample_count < 10 or int(release.get("sample_count", 0)) != sample_count:
        raise CandidateError("promoted RAG 样本数不足")
    metrics: dict[str, float] = {}
    for item in gate.get("checks", []):
        if not isinstance(item, dict) or item.get("passed") is not True:
            raise CandidateError("RAG 门禁包含未通过检查")
        check_id = str(item.get("check_id", ""))
        if check_id.startswith("metric:") and isinstance(item.get("actual"), (int, float)):
            metrics[check_id[len("metric:") :]] = float(item["actual"])
    return {"status": "passed", "sample_count": sample_count, "metrics": metrics}


def validate_support(directory: Path, expected: str) -> dict[str, Any]:
    if not directory.is_dir() or directory.is_symlink():
        raise CandidateError("支持包目录不存在或不是普通目录")
    report = load_json(directory / "report.json")
    environment = load_json(directory / "environment.json")
    manifest = load_json(directory / "manifest.json")
    git = environment.get("git")
    if not isinstance(git, dict):
        raise CandidateError("支持包缺少 Git 状态")
    require_revision(git.get("revision"), expected, "支持包")
    if git.get("worktree_dirty") is not False:
        raise CandidateError("支持包必须来自干净工作区")
    if report.get("archive") is not True or report.get("status") not in {"healthy", "degraded"}:
        raise CandidateError("支持包报告状态非法")
    checks = report.get("checks")
    if not isinstance(checks, list) or any(
        not isinstance(item, dict) or item.get("status") not in {"passed", "warning"}
        for item in checks
    ):
        raise CandidateError("支持包 checks 不完整或包含失败")
    warnings = {str(item.get("id", "")) for item in checks if item.get("status") == "warning"}
    if not warnings.issubset(ALLOWED_SUPPORT_WARNINGS):
        raise CandidateError("支持包存在 RC2 不允许的降级项")
    if (report.get("status") == "healthy") != (not warnings):
        raise CandidateError("支持包总状态与 warning checks 不一致")
    archive = directory.parent / f"{directory.name}.tar.gz"
    if not archive.is_file() or archive.is_symlink():
        raise CandidateError("支持包归档不存在")
    with tarfile.open(archive, "r:gz") as handle:
        members = {item.name for item in handle.getmembers() if item.isfile()}
    if members != SUPPORT_MEMBERS:
        raise CandidateError("支持包成员不符合白名单")
    manifest_files = manifest.get("files")
    if not isinstance(manifest_files, list):
        raise CandidateError("支持包 manifest files 非法")
    expected_manifest_files = SUPPORT_MEMBERS - {"manifest.json"}
    indexed: dict[str, dict[str, Any]] = {}
    for item in manifest_files:
        if not isinstance(item, dict) or not isinstance(item.get("file"), str):
            raise CandidateError("支持包 manifest 条目非法")
        indexed[item["file"]] = item
    if set(indexed) != expected_manifest_files:
        raise CandidateError("支持包 manifest 文件集合不一致")
    for name in expected_manifest_files:
        path = directory / name
        if (
            not path.is_file()
            or path.is_symlink()
            or indexed[name].get("bytes") != path.stat().st_size
            or indexed[name].get("sha256") != sha256(path)
        ):
            raise CandidateError("支持包 manifest hash/size 校验失败")
    excluded = set(manifest.get("excluded", []))
    if not {"raw_logs", "audit_logs", "database_backups", "task_and_model_content"}.issubset(
        excluded
    ):
        raise CandidateError("支持包 manifest 未声明关键排除项")
    return {
        "status": str(report["status"]),
        "warning_checks": sorted(warnings),
        "archive_bytes": archive.stat().st_size,
        "archive_sha256": sha256(archive),
    }


def validate_data(value: dict[str, Any], expected: str) -> dict[str, Any]:
    require_revision(value.get("revision"), expected, "data lifecycle")
    if (
        value.get("operation") not in {"restore-drill", "upgrade"}
        or value.get("status") != "passed"
    ):
        raise CandidateError("数据恢复证据未通过")
    if value.get("code_head") != value.get("database_head_after"):
        raise CandidateError("数据恢复证据的 code/database head 不一致")
    restore = value.get("restore_verification")
    required = (
        "isolated_database",
        "temporary_database_removed",
        "migration_revision_match",
        "public_table_set_match",
        "row_counts_match",
    )
    if not isinstance(restore, dict) or any(restore.get(key) is not True for key in required):
        raise CandidateError("隔离恢复对账证据不完整")
    return {
        "status": "passed",
        "migration_head": str(value.get("code_head", "")),
        "compared_table_count": int(restore.get("compared_table_count", 0)),
    }


def validate_fault(value: dict[str, Any], expected: str) -> dict[str, Any]:
    require_revision(value.get("revision"), expected, "runtime fault drill")
    if value.get("status") != "passed" or value.get("worktree_dirty") is not False:
        raise CandidateError("Runtime 故障注入证据未通过或工作区不干净")
    checks = value.get("checks")
    if not isinstance(checks, list) or any(
        not isinstance(item, dict) or item.get("status") != "passed" for item in checks
    ):
        raise CandidateError("Runtime 故障注入 checks 不完整")
    names = {str(item.get("name", "")) for item in checks}
    if not REQUIRED_FAULT_CHECKS.issubset(names):
        raise CandidateError("Runtime 故障注入缺少必要场景")
    return {"status": "passed", "passed_scenarios": len(checks)}


def validate_audit(value: dict[str, Any], expected: str) -> dict[str, Any]:
    source = value.get("source")
    if not isinstance(source, dict):
        raise CandidateError("审计演练缺少 source state")
    require_revision(source.get("revision"), expected, "audit retention drill")
    audit = value.get("audit")
    if (
        value.get("status") != "passed"
        or source.get("worktree_dirty") is not False
        or not isinstance(audit, dict)
        or audit.get("candidate_ids_exposed") is not False
        or value.get("database_safety_check") != "passed"
    ):
        raise CandidateError("审计保留或脱敏证据未通过")
    return {
        "status": "passed",
        "permission_decision_records": int(audit.get("permission_decision_records", 0)),
        "applied_records": int(audit.get("applied_records", 0)),
        "candidate_ids_exposed": False,
    }


def evidence_hash(path: Path) -> dict[str, Any]:
    return {"bytes": path.stat().st_size, "sha256": sha256(path)}


def current_git_state(repo: Path) -> tuple[str, bool]:
    revision = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=repo,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        check=True,
        text=True,
    ).stdout.strip()
    dirty = bool(
        subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=repo,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            check=True,
            text=True,
        ).stdout.strip()
    )
    return revision, dirty


def build_record(
    *,
    expected: str,
    gate_path: Path,
    support_dir: Path,
    data_path: Path,
    fault_path: Path,
    audit_path: Path,
) -> dict[str, Any]:
    gate_value = load_json(gate_path)
    rag_path = gate_path.parent / "rag-flywheel/flywheel.json"
    rag_value = load_json(rag_path)
    data_value = load_json(data_path)
    fault_value = load_json(fault_path)
    audit_value = load_json(audit_path)
    gate = validate_gate(gate_value, expected)
    rag = validate_rag(rag_value)
    support = validate_support(support_dir, expected)
    data = validate_data(data_value, expected)
    fault = validate_fault(fault_value, expected)
    audit = validate_audit(audit_value, expected)
    created = utc_now()
    return {
        "schema_version": SCHEMA_VERSION,
        "candidate_id": f"RC2-{created.strftime('%Y%m%dT%H%M%SZ')}-{expected[:8]}",
        "release_candidate": "RC2",
        "status": "passed",
        "revision": expected,
        "created_at": iso(created),
        "release_candidate_eligible": True,
        "gate": gate,
        "rag": rag,
        "support": support,
        "data_recovery": data,
        "runtime_hardening": fault,
        "audit_retention": audit,
        "evidence": {
            "rc2_gate": evidence_hash(gate_path),
            "rag_gate": evidence_hash(rag_path),
            "data_recovery": evidence_hash(data_path),
            "runtime_hardening": evidence_hash(fault_path),
            "audit_retention": evidence_hash(audit_path),
        },
        "excluded": [
            "task_and_run_ids",
            "prompts_and_model_outputs",
            "tool_arguments",
            "raw_logs",
            "audit_record_content",
            "database_urls_and_secrets",
            "local_absolute_paths",
        ],
    }


def write_record(record: dict[str, Any], output_root: Path) -> tuple[Path, Path]:
    directory = output_root / str(record["candidate_id"])
    directory.mkdir(parents=True, exist_ok=False)
    os.chmod(directory, 0o700)
    record_path = directory / "record.json"
    record_path.write_text(
        json.dumps(record, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    os.chmod(record_path, 0o600)
    digest_path = directory / "record.sha256"
    digest_path.write_text(f"{sha256(record_path)}  record.json\n", encoding="utf-8")
    os.chmod(digest_path, 0o600)
    return record_path, digest_path


def self_test() -> int:
    revision = "a" * 40
    steps = [{"name": name, "status": "passed"} for name in sorted(REQUIRED_GATE_STEPS)]
    gate = {
        "mode": "rc2",
        "status": "passed",
        "revision": revision,
        "worktree_clean": True,
        "release_candidate_eligible": True,
        "failed_steps": [],
        "passed_steps": len(steps),
        "steps": steps,
    }
    assert validate_gate(gate, revision)["status"] == "passed"
    fault = {
        "revision": revision,
        "status": "passed",
        "worktree_dirty": False,
        "checks": [{"name": name, "status": "passed"} for name in REQUIRED_FAULT_CHECKS],
    }
    assert validate_fault(fault, revision)["passed_scenarios"] == len(REQUIRED_FAULT_CHECKS)
    assert (
        validate_rag(
            {
                "automation": {"promoted_replay": "automatic_current_pipeline"},
                "privacy": {
                    "raw_query_included": False,
                    "raw_answer_included": False,
                    "raw_chunk_content_included": False,
                    "embedding_vectors_included": False,
                },
                "release_report": {
                    "source": "production-rag-promoted-replay",
                    "sample_count": 10,
                    "eligible_label_statuses": ["promoted"],
                },
                "release_gate": {
                    "status": "passed",
                    "sample_count": 10,
                    "failed_checks": [],
                    "checks": [
                        {
                            "check_id": "metric:candidate.recall@5",
                            "passed": True,
                            "actual": 0.9,
                        }
                    ],
                },
            }
        )["sample_count"]
        == 10
    )
    data = {
        "revision": revision,
        "operation": "restore-drill",
        "status": "passed",
        "code_head": "head",
        "database_head_after": "head",
        "restore_verification": {
            "isolated_database": True,
            "temporary_database_removed": True,
            "migration_revision_match": True,
            "public_table_set_match": True,
            "row_counts_match": True,
            "compared_table_count": 3,
        },
    }
    assert validate_data(data, revision)["compared_table_count"] == 3
    audit = {
        "status": "passed",
        "database_safety_check": "passed",
        "source": {"revision": revision, "worktree_dirty": False},
        "audit": {
            "permission_decision_records": 1,
            "applied_records": 1,
            "candidate_ids_exposed": False,
        },
    }
    assert validate_audit(audit, revision)["candidate_ids_exposed"] is False
    try:
        validate_gate({**gate, "revision": "b" * 40}, revision)
    except CandidateError:
        pass
    else:
        raise AssertionError("mismatched revision was accepted")
    with tempfile.TemporaryDirectory() as temp:
        output = Path(temp)
        support = output / "support"
        support.mkdir()
        for name in SUPPORT_MEMBERS:
            value: dict[str, Any] = {}
            if name == "environment.json":
                value = {"git": {"revision": revision, "worktree_dirty": False}}
            elif name == "report.json":
                value = {
                    "archive": True,
                    "status": "degraded",
                    "checks": [{"id": "runtime.dead_letters", "status": "warning"}],
                }
            elif name == "manifest.json":
                value = {
                    "files": [],
                    "excluded": [
                        "raw_logs",
                        "audit_logs",
                        "database_backups",
                        "task_and_model_content",
                    ],
                }
            (support / name).write_text(json.dumps(value), encoding="utf-8")
        manifest = json.loads((support / "manifest.json").read_text(encoding="utf-8"))
        manifest["files"] = [
            {
                "file": name,
                "bytes": (support / name).stat().st_size,
                "sha256": sha256(support / name),
            }
            for name in sorted(SUPPORT_MEMBERS - {"manifest.json"})
        ]
        (support / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
        with tarfile.open(output / "support.tar.gz", "w:gz") as handle:
            for name in sorted(SUPPORT_MEMBERS):
                handle.add(support / name, arcname=name)
        assert validate_support(support, revision)["status"] == "degraded"
        record = {
            "candidate_id": "RC2-test-aaaaaaaa",
            "status": "passed",
            "revision": revision,
        }
        record_path, digest_path = write_record(record, output)
        assert digest_path.read_text(encoding="utf-8").startswith(sha256(record_path))
    print("[rc2-candidate] self-test passed")
    return 0


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("--repo", default=Path(__file__).resolve().parent.parent)
    result.add_argument("--gate-report", type=Path)
    result.add_argument("--support-dir", type=Path)
    result.add_argument("--data-report", type=Path)
    result.add_argument("--runtime-fault-report", type=Path)
    result.add_argument("--audit-report", type=Path)
    result.add_argument("--output")
    result.add_argument("--self-test", action="store_true")
    return result


def main() -> int:
    args = parser().parse_args()
    if args.self_test:
        return self_test()
    required = (
        args.gate_report,
        args.support_dir,
        args.data_report,
        args.runtime_fault_report,
        args.audit_report,
    )
    if any(value is None for value in required):
        parser().error("必须提供 gate、support、data、runtime fault 和 audit 五类证据")
    repo = Path(args.repo).resolve()
    try:
        revision, dirty = current_git_state(repo)
        if not REVISION_RE.fullmatch(revision):
            raise CandidateError("当前 Git revision 非法")
        if dirty:
            raise CandidateError("RC2 候选记录要求干净工作区")
        record = build_record(
            expected=revision,
            gate_path=args.gate_report.resolve(),
            support_dir=args.support_dir.resolve(),
            data_path=args.data_report.resolve(),
            fault_path=args.runtime_fault_report.resolve(),
            audit_path=args.audit_report.resolve(),
        )
        output_root = Path(
            args.output or os.getenv("JARVIS_RC2_OUTPUT_DIR", repo / ".local/release-candidates")
        ).resolve()
        record_path, digest_path = write_record(record, output_root)
    except (
        CandidateError,
        OSError,
        ValueError,
        TypeError,
        subprocess.SubprocessError,
        tarfile.TarError,
    ) as exc:
        print(f"[rc2-candidate] FAILED: {exc}", file=sys.stderr)
        return 1
    print(f"[rc2-candidate] PASSED candidate={record['candidate_id']}")
    print(f"[rc2-candidate] record={record_path}")
    print(f"[rc2-candidate] digest={digest_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
