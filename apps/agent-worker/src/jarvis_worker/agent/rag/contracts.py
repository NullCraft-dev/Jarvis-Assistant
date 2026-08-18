"""RAG ingestion v1 领域契约。

本模块只定义纯 Python 业务对象和可替换能力端口，不依赖 SQLAlchemy、
具体 Embedding Provider 或向量数据库。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field, replace
from datetime import datetime
from enum import Enum
from pathlib import PurePosixPath
from typing import Protocol, Sequence
from uuid import UUID

from jarvis_worker.shared.domain.models import utcnow

_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")


class RagDocumentStatus(str, Enum):
    INDEXING = "indexing"
    READY = "ready"
    FAILED = "failed"
    DISABLED = "disabled"


class RagIngestionStatus(str, Enum):
    QUEUED = "queued"
    PARSING = "parsing"
    CHUNKING = "chunking"
    EMBEDDING = "embedding"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class RagElementType(str, Enum):
    IMAGE = "image"
    FIGURE = "figure"
    CHART = "chart"
    TABLE = "table"
    DIAGRAM = "diagram"
    EQUATION = "equation"


class RagExtractionMethod(str, Enum):
    NATIVE = "native"
    OCR = "ocr"
    VISION = "vision"
    HYBRID = "hybrid"


class RagAssetKind(str, Enum):
    CROP = "crop"
    EMBEDDED_IMAGE = "embedded_image"
    PAGE_RENDER = "page_render"


class RagChunkElementRelation(str, Enum):
    CONTAINS = "contains"
    REFERENCES = "references"
    EXPLAINS = "explains"
    CAPTION_OF = "caption_of"
    NEARBY = "nearby"


@dataclass(frozen=True, slots=True)
class RagJobProgress:
    """RAG 作业的持久化进度快照；只记录真实完成量，不推算百分比。"""

    active_executor: str | None = None
    page_count: int = 0
    native_extraction_done: bool = False
    visual_pages_total: int = 0
    visual_pages_completed: int = 0
    visual_route_counts: dict[str, int] = field(default_factory=dict)
    chunks_total: int = 0
    embedding_total: int = 0
    embedding_completed: int = 0

    def __post_init__(self) -> None:
        counters = (
            self.page_count,
            self.visual_pages_total,
            self.visual_pages_completed,
            self.chunks_total,
            self.embedding_total,
            self.embedding_completed,
        )
        if any(value < 0 for value in counters):
            raise ValueError("RAG 作业进度计数不能为负数")
        if self.visual_pages_completed > self.visual_pages_total:
            raise ValueError("RAG 视觉解析完成页数不能超过总页数")
        if self.embedding_completed > self.embedding_total:
            raise ValueError("RAG Embedding 完成数不能超过总数")
        if self.active_executor is not None and not self.active_executor.strip():
            raise ValueError("RAG active_executor 不能为空字符串")
        if any(not key.strip() or value < 0 for key, value in self.visual_route_counts.items()):
            raise ValueError("RAG 视觉路由原因计数无效")


_ACTIVE_INGESTION_STATUSES = {
    RagIngestionStatus.PARSING,
    RagIngestionStatus.CHUNKING,
    RagIngestionStatus.EMBEDDING,
}
_TERMINAL_INGESTION_STATUSES = {
    RagIngestionStatus.COMPLETED,
    RagIngestionStatus.CANCELLED,
}


def _require_sha256(value: str, field_name: str) -> None:
    if not _SHA256_PATTERN.fullmatch(value):
        raise ValueError(f"{field_name} 必须是 64 位小写 SHA-256")


@dataclass
class RagDocument:
    """一个 Workspace 内由受控 Artifact 派生的可检索文档。"""

    id: UUID
    workspace_id: UUID
    source_artifact_id: UUID
    title: str
    mime_type: str
    source_content_hash: str
    ingestion_policy_version: str
    status: RagDocumentStatus = RagDocumentStatus.INDEXING
    parser_version: str = ""
    chunker_version: str = ""
    embedding_provider: str = ""
    embedding_model: str = ""
    embedding_dimensions: int | None = None
    chunk_count: int = 0
    indexed_at: datetime | None = None
    disabled_at: datetime | None = None
    version: int = 1
    created_at: datetime = field(default_factory=utcnow)
    updated_at: datetime = field(default_factory=utcnow)

    def __post_init__(self) -> None:
        if not self.title.strip():
            raise ValueError("RAG document title 不能为空")
        if not self.mime_type.strip():
            raise ValueError("RAG document mime_type 不能为空")
        if not self.ingestion_policy_version.strip():
            raise ValueError("RAG ingestion_policy_version 不能为空")
        _require_sha256(self.source_content_hash, "source_content_hash")
        if self.chunk_count < 0:
            raise ValueError("RAG document chunk_count 不能为负数")
        if self.embedding_dimensions is not None and self.embedding_dimensions <= 0:
            raise ValueError("RAG embedding_dimensions 必须大于 0")

    def mark_ready(
        self,
        *,
        parser_version: str,
        chunker_version: str,
        embedding_provider: str,
        embedding_model: str,
        embedding_dimensions: int,
        chunk_count: int,
        now: datetime,
    ) -> None:
        if self.status is not RagDocumentStatus.INDEXING:
            raise ValueError("只有 indexing RAG document 可以进入 ready")
        if not all(
            value.strip()
            for value in (parser_version, chunker_version, embedding_provider, embedding_model)
        ):
            raise ValueError("RAG 索引版本与 Embedding 标识不能为空")
        if embedding_dimensions <= 0 or chunk_count <= 0:
            raise ValueError("RAG ready document 必须具有有效维度和至少一个 chunk")
        self.status = RagDocumentStatus.READY
        self.parser_version = parser_version
        self.chunker_version = chunker_version
        self.embedding_provider = embedding_provider
        self.embedding_model = embedding_model
        self.embedding_dimensions = embedding_dimensions
        self.chunk_count = chunk_count
        self.indexed_at = now
        self.updated_at = now
        self.version += 1

    def mark_failed(self, *, now: datetime) -> None:
        if self.status is not RagDocumentStatus.INDEXING:
            raise ValueError("只有 indexing RAG document 可以进入 failed")
        self.status = RagDocumentStatus.FAILED
        self.updated_at = now
        self.version += 1

    def record_prepared(
        self,
        *,
        parser_version: str,
        chunker_version: str,
        chunk_count: int,
        now: datetime,
    ) -> None:
        """记录已持久化的解析/分片投影，但不伪装成向量索引完成。"""

        if self.status is not RagDocumentStatus.INDEXING:
            raise ValueError("只有 indexing RAG document 可以记录 prepared 投影")
        if not parser_version.strip() or not chunker_version.strip():
            raise ValueError("RAG prepared parser/chunker version 不能为空")
        if chunk_count < 1:
            raise ValueError("RAG prepared document 必须至少包含一个 chunk")
        self.parser_version = parser_version
        self.chunker_version = chunker_version
        self.chunk_count = chunk_count
        self.updated_at = now
        self.version += 1

    def begin_indexing(self, *, ingestion_policy_version: str, now: datetime) -> None:
        if self.status is RagDocumentStatus.INDEXING:
            raise ValueError("RAG document 已经处于 indexing")
        if not ingestion_policy_version.strip():
            raise ValueError("RAG ingestion_policy_version 不能为空")
        self.status = RagDocumentStatus.INDEXING
        self.ingestion_policy_version = ingestion_policy_version
        self.parser_version = ""
        self.chunker_version = ""
        self.embedding_provider = ""
        self.embedding_model = ""
        self.embedding_dimensions = None
        self.chunk_count = 0
        self.indexed_at = None
        self.disabled_at = None
        self.updated_at = now
        self.version += 1

    def restart_indexing(self, *, now: datetime) -> None:
        """显式重新执行时清空旧索引元数据，但保留文档与来源身份。"""

        if self.status is RagDocumentStatus.DISABLED:
            raise ValueError("已停用的 RAG document 不能重新执行")
        self.status = RagDocumentStatus.INDEXING
        self.parser_version = ""
        self.chunker_version = ""
        self.embedding_provider = ""
        self.embedding_model = ""
        self.embedding_dimensions = None
        self.chunk_count = 0
        self.indexed_at = None
        self.disabled_at = None
        self.updated_at = now
        self.version += 1

    def disable(self, *, now: datetime) -> None:
        if self.status is RagDocumentStatus.DISABLED:
            return
        if self.status is not RagDocumentStatus.READY:
            raise ValueError("只有 ready RAG document 可以停用")
        self.status = RagDocumentStatus.DISABLED
        self.disabled_at = now
        self.updated_at = now
        self.version += 1

    def enable(self, *, now: datetime) -> None:
        if self.status is not RagDocumentStatus.DISABLED:
            raise ValueError("只有 disabled RAG document 可以启用")
        if (
            not all(
                value.strip()
                for value in (
                    self.parser_version,
                    self.chunker_version,
                    self.embedding_provider,
                    self.embedding_model,
                )
            )
            or self.embedding_dimensions is None
            or self.chunk_count < 1
        ):
            raise ValueError("缺少完整索引元数据的 RAG document 不能启用")
        self.status = RagDocumentStatus.READY
        self.disabled_at = None
        self.updated_at = now
        self.version += 1


@dataclass
class RagIngestionJob:
    """可恢复、可重试且有明确终态的 RAG 入库作业。"""

    id: UUID
    document_id: UUID
    workspace_id: UUID
    idempotency_key: str
    ingestion_policy_version: str
    status: RagIngestionStatus = RagIngestionStatus.QUEUED
    attempts: int = 0
    max_attempts: int = 3
    embedding_attempts: int = 0
    embedding_max_attempts: int = 3
    claimed_by: str | None = None
    lease_until: datetime | None = None
    next_retry_at: datetime | None = None
    error_code: str | None = None
    progress: RagJobProgress = field(default_factory=RagJobProgress)
    started_at: datetime | None = None
    completed_at: datetime | None = None
    failed_at: datetime | None = None
    cancelled_at: datetime | None = None
    created_at: datetime = field(default_factory=utcnow)
    updated_at: datetime = field(default_factory=utcnow)

    def __post_init__(self) -> None:
        _require_sha256(self.idempotency_key, "idempotency_key")
        if not self.ingestion_policy_version.strip():
            raise ValueError("RAG ingestion_policy_version 不能为空")
        if self.attempts < 0 or self.max_attempts < 1 or self.attempts > self.max_attempts:
            raise ValueError("RAG ingestion attempts 超出允许范围")
        if (
            self.embedding_attempts < 0
            or self.embedding_max_attempts < 1
            or self.embedding_attempts > self.embedding_max_attempts
        ):
            raise ValueError("RAG embedding attempts 超出允许范围")

    @property
    def is_terminal(self) -> bool:
        return self.status in _TERMINAL_INGESTION_STATUSES or (
            self.status is RagIngestionStatus.FAILED
            and (self.next_retry_at is None or self.attempts >= self.max_attempts)
        )

    def start(self, *, worker_id: str, lease_until: datetime, now: datetime) -> None:
        if self.status not in {RagIngestionStatus.QUEUED, RagIngestionStatus.FAILED}:
            raise ValueError("只有 queued/failed RAG ingestion job 可以被领取")
        if self.status is RagIngestionStatus.FAILED and self.next_retry_at is None:
            raise ValueError("未安排重试的 failed RAG ingestion job 不能被领取")
        self._claim(worker_id=worker_id, lease_until=lease_until, now=now)

    def recover_stale(self, *, worker_id: str, lease_until: datetime, now: datetime) -> None:
        if self.status not in _ACTIVE_INGESTION_STATUSES:
            raise ValueError("只有运行中的 RAG ingestion job 可以恢复过期 lease")
        if self.lease_until is None or self.lease_until > now:
            raise ValueError("RAG ingestion lease 尚未过期")
        self._claim(worker_id=worker_id, lease_until=lease_until, now=now)

    def exhaust_ingestion(self, *, now: datetime) -> None:
        """将 lease 已过期且耗尽预算的解析作业收敛为明确终态。"""

        if self.status not in {RagIngestionStatus.PARSING, RagIngestionStatus.CHUNKING}:
            raise ValueError("只有 parsing/chunking RAG ingestion job 可以耗尽")
        if self.attempts < self.max_attempts:
            raise ValueError("RAG ingestion job 尚未耗尽尝试次数")
        if self.lease_until is not None and self.lease_until > now:
            raise ValueError("仍具有有效 lease 的 RAG ingestion job 不能被耗尽")
        self.fail(
            error_code="RAG_INGESTION_ATTEMPTS_EXHAUSTED",
            next_retry_at=None,
            now=now,
        )

    def renew_lease(self, *, worker_id: str, lease_until: datetime, now: datetime) -> None:
        if self.status not in _ACTIVE_INGESTION_STATUSES:
            raise ValueError("只有运行中的 RAG ingestion job 可以续期 lease")
        if self.claimed_by != worker_id:
            raise ValueError("RAG ingestion lease 不属于当前 worker")
        if self.lease_until is None or self.lease_until <= now:
            raise ValueError("RAG ingestion lease 已经过期")
        if lease_until <= self.lease_until:
            raise ValueError("RAG ingestion 新 lease 必须晚于当前 lease")
        self.lease_until = lease_until
        self.updated_at = now

    def report_progress(self, *, progress: RagJobProgress, worker_id: str, now: datetime) -> None:
        if self.status not in _ACTIVE_INGESTION_STATUSES:
            raise ValueError("只有运行中的 RAG ingestion job 可以更新进度")
        if self.claimed_by != worker_id:
            raise ValueError("RAG ingestion lease 不属于当前 worker")
        self.progress = progress
        self.updated_at = now

    def _claim(self, *, worker_id: str, lease_until: datetime, now: datetime) -> None:
        if self.attempts >= self.max_attempts:
            raise ValueError("RAG ingestion job 已达到最大尝试次数")
        if not worker_id.strip() or lease_until <= now:
            raise ValueError("RAG ingestion claim 必须具有 worker 和未来 lease")
        if self.next_retry_at is not None and self.next_retry_at > now:
            raise ValueError("RAG ingestion job 尚未到重试时间")
        self.status = RagIngestionStatus.PARSING
        self.attempts += 1
        self.claimed_by = worker_id
        self.lease_until = lease_until
        self.next_retry_at = None
        self.error_code = None
        self.progress = RagJobProgress(active_executor="pymupdf")
        self.failed_at = None
        self.started_at = self.started_at or now
        self.updated_at = now

    def advance(self, next_status: RagIngestionStatus, *, now: datetime) -> None:
        allowed = {
            RagIngestionStatus.PARSING: RagIngestionStatus.CHUNKING,
            RagIngestionStatus.CHUNKING: RagIngestionStatus.EMBEDDING,
            RagIngestionStatus.EMBEDDING: RagIngestionStatus.COMPLETED,
        }
        if allowed.get(self.status) is not next_status:
            raise ValueError(
                f"非法 RAG ingestion 状态转换: {self.status.value} -> {next_status.value}"
            )
        self.status = next_status
        self.updated_at = now
        if next_status is RagIngestionStatus.COMPLETED:
            self.completed_at = now
            self.claimed_by = None
            self.lease_until = None
            self.progress = replace(self.progress, active_executor=None)

    def handoff_to_embedding(self, *, now: datetime) -> None:
        """完成解析/分片后释放 lease，等待独立 Embedding 阶段领取。"""

        if self.status is not RagIngestionStatus.CHUNKING:
            raise ValueError("只有 chunking RAG ingestion job 可以交接给 embedding")
        self.status = RagIngestionStatus.EMBEDDING
        self.claimed_by = None
        self.lease_until = None
        self.progress = replace(self.progress, active_executor=None)
        self.updated_at = now

    def claim_embedding(self, *, worker_id: str, lease_until: datetime, now: datetime) -> None:
        """领取独立 Embedding 阶段，不回退或重复执行解析/分块。"""

        if self.status is not RagIngestionStatus.EMBEDDING:
            raise ValueError("只有 embedding RAG ingestion job 可以被领取")
        if self.claimed_by is not None and self.lease_until is not None and self.lease_until > now:
            raise ValueError("RAG embedding job 已被其他 worker 领取")
        if self.embedding_attempts >= self.embedding_max_attempts:
            raise ValueError("RAG embedding job 已达到最大尝试次数")
        if not worker_id.strip() or lease_until <= now:
            raise ValueError("RAG embedding claim 必须具有 worker 和未来 lease")
        if self.next_retry_at is not None and self.next_retry_at > now:
            raise ValueError("RAG embedding job 尚未到重试时间")
        self.embedding_attempts += 1
        self.claimed_by = worker_id
        self.lease_until = lease_until
        self.next_retry_at = None
        self.error_code = None
        self.failed_at = None
        self.updated_at = now

    def fail_embedding(
        self,
        *,
        worker_id: str,
        error_code: str,
        recoverable: bool,
        next_retry_at: datetime | None,
        now: datetime,
    ) -> None:
        """Embedding 失败时保留已生成 chunks；耗尽预算后才进入 failed。"""

        if self.status is not RagIngestionStatus.EMBEDDING:
            raise ValueError("只有 embedding RAG ingestion job 可以记录失败")
        if self.claimed_by != worker_id:
            raise ValueError("RAG embedding lease 不属于当前 worker")
        if not error_code.strip():
            raise ValueError("RAG embedding error_code 不能为空")
        terminal = not recoverable or self.embedding_attempts >= self.embedding_max_attempts
        if terminal and next_retry_at is not None:
            raise ValueError("达到最大尝试次数后不能继续安排重试")
        if not terminal and next_retry_at is None:
            raise ValueError("可恢复的 embedding 失败必须安排重试")
        if next_retry_at is not None and next_retry_at <= now:
            raise ValueError("RAG embedding next_retry_at 必须晚于当前时间")
        self.status = RagIngestionStatus.FAILED if terminal else RagIngestionStatus.EMBEDDING
        self.error_code = error_code
        self.next_retry_at = None if terminal else next_retry_at
        self.failed_at = now if terminal else None
        self.claimed_by = None
        self.lease_until = None
        self.progress = replace(self.progress, active_executor=None)
        self.updated_at = now

    def exhaust_embedding(self, *, now: datetime) -> None:
        """将已耗尽预算且无人可安全恢复的 Embedding 作业收敛为终态。"""

        if self.status is not RagIngestionStatus.EMBEDDING:
            raise ValueError("只有 embedding RAG ingestion job 可以耗尽")
        if self.embedding_attempts < self.embedding_max_attempts:
            raise ValueError("RAG embedding job 尚未耗尽尝试次数")
        if self.lease_until is not None and self.lease_until > now:
            raise ValueError("仍具有有效 lease 的 RAG embedding job 不能被耗尽")
        self.status = RagIngestionStatus.FAILED
        self.error_code = "RAG_EMBEDDING_ATTEMPTS_EXHAUSTED"
        self.next_retry_at = None
        self.failed_at = now
        self.claimed_by = None
        self.lease_until = None
        self.progress = replace(self.progress, active_executor=None)
        self.updated_at = now

    def fail(
        self,
        *,
        error_code: str,
        next_retry_at: datetime | None,
        now: datetime,
    ) -> None:
        if self.status not in _ACTIVE_INGESTION_STATUSES:
            raise ValueError("只有运行中的 RAG ingestion job 可以失败")
        if not error_code.strip():
            raise ValueError("RAG ingestion error_code 不能为空")
        if self.attempts >= self.max_attempts and next_retry_at is not None:
            raise ValueError("达到最大尝试次数后不能继续安排重试")
        if next_retry_at is not None and next_retry_at <= now:
            raise ValueError("RAG ingestion next_retry_at 必须晚于当前时间")
        self.status = RagIngestionStatus.FAILED
        self.error_code = error_code
        self.next_retry_at = next_retry_at
        self.failed_at = now
        self.claimed_by = None
        self.lease_until = None
        self.progress = replace(self.progress, active_executor=None)
        self.updated_at = now

    def cancel(self, *, now: datetime) -> None:
        if self.is_terminal:
            raise ValueError("终态 RAG ingestion job 不能取消")
        self.status = RagIngestionStatus.CANCELLED
        self.cancelled_at = now
        self.claimed_by = None
        self.lease_until = None
        self.next_retry_at = None
        self.progress = replace(self.progress, active_executor=None)
        self.updated_at = now

    def restart(self, *, now: datetime) -> None:
        """用户显式要求从解析起点重新执行同一个幂等作业。"""

        self.status = RagIngestionStatus.QUEUED
        self.attempts = 0
        self.embedding_attempts = 0
        self.claimed_by = None
        self.lease_until = None
        self.next_retry_at = None
        self.error_code = None
        self.progress = RagJobProgress()
        self.started_at = None
        self.completed_at = None
        self.failed_at = None
        self.cancelled_at = None
        self.updated_at = now


@dataclass(frozen=True)
class RagChunk:
    id: UUID
    document_id: UUID
    ingestion_job_id: UUID
    workspace_id: UUID
    ordinal: int
    content: str
    content_hash: str
    token_count: int
    source_locator: dict = field(default_factory=dict)
    embedding_key: str | None = None
    created_at: datetime = field(default_factory=utcnow)

    def __post_init__(self) -> None:
        if self.ordinal < 0:
            raise ValueError("RAG chunk ordinal 不能为负数")
        if not self.content.strip():
            raise ValueError("RAG chunk content 不能为空")
        _require_sha256(self.content_hash, "content_hash")
        if self.token_count <= 0:
            raise ValueError("RAG chunk token_count 必须大于 0")


@dataclass(frozen=True)
class RagElement:
    """表格、图片、图表、流程图和公式等非文本检索元素。"""

    id: UUID
    document_id: UUID
    workspace_id: UUID
    element_type: RagElementType
    page_number: int
    bounding_box: tuple[float, float, float, float]
    page_width: float
    page_height: float
    locator_key: str
    content_hash: str
    extraction_method: RagExtractionMethod
    extraction_version: str
    confidence: float
    caption_text: str = ""
    ocr_text: str = ""
    structured_data: dict = field(default_factory=dict)
    derived_description: str = ""
    created_at: datetime = field(default_factory=utcnow)

    def __post_init__(self) -> None:
        if self.page_number < 1:
            raise ValueError("RAG element page_number 必须从 1 开始")
        if self.page_width <= 0 or self.page_height <= 0:
            raise ValueError("RAG element 页面尺寸必须大于 0")
        x0, y0, x1, y1 = self.bounding_box
        if not (0 <= x0 < x1 <= self.page_width and 0 <= y0 < y1 <= self.page_height):
            raise ValueError("RAG element bounding_box 必须位于页面范围内")
        _require_sha256(self.locator_key, "locator_key")
        _require_sha256(self.content_hash, "content_hash")
        if not self.extraction_version.strip():
            raise ValueError("RAG element extraction_version 不能为空")
        if not 0.0 <= self.confidence <= 1.0:
            raise ValueError("RAG element confidence 必须在 0..1")


@dataclass(frozen=True)
class RagAsset:
    """非文本元素的受控二进制文件元数据；数据库不保存文件正文。"""

    id: UUID
    document_id: UUID
    element_id: UUID
    workspace_id: UUID
    asset_kind: RagAssetKind
    storage_reference: str
    mime_type: str
    content_hash: str
    size_bytes: int
    width: int | None = None
    height: int | None = None
    created_at: datetime = field(default_factory=utcnow)

    def __post_init__(self) -> None:
        reference = PurePosixPath(self.storage_reference)
        if (
            not self.storage_reference.strip()
            or self.storage_reference == "."
            or reference.is_absolute()
            or ".." in reference.parts
            or "\\" in self.storage_reference
            or (reference.parts and reference.parts[0].endswith(":"))
        ):
            raise ValueError("RAG asset storage_reference 必须是安全的内部相对引用")
        if not self.mime_type.strip():
            raise ValueError("RAG asset mime_type 不能为空")
        _require_sha256(self.content_hash, "content_hash")
        if self.size_bytes <= 0:
            raise ValueError("RAG asset size_bytes 必须大于 0")
        if (self.width is None) is not (self.height is None):
            raise ValueError("RAG asset width/height 必须同时提供或同时省略")
        if self.width is not None and (self.width <= 0 or self.height is None or self.height <= 0):
            raise ValueError("RAG asset 图片尺寸必须大于 0")


@dataclass(frozen=True)
class RagChunkElementLink:
    id: UUID
    document_id: UUID
    workspace_id: UUID
    chunk_id: UUID
    element_id: UUID
    relation_type: RagChunkElementRelation
    confidence: float
    order_index: int = 0
    created_at: datetime = field(default_factory=utcnow)

    def __post_init__(self) -> None:
        if not 0.0 <= self.confidence <= 1.0:
            raise ValueError("RAG chunk-element confidence 必须在 0..1")
        if self.order_index < 0:
            raise ValueError("RAG chunk-element order_index 不能为负数")


@dataclass(frozen=True)
class OcrSpan:
    text: str
    bounding_box: tuple[float, float, float, float]
    confidence: float

    def __post_init__(self) -> None:
        x0, y0, x1, y1 = self.bounding_box
        if not self.text.strip():
            raise ValueError("OCR span text 不能为空")
        if not (0 <= x0 < x1 and 0 <= y0 < y1):
            raise ValueError("OCR span bounding_box 非法")
        if not 0.0 <= self.confidence <= 1.0:
            raise ValueError("OCR span confidence 必须在 0..1")


@dataclass(frozen=True)
class OcrResult:
    text: str
    spans: tuple[OcrSpan, ...]
    language: str | None
    provider: str
    model_version: str

    def __post_init__(self) -> None:
        if not self.provider.strip() or not self.model_version.strip():
            raise ValueError("OCR provider/model_version 不能为空")


@dataclass(frozen=True)
class VisualDescription:
    text: str
    confidence: float
    provider: str
    model_version: str

    def __post_init__(self) -> None:
        if not self.text.strip():
            raise ValueError("视觉描述不能为空")
        if not 0.0 <= self.confidence <= 1.0:
            raise ValueError("视觉描述 confidence 必须在 0..1")
        if not self.provider.strip() or not self.model_version.strip():
            raise ValueError("视觉描述 provider/model_version 不能为空")


@dataclass(frozen=True)
class RagVectorRecord:
    chunk_id: UUID
    document_id: UUID
    workspace_id: UUID
    embedding: Sequence[float]
    content_hash: str
    provider_name: str
    model_name: str

    def __post_init__(self) -> None:
        _require_sha256(self.content_hash, "content_hash")
        if not self.provider_name.strip() or not self.model_name.strip():
            raise ValueError("RAG vector provider/model 不能为空")
        if not self.embedding:
            raise ValueError("RAG vector embedding 不能为空")


@dataclass(frozen=True)
class RagSearchMatch:
    chunk_id: UUID
    document_id: UUID
    workspace_id: UUID
    score: float


class EmbeddingProvider(Protocol):
    provider_name: str
    model_name: str
    dimensions: int

    async def embed_documents(self, texts: Sequence[str]) -> list[list[float]]: ...

    async def embed_query(self, text: str) -> list[float]: ...


class VectorIndex(Protocol):
    async def upsert(self, records: Sequence[RagVectorRecord]) -> None: ...

    async def search(
        self, *, workspace_id: UUID, query_vector: Sequence[float], limit: int
    ) -> list[RagSearchMatch]: ...

    async def delete_document(self, *, workspace_id: UUID, document_id: UUID) -> None: ...


class OcrProvider(Protocol):
    provider_name: str
    model_version: str

    async def recognize(
        self, *, image: bytes, mime_type: str, languages: Sequence[str]
    ) -> OcrResult: ...


class VisualDescriptionProvider(Protocol):
    provider_name: str
    model_version: str

    async def describe(
        self, *, image: bytes, mime_type: str, context: str
    ) -> VisualDescription: ...


class RagAssetFileStore(Protocol):
    def write(
        self,
        *,
        asset_id: UUID,
        content: bytes,
        expected_hash: str,
        mime_type: str | None = None,
    ) -> str: ...

    def read(self, asset: RagAsset) -> bytes: ...

    def delete(self, asset: RagAsset) -> None: ...
