#!/usr/bin/env python3
"""生成不进入 Git 的表格、公式与折线图 PDF 验收样本。"""

from __future__ import annotations

import argparse
from pathlib import Path

import pymupdf


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    args.output.parent.mkdir(parents=True, exist_ok=True)

    document = pymupdf.open()
    page = document.new_page(width=595, height=842)
    page.insert_text((50, 60), "Multimodal RAG Evaluation", fontsize=22)
    page.insert_text((50, 90), "Table 1. Quarterly revenue", fontsize=13)

    left, top, width, row_height = 50, 110, 330, 34
    rows = [
        ("Quarter", "Revenue", "Growth"),
        ("Q1", "10", "-"),
        ("Q2", "14", "40%"),
        ("Q3", "18", "29%"),
        ("Q4", "25", "39%"),
    ]
    columns = (0, 120, 230, width)
    for row_index, row in enumerate(rows):
        y0 = top + row_index * row_height
        y1 = y0 + row_height
        page.draw_rect((left, y0, left + width, y1), color=(0, 0, 0), width=1)
        for column in columns[1:-1]:
            page.draw_line((left + column, y0), (left + column, y1), color=(0, 0, 0))
        for column_index, value in enumerate(row):
            page.insert_text((left + columns[column_index] + 8, y0 + 22), value, fontsize=11)

    chart_top = 330
    page.insert_text((50, chart_top), "Figure 1. Revenue trend", fontsize=13)
    origin_x, origin_y = 80, 540
    page.draw_line((origin_x, origin_y), (origin_x + 350, origin_y), color=(0, 0, 0), width=1.5)
    page.draw_line((origin_x, origin_y), (origin_x, origin_y - 170), color=(0, 0, 0), width=1.5)
    points = [(100, 500), (190, 470), (280, 430), (370, 370)]
    for start, end in zip(points, points[1:]):
        page.draw_line(start, end, color=(0.1, 0.35, 0.8), width=3)
    for index, (x, y) in enumerate(points, start=1):
        page.draw_circle((x, y), 5, color=(0.1, 0.35, 0.8), fill=(0.1, 0.35, 0.8))
        page.insert_text((x - 8, origin_y + 20), f"Q{index}", fontsize=10)
    page.insert_text((440, 385), "Revenue", fontsize=10)

    page.insert_text((50, 610), "Equation 1", fontsize=13)
    page.insert_text((90, 650), "E = m c^2", fontsize=20)
    page.insert_text(
        (50, 700),
        "The table and chart show accelerating quarterly revenue growth.",
        fontsize=11,
    )
    document.save(str(args.output))
    document.close()
    print(args.output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
