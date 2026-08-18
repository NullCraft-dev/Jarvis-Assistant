#!/usr/bin/env python3
"""执行 P4-2 扩展知识质量集；复用生产 Gateway runner 与确定性评分。"""

from __future__ import annotations

import sys
from pathlib import Path

RUNNERS_ROOT = Path(__file__).resolve().parent
if str(RUNNERS_ROOT) not in sys.path:
    sys.path.insert(0, str(RUNNERS_ROOT))

import run_production_p0 as production_runner  # noqa: E402


def main() -> int:
    production_runner.DEFAULT_SUITE = RUNNERS_ROOT.parent / "tasks" / "production-rag-p4-v1.json"
    return production_runner.main()


if __name__ == "__main__":
    raise SystemExit(main())
