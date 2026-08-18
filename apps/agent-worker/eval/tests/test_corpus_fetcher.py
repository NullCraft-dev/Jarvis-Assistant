from __future__ import annotations

import hashlib
import importlib.util
import json
from pathlib import Path

import pytest

FETCHER_PATH = Path(__file__).resolve().parents[1] / "runners" / "fetch_corpus.py"
SPEC = importlib.util.spec_from_file_location("rag_corpus_fetcher", FETCHER_PATH)
assert SPEC is not None and SPEC.loader is not None
fetcher = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(fetcher)


def _write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf-8")


def test_safe_path_rejects_escape(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(fetcher, "DATASET_ROOT", tmp_path)

    with pytest.raises(fetcher.CorpusFetchError, match="越出 eval"):
        fetcher._safe_path("../outside.pdf")


def test_cached_public_pdf_is_verified_without_network(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(fetcher, "DATASET_ROOT", tmp_path)
    content = b"%PDF-1.7\nfixture"
    document_path = tmp_path / "corpus" / "documents" / "fixture.pdf"
    document_path.parent.mkdir(parents=True)
    document_path.write_bytes(content)
    case_path = tmp_path / "cases" / "fixture" / "case.json"
    _write_json(
        case_path,
        {
            "case_id": "fixture-public-v1",
            "document": {
                "kind": "public",
                "download_url": "https://example.invalid/fixture.pdf",
                "relative_path": "corpus/documents/fixture.pdf",
                "sha256": hashlib.sha256(content).hexdigest(),
            },
        },
    )
    manifest_path = tmp_path / "manifests" / "corpus-v1.json"
    _write_json(manifest_path, {"cases": ["cases/fixture/case.json"]})

    results = fetcher.fetch_manifest(manifest_path, selected_case_ids=set())

    assert results == [
        {
            "case_id": "fixture-public-v1",
            "status": "cached",
            "bytes": len(content),
            "sha256": hashlib.sha256(content).hexdigest(),
        }
    ]
