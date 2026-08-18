#!/usr/bin/env python3
"""Recompute gold-based metrics from saved pipeline artifacts without rerunning VLM."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from pipeline_eval import (
    EVALUATOR_VERSION,
    align_gold_nodes,
    evaluate_chunking,
    evaluate_preprocessing,
)
from run_pipeline_eval import EVAL_ROOT, _finish_report


def rescore(run_dir: Path) -> dict[str, Any]:
    run_dir = run_dir.resolve()
    reports_root = (EVAL_ROOT / "reports").resolve()
    if not run_dir.is_relative_to(reports_root):
        raise ValueError("run-dir 必须位于 eval/reports 下")
    report = _load(run_dir / "report.json")
    annotation = _load(EVAL_ROOT / "annotations" / f"{report['case_id']}.json")
    fused = _load(run_dir / "03-preprocessed-fused.json")
    chunk_artifact = _load(run_dir / "04-chunks.json")
    nodes = [_node(value) for value in fused["nodes"]]
    chunks = [_chunk(value) for value in chunk_artifact["chunks"]]

    matches, alignment = align_gold_nodes(annotation, nodes)
    report["evaluator_version"] = EVALUATOR_VERSION
    report["rescored"] = True
    report["stages"]["preprocessing"]["metrics"] = evaluate_preprocessing(
        annotation, nodes, matches, alignment
    )
    report["stages"]["chunking"]["metrics"] = evaluate_chunking(annotation, chunks, matches)
    fused["gold_to_output_nodes"] = matches
    fused.pop("gold_to_output_node", None)
    _write(run_dir / "03-preprocessed-fused.json", fused)
    _finish_report(report, run_dir)
    return report


def _node(value: dict[str, Any]) -> SimpleNamespace:
    return SimpleNamespace(
        node_id=value["node_id"],
        node_type=SimpleNamespace(value=value["type"]),
        page_number=value["page_number"],
        order_index=value["order_index"],
        bounding_box=tuple(value["bounding_box"]),
        text=value["text"],
    )


def _chunk(value: dict[str, Any]) -> SimpleNamespace:
    return SimpleNamespace(
        ordinal=value["ordinal"],
        token_count=value["token_count"],
        page_start=value["page_start"],
        page_end=value["page_end"],
        modality=SimpleNamespace(value=value["modality"]),
        node_ids=tuple(value["node_ids"]),
        element_node_ids=tuple(value["element_node_ids"]),
    )


def _load(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"JSON 根节点必须为 object: {path}")
    return value


def _write(path: Path, value: dict[str, Any]) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description="从已保存产物重新计算 RAG 评估指标")
    parser.add_argument("run_dir", type=Path)
    args = parser.parse_args()
    try:
        report = rescore(args.run_dir)
    except (OSError, ValueError, KeyError, json.JSONDecodeError) as exc:
        print(json.dumps({"status": "invalid", "error": str(exc)}, ensure_ascii=False))
        return 1
    print(
        json.dumps(
            {
                "status": report["status"],
                "evaluator_version": report["evaluator_version"],
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
