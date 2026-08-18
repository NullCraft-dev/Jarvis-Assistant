#!/usr/bin/env python3
"""Validate the RAG evaluation manifest without adding runtime dependencies."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from pathlib import Path
from typing import Any

DATASET_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_MANIFEST = DATASET_ROOT / "manifests" / "corpus-v1.json"
CASE_ID_PATTERN = re.compile(r"^[a-z0-9][a-z0-9-]+$")
ACTIVE_STATUSES = {"annotated", "verified"}
KNOWN_STATUSES = {"planned", "annotated", "verified", "quarantined"}
KNOWN_SPLITS = {"development", "regression", "blind", "quarantine"}
KNOWN_STAGES = {
    "preprocessing",
    "chunking",
    "embedding",
    "retrieval",
    "generation",
    "end_to_end",
}


class DatasetValidationError(ValueError):
    pass


def _load_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise DatasetValidationError(f"无法读取 JSON: {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise DatasetValidationError(f"JSON 根节点必须是 object: {path}")
    return value


def _safe_path(relative_path: object, *, owner: Path) -> Path:
    if not isinstance(relative_path, str) or not relative_path.strip():
        raise DatasetValidationError(f"相对路径不能为空: {owner}")
    candidate = (DATASET_ROOT / relative_path).resolve()
    if not candidate.is_relative_to(DATASET_ROOT.resolve()):
        raise DatasetValidationError(f"路径越出评测集目录: {owner}: {relative_path}")
    return candidate


def _require_strings(value: object, *, field: str, owner: Path) -> None:
    if (
        not isinstance(value, list)
        or not value
        or not all(isinstance(item, str) and item.strip() for item in value)
    ):
        raise DatasetValidationError(f"{field} 必须是非空字符串数组: {owner}")
    if len(set(value)) != len(value):
        raise DatasetValidationError(f"{field} 不允许重复值: {owner}")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as file:
        for block in iter(lambda: file.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _validate_annotation(path: Path, *, case_id: str, verified: bool) -> None:
    annotation = _load_json(path)
    if annotation.get("schema_version") != 1:
        raise DatasetValidationError(f"annotation schema_version 必须为 1: {path}")
    if annotation.get("case_id") != case_id:
        raise DatasetValidationError(f"annotation case_id 与案例不一致: {path}")
    review = annotation.get("review")
    if not isinstance(review, dict):
        raise DatasetValidationError(f"annotation 缺少 review: {path}")
    if verified and (
        review.get("status") != "reviewed"
        or not review.get("reviewer")
        or not review.get("reviewed_at")
    ):
        raise DatasetValidationError(f"verified 案例必须完成独立复核: {path}")
    if not isinstance(annotation.get("pages"), list):
        raise DatasetValidationError(f"annotation pages 必须是数组: {path}")
    if not isinstance(annotation.get("chunking"), dict):
        raise DatasetValidationError(f"annotation 缺少 chunking 金标: {path}")
    if not isinstance(annotation.get("queries"), list):
        raise DatasetValidationError(f"annotation queries 必须是数组: {path}")


def _validate_case(path: Path) -> tuple[str, str]:
    case = _load_json(path)
    if case.get("schema_version") != 1:
        raise DatasetValidationError(f"case schema_version 必须为 1: {path}")

    case_id = case.get("case_id")
    if not isinstance(case_id, str) or not CASE_ID_PATTERN.fullmatch(case_id):
        raise DatasetValidationError(f"case_id 格式无效: {path}")
    status = case.get("status")
    if status not in KNOWN_STATUSES:
        raise DatasetValidationError(f"未知案例状态: {path}: {status}")
    if case.get("split") not in KNOWN_SPLITS:
        raise DatasetValidationError(f"未知数据集 split: {path}: {case.get('split')}")
    if status == "quarantined" and case.get("split") != "quarantine":
        raise DatasetValidationError(f"quarantined 案例必须进入 quarantine split: {path}")

    document = case.get("document")
    if not isinstance(document, dict):
        raise DatasetValidationError(f"case 缺少 document: {path}")
    source_path = _safe_path(document.get("relative_path"), owner=path)
    if document.get("mime_type") != "application/pdf":
        raise DatasetValidationError(f"当前评测集只接受 application/pdf: {path}")
    _require_strings(document.get("languages"), field="document.languages", owner=path)
    if not isinstance(document.get("license"), str) or not document["license"].strip():
        raise DatasetValidationError(f"document.license 不能为空: {path}")
    if not isinstance(document.get("attribution"), str) or not document["attribution"].strip():
        raise DatasetValidationError(f"document.attribution 不能为空: {path}")
    if document.get("kind") == "public":
        for field in ("source_url", "download_url", "license_url"):
            value = document.get(field)
            if not isinstance(value, str) or not value.startswith("https://"):
                raise DatasetValidationError(f"公开文档 {field} 必须是 HTTPS URL: {path}")

    coverage = case.get("coverage")
    if not isinstance(coverage, dict):
        raise DatasetValidationError(f"case 缺少 coverage: {path}")
    for field in ("document_types", "layouts", "modalities", "risks"):
        _require_strings(coverage.get(field), field=f"coverage.{field}", owner=path)
    stages = case.get("evaluation_stages")
    _require_strings(stages, field="evaluation_stages", owner=path)
    if not set(stages).issubset(KNOWN_STAGES):
        raise DatasetValidationError(f"evaluation_stages 包含未知阶段: {path}")

    expected_hash = document.get("sha256")
    if source_path.is_file() and isinstance(expected_hash, str):
        if document.get("size_bytes") != source_path.stat().st_size:
            raise DatasetValidationError(f"原始文档 size_bytes 不匹配: {path}")
        if _sha256(source_path) != expected_hash:
            raise DatasetValidationError(f"原始文档 SHA-256 不匹配: {path}")

    if status in ACTIVE_STATUSES:
        if not source_path.is_file():
            raise DatasetValidationError(f"活跃案例缺少原始文档: {source_path}")
        if not isinstance(expected_hash, str) or not re.fullmatch(r"[a-f0-9]{64}", expected_hash):
            raise DatasetValidationError(f"活跃案例必须填写合法 SHA-256: {path}")
        annotation_path = _safe_path(case.get("annotation_path"), owner=path)
        if not annotation_path.is_file():
            raise DatasetValidationError(f"活跃案例缺少 annotation: {annotation_path}")
        _validate_annotation(
            annotation_path,
            case_id=case_id,
            verified=status == "verified",
        )
    return case_id, status


def validate_manifest(path: Path) -> dict[str, int]:
    manifest = _load_json(path)
    if manifest.get("schema_version") != 1:
        raise DatasetValidationError("manifest schema_version 必须为 1")
    case_paths = manifest.get("cases")
    if not isinstance(case_paths, list) or not case_paths:
        raise DatasetValidationError("manifest cases 必须是非空数组")

    seen: set[str] = set()
    counts = {status: 0 for status in sorted(KNOWN_STATUSES)}
    for relative_path in case_paths:
        case_path = _safe_path(relative_path, owner=path)
        if not case_path.is_file():
            raise DatasetValidationError(f"manifest 引用的 case 不存在: {case_path}")
        case_id, status = _validate_case(case_path)
        if case_id in seen:
            raise DatasetValidationError(f"manifest 中 case_id 重复: {case_id}")
        seen.add(case_id)
        counts[status] += 1
    counts["total"] = len(seen)
    return counts


def main() -> int:
    parser = argparse.ArgumentParser(description="校验 Jarvis RAG evaluation dataset")
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    args = parser.parse_args()
    try:
        counts = validate_manifest(args.manifest.resolve())
    except DatasetValidationError as exc:
        print(f"RAG evaluation dataset invalid: {exc}")
        return 1
    print(json.dumps({"valid": True, "counts": counts}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
