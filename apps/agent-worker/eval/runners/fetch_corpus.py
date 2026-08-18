#!/usr/bin/env python3
"""Download public RAG evaluation PDFs from case manifests."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import tempfile
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

DATASET_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_MANIFEST = DATASET_ROOT / "manifests" / "corpus-v1.json"
MAX_DOCUMENT_BYTES = 100 * 1024 * 1024
USER_AGENT = "Jarvis-RAG-Evaluation-Corpus/1.0"


class CorpusFetchError(RuntimeError):
    pass


def _load_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise CorpusFetchError(f"JSON 根节点必须是 object: {path}")
    return value


def _safe_path(relative_path: str) -> Path:
    path = (DATASET_ROOT / relative_path).resolve()
    if not path.is_relative_to(DATASET_ROOT.resolve()):
        raise CorpusFetchError(f"目标路径越出 eval 目录: {relative_path}")
    return path


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as file:
        for block in iter(lambda: file.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _download(url: str, target: Path) -> tuple[int, str]:
    target.parent.mkdir(parents=True, exist_ok=True)
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    temporary_path: Path | None = None
    try:
        with urllib.request.urlopen(request, timeout=120) as response:
            content_type = response.headers.get_content_type()
            with tempfile.NamedTemporaryFile(
                prefix=f".{target.name}.", suffix=".part", dir=target.parent, delete=False
            ) as temporary:
                temporary_path = Path(temporary.name)
                total = 0
                first_block = True
                for block in iter(lambda: response.read(1024 * 1024), b""):
                    total += len(block)
                    if total > MAX_DOCUMENT_BYTES:
                        raise CorpusFetchError(f"文档超过 100 MiB 上限: {url}")
                    if first_block and not block.startswith(b"%PDF-"):
                        raise CorpusFetchError(
                            f"下载内容不是 PDF: {url} (content-type={content_type})"
                        )
                    first_block = False
                    temporary.write(block)
        if temporary_path is None or total == 0:
            raise CorpusFetchError(f"下载结果为空: {url}")
        digest = _sha256(temporary_path)
        os.replace(temporary_path, target)
        return total, digest
    except (OSError, urllib.error.URLError) as exc:
        raise CorpusFetchError(f"下载失败: {url}: {exc}") from exc
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)


def fetch_manifest(path: Path, *, selected_case_ids: set[str]) -> list[dict[str, object]]:
    manifest = _load_json(path)
    results: list[dict[str, object]] = []
    for case_relative_path in manifest.get("cases", []):
        case_path = _safe_path(case_relative_path)
        case = _load_json(case_path)
        case_id = str(case.get("case_id"))
        if selected_case_ids and case_id not in selected_case_ids:
            continue
        document = case.get("document")
        if not isinstance(document, dict) or document.get("kind") != "public":
            continue
        download_url = document.get("download_url")
        relative_path = document.get("relative_path")
        if not isinstance(download_url, str) or not isinstance(relative_path, str):
            raise CorpusFetchError(f"公开案例缺少下载信息: {case_path}")
        target = _safe_path(relative_path)
        expected_hash = document.get("sha256")
        if target.is_file():
            digest = _sha256(target)
            if isinstance(expected_hash, str) and digest != expected_hash:
                raise CorpusFetchError(f"已有文件 SHA-256 不匹配: {target}")
            results.append(
                {
                    "case_id": case_id,
                    "status": "cached",
                    "bytes": target.stat().st_size,
                    "sha256": digest,
                }
            )
            continue
        size, digest = _download(download_url, target)
        if isinstance(expected_hash, str) and digest != expected_hash:
            target.unlink(missing_ok=True)
            raise CorpusFetchError(f"下载文件 SHA-256 不匹配: {case_id}")
        results.append(
            {"case_id": case_id, "status": "downloaded", "bytes": size, "sha256": digest}
        )
    missing = selected_case_ids - {str(result["case_id"]) for result in results}
    if missing:
        raise CorpusFetchError(f"未找到可下载的公开案例: {', '.join(sorted(missing))}")
    return results


def main() -> int:
    parser = argparse.ArgumentParser(description="下载 Jarvis RAG evaluation 公开 PDF")
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    parser.add_argument("--case", action="append", default=[], help="只下载指定 case_id，可重复")
    args = parser.parse_args()
    try:
        results = fetch_manifest(args.manifest.resolve(), selected_case_ids=set(args.case))
    except (CorpusFetchError, OSError, json.JSONDecodeError) as exc:
        print(f"RAG evaluation corpus fetch failed: {exc}")
        return 1
    print(json.dumps({"documents": results}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
