"""开发者内部 RAG 轨迹审核与标签生命周期。"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timezone
from uuid import UUID, uuid4

from jarvis_worker.agent.rag.contracts import RagChunk
from jarvis_worker.database.unit_of_work import PostgresUnitOfWork
from jarvis_worker.shared.domain.models import AuditLog

from .contracts import LabelStatus, RagEvaluationLabel, RagEvaluationTrace


@dataclass(frozen=True, slots=True)
class RagEvaluationReview:
    trace: RagEvaluationTrace
    label: RagEvaluationLabel | None
    chunks: tuple[RagChunk, ...]


class RagEvaluationReviewService:
    def __init__(self, uow_factory) -> None:
        self._uow_factory = uow_factory

    async def list_traces(
        self, *, privacy_status: str | None = "pending",
        workspace_id: UUID | None = None, limit: int = 100
    ) -> tuple[tuple[RagEvaluationTrace, RagEvaluationLabel | None], ...]:
        session_factory = self._uow_factory()
        async with session_factory() as session:
            async with PostgresUnitOfWork(session).transaction() as tx:
                traces = await tx.rag_evaluation_traces.list_filtered(
                    privacy_status=privacy_status,
                    workspace_id=workspace_id,
                    limit=limit,
                )
                values = []
                for trace in traces:
                    label = await tx.rag_evaluation_labels.get_for_trace(trace.id)
                    values.append((trace, label))
                return tuple(values)

    async def inspect(
        self, trace_id: UUID, *, workspace_id: UUID | None = None
    ) -> RagEvaluationReview:
        session_factory = self._uow_factory()
        async with session_factory() as session:
            async with PostgresUnitOfWork(session).transaction() as tx:
                trace = await tx.rag_evaluation_traces.get(trace_id)
                if trace is None:
                    raise ValueError("RAG evaluation trace 不存在")
                _require_workspace(trace, workspace_id)
                ranked_ids = [
                    UUID(str(value["chunk_id"]))
                    for value in (*trace.candidate_ranking, *trace.reranked_ranking)
                ]
                chunk_ids = list(
                    dict.fromkeys((*ranked_ids, *trace.context_chunk_ids))
                )
                chunks = await tx.rag_chunks.list_by_ids(
                    workspace_id=trace.workspace_id,
                    chunk_ids=chunk_ids,
                )
                label = await tx.rag_evaluation_labels.get_for_trace(trace.id)
                return RagEvaluationReview(trace, label, tuple(chunks))

    async def list_documents(self, trace_id: UUID, *, limit: int = 100) -> tuple:
        session_factory = self._uow_factory()
        async with session_factory() as session:
            async with PostgresUnitOfWork(session).transaction() as tx:
                trace = await tx.rag_evaluation_traces.get(trace_id)
                if trace is None:
                    raise ValueError("RAG evaluation trace 不存在")
                documents = await tx.rag_documents.list_by_workspace(
                    workspace_id=trace.workspace_id,
                    include_disabled=False,
                    limit=limit,
                )
                return tuple(documents)

    async def list_document_chunks(
        self, trace_id: UUID, document_id: UUID, *, limit: int = 200
    ) -> tuple[RagChunk, ...]:
        session_factory = self._uow_factory()
        async with session_factory() as session:
            async with PostgresUnitOfWork(session).transaction() as tx:
                trace = await tx.rag_evaluation_traces.get(trace_id)
                if trace is None:
                    raise ValueError("RAG evaluation trace 不存在")
                chunks = await tx.rag_chunks.list_by_document(
                    workspace_id=trace.workspace_id,
                    document_id=document_id,
                    limit=limit,
                )
                if not chunks:
                    raise ValueError("文档不存在、跨 Workspace 或没有可审核 Chunk")
                return tuple(chunks)

    async def review_privacy(
        self, trace_id: UUID, *, approved: bool, workspace_id: UUID | None = None
    ) -> None:
        status = "approved" if approved else "rejected"
        session_factory = self._uow_factory()
        async with session_factory() as session:
            async with PostgresUnitOfWork(session).transaction() as tx:
                trace = await tx.rag_evaluation_traces.get(trace_id)
                if trace is None:
                    raise ValueError("RAG evaluation trace 不存在")
                _require_workspace(trace, workspace_id)
                label = await tx.rag_evaluation_labels.get_for_trace(trace_id)
                if not approved and label is not None and label.status in {
                    "confirmed",
                    "promoted",
                }:
                    raise ValueError("已确认/晋升标签的 trace 不能直接拒绝隐私复核")
                await tx.rag_evaluation_traces.set_privacy_status(trace_id, status)
                await _audit(
                    tx, trace, event_type="rag.evaluation.privacy_reviewed",
                    action_summary="复核 RAG 评估轨迹隐私", details={"privacy_status": status},
                    result_summary=f"隐私状态已更新为 {status}",
                )
                await tx.commit()

    async def set_label(
        self,
        *,
        trace_id: UUID,
        positive_chunk_ids: tuple[UUID, ...],
        hard_negative_chunk_ids: tuple[UUID, ...] = (),
        notes: str = "",
        status: LabelStatus = "confirmed",
        workspace_id: UUID | None = None,
    ) -> RagEvaluationLabel:
        session_factory = self._uow_factory()
        async with session_factory() as session:
            async with PostgresUnitOfWork(session).transaction() as tx:
                trace = await tx.rag_evaluation_traces.get(trace_id)
                if trace is None:
                    raise ValueError("RAG evaluation trace 不存在")
                _require_workspace(trace, workspace_id)
                if status == "promoted":
                    raise ValueError("标签晋升必须使用独立 promote 操作")
                if status in {"confirmed", "promoted"} and trace.privacy_status != "approved":
                    raise ValueError("标签确认前必须先通过 trace 隐私复核")
                await _require_scoped_chunks(
                    tx,
                    trace,
                    (*positive_chunk_ids, *hard_negative_chunk_ids),
                )
                existing = await tx.rag_evaluation_labels.get_for_trace(trace_id)
                if existing is not None and existing.status == "promoted":
                    raise ValueError("已晋升标签不可直接修改")
                now = datetime.now(timezone.utc)
                if existing is None:
                    label = RagEvaluationLabel(
                        id=uuid4(),
                        trace_id=trace_id,
                        positive_chunk_ids=positive_chunk_ids,
                        hard_negative_chunk_ids=hard_negative_chunk_ids,
                        source="human_review",
                        status=status,
                        notes=notes.strip(),
                        created_at=now,
                        updated_at=now,
                    )
                    await tx.rag_evaluation_labels.create(label)
                else:
                    label = replace(
                        existing,
                        positive_chunk_ids=positive_chunk_ids,
                        hard_negative_chunk_ids=hard_negative_chunk_ids,
                        source="human_review",
                        status=status,
                        notes=notes.strip(),
                        updated_at=now,
                    )
                    await tx.rag_evaluation_labels.save(label)
                await _audit(
                    tx, trace, event_type="rag.evaluation.label_reviewed",
                    action_summary="复核 RAG 证据标签",
                    details={
                        "label_id": str(label.id), "label_status": label.status,
                        "positive_count": len(label.positive_chunk_ids),
                        "hard_negative_count": len(label.hard_negative_chunk_ids),
                    },
                    result_summary=f"标签状态已更新为 {label.status}",
                )
                await tx.commit()
                return label

    async def promote(
        self, trace_id: UUID, *, workspace_id: UUID | None = None
    ) -> RagEvaluationReview:
        session_factory = self._uow_factory()
        async with session_factory() as session:
            async with PostgresUnitOfWork(session).transaction() as tx:
                trace = await tx.rag_evaluation_traces.get(trace_id)
                label = await tx.rag_evaluation_labels.get_for_trace(trace_id)
                if trace is None or label is None:
                    raise ValueError("RAG evaluation trace 或 label 不存在")
                _require_workspace(trace, workspace_id)
                if trace.privacy_status != "approved" or label.status not in {
                    "confirmed",
                    "promoted",
                }:
                    raise ValueError("只有隐私已批准且标签已确认的 trace 可以晋升")
                promoted = replace(
                    label,
                    status="promoted",
                    updated_at=datetime.now(timezone.utc),
                )
                await tx.rag_evaluation_labels.save(promoted)
                chunk_ids = list(
                    dict.fromkeys(
                        (*promoted.positive_chunk_ids, *promoted.hard_negative_chunk_ids)
                    )
                )
                chunks = await tx.rag_chunks.list_by_ids(
                    workspace_id=trace.workspace_id,
                    chunk_ids=chunk_ids,
                )
                await _audit(
                    tx, trace, event_type="rag.evaluation.label_promoted",
                    action_summary="晋升 RAG 回归候选",
                    details={"label_id": str(promoted.id), "query_hash": trace.query_hash},
                    result_summary="标签已晋升；等待纳入版本化 cohort manifest",
                )
                await tx.commit()
                return RagEvaluationReview(trace, promoted, tuple(chunks))


async def _require_scoped_chunks(tx, trace, chunk_ids) -> None:
    unique_ids = list(dict.fromkeys(chunk_ids))
    chunks = await tx.rag_chunks.list_by_ids(
        workspace_id=trace.workspace_id,
        chunk_ids=unique_ids,
    )
    if {chunk.id for chunk in chunks} != set(unique_ids):
        raise ValueError("标签包含不存在或跨 Workspace 的 RAG chunk")


def _require_workspace(trace: RagEvaluationTrace, workspace_id: UUID | None) -> None:
    if workspace_id is not None and trace.workspace_id != workspace_id:
        raise ValueError("RAG evaluation trace 不属于当前 Workspace")


async def _audit(
    tx, trace: RagEvaluationTrace, *, event_type: str, action_summary: str,
    details: dict, result_summary: str,
) -> None:
    await tx.audits.create(
        AuditLog(
            id=uuid4(), event_type=event_type, actor="operator",
            action_summary=action_summary, task_id=trace.task_id, run_id=trace.run_id,
            risk_level="L1", details={"trace_id": str(trace.id), **details},
            result_summary=result_summary,
        )
    )
