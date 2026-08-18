"""生产检索轨迹采集服务。"""

from __future__ import annotations

from uuid import UUID

from jarvis_worker.database.unit_of_work import PostgresUnitOfWork

from .contracts import PrivacyStatus, RagEvaluationLabel, RagEvaluationTrace


class RagEvaluationTraceService:
    def __init__(self, uow_factory) -> None:
        self._uow_factory = uow_factory

    async def capture(
        self,
        *,
        task_id: UUID,
        run_id: UUID,
        step_id: UUID | None,
        request,
        package,
    ) -> RagEvaluationTrace:
        trace = RagEvaluationTrace.capture(
            task_id=task_id,
            run_id=run_id,
            step_id=step_id,
            request=request,
            package=package,
        )
        session_factory = self._uow_factory()
        async with session_factory() as session:
            async with PostgresUnitOfWork(session).transaction() as tx:
                await tx.rag_evaluation_traces.create(trace)
                await tx.commit()
        return trace

    async def review_privacy(self, trace_id: UUID, status: PrivacyStatus) -> None:
        if status == "pending":
            raise ValueError("隐私复核结果只能是 approved/rejected")
        session_factory = self._uow_factory()
        async with session_factory() as session:
            async with PostgresUnitOfWork(session).transaction() as tx:
                changed = await tx.rag_evaluation_traces.set_privacy_status(trace_id, status)
                if not changed:
                    raise ValueError("RAG evaluation trace 不存在")
                await tx.commit()

    async def create_label(self, label: RagEvaluationLabel) -> RagEvaluationLabel:
        session_factory = self._uow_factory()
        async with session_factory() as session:
            async with PostgresUnitOfWork(session).transaction() as tx:
                trace = await tx.rag_evaluation_traces.get(label.trace_id)
                if trace is None:
                    raise ValueError("RAG evaluation trace 不存在")
                if label.status in {"confirmed", "promoted"} and trace.privacy_status != "approved":
                    raise ValueError("标签确认前必须先通过 trace 隐私复核")
                chunk_ids = list(
                    dict.fromkeys(
                        (*label.positive_chunk_ids, *label.hard_negative_chunk_ids)
                    )
                )
                chunks = await tx.rag_chunks.list_by_ids(
                    workspace_id=trace.workspace_id,
                    chunk_ids=chunk_ids,
                )
                if {chunk.id for chunk in chunks} != set(chunk_ids):
                    raise ValueError("标签包含不存在或跨 Workspace 的 RAG chunk")
                created = await tx.rag_evaluation_labels.create(label)
                await tx.commit()
                return created
