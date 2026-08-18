"""RAG 文本分片的纯领域契约。"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class ChunkModality(str, Enum):
    TEXT = "text"
    TABLE = "table"
    IMAGE = "image"
    CHART = "chart"
    FORMULA = "formula"
    CODE = "code"


@dataclass(frozen=True, slots=True)
class ChunkPolicy:
    target_tokens: int = 500
    max_tokens: int = 700
    min_tokens: int = 80
    semantic_overlap_tokens: int = 64
    policy_version: str = "semantic-block-chunker-v1"

    def __post_init__(self) -> None:
        if not 1 <= self.min_tokens <= self.target_tokens <= self.max_tokens:
            raise ValueError("Chunk token policy 顺序无效")
        if not 0 <= self.semantic_overlap_tokens < self.max_tokens:
            raise ValueError("Chunk overlap 必须在 0..max_tokens")
        if not self.policy_version.strip():
            raise ValueError("Chunk policy_version 不能为空")


@dataclass(frozen=True, slots=True)
class ChunkDraft:
    ordinal: int
    content: str
    token_count: int
    content_hash: str
    page_start: int
    page_end: int
    block_start: int
    block_end: int
    heading_path: tuple[str, ...]
    overlap_tokens: int = 0
    modality: ChunkModality = ChunkModality.TEXT
    node_ids: tuple[str, ...] = ()
    element_node_ids: tuple[str, ...] = ()
