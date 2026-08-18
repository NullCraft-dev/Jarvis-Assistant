"""用户 RAG 反馈回流与内部审核队列。"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from uuid import UUID, uuid4

from jarvis_worker.database.unit_of_work import PostgresUnitOfWork
from jarvis_worker.shared.domain.models import AuditLog

from .contracts import (
    FailureCategory,
    FeedbackKind,
    FeedbackStatus,
    RagEvaluationFeedback,
    RagEvaluationLabel,
    RagEvaluationTrace,
)

FAILURE_CATEGORIES = frozenset(
    {
        "candidate_miss", "reranker_miss", "context_omission", "context_truncated",
        "citation_mismatch", "answer_generation", "insufficient_evidence", "other",
    }
)


@dataclass(frozen=True, slots=True)
class RagFeedbackReviewItem:
    feedback: RagEvaluationFeedback
    query_hash: str
    pipeline_versions: dict[str, str]
    result_count: int
    context_truncated: bool


@dataclass(frozen=True, slots=True)
class RagFeedbackEvidence:
    chunk_id: UUID
    document_id: UUID
    content_hash: str
    candidate_rank: int | None
    reranked_rank: int | None
    in_context: bool
    sources: tuple[str, ...]
    snippet: str | None


@dataclass(frozen=True, slots=True)
class RagFeedbackReviewDetail:
    feedback: RagEvaluationFeedback
    query_hash: str
    query: str | None
    privacy_status: str
    pipeline_versions: dict[str, str]
    result_count: int
    context_truncated: bool
    evidence: tuple[RagFeedbackEvidence, ...]
    label: RagEvaluationLabel | None


class RagEvaluationFeedbackService:
    """反馈只生成候选；不修改 trace 隐私状态或 label 生命周期。"""

    def __init__(self, uow_factory) -> None:
        self._uow_factory = uow_factory

    async def submit(
        self,
        *,
        message_id: UUID,
        kind: FeedbackKind,
        citation_chunk_id: UUID | None = None,
    ) -> RagEvaluationFeedback:
        if kind == "citation_incorrect" and citation_chunk_id is None:
            raise ValueError("引用问题必须指定 citation_chunk_id")
        if kind != "citation_incorrect" and citation_chunk_id is not None:
            raise ValueError("只有引用问题可以指定 citation_chunk_id")

        session_factory = self._uow_factory()
        async with session_factory() as session:
            async with PostgresUnitOfWork(session).transaction() as tx:
                message = await tx.messages.get(message_id)
                if message is None or message.role != "assistant" or message.run_id is None:
                    raise ValueError("只能反馈已持久化的助手回复")
                trace = await tx.rag_evaluation_traces.get_latest_for_run(message.run_id)
                if trace is None or trace.task_id != message.task_id:
                    raise ValueError("该回复没有可关联的 RAG 检索轨迹")
                if (
                    citation_chunk_id is not None
                    and citation_chunk_id not in trace.context_chunk_ids
                ):
                    raise ValueError("引用 chunk 不属于该次 RAG 上下文")

                fingerprint = hashlib.sha256(
                    ":".join(
                        (
                            str(trace.id),
                            str(message.id),
                            str(citation_chunk_id or "answer"),
                        )
                    ).encode("utf-8")
                ).hexdigest()
                feedback = await tx.rag_evaluation_feedback.create_or_get(
                    RagEvaluationFeedback(
                        id=uuid4(),
                        trace_id=trace.id,
                        workspace_id=trace.workspace_id,
                        task_id=trace.task_id,
                        run_id=trace.run_id,
                        message_id=message.id,
                        kind=kind,
                        citation_chunk_id=citation_chunk_id,
                        fingerprint=fingerprint,
                    )
                )
                await tx.audits.create(
                    AuditLog(
                        id=uuid4(),
                        event_type="rag.feedback.submitted",
                        actor="user",
                        action_summary="提交 RAG 回答反馈",
                        task_id=trace.task_id,
                        run_id=trace.run_id,
                        risk_level="L1",
                        details={
                            "feedback_id": str(feedback.id),
                            "kind": feedback.kind,
                            "citation_scoped": feedback.citation_chunk_id is not None,
                        },
                        result_summary="已进入待审核队列",
                    )
                )
                await tx.commit()
                return feedback

    async def list_queue(
        self,
        *,
        workspace_id: UUID,
        status: FeedbackStatus | None = "pending",
        limit: int = 50,
    ) -> tuple[RagFeedbackReviewItem, ...]:
        session_factory = self._uow_factory()
        async with session_factory() as session:
            async with PostgresUnitOfWork(session).transaction() as tx:
                feedback_items = await tx.rag_evaluation_feedback.list_by_workspace(
                    workspace_id=workspace_id,
                    status=status,
                    limit=min(max(limit, 1), 100),
                )
                output: list[RagFeedbackReviewItem] = []
                for feedback in feedback_items:
                    trace = await tx.rag_evaluation_traces.get(feedback.trace_id)
                    if trace is not None:
                        output.append(_review_item(feedback, trace))
                return tuple(output)

    async def inspect(self, feedback_id: UUID) -> RagFeedbackReviewDetail:
        session_factory = self._uow_factory()
        async with session_factory() as session:
            async with PostgresUnitOfWork(session).transaction() as tx:
                feedback = await tx.rag_evaluation_feedback.get(feedback_id)
                if feedback is None:
                    raise ValueError("RAG 反馈不存在")
                trace = await tx.rag_evaluation_traces.get(feedback.trace_id)
                if trace is None:
                    raise ValueError("RAG 反馈关联的检索轨迹不存在")
                candidate = _ranking(trace.candidate_ranking)
                reranked = _ranking(trace.reranked_ranking)
                chunk_ids = list(dict.fromkeys((*candidate, *reranked, *trace.context_chunk_ids)))[:100]
                chunks = await tx.rag_chunks.list_by_ids(
                    workspace_id=trace.workspace_id, chunk_ids=chunk_ids
                )
                by_id = {chunk.id: chunk for chunk in chunks}
                approved = trace.privacy_status == "approved"
                evidence = tuple(
                    RagFeedbackEvidence(
                        chunk_id=chunk_id,
                        document_id=by_id[chunk_id].document_id,
                        content_hash=by_id[chunk_id].content_hash,
                        candidate_rank=candidate.get(chunk_id, {}).get("rank"),
                        reranked_rank=reranked.get(chunk_id, {}).get("rank"),
                        in_context=chunk_id in trace.context_chunk_ids,
                        sources=tuple(
                            dict.fromkeys(
                                (*candidate.get(chunk_id, {}).get("sources", ()),
                                 *reranked.get(chunk_id, {}).get("sources", ()))
                            )
                        ),
                        snippet=_snippet(by_id[chunk_id].content) if approved else None,
                    )
                    for chunk_id in chunk_ids
                    if chunk_id in by_id
                )
                label = await tx.rag_evaluation_labels.get_for_trace(trace.id)
                return RagFeedbackReviewDetail(
                    feedback=feedback,
                    query_hash=trace.query_hash,
                    query=trace.query if approved else None,
                    privacy_status=trace.privacy_status,
                    pipeline_versions=trace.pipeline_versions,
                    result_count=trace.result_count,
                    context_truncated=trace.context_truncated,
                    evidence=evidence,
                    label=label,
                )

    async def triage(
        self,
        feedback_id: UUID,
        *,
        failure_category: FailureCategory,
        positive_chunk_ids: tuple[UUID, ...] = (),
        hard_negative_chunk_ids: tuple[UUID, ...] = (),
    ) -> tuple[RagEvaluationFeedback, RagEvaluationLabel | None]:
        positive_chunk_ids = tuple(dict.fromkeys(positive_chunk_ids))
        hard_negative_chunk_ids = tuple(dict.fromkeys(hard_negative_chunk_ids))
        if failure_category not in FAILURE_CATEGORIES:
            raise ValueError("不支持的 RAG 失败分类")
        if not positive_chunk_ids and hard_negative_chunk_ids:
            raise ValueError("hard-negative 必须与 positive 草稿标签一起提交")
        if set(positive_chunk_ids) & set(hard_negative_chunk_ids):
            raise ValueError("positive 与 hard-negative chunk 不得重叠")
        session_factory = self._uow_factory()
        async with session_factory() as session:
            async with PostgresUnitOfWork(session).transaction() as tx:
                current = await tx.rag_evaluation_feedback.get(feedback_id)
                if current is None:
                    raise ValueError("RAG 反馈不存在")
                if current.status != "pending":
                    raise ValueError("RAG 反馈已经处理")
                trace = await tx.rag_evaluation_traces.get(current.trace_id)
                if trace is None:
                    raise ValueError("RAG 反馈关联的检索轨迹不存在")
                existing = await tx.rag_evaluation_labels.get_for_trace(trace.id)
                label = existing
                selected = tuple(dict.fromkeys((*positive_chunk_ids, *hard_negative_chunk_ids)))
                if selected:
                    if trace.privacy_status != "approved":
                        raise ValueError("生成反馈标签前必须先通过 trace 隐私复核")
                    allowed = {
                        UUID(str(value["chunk_id"]))
                        for value in (*trace.candidate_ranking, *trace.reranked_ranking)
                    } | set(trace.context_chunk_ids)
                    if not set(selected) <= allowed:
                        raise ValueError("反馈标签只能选择本次检索轨迹中的证据")
                    chunks = await tx.rag_chunks.list_by_ids(
                        workspace_id=trace.workspace_id, chunk_ids=list(selected)
                    )
                    if {chunk.id for chunk in chunks} != set(selected):
                        raise ValueError("反馈标签包含不存在或跨 Workspace 的 chunk")
                    if existing is not None and not (
                        existing.status == "draft" and existing.source == "user_feedback"
                    ):
                        raise ValueError("已有人工或终态标签；本次只能保存失败分类")
                    now = datetime.now(timezone.utc)
                    if existing is None:
                        label = RagEvaluationLabel(
                            id=uuid4(), trace_id=trace.id,
                            positive_chunk_ids=positive_chunk_ids,
                            hard_negative_chunk_ids=hard_negative_chunk_ids,
                            source="user_feedback", status="draft",
                            notes=f"feedback:{current.id}; category:{failure_category}",
                            created_at=now, updated_at=now,
                        )
                        await tx.rag_evaluation_labels.create(label)
                    else:
                        label = replace(
                            existing,
                            positive_chunk_ids=positive_chunk_ids,
                            hard_negative_chunk_ids=hard_negative_chunk_ids,
                            notes=f"feedback:{current.id}; category:{failure_category}",
                            updated_at=now,
                        )
                        await tx.rag_evaluation_labels.save(label)
                updated = await tx.rag_evaluation_feedback.set_review(
                    feedback_id, status="reviewed", failure_category=failure_category
                )
                assert updated is not None
                await tx.audits.create(
                    AuditLog(
                        id=uuid4(), event_type="rag.feedback.triaged", actor="operator",
                        action_summary="诊断 RAG 反馈候选", task_id=current.task_id,
                        run_id=current.run_id, risk_level="L1",
                        details={
                            "feedback_id": str(current.id),
                            "failure_category": failure_category,
                            "draft_label_written": bool(selected),
                            "positive_count": len(positive_chunk_ids),
                            "hard_negative_count": len(hard_negative_chunk_ids),
                        },
                        result_summary=(
                            "失败分类已保存；反馈标签保持 draft"
                            if selected else "失败分类已保存；标签生命周期未改变"
                        ),
                    )
                )
                await tx.commit()
                return updated, label

    async def resolve(self, feedback_id: UUID, *, status: FeedbackStatus) -> RagEvaluationFeedback:
        if status not in {"reviewed", "dismissed"}:
            raise ValueError("审核结果只支持 reviewed 或 dismissed")
        session_factory = self._uow_factory()
        async with session_factory() as session:
            async with PostgresUnitOfWork(session).transaction() as tx:
                current = await tx.rag_evaluation_feedback.get(feedback_id)
                if current is None:
                    raise ValueError("RAG 反馈不存在")
                if current.status != "pending":
                    raise ValueError("RAG 反馈已经处理")
                updated = await tx.rag_evaluation_feedback.set_review(
                    feedback_id, status=status
                )
                assert updated is not None
                await tx.audits.create(
                    AuditLog(
                        id=uuid4(),
                        event_type="rag.feedback.resolved",
                        actor="operator",
                        action_summary="处理 RAG 反馈候选",
                        task_id=current.task_id,
                        run_id=current.run_id,
                        risk_level="L1",
                        details={"feedback_id": str(current.id), "status": status},
                        result_summary="候选状态已更新；未自动生成金标",
                    )
                )
                await tx.commit()
                return updated


def _review_item(
    feedback: RagEvaluationFeedback, trace: RagEvaluationTrace
) -> RagFeedbackReviewItem:
    return RagFeedbackReviewItem(
        feedback=feedback,
        query_hash=trace.query_hash,
        pipeline_versions=trace.pipeline_versions,
        result_count=trace.result_count,
        context_truncated=trace.context_truncated,
    )


def _ranking(values: tuple[dict, ...]) -> dict[UUID, dict]:
    return {UUID(str(value["chunk_id"])): value for value in values}


def _snippet(content: str, limit: int = 320) -> str:
    normalized = " ".join(content.split())
    return normalized if len(normalized) <= limit else normalized[:limit].rstrip() + "…"
