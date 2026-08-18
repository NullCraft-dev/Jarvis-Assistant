"""供应商无关的长期记忆候选提取契约。

Extractor 只能返回候选规格，不能持久化 Memory、Candidate、Job 或修改 Task/Run。
来源 ID、workspace 边界和提取策略版本由调用方绑定，不能由模型输出决定。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol
from uuid import UUID


MEMORY_EXTRACTION_POLICY_VERSION = "memory-extraction-v2"


@dataclass(frozen=True)
class ExistingMemoryReference:
    key: str
    content: str


@dataclass(frozen=True)
class MemoryExtractionInput:
    source_task_id: UUID
    source_run_id: UUID
    workspace_id: UUID | None
    user_goal: str
    final_response: str
    source_message_ids: tuple[UUID, ...]
    input_fingerprint: str
    existing_memories: tuple[ExistingMemoryReference, ...] = ()


@dataclass(frozen=True)
class ExtractedMemoryCandidateSpec:
    scope_type: str
    category: str
    suggested_key: str
    content: str
    confidence: float
    importance: int
    evidence_source: str
    evidence_quote: str
    sensitivity: str = "normal"


class MemoryExtractor(Protocol):
    @property
    def provider_name(self) -> str: ...

    @property
    def model_name(self) -> str: ...

    async def extract(
        self, extraction_input: MemoryExtractionInput
    ) -> list[ExtractedMemoryCandidateSpec]: ...
