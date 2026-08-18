from dataclasses import replace
from datetime import datetime, timezone
from types import SimpleNamespace
from uuid import uuid4

import pytest

from jarvis_worker.agent.rag.contracts import RagChunk
from jarvis_worker.agent.rag.evaluation import feedback_service as feedback_module
from jarvis_worker.agent.rag.evaluation import review_service as review_module
from jarvis_worker.agent.rag.evaluation.contracts import RagEvaluationTrace
from jarvis_worker.agent.rag.evaluation.feedback_service import RagEvaluationFeedbackService
from jarvis_worker.agent.rag.evaluation.postgres import _feedback, _label
from jarvis_worker.agent.rag.evaluation.review_service import RagEvaluationReviewService
from jarvis_worker.shared.domain.models import Message


class _Messages:
    def __init__(self, message):
        self.message = message

    async def get(self, message_id):
        return self.message if self.message.id == message_id else None


class _Traces:
    def __init__(self, trace):
        self.trace = trace

    async def get_latest_for_run(self, run_id):
        return self.trace if self.trace.run_id == run_id else None

    async def get(self, trace_id):
        return self.trace if self.trace.id == trace_id else None

    async def list_filtered(self, *, privacy_status=None, workspace_id=None, limit=100):
        if privacy_status is not None and self.trace.privacy_status != privacy_status:
            return []
        if workspace_id is not None and self.trace.workspace_id != workspace_id:
            return []
        return [self.trace][:limit]

    async def set_privacy_status(self, trace_id, status):
        if self.trace.id == trace_id:
            self.trace = replace(self.trace, privacy_status=status)


class _Feedback:
    def __init__(self):
        self.by_fingerprint = {}

    async def create_or_get(self, feedback):
        existing = self.by_fingerprint.get(feedback.fingerprint)
        value = replace(existing, kind=feedback.kind, status="pending") if existing else feedback
        self.by_fingerprint[feedback.fingerprint] = value
        return value

    async def get(self, feedback_id):
        return next((item for item in self.by_fingerprint.values() if item.id == feedback_id), None)

    async def list_by_workspace(self, *, workspace_id, status, limit):
        return [
            item
            for item in self.by_fingerprint.values()
            if item.workspace_id == workspace_id and item.status == status
        ][:limit]

    async def set_review(self, feedback_id, *, status, failure_category=None):
        current = await self.get(feedback_id)
        if current is None:
            return None
        updated = replace(current, status=status, failure_category=failure_category)
        self.by_fingerprint[current.fingerprint] = updated
        return updated


class _Audits:
    def __init__(self):
        self.items = []

    async def create(self, item):
        self.items.append(item)
        return item


class _Chunks:
    def __init__(self, chunks): self.chunks = chunks
    async def list_by_ids(self, *, workspace_id, chunk_ids):
        return [item for item in self.chunks if item.workspace_id == workspace_id and item.id in chunk_ids]


class _Labels:
    def __init__(self):
        self.label = None

    async def get_for_trace(self, trace_id):
        return self.label if self.label and self.label.trace_id == trace_id else None

    async def create(self, label):
        self.label = label
        return label

    async def save(self, label):
        self.label = label
        return label


class _Uow:
    def __init__(self, message, trace, chunks):
        self.messages = _Messages(message)
        self.rag_evaluation_traces = _Traces(trace)
        self.rag_evaluation_feedback = _Feedback()
        self.rag_chunks = _Chunks(chunks)
        self.rag_evaluation_labels = _Labels()
        self.audits = _Audits()
        self.commits = 0

    def transaction(self):
        return self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return None

    async def commit(self):
        self.commits += 1


def _service(monkeypatch):
    task_id, run_id, workspace_id, chunk_id = uuid4(), uuid4(), uuid4(), uuid4()
    message = Message(
        id=uuid4(),
        conversation_id=uuid4(),
        task_id=task_id,
        run_id=run_id,
        role="assistant",
        content="answer",
    )
    chunk = RagChunk(id=chunk_id, document_id=uuid4(), ingestion_job_id=uuid4(), workspace_id=workspace_id, ordinal=0, content="private evidence body", content_hash="b" * 64, token_count=3)
    trace = RagEvaluationTrace(
        id=uuid4(),
        workspace_id=workspace_id,
        task_id=task_id,
        run_id=run_id,
        step_id=None,
        query="question",
        query_hash="a" * 64,
        request={},
        pipeline_versions={"retriever": "hybrid-v1"},
        candidate_ranking=({"chunk_id": str(chunk_id), "rank": 1, "sources": ["semantic"]},),
        reranked_ranking=({"chunk_id": str(chunk_id), "rank": 1, "sources": ["semantic", "reranker"]},),
        context_chunk_ids=(chunk_id,),
        context_truncated=False,
        result_count=1,
    )
    uow = _Uow(message, trace, [chunk])
    monkeypatch.setattr(feedback_module, "PostgresUnitOfWork", lambda session: session)
    return RagEvaluationFeedbackService(lambda: lambda: uow), uow, message, trace, chunk_id


def _review_service(monkeypatch):
    _feedback_service, uow, _message, trace, chunk_id = _service(monkeypatch)
    monkeypatch.setattr(review_module, "PostgresUnitOfWork", lambda session: session)
    return RagEvaluationReviewService(lambda: lambda: uow), uow, trace, chunk_id


@pytest.mark.asyncio
async def test_answer_feedback_is_deduplicated_and_can_be_revised(monkeypatch):
    service, uow, message, _trace, _chunk_id = _service(monkeypatch)

    first = await service.submit(message_id=message.id, kind="helpful")
    revised = await service.submit(message_id=message.id, kind="unhelpful")

    assert revised.id == first.id
    assert revised.kind == "unhelpful"
    assert len(uow.rag_evaluation_feedback.by_fingerprint) == 1
    assert [item.event_type for item in uow.audits.items] == [
        "rag.feedback.submitted",
        "rag.feedback.submitted",
    ]


@pytest.mark.asyncio
async def test_citation_feedback_must_reference_actual_context_chunk(monkeypatch):
    service, _uow, message, _trace, _chunk_id = _service(monkeypatch)

    with pytest.raises(ValueError, match="不属于该次 RAG 上下文"):
        await service.submit(
            message_id=message.id,
            kind="citation_incorrect",
            citation_chunk_id=uuid4(),
        )


@pytest.mark.asyncio
async def test_review_queue_is_hash_only_and_does_not_create_gold_label(monkeypatch):
    service, uow, message, trace, _chunk_id = _service(monkeypatch)
    feedback = await service.submit(message_id=message.id, kind="evidence_insufficient")

    queue = await service.list_queue(workspace_id=trace.workspace_id)
    resolved = await service.resolve(feedback.id, status="reviewed")

    assert queue[0].query_hash == "a" * 64
    assert not hasattr(queue[0], "query")
    assert resolved.status == "reviewed"
    assert uow.audits.items[-1].actor == "operator"


@pytest.mark.asyncio
async def test_inspect_redacts_content_until_privacy_is_approved(monkeypatch):
    service, uow, message, trace, _chunk_id = _service(monkeypatch)
    feedback = await service.submit(message_id=message.id, kind="unhelpful")

    redacted = await service.inspect(feedback.id)
    uow.rag_evaluation_traces.trace = replace(trace, privacy_status="approved")
    approved = await service.inspect(feedback.id)

    assert redacted.query is None and redacted.evidence[0].snippet is None
    assert approved.query == "question"
    assert approved.evidence[0].snippet == "private evidence body"


@pytest.mark.asyncio
async def test_triage_creates_only_user_feedback_draft_after_privacy_review(monkeypatch):
    service, uow, message, trace, chunk_id = _service(monkeypatch)
    feedback = await service.submit(message_id=message.id, kind="unhelpful")
    uow.rag_evaluation_traces.trace = replace(trace, privacy_status="approved")

    updated, label = await service.triage(
        feedback.id, failure_category="answer_generation", positive_chunk_ids=(chunk_id,)
    )

    assert updated.failure_category == "answer_generation"
    assert updated.status == "reviewed"
    assert label is not None and label.status == "draft" and label.source == "user_feedback"
    assert uow.audits.items[-1].event_type == "rag.feedback.triaged"


def test_postgres_mappers_keep_failure_category_on_feedback_not_label():
    now = datetime.now(timezone.utc)
    label = _label(SimpleNamespace(
        id=uuid4(), trace_id=uuid4(), positive_chunk_ids_json=[str(uuid4())],
        hard_negative_chunk_ids_json=[], source="human_review", status="confirmed",
        notes="", created_at=now, updated_at=now,
    ))
    feedback = _feedback(SimpleNamespace(
        id=uuid4(), trace_id=uuid4(), workspace_id=uuid4(), task_id=uuid4(), run_id=uuid4(),
        message_id=uuid4(), kind="unhelpful", citation_chunk_id=None, status="reviewed",
        failure_category="answer_generation", fingerprint="a" * 64,
        created_at=now, updated_at=now,
    ))

    assert label.status == "confirmed"
    assert feedback.failure_category == "answer_generation"


@pytest.mark.asyncio
async def test_review_service_scopes_privacy_label_and_promotion_with_audits(monkeypatch):
    service, uow, trace, chunk_id = _review_service(monkeypatch)

    assert await service.list_traces(workspace_id=uuid4()) == ()
    with pytest.raises(ValueError, match="不属于当前 Workspace"):
        await service.inspect(trace.id, workspace_id=uuid4())

    await service.review_privacy(trace.id, approved=True, workspace_id=trace.workspace_id)
    label = await service.set_label(
        trace_id=trace.id,
        workspace_id=trace.workspace_id,
        positive_chunk_ids=(chunk_id,),
        status="confirmed",
        notes="human checked",
    )
    promoted = await service.promote(trace.id, workspace_id=trace.workspace_id)

    assert label.source == "human_review" and label.status == "confirmed"
    assert promoted.label is not None and promoted.label.status == "promoted"
    assert [item.event_type for item in uow.audits.items[-3:]] == [
        "rag.evaluation.privacy_reviewed",
        "rag.evaluation.label_reviewed",
        "rag.evaluation.label_promoted",
    ]
    with pytest.raises(ValueError, match="已晋升标签不可直接修改"):
        await service.set_label(
            trace_id=trace.id,
            workspace_id=trace.workspace_id,
            positive_chunk_ids=(chunk_id,),
            status="draft",
        )
