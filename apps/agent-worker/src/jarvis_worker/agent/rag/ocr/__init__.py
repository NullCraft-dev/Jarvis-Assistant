"""OCR Provider 与按需回退策略。"""

from jarvis_worker.agent.rag.contracts import OcrProvider, OcrResult, OcrSpan
from jarvis_worker.agent.rag.ocr.baidu import BaiduOcrError, BaiduOcrProvider

__all__ = [
    "BaiduOcrError",
    "BaiduOcrProvider",
    "OcrProvider",
    "OcrResult",
    "OcrSpan",
]
