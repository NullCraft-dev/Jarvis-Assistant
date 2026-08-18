"""Memory v2 候选确认闭环的业务不变量。"""

import asyncio
from datetime import timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest

import jarvis_worker.agent.memory.candidate_service as candidate_module
from jarvis_worker.agent.memory.candidate_service import (
    ExtractedMemoryCandidateInput,
    MemoryCandidateApplicationService,
    MemoryCandidateMaintenanceWorker,
    ResolveMemoryCandidateInput,
    UpdateMemoryCandidateInput,
)
from jarvis_worker.shared.domain.models import (
    AgentRun,
    MemoryCandidateStatus,
    RunStatus,
    Task,
    TaskStatus,
    WorkspaceSource,
    WorkspaceStatus,
    utcnow,
)
from jarvis_worker.shared.errors.application import AppError


class _MemoryRepo:
    def __init__(self):
        self.items = []

    async def get_by_identity(self, *, scope_type, workspace_id, category, key):
        return next((item for item in self.items if (
            item.scope_type.value == scope_type and item.workspace_id == workspace_id
            and item.category.value == category and item.key == key
        )), None)

    async def create(self, memory):
        self.items.append(memory)
        return memory


class _CandidateRepo:
    def __init__(self):
        self.items = []

    async def create(self, candidate):
        self.items.append(candidate)
        return candidate

    async def get_for_update(self, candidate_id):
        return next((item for item in self.items if item.id == candidate_id), None)

    async def get_pending_by_deduplication_key(self, deduplication_key):
        return next((item for item in self.items if (
            item.status is MemoryCandidateStatus.PENDING
            and item.deduplication_key == deduplication_key
        )), None)

    async def list_filtered(self, *, status=None, workspace_id=None, limit=100):
        items = self.items
        if status:
            items = [item for item in items if item.status.value == status]
        if workspace_id:
            items = [item for item in items if item.workspace_id == workspace_id]
        return items[:limit]

    async def update(self, candidate):
        return None

    async def list_due_for_update(self, *, now, limit=100):
        return [item for item in self.items if (
            item.status is MemoryCandidateStatus.PENDING
            and item.expires_at is not None
            and item.expires_at <= now
        )][:limit]


class _RepoByID:
    def __init__(self, item):
        self.item = item

    async def get(self, item_id):
        return self.item if self.item and self.item.id == item_id else None


class _AuditRepo:
    def __init__(self):
        self.items = []

    async def create(self, item):
        self.items.append(item)
        return item


class _FakeUow:
    def __init__(self, task, run, workspace=None):
        self.tasks = _RepoByID(task)
        self.runs = _RepoByID(run)
        self.workspaces = _RepoByID(workspace)
        self.memories = _MemoryRepo()
        self.memory_candidates = _CandidateRepo()
        self.audits = _AuditRepo()
        self.commits = 0

    def transaction(self):
        return self

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return None

    async def commit(self):
        self.commits += 1

    async def rollback(self):
        return None


def _service(monkeypatch, *, workspace=False):
    task_id, run_id = uuid4(), uuid4()
    workspace_id = uuid4() if workspace else None
    task = Task(
        id=task_id, title="done", user_goal="记住以后使用中文", conversation_id=uuid4(),
        workspace_id=workspace_id, status=TaskStatus.COMPLETED,
    )
    run = AgentRun(id=run_id, task_id=task_id, status=RunStatus.COMPLETED)
    ws = None
    if workspace:
        ws = SimpleNamespace(
            id=workspace_id, status=WorkspaceStatus.ACTIVE, source=WorkspaceSource.USER_PICKER
        )
    uow = _FakeUow(task, run, ws)
    monkeypatch.setattr(candidate_module, "PostgresUnitOfWork", lambda session: session)
    service = MemoryCandidateApplicationService(lambda: lambda: uow)
    return service, uow, task, run


def _input(task, run, **overrides):
    data = {
        "scope_type": "global",
        "category": "preference",
        "suggested_key": "response.language",
        "content": "用户偏好使用中文回复",
        "source_task_id": task.id,
        "source_run_id": run.id,
        "extraction_input_fingerprint": "a" * 64,
        "confidence": 0.95,
        "importance": 80,
        "extraction_policy_version": "memory-extraction-v1",
        "extractor_provider": "fake",
        "extractor_model": "fake-v1",
    }
    data.update(overrides)
    return ExtractedMemoryCandidateInput(**data)


@pytest.mark.asyncio
async def test_candidate_is_not_a_formal_memory_until_user_approves(monkeypatch):
    service, uow, task, run = _service(monkeypatch)
    candidate = await service.create_candidate(_input(task, run))

    assert candidate is not None
    assert candidate.status is MemoryCandidateStatus.PENDING
    assert uow.memories.items == []

    approved, memory = await service.approve_candidate(
        candidate.id, ResolveMemoryCandidateInput(expected_version=1)
    )

    assert approved.status is MemoryCandidateStatus.APPROVED
    assert approved.approved_memory_id == memory.id
    assert memory.source_type.value == "candidate_approved"
    assert uow.memories.items == [memory]


@pytest.mark.asyncio
async def test_rejected_candidate_never_creates_memory(monkeypatch):
    service, uow, task, run = _service(monkeypatch)
    candidate = await service.create_candidate(_input(task, run))
    rejected = await service.reject_candidate(
        candidate.id, ResolveMemoryCandidateInput(expected_version=1, note="一次性要求")
    )
    assert rejected.status is MemoryCandidateStatus.REJECTED
    assert rejected.resolution_note == "一次性要求"
    assert uow.memories.items == []
    with pytest.raises(AppError, match="已经处理"):
        await service.approve_candidate(
            candidate.id, ResolveMemoryCandidateInput(expected_version=2)
        )


@pytest.mark.asyncio
async def test_duplicate_pending_candidate_is_suppressed_across_extractions(monkeypatch):
    service, uow, task, run = _service(monkeypatch)
    first = await service.create_candidate(_input(task, run))
    duplicate = await service.create_candidate(
        _input(
            task,
            run,
            extraction_policy_version="memory-extraction-v2",
            extraction_input_fingerprint="b" * 64,
        )
    )

    assert first is not None
    assert duplicate is None
    assert uow.memory_candidates.items == [first]


@pytest.mark.asyncio
async def test_candidate_edit_uses_optimistic_version(monkeypatch):
    service, _, task, run = _service(monkeypatch)
    candidate = await service.create_candidate(_input(task, run))
    updated = await service.update_candidate(
        candidate.id,
        UpdateMemoryCandidateInput(
            expected_version=1, content="用户明确偏好简洁中文回复", importance=90
        ),
    )
    assert updated.version == 2
    assert updated.importance == 90
    with pytest.raises(AppError, match="刷新后重试"):
        await service.update_candidate(
            candidate.id,
            UpdateMemoryCandidateInput(expected_version=1, content="旧请求"),
        )


@pytest.mark.asyncio
async def test_sensitive_candidate_is_not_persisted(monkeypatch):
    service, uow, task, run = _service(monkeypatch)
    with pytest.raises(AppError, match="敏感内容"):
        await service.create_candidate(_input(task, run, sensitivity="sensitive"))
    assert uow.memory_candidates.items == []
    assert uow.memories.items == []


@pytest.mark.asyncio
async def test_sensitive_pattern_is_rejected_even_if_model_marks_normal(monkeypatch):
    service, uow, task, run = _service(monkeypatch)
    with pytest.raises(AppError, match="敏感内容"):
        await service.create_candidate(
            _input(task, run, content="api_key=sk-example-secret-123456789")
        )
    assert uow.memory_candidates.items == []


@pytest.mark.asyncio
async def test_expired_candidate_cannot_be_approved(monkeypatch):
    service, uow, task, run = _service(monkeypatch)
    candidate = await service.create_candidate(
        _input(task, run, expires_at=utcnow() - timedelta(seconds=1))
    )
    with pytest.raises(AppError, match="已过期"):
        await service.approve_candidate(
            candidate.id, ResolveMemoryCandidateInput(expected_version=1)
        )
    assert candidate.status is MemoryCandidateStatus.EXPIRED
    assert uow.memories.items == []


@pytest.mark.asyncio
async def test_due_candidates_are_expired_by_maintenance_with_audit(monkeypatch):
    service, uow, task, run = _service(monkeypatch)
    now = utcnow()
    due = await service.create_candidate(
        _input(task, run, expires_at=now - timedelta(seconds=1))
    )

    assert await service.expire_due_candidates(now=now) == 1
    assert due.status is MemoryCandidateStatus.EXPIRED
    assert due.resolved_at == now
    assert any(
        item.event_type == "memory.candidate.expired" for item in uow.audits.items
    )


@pytest.mark.asyncio
async def test_candidate_maintenance_worker_runs_and_stops_cleanly():
    class Service:
        def __init__(self):
            self.called = asyncio.Event()

        async def expire_due_candidates(self):
            self.called.set()
            return 0

    service = Service()
    worker = MemoryCandidateMaintenanceWorker(service, poll_interval=1)
    await worker.start()
    await asyncio.wait_for(service.called.wait(), timeout=1)
    await worker.stop()
    assert worker._task is None


@pytest.mark.asyncio
async def test_edit_clears_conflict_when_content_matches_existing_memory(monkeypatch):
    service, _, task, run = _service(monkeypatch)
    first = await service.create_candidate(_input(task, run))
    await service.approve_candidate(
        first.id, ResolveMemoryCandidateInput(expected_version=1)
    )
    conflicting = await service.create_candidate(
        _input(task, run, content="用户偏好使用英文回复")
    )
    assert conflicting.conflict_memory_id is not None

    updated = await service.update_candidate(
        conflicting.id,
        UpdateMemoryCandidateInput(
            expected_version=1,
            content="用户偏好使用中文回复",
        ),
    )
    assert updated.conflict_memory_id is None


@pytest.mark.asyncio
async def test_workspace_candidate_must_match_source_task(monkeypatch):
    service, uow, task, run = _service(monkeypatch, workspace=True)
    with pytest.raises(AppError, match="工作区与来源任务不一致"):
        await service.create_candidate(_input(
            task, run, scope_type="workspace", workspace_id=uuid4(),
            category="project_fact", suggested_key="project.stack",
        ))
    assert uow.memory_candidates.items == []
