"""异步 MemoryExtractor 的结构化输出与执行编排测试。"""

from __future__ import annotations

import json
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest

from jarvis_worker.agent.memory.deepseek_extractor import (
    DeepSeekMemoryExtractor,
    MemoryExtractionError,
)
from jarvis_worker.agent.memory.extraction_service import (
    MemoryExtractionApplicationService,
)
from jarvis_worker.agent.memory.extractor import (
    ExistingMemoryReference,
    ExtractedMemoryCandidateSpec,
    MemoryExtractionInput,
)
from jarvis_worker.shared.domain.models import MemoryExtractionJob
from jarvis_worker.shared.domain.models import AgentRun, RunStatus, Task, TaskStatus, utcnow
from jarvis_worker.runtime.events import build_envelope, build_runtime_event
from jarvis_worker.runtime.service import RuntimeApplicationService


def _input() -> MemoryExtractionInput:
    return MemoryExtractionInput(
        source_task_id=uuid4(),
        source_run_id=uuid4(),
        workspace_id=uuid4(),
        user_goal="以后默认使用中文回答",
        final_response="好的。",
        source_message_ids=(uuid4(), uuid4()),
        input_fingerprint="a" * 64,
    )


def _extractor(monkeypatch, content: object, *, status: int = 200):
    monkeypatch.setenv("TEST_DEEPSEEK_KEY", "test-secret")

    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["Authorization"] == "Bearer test-secret"
        return httpx.Response(
            status,
            json={
                "choices": [
                    {
                        "finish_reason": "stop",
                        "message": {"content": json.dumps(content, ensure_ascii=False)},
                    }
                ]
            },
        )

    transport = httpx.MockTransport(handler)
    return DeepSeekMemoryExtractor(
        base_url="https://api.deepseek.test",
        model="deepseek-test",
        api_key_env="TEST_DEEPSEEK_KEY",
        thinking_mode="disabled",
        client_factory=lambda: httpx.AsyncClient(transport=transport),
    )


@pytest.mark.asyncio
async def test_deepseek_extractor_accepts_strict_candidate_json(monkeypatch):
    extractor = _extractor(
        monkeypatch,
        {
            "candidates": [
                {
                    "scope_type": "global",
                    "category": "preference",
                    "suggested_key": "response.language",
                    "content": "用户偏好使用中文回答。",
                    "confidence": 0.96,
                    "importance": 80,
                    "evidence_source": "user_goal",
                    "evidence_quote": "以后默认使用中文回答",
                    "sensitivity": "normal",
                }
            ]
        },
    )

    result = await extractor.extract(_input())

    assert len(result) == 1
    assert result[0].suggested_key == "response.language"
    assert extractor.provider_name == "deepseek"


@pytest.mark.asyncio
async def test_deepseek_extractor_rejects_unknown_category(monkeypatch):
    extractor = _extractor(
        monkeypatch,
        {
            "candidates": [
                {
                    "scope_type": "global",
                    "category": "temporary",
                    "suggested_key": "response.language",
                    "content": "中文",
                    "confidence": 0.9,
                    "importance": 80,
                    "evidence_source": "user_goal",
                    "evidence_quote": "以后默认使用中文回答",
                    "sensitivity": "normal",
                }
            ]
        },
    )

    with pytest.raises(MemoryExtractionError) as exc:
        await extractor.extract(_input())
    assert exc.value.code == "MEMORY_EXTRACTOR_OUTPUT_INVALID"
    assert exc.value.recoverable is False


class _FakeExtractor:
    provider_name = "fake"
    model_name = "fake-v1"

    def __init__(self, specs=None, error: Exception | None = None):
        self.specs = specs or []
        self.error = error

    async def extract(self, extraction_input):
        if self.error:
            raise self.error
        return self.specs


class _FakeCandidateService:
    def __init__(self):
        self.inputs = []

    async def create_candidate(self, data):
        self.inputs.append(data)
        return object()


@pytest.mark.asyncio
async def test_processor_filters_low_value_and_completes_without_blocking(monkeypatch):
    high = ExtractedMemoryCandidateSpec(
        scope_type="global", category="preference",
        suggested_key="response.language", content="使用中文",
        confidence=0.95, importance=80, evidence_source="user_goal",
        evidence_quote="以后默认使用中文回答",
    )
    low = ExtractedMemoryCandidateSpec(
        scope_type="global", category="preference",
        suggested_key="response.temporary", content="临时要求",
        confidence=0.5, importance=20, evidence_source="user_goal",
        evidence_quote="以后默认使用中文回答",
    )
    service = MemoryExtractionApplicationService(
        lambda: None, _FakeExtractor([high, low])
    )
    candidate_service = _FakeCandidateService()
    service._candidate_service = candidate_service
    job = MemoryExtractionJob(
        id=uuid4(), source_task_id=uuid4(), source_run_id=uuid4(),
        extraction_policy_version="memory-extraction-v1", attempts=1,
    )
    extraction_input = _input()
    completed = {}

    async def claim():
        return job

    async def load(_job):
        return extraction_input

    async def mark(_job, *, created, skipped):
        completed.update(created=created, skipped=skipped)

    service._claim_next = claim
    service._load_input = load
    service._mark_completed = mark

    assert await service.process_next() is True
    assert len(candidate_service.inputs) == 1
    assert completed == {"created": 1, "skipped": 1}


@pytest.mark.asyncio
async def test_processor_rejects_preference_derived_from_assistant_response():
    repeated = ExtractedMemoryCandidateSpec(
        scope_type="workspace",
        category="rule",
        suggested_key="report.weekly.structure",
        content="项目周报先给出三条摘要，再展开详细内容",
        confidence=0.95,
        importance=80,
        evidence_source="final_response",
        evidence_quote="先给出三条摘要，再展开详细内容",
    )
    service = MemoryExtractionApplicationService(
        lambda: None, _FakeExtractor([repeated])
    )
    candidate_service = _FakeCandidateService()
    service._candidate_service = candidate_service
    job = MemoryExtractionJob(
        id=uuid4(), source_task_id=uuid4(), source_run_id=uuid4(),
        extraction_policy_version="memory-extraction-v2", attempts=1,
    )
    completed = {}

    async def claim():
        return job

    async def load(_job):
        return _input()

    async def mark(_job, *, created, skipped):
        completed.update(created=created, skipped=skipped)

    service._claim_next = claim
    service._load_input = load
    service._mark_completed = mark

    assert await service.process_next() is True
    assert candidate_service.inputs == []
    assert completed == {"created": 0, "skipped": 1}


@pytest.mark.asyncio
async def test_processor_rejects_question_as_preference_evidence():
    inferred = ExtractedMemoryCandidateSpec(
        scope_type="workspace",
        category="preference",
        suggested_key="report.weekly.format",
        content="项目周报默认先给出三条摘要",
        confidence=0.95,
        importance=80,
        evidence_source="user_goal",
        evidence_quote="请告诉我项目周报的默认结构是什么",
    )
    service = MemoryExtractionApplicationService(
        lambda: None, _FakeExtractor([inferred])
    )
    candidate_service = _FakeCandidateService()
    service._candidate_service = candidate_service
    job = MemoryExtractionJob(
        id=uuid4(), source_task_id=uuid4(), source_run_id=uuid4(),
        extraction_policy_version="memory-extraction-v2", attempts=1,
    )
    completed = {}

    async def claim():
        return job

    async def load(_job):
        base = _input()
        return MemoryExtractionInput(
            **{**base.__dict__, "user_goal": "请告诉我项目周报的默认结构是什么"}
        )

    async def mark(_job, *, created, skipped):
        completed.update(created=created, skipped=skipped)

    service._claim_next = claim
    service._load_input = load
    service._mark_completed = mark

    assert await service.process_next() is True
    assert candidate_service.inputs == []
    assert completed == {"created": 0, "skipped": 1}


@pytest.mark.asyncio
async def test_processor_rejects_existing_memory_key():
    duplicate = ExtractedMemoryCandidateSpec(
        scope_type="workspace",
        category="preference",
        suggested_key="report.weekly.structure",
        content="项目周报先给出三条摘要，再展开详细内容",
        confidence=0.95,
        importance=80,
        evidence_source="user_goal",
        evidence_quote="以后默认先给出三条摘要，再展开详细内容",
    )
    service = MemoryExtractionApplicationService(
        lambda: None, _FakeExtractor([duplicate])
    )
    candidate_service = _FakeCandidateService()
    service._candidate_service = candidate_service
    job = MemoryExtractionJob(
        id=uuid4(), source_task_id=uuid4(), source_run_id=uuid4(),
        extraction_policy_version="memory-extraction-v2", attempts=1,
    )
    extraction_input = MemoryExtractionInput(
        **{
            **_input().__dict__,
            "user_goal": "以后默认先给出三条摘要，再展开详细内容",
            "existing_memories": (
                ExistingMemoryReference(
                    key="report.weekly.structure",
                    content="项目周报先给出三条摘要，再展开详细内容",
                ),
            ),
        }
    )
    completed = {}

    async def claim():
        return job

    async def load(_job):
        return extraction_input

    async def mark(_job, *, created, skipped):
        completed.update(created=created, skipped=skipped)

    service._claim_next = claim
    service._load_input = load
    service._mark_completed = mark

    assert await service.process_next() is True
    assert candidate_service.inputs == []
    assert completed == {"created": 0, "skipped": 1}


@pytest.mark.asyncio
async def test_processor_schedules_recoverable_failure(monkeypatch):
    error = MemoryExtractionError(
        "MEMORY_EXTRACTOR_TIMEOUT", "timeout", recoverable=True
    )
    service = MemoryExtractionApplicationService(
        lambda: None, _FakeExtractor(error=error)
    )
    job = MemoryExtractionJob(
        id=uuid4(), source_task_id=uuid4(), source_run_id=uuid4(),
        extraction_policy_version="memory-extraction-v1", attempts=1,
    )
    failed = []

    async def claim():
        return job

    async def load(_job):
        return _input()

    async def mark_failed(_job, exc):
        failed.append(exc)

    service._claim_next = claim
    service._load_input = load
    service._mark_failed = mark_failed

    assert await service.process_next() is True
    assert failed == [error]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("scheduled_execution_id", "expected_job_count"),
    [(None, 1), (uuid4(), 0)],
    ids=("interactive-run", "scheduled-run"),
)
async def test_completed_run_projection_only_enqueues_extraction_for_interactive_run(
    scheduled_execution_id,
    expected_job_count,
):
    task_id, run_id, trace_id = uuid4(), uuid4(), uuid4()
    task = Task(
        id=task_id,
        title="done",
        user_goal="以后默认使用中文",
        conversation_id=uuid4(),
        status=TaskStatus.RUNNING,
        scheduled_execution_id=scheduled_execution_id,
    )
    run = AgentRun(id=run_id, task_id=task_id, status=RunStatus.RUNNING)

    class Runs:
        async def update_with_lock(self, **kwargs):
            return True

    class Tasks:
        async def update(self, item):
            return None

    class Permissions:
        async def list_pending_by_run(self, _run_id):
            return []

    class Collector:
        def __init__(self):
            self.items = []

        async def create(self, item):
            self.items.append(item)
            return item

    class Jobs(Collector):
        async def get_by_run_policy(self, _run_id, _policy):
            return None

    messages, jobs = Collector(), Jobs()
    tx = SimpleNamespace(
        runs=Runs(),
        tasks=Tasks(),
        permissions=Permissions(),
        messages=messages,
        memory_extraction_jobs=jobs,
    )
    event = build_runtime_event(
        "agent.run.completed",
        str(task_id),
        str(run_id),
        payload={"output": "好的，以后使用中文。"},
    )
    envelope = build_envelope(event, str(trace_id), "worker-test")

    await RuntimeApplicationService(lambda: None)._apply_projection(
        tx,
        envelope,
        run,
        task,
        task_id,
        run_id,
        None,
        utcnow(),
    )

    assert task.status is TaskStatus.COMPLETED
    assert run.status is RunStatus.COMPLETED
    assert len(messages.items) == 1
    assert len(jobs.items) == expected_job_count
    if jobs.items:
        assert jobs.items[0].source_run_id == run_id
