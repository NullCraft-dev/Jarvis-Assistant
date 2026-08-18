"""多模态文档结构 Provider adapters。"""

from jarvis_worker.agent.rag.preprocessing.providers.paddleocr_vl import (
    PaddleOcrVlConfig,
    PaddleOcrVlError,
    PaddleOcrVlProvider,
)

__all__ = ["PaddleOcrVlConfig", "PaddleOcrVlError", "PaddleOcrVlProvider"]
