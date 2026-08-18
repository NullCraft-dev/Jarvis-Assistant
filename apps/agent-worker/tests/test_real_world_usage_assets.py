from __future__ import annotations

import csv
import importlib.util
import json
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
EVAL_ROOT = REPO_ROOT / "eval" / "real-world-usage"
EXPECTED_COLUMNS = {
    "case_id",
    "priority",
    "pack",
    "persona",
    "entry",
    "chain_id",
    "sequence",
    "decision",
    "fault",
    "user_task",
    "precondition",
    "expected",
    "forbidden",
    "evidence",
    "diagnosis_tags",
}


def _load_cases(name: str) -> list[dict[str, str]]:
    with (EVAL_ROOT / name).open(encoding="utf-8", newline="") as stream:
        return list(csv.DictReader(stream))


def test_base_and_codex_agent_case_sets_have_separate_release_scope() -> None:
    base_cases = _load_cases("cases-v1.csv")
    codex_cases = _load_cases("codex-agent-cases-v1.csv")

    assert len(base_cases) == 84
    assert sum(case["priority"] == "P0" for case in base_cases) == 36
    assert {case["case_id"] for case in base_cases if case["pack"] == "document_evidence"} == {
        "DOC-01",
        "DOC-02",
    }
    assert not any(case["case_id"].startswith("CODE-") for case in base_cases)
    assert len(codex_cases) == 14
    assert all(case["case_id"].startswith("CODE-") for case in codex_cases)
    assert all(case["pack"] == "code_evidence" for case in codex_cases)

    for cases in (base_cases, codex_cases):
        assert all(set(case) == EXPECTED_COLUMNS for case in cases)
        case_ids = [case["case_id"] for case in cases]
        assert len(case_ids) == len(set(case_ids))


def test_fixture_contains_domain_neutral_multi_document_evidence(tmp_path: Path) -> None:
    script = EVAL_ROOT / "prepare-fixtures.py"
    spec = importlib.util.spec_from_file_location("real_world_usage_fixtures", script)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    workspace = tmp_path / "workspace"
    manifest = module.prepare(workspace)

    expected = manifest["expected"]["procurement_request"]
    assert expected == {
        "request_id": "PR-2026-017",
        "amount_cny": "12800",
        "approval_threshold_cny": "10000",
        "requires_finance_approval": True,
        "current_status": "等待财务审批",
        "policy_source": "procurement/policy.md",
    }
    assert (workspace / "procurement/policy.md").is_file()
    assert (workspace / "procurement/procedure.md").is_file()
    assert (workspace / "procurement/requests/PR-2026-017.md").is_file()
    assert manifest["knowledge_vault"] == "vault/Jarvis"
    assert (tmp_path / "vault/Jarvis").is_dir()
    assert manifest["fixture_catalog"] == {
        "nist_ai_rmf": {
            "path": "apps/agent-worker/eval/corpus/documents/nist-ai-rmf-1-0.pdf",
            "filename": "nist-ai-rmf-1-0.pdf",
            "size_bytes": 1946127,
            "sha256": "7576edb531d9848825814ee88e28b1795d3a84b435b4b797d3670eafdc4a89f1",
            "uniqueness_group": "rag_write_content",
            "must_be_new_in_workspace": True,
        },
        "nasa_handbook": {
            "path": "apps/agent-worker/eval/corpus/documents/nasa-systems-engineering-handbook-rev2.pdf",
            "filename": "nasa-systems-engineering-handbook-rev2.pdf",
            "size_bytes": 4122125,
            "sha256": "3153ae2e53e29452d5997efafe280a5f05cd21b43a047e988a17e1dd5207a38e",
            "uniqueness_group": "rag_write_content",
            "must_be_new_in_workspace": True,
        },
        "long_pdf_crash": {
            "path": "apps/agent-worker/eval/corpus/documents/world-bank-data-driven-development-2018.pdf",
            "filename": "world-bank-data-driven-development-2018.pdf",
            "size_bytes": 10186138,
            "sha256": "59e2044ba1963f34d5897fd42262e7232a81e39a276405d9360f5e932b355cad",
            "uniqueness_group": "rag_write_content",
            "must_be_new_in_workspace": True,
        },
    }


def test_p0_execution_contracts_are_valid_and_match_versioned_cases() -> None:
    script = EVAL_ROOT / "validate-run.py"
    spec = importlib.util.spec_from_file_location("real_world_usage_validator", script)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    summary = module.validate_contracts(
        EVAL_ROOT / "cases-v1.csv",
        EVAL_ROOT / "p0-execution-contracts-v1.json",
    )
    contracts = json.loads(
        (EVAL_ROOT / "p0-execution-contracts-v1.json").read_text(encoding="utf-8")
    )

    assert summary == {
        "cases": 84,
        "p0_cases": 36,
        "profiles": 12,
        "fixtures": 3,
        "bindings": 13,
    }
    assert (
        "不以每份文档的最少 chunk 数"
        in contracts["profiles"]["multi_document_evidence"]["non_requirements"][0]
    )
    assert set(contracts["bindings"]["REC-08"]) == {"profiles", "fixtures"}
    assert contracts["profiles"]["ingestion_worker_crash"]["runtime_ids"] == "optional"


def test_fixture_freshness_is_enforced_by_policy_not_case_name() -> None:
    script = EVAL_ROOT / "validate-run.py"
    spec = importlib.util.spec_from_file_location("fixture_freshness_validator", script)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    contracts_path = EVAL_ROOT / "p0-execution-contracts-v1.json"
    contracts = json.loads(contracts_path.read_text(encoding="utf-8"))
    digest = contracts["fixtures"]["long_pdf_crash"]["sha256"]

    with pytest.raises(module.ValidationError, match="已存在于目标 Workspace"):
        module.validate_fixture_freshness("REC-08", contracts_path, {digest})

    assert module.validate_fixture_freshness("REC-08", contracts_path, set()) == {
        "case_id": "REC-08",
        "fixtures_checked": ["long_pdf_crash"],
    }


def test_results_validator_rejects_placeholder_evidence(tmp_path: Path) -> None:
    script = EVAL_ROOT / "validate-run.py"
    spec = importlib.util.spec_from_file_location("real_world_usage_result_validator", script)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    revision = "a" * 40
    cases = _load_cases("cases-v1.csv")
    p0_ids = [case["case_id"] for case in cases if case["priority"] == "P0"]
    fieldnames = next(
        csv.reader((EVAL_ROOT / "run-record-template.csv").read_text(encoding="utf-8").splitlines())
    )
    rows = []
    for case_id in p0_ids:
        row = {field: "" for field in fieldnames}
        row.update(
            {
                "evaluation_id": "test-run",
                "revision": revision,
                "environment": "isolated",
                "case_id": case_id,
                "attempt": "1",
                "status": "not_run",
                "blocking_behavior": "false",
            }
        )
        rows.append(row)
    target = next(row for row in rows if row["case_id"] == "REC-01")
    target.update(
        {
            "status": "passed",
            "task_id": "task-1",
            "run_id": "run-1",
            "started_at": "2026-08-14T00:00:00Z",
            "finished_at": "2026-08-14T00:00:10Z",
            "duration_seconds": "10",
            "evidence_refs": "PostgreSQL + UI + filesystem",
            **{field: "2" for field in module.SCORE_FIELDS},
        }
    )
    results = tmp_path / "results.csv"
    with results.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)

    with pytest.raises(module.ValidationError, match="evidence_refs"):
        module.validate_results(results, EVAL_ROOT / "cases-v1.csv", revision)


def test_results_validator_separates_valid_evidence_from_release_eligibility(
    tmp_path: Path,
) -> None:
    script = EVAL_ROOT / "validate-run.py"
    spec = importlib.util.spec_from_file_location("real_world_usage_release_verdict", script)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    revision = "b" * 40
    fieldnames = list(module.RESULT_COLUMNS)
    rows = []
    for case in _load_cases("cases-v1.csv"):
        if case["priority"] != "P0":
            continue
        row = {field: "" for field in fieldnames}
        row.update(
            {
                "evaluation_id": "complete-but-not-green",
                "revision": revision,
                "environment": "isolated",
                "case_id": case["case_id"],
                "attempt": "1",
                "status": "not_run",
                "blocking_behavior": "false",
            }
        )
        rows.append(row)
    results = tmp_path / "results.csv"
    with results.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)

    summary = module.validate_results(results, EVAL_ROOT / "cases-v1.csv", revision)
    assert summary["status_counts"] == {"not_run": 36}
    assert summary["release_candidate_eligible"] is False
    assert summary["verdict"] == "not_eligible"

    # Knowledge 中心的 RAG 直传由 Document/Job owner 执行，不创建 Task/Run。
    # REC-08 必须用具体 Document/Job 证据验真，不能伪造 runtime IDs。
    rec08 = next(row for row in rows if row["case_id"] == "REC-08")
    rec08.update(
        {
            "status": "passed",
            "started_at": "2026-08-14T00:00:00Z",
            "finished_at": "2026-08-14T00:05:00Z",
            "duration_seconds": "300",
            "evidence_refs": "document:doc-1;job:job-1;chunks:370;vectors:370",
            **{field: "2" for field in module.SCORE_FIELDS},
        }
    )
    with results.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)

    summary = module.validate_results(results, EVAL_ROOT / "cases-v1.csv", revision)
    assert summary["status_counts"] == {"passed": 1, "not_run": 35}


def test_results_validator_rejects_template_or_header_drift(tmp_path: Path) -> None:
    script = EVAL_ROOT / "validate-run.py"
    spec = importlib.util.spec_from_file_location("real_world_usage_header_validator", script)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    results = tmp_path / "results.csv"
    results.write_text("case_id,status\n<case-id>,not_run\n", encoding="utf-8")
    with pytest.raises(module.ValidationError, match="CSV header"):
        module.validate_results(results, EVAL_ROOT / "cases-v1.csv", "c" * 40)
