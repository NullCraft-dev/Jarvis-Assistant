"""RAG 评估轨迹与人工标签持久化端口。"""

from abc import ABC, abstractmethod
from uuid import UUID

from .contracts import (
    RagEvaluationFeedback,
    RagEvaluationLabel,
    RagEvaluationTrace,
    RagQualityGateRun,
    RagQualityIssue,
)


class RagEvaluationTraceRepository(ABC):
    @abstractmethod
    async def create(self, trace: RagEvaluationTrace) -> RagEvaluationTrace: ...

    @abstractmethod
    async def get(self, trace_id: UUID) -> RagEvaluationTrace | None: ...

    @abstractmethod
    async def list_unreviewed(self, *, limit: int = 100) -> list[RagEvaluationTrace]: ...

    @abstractmethod
    async def list_filtered(
        self, *, privacy_status: str | None = None, workspace_id: UUID | None = None,
        limit: int = 100
    ) -> list[RagEvaluationTrace]: ...

    @abstractmethod
    async def set_privacy_status(self, trace_id: UUID, status: str) -> bool: ...

    @abstractmethod
    async def get_latest_for_run(self, run_id: UUID) -> RagEvaluationTrace | None: ...


class RagEvaluationLabelRepository(ABC):
    @abstractmethod
    async def create(self, label: RagEvaluationLabel) -> RagEvaluationLabel: ...

    @abstractmethod
    async def save(self, label: RagEvaluationLabel) -> RagEvaluationLabel: ...

    @abstractmethod
    async def get_for_trace(self, trace_id: UUID) -> RagEvaluationLabel | None: ...

    @abstractmethod
    async def get_confirmed_for_trace(self, trace_id: UUID) -> RagEvaluationLabel | None: ...

    @abstractmethod
    async def list_confirmed(self, *, limit: int = 100) -> list[RagEvaluationLabel]: ...


class RagEvaluationFeedbackRepository(ABC):
    @abstractmethod
    async def create_or_get(self, feedback: RagEvaluationFeedback) -> RagEvaluationFeedback: ...

    @abstractmethod
    async def get(self, feedback_id: UUID) -> RagEvaluationFeedback | None: ...

    @abstractmethod
    async def list_by_workspace(
        self, *, workspace_id: UUID, status: str | None = "pending", limit: int = 100
    ) -> list[RagEvaluationFeedback]: ...

    @abstractmethod
    async def set_review(
        self, feedback_id: UUID, *, status: str, failure_category: str | None = None
    ) -> RagEvaluationFeedback | None: ...


class RagQualityGateRunRepository(ABC):
    @abstractmethod
    async def create(self, run: RagQualityGateRun) -> RagQualityGateRun: ...

    @abstractmethod
    async def get(self, run_id: UUID) -> RagQualityGateRun | None: ...

    @abstractmethod
    async def list_latest(self, *, limit: int = 20) -> list[RagQualityGateRun]: ...


class RagQualityIssueRepository(ABC):
    @abstractmethod
    async def create(self, issue: RagQualityIssue) -> RagQualityIssue: ...
    @abstractmethod
    async def get(self, issue_id: UUID) -> RagQualityIssue | None: ...
    @abstractmethod
    async def get_by_candidate_id(self, candidate_id: str) -> RagQualityIssue | None: ...
    @abstractmethod
    async def list_resolved(self, *, gate_id: str, cohort_id: str) -> list[RagQualityIssue]: ...
    @abstractmethod
    async def list_filtered(
        self, *, status: str | None, owner: str | None,
        failure_type: str | None, limit: int,
    ) -> list[RagQualityIssue]: ...
    @abstractmethod
    async def count_by_status(self) -> dict[str, int]: ...
    @abstractmethod
    async def save(self, issue: RagQualityIssue, *, expected_version: int) -> RagQualityIssue | None: ...
