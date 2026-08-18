"""RAG 包必须在全新进程中支持任意合法导入顺序。"""

from __future__ import annotations

import subprocess
import sys

import pytest


@pytest.mark.parametrize(
    "statement",
    [
        (
            "from jarvis_worker.agent.rag.chunking import MultimodalChunkRouter; "
            "from jarvis_worker.agent.rag.ingestion.service import RagIngestionService"
        ),
        (
            "from jarvis_worker.agent.rag.ingestion import RagIngestionService; "
            "from jarvis_worker.agent.rag.chunking import MultimodalChunkRouter"
        ),
    ],
)
def test_rag_cold_import_order_is_stable(statement: str):
    result = subprocess.run(
        [sys.executable, "-c", statement],
        check=False,
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert result.returncode == 0, result.stderr
