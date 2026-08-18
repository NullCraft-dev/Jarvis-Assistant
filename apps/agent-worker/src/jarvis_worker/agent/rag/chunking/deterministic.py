"""确定性、结构感知的文本分片器。

普通段落与标题边界不制造机械 overlap；只有单个语义块超过硬上限时，
才使用有限 overlap，避免长段落切点附近的信息丢失。
"""

from __future__ import annotations

import hashlib
import math
import re
from dataclasses import dataclass

from jarvis_worker.agent.rag.chunking.contracts import ChunkDraft, ChunkPolicy
from jarvis_worker.agent.rag.ingestion.contracts import DocumentBlock, PdfBlockType
from jarvis_worker.agent.rag.ingestion.sanitization import remove_nul


_BOUNDARY = re.compile(r"(?<=[。！？!?；;\.])\s+|\n+")


def estimate_tokens(text: str) -> int:
    """无需绑定模型 tokenizer 的稳定预算估算器。"""

    if not text:
        return 0
    cjk = sum(1 for char in text if "\u3400" <= char <= "\u9fff")
    remaining = max(len(text) - cjk, 0)
    return max(1, cjk + math.ceil(remaining / 4))


@dataclass(frozen=True, slots=True)
class _Unit:
    text: str
    page_number: int
    block_start: int
    block_end: int
    heading_path: tuple[str, ...]
    overlap_tokens: int = 0

    @property
    def token_count(self) -> int:
        return estimate_tokens(self.text)


class DeterministicBlockChunker:
    def __init__(self, policy: ChunkPolicy | None = None):
        self._policy = policy or ChunkPolicy()

    @property
    def version(self) -> str:
        return self._policy.policy_version

    def chunk(self, blocks: tuple[DocumentBlock, ...]) -> tuple[ChunkDraft, ...]:
        ordered = sorted(
            enumerate(blocks),
            key=lambda item: (
                item[1].page_number,
                item[1].bounding_box[1],
                item[1].bounding_box[0],
                item[1].order_index,
            ),
        )
        units = self._build_units(ordered)
        drafts: list[ChunkDraft] = []
        pending: list[_Unit] = []

        for unit in units:
            if unit.token_count > self._policy.max_tokens:
                if pending:
                    drafts.append(self._draft(len(drafts), pending))
                    pending = []
                for split_unit in self._split_oversized(unit):
                    drafts.append(self._draft(len(drafts), [split_unit]))
                continue

            pending_tokens = estimate_tokens("\n\n".join(item.text for item in pending))
            heading_boundary = bool(
                pending
                and unit.heading_path != pending[-1].heading_path
                and pending_tokens >= self._policy.min_tokens
            )
            would_exceed = bool(
                pending
                and estimate_tokens("\n\n".join([*(item.text for item in pending), unit.text]))
                > self._policy.max_tokens
            )
            reached_target = pending_tokens >= self._policy.target_tokens
            if heading_boundary or would_exceed or reached_target:
                drafts.append(self._draft(len(drafts), pending))
                pending = []
            pending.append(unit)

        if pending:
            drafts.append(self._draft(len(drafts), pending))
        return tuple(drafts)

    def _build_units(
        self, ordered: list[tuple[int, DocumentBlock]]
    ) -> list[_Unit]:
        heading_stack: list[str] = []
        result: list[_Unit] = []
        for source_index, block in ordered:
            text = remove_nul(block.text).strip()
            if not text:
                continue
            if block.block_type is PdfBlockType.HEADING:
                level = block.heading_level or 1
                heading_stack = heading_stack[: level - 1]
                heading_stack.append(text)
            result.append(
                _Unit(
                    text=text,
                    page_number=block.page_number,
                    block_start=source_index,
                    block_end=source_index,
                    heading_path=tuple(heading_stack),
                )
            )
        return result

    def _split_oversized(self, unit: _Unit) -> list[_Unit]:
        sentences = [part.strip() for part in _BOUNDARY.split(unit.text) if part.strip()]
        if len(sentences) == 1:
            sentences = list(unit.text)

        chunks: list[str] = []
        current = ""
        for part in sentences:
            separator = " " if current and len(part) > 1 else ""
            candidate = current + separator + part
            if current and estimate_tokens(candidate) > self._policy.max_tokens:
                chunks.append(current.strip())
                overlap = _suffix_for_budget(current, self._policy.semantic_overlap_tokens)
                current = (overlap + separator + part).strip()
            else:
                current = candidate
        if current.strip():
            chunks.append(current.strip())

        return [
            _Unit(
                text=text,
                page_number=unit.page_number,
                block_start=unit.block_start,
                block_end=unit.block_end,
                heading_path=unit.heading_path,
                overlap_tokens=(
                    min(self._policy.semantic_overlap_tokens, estimate_tokens(text))
                    if index > 0
                    else 0
                ),
            )
            for index, text in enumerate(chunks)
        ]

    @staticmethod
    def _draft(ordinal: int, units: list[_Unit]) -> ChunkDraft:
        content = "\n\n".join(unit.text for unit in units).strip()
        return ChunkDraft(
            ordinal=ordinal,
            content=content,
            token_count=estimate_tokens(content),
            content_hash=hashlib.sha256(content.encode("utf-8")).hexdigest(),
            page_start=min(unit.page_number for unit in units),
            page_end=max(unit.page_number for unit in units),
            block_start=min(unit.block_start for unit in units),
            block_end=max(unit.block_end for unit in units),
            heading_path=units[-1].heading_path,
            overlap_tokens=max(unit.overlap_tokens for unit in units),
        )


def _suffix_for_budget(text: str, token_budget: int) -> str:
    if token_budget <= 0:
        return ""
    low, high = 0, len(text)
    while low < high:
        middle = (low + high) // 2
        if estimate_tokens(text[middle:]) > token_budget:
            low = middle + 1
        else:
            high = middle
    return text[low:].strip()
