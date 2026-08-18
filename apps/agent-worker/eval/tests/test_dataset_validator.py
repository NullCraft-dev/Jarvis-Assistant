from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

VALIDATOR_PATH = Path(__file__).resolve().parents[1] / "runners" / "validate_dataset.py"
SPEC = importlib.util.spec_from_file_location("rag_dataset_validator", VALIDATOR_PATH)
assert SPEC is not None and SPEC.loader is not None
validator = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(validator)


def _write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf-8")


def _case(*, status: str) -> dict[str, object]:
    return {
        "schema_version": 1,
        "case_id": "fixture-case-v1",
        "title": "fixture",
        "status": status,
        "split": "development",
        "document": {
            "kind": "generated",
            "relative_path": "corpus/generated/missing.pdf",
            "mime_type": "application/pdf",
            "size_bytes": None,
            "page_count": None,
            "sha256": None,
            "source_url": None,
            "download_url": None,
            "license": "generated",
            "license_url": None,
            "attribution": "test fixture",
            "license_notes": None,
            "generator": "fixture-generator",
            "languages": ["en"],
            "privacy": "public",
        },
        "coverage": {
            "document_types": ["fixture"],
            "layouts": ["single-column"],
            "modalities": ["text"],
            "risks": ["reading-order"],
        },
        "evaluation_stages": ["preprocessing", "chunking"],
        "annotation_path": None,
    }


def _manifest(root: Path, case: dict[str, object]) -> Path:
    case_path = root / "cases" / "fixture" / "case.json"
    manifest_path = root / "manifests" / "corpus-v1.json"
    _write_json(case_path, case)
    _write_json(
        manifest_path,
        {
            "schema_version": 1,
            "dataset_id": "test-corpus",
            "cases": ["cases/fixture/case.json"],
        },
    )
    return manifest_path


def test_planned_case_can_register_coverage_before_source_exists(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(validator, "DATASET_ROOT", tmp_path)

    counts = validator.validate_manifest(_manifest(tmp_path, _case(status="planned")))

    assert counts["planned"] == 1
    assert counts["total"] == 1


def test_annotated_case_requires_source_hash_and_annotation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(validator, "DATASET_ROOT", tmp_path)

    with pytest.raises(validator.DatasetValidationError, match="缺少原始文档"):
        validator.validate_manifest(_manifest(tmp_path, _case(status="annotated")))
