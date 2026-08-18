#!/usr/bin/env python3
"""对单张本地图片执行 PaddleOCR-VL + MLX-VLM 真实冒烟验收。"""

from __future__ import annotations

import argparse
import json
import time
from pathlib import Path
from urllib.request import urlopen


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("input", type=Path)
    parser.add_argument("--server-url", default="http://127.0.0.1:8111/")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()

    if not args.input.is_file():
        raise SystemExit("input file does not exist")
    if args.server_url != "http://127.0.0.1:8111/":
        raise SystemExit("smoke test only permits the fixed localhost MLX-VLM URL")
    health_url = args.server_url.rstrip("/") + "/openapi.json"
    with urlopen(health_url, timeout=5) as response:
        if response.status != 200:
            raise SystemExit("MLX-VLM server is not healthy")

    from paddleocr import PaddleOCRVL

    started = time.monotonic()
    pipeline = PaddleOCRVL(
        pipeline_version="v1.6",
        vl_rec_backend="mlx-vlm-server",
        vl_rec_server_url=args.server_url,
        vl_rec_api_model_name="PaddlePaddle/PaddleOCR-VL-1.6",
        vl_rec_max_concurrency=1,
        use_layout_detection=True,
        use_chart_recognition=True,
        device="cpu",
    )
    initialized_seconds = time.monotonic() - started
    inference_started = time.monotonic()
    results = list(
        pipeline.predict(
            str(args.input.resolve()),
            use_layout_detection=True,
            use_chart_recognition=True,
            use_ocr_for_image_block=True,
            format_block_content=True,
            max_new_tokens=4096,
            temperature=0.0,
        )
    )
    inference_seconds = time.monotonic() - inference_started
    if len(results) != 1 or not isinstance(results[0].json, dict):
        raise SystemExit("PaddleOCR-VL returned an unexpected result")
    payload = results[0].json
    root = payload.get("res") if isinstance(payload.get("res"), dict) else payload
    blocks = root.get("parsing_res_list")
    if not isinstance(blocks, list):
        raise SystemExit("PaddleOCR-VL result has no parsing_res_list")
    summary = {
        "input": args.input.name,
        "pipeline_version": "v1.6",
        "model": "PaddlePaddle/PaddleOCR-VL-1.6",
        "initialized_seconds": round(initialized_seconds, 3),
        "inference_seconds": round(inference_seconds, 3),
        "block_count": len(blocks),
        "blocks": [
            {
                "label": str(block.get("block_label") or ""),
                "order": block.get("block_order"),
                "bbox": block.get("block_bbox"),
                "content": str(block.get("block_content") or "")[:1000],
            }
            for block in blocks
            if isinstance(block, dict)
        ],
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
