"""生产 RAG 轨迹、反馈标签与数据飞轮持久化边界。"""

from .contracts import RagEvaluationLabel, RagEvaluationTrace

__all__ = [
    "RagEvaluationLabel",
    "RagEvaluationTrace",
]
