"""RuntimeApplicationService — Worker 关键事件的唯一持久化入口。"""

from __future__ import annotations

import json
import re
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from pathlib import PurePosixPath
from typing import Any
from urllib.parse import urlparse
from uuid import NAMESPACE_URL, UUID, uuid5

from jarvis_worker.agent.artifacts.file_store import LocalArtifactFileStore
from jarvis_worker.agent.core.checkpoint import (
    is_resumable_run_checkpoint,
    validate_run_checkpoint,
)
from jarvis_worker.agent.core.evidence_navigation import (
    sanitize_source_navigation_guard_details,
)
from jarvis_worker.agent.core.final_answer import (
    sanitize_final_answer_validation_details,
)
from jarvis_worker.agent.memory.extractor import (
    MEMORY_EXTRACTION_POLICY_VERSION,
)
from jarvis_worker.database.unit_of_work import PostgresUnitOfWork
from jarvis_worker.runtime.permissions.policy import permission_request_deadline
from jarvis_worker.runtime_bus.messages import RuntimeEventEnvelope
from jarvis_worker.shared.domain.models import (
    Artifact,
    AuditLog,
    ExecutionStep,
    MemoryExtractionJob,
    Message,
    OutboxEvent,
    PermissionRequest,
    PermissionStatus,
    RunStatus,
    RuntimeEvent,
    StepStatus,
    StepType,
    TaskStatus,
    ToolCall,
    new_id,
)
from jarvis_worker.shared.storage_capacity import StorageCapacityExceeded

SCHEMA_VERSION = "2B-1a.1"

DURABLE_EVENT_TYPES = {
    "task.created",
    "task.updated",
    "agent.run.started",
    "agent.run.paused",
    "agent.run.resumed",
    "agent.run.completed",
    "agent.run.failed",
    "agent.run.cancelled",
    "agent.step.started",
    "agent.step.updated",
    "agent.step.completed",
    "agent.step.failed",
    "model.call.started",
    "model.context.prepared",
    "model.call.completed",
    "model.call.failed",
    "tool.call.started",
    "tool.call.finished",
    "tool.call.failed",
    "permission.required",
    "permission.resolved",
    "permission.expired",
    "artifact.created",
}


class RuntimeApplicationService:
    """把领域投影、RuntimeEvent 和 Outbox 写入同一 PostgreSQL 事务。"""

    def __init__(
        self,
        uow_factory,
        *,
        artifact_file_store: LocalArtifactFileStore | None = None,
        artifact_inline_max_bytes: int = 8 * 1024,
        memory_extraction_enabled: bool = True,
    ):
        if artifact_inline_max_bytes < 1:
            raise ValueError("artifact_inline_max_bytes 必须大于 0")
        self._uow_factory = uow_factory
        self._artifact_file_store = artifact_file_store
        self._artifact_inline_max_bytes = artifact_inline_max_bytes
        self._memory_extraction_enabled = memory_extraction_enabled

    @staticmethod
    def is_durable(event_type: str) -> bool:
        return event_type in DURABLE_EVENT_TYPES

    async def record_envelope(self, envelope: RuntimeEventEnvelope) -> bool:
        """持久化关键事件；重复 event_id 幂等返回 False。"""
        if not self.is_durable(envelope.event_type):
            return False

        envelope.validate()
        public_envelope, checkpoint, run_checkpoint = self._extract_internal_checkpoints(envelope)
        public_envelope, tool_deliverables = self._prepare_tool_deliverables(public_envelope)
        public_envelope.validate()
        event_uuid = UUID(envelope.event_id)
        task_id = UUID(envelope.task_id)
        run_id = UUID(envelope.run_id)
        trace_id = UUID(envelope.trace_id)
        initial_raw = public_envelope.runtime_event
        step_id = UUID(initial_raw["step_id"]) if initial_raw.get("step_id") else None
        created_at = _parse_timestamp(initial_raw["timestamp"])
        public_envelope = self._project_permission_expiry(
            public_envelope, created_at
        )

        session_factory = self._uow_factory()
        async with session_factory() as session:
            uow = PostgresUnitOfWork(session)
            async with uow.transaction() as tx:
                first_seen = await tx.inbox.try_insert("runtime-event", envelope.event_id)
                if not first_seen:
                    await tx.commit()
                    return False

                # Lock the Run row before reading and projecting it.  This keeps
                # event_sequence, ExecutionStep.order_index and AgentRun.step_count
                # in one serial order even if multiple consumers race on a Run.
                sequence = await tx.events.get_next_sequence(run_id)
                run = await tx.runs.get(run_id)
                task = await tx.tasks.get(task_id)
                if run is None or task is None:
                    raise ValueError("RuntimeEvent 关联的 Task 或 AgentRun 不存在")

                projection_envelope = public_envelope
                public_envelope = self._externalize_large_artifact(
                    public_envelope,
                    workspace_id=task.workspace_id,
                    workspace_path=task.workspace_path or "",
                )
                if envelope.event_type == "artifact.created":
                    projection_envelope = public_envelope
                public_envelope = self._bound_large_completed_output(
                    public_envelope, run.final_output_artifact_id
                )
                raw = public_envelope.runtime_event
                await self._apply_projection(
                    tx,
                    projection_envelope,
                    run,
                    task,
                    task_id,
                    run_id,
                    step_id,
                    created_at,
                    checkpoint,
                    run_checkpoint,
                )

                event = RuntimeEvent(
                    id=event_uuid,
                    event_id=event_uuid,
                    task_id=task_id,
                    run_id=run_id,
                    step_id=step_id,
                    type=envelope.event_type,
                    event_sequence=sequence,
                    payload=raw.get("payload", {}),
                    created_at=created_at,
                )
                outbox = OutboxEvent(
                    id=new_id(),
                    event_id=event_uuid,
                    aggregate_type="AgentRun",
                    aggregate_id=run_id,
                    event_type=envelope.event_type,
                    schema_version=SCHEMA_VERSION,
                    payload=json.loads(public_envelope.to_payload_json()),
                    trace_id=trace_id,
                    created_at=created_at,
                )
                events = [event]
                outboxes = [outbox]
                if tool_deliverables:
                    derived_events, derived_outboxes = await self._build_tool_deliverable_records(
                        tx,
                        public_envelope,
                        tool_deliverables,
                        task_id=task_id,
                        run_id=run_id,
                        step_id=step_id,
                        trace_id=trace_id,
                        start_sequence=sequence + 1,
                        created_at=created_at,
                    )
                    events.extend(derived_events)
                    outboxes.extend(derived_outboxes)
                await tx.events.append(events)
                await tx.outbox.create(outboxes)
                await tx.inbox.mark_processed("runtime-event", envelope.event_id)
                await tx.commit()
                return True

    @staticmethod
    def _project_permission_expiry(
        envelope: RuntimeEventEnvelope, created_at: datetime
    ) -> RuntimeEventEnvelope:
        """Overwrite model/worker input with the host-owned durable deadline."""
        if envelope.event_type != "permission.required":
            return envelope
        raw = deepcopy(envelope.runtime_event)
        request = (raw.get("payload") or {}).get("request")
        if not isinstance(request, dict):
            raise ValueError("permission.required 缺少 request")
        request["expires_at"] = permission_request_deadline(created_at).isoformat()
        return RuntimeEventEnvelope(
            event_id=envelope.event_id,
            trace_id=envelope.trace_id,
            task_id=envelope.task_id,
            run_id=envelope.run_id,
            event_type=envelope.event_type,
            runtime_event=raw,
            produced_by=envelope.produced_by,
            schema_version=envelope.schema_version,
        )

    @staticmethod
    def _extract_permission_checkpoint(
        envelope: RuntimeEventEnvelope,
    ) -> tuple[RuntimeEventEnvelope, dict]:
        """分离内部恢复点，保证 RuntimeEvent、Outbox 和 Web DTO 永不携带它。"""
        public, permission_checkpoint, _run_checkpoint = (
            RuntimeApplicationService._extract_internal_checkpoints(envelope)
        )
        return public, permission_checkpoint

    @staticmethod
    def _prepare_tool_deliverables(
        envelope: RuntimeEventEnvelope,
    ) -> tuple[RuntimeEventEnvelope, list[dict[str, object]]]:
        """校验成功 ToolResult 中的可信交付物描述并分配确定性 Artifact id。

        只允许显式白名单中的内置工具产生交付物；MCP 原始结果不能成为文件事实来源。
        任意字段不一致都 fail closed，避免根据模型参数或不可信工具摘要伪造 Artifact。
        """
        if envelope.event_type != "tool.call.finished":
            return envelope, []
        raw = deepcopy(envelope.runtime_event)
        tool_call = (raw.get("payload") or {}).get("tool_call")
        if not isinstance(tool_call, dict):
            raise ValueError("tool.call.finished 缺少 tool_call")
        result = tool_call.get("result")
        if not isinstance(result, dict):
            raise ValueError("tool.call.finished 缺少 result")
        candidates = result.get("deliverables")
        if candidates is None:
            return envelope, []
        tool_name = tool_call.get("tool_name")
        if (
            tool_name not in {"workspace.create_file", "literature.download_arxiv_pdf"}
            or result.get("kind") != "file"
            or not isinstance(candidates, list)
            or len(candidates) != 1
        ):
            raise ValueError("工具不允许产生文件交付物")
        data = result.get("data")
        if not isinstance(data, dict):
            raise ValueError("文件交付物缺少可信结果")
        if tool_name == "workspace.create_file" and data.get("created") is not True:
            raise ValueError("文件交付物缺少可信 create_file 结果")
        if tool_name == "literature.download_arxiv_pdf" and (
            data.get("downloaded") is not True or data.get("source") != "arxiv"
        ):
            raise ValueError("文献交付物缺少可信下载结果")
        tool_call_id = UUID(str(tool_call.get("id")))
        prepared: list[dict[str, object]] = []
        for index, candidate in enumerate(candidates):
            if not isinstance(candidate, dict) or candidate.get("kind") != "file":
                raise ValueError("文件交付物描述无效")
            path = candidate.get("path")
            size_bytes = candidate.get("size_bytes")
            mime_type = candidate.get("mime_type")
            content_hash = candidate.get("content_hash")
            if not isinstance(path, str) or not path or len(path) > 4096:
                raise ValueError("文件交付物 path 无效")
            parsed_path = PurePosixPath(path)
            if (
                parsed_path.is_absolute()
                or str(parsed_path) != path
                or any(part in ("", ".", "..") for part in parsed_path.parts)
            ):
                raise ValueError("文件交付物 path 必须是规范 workspace 相对路径")
            if (
                not isinstance(size_bytes, int)
                or isinstance(size_bytes, bool)
                or size_bytes < 0
                or size_bytes > 100 * 1024 * 1024
            ):
                raise ValueError("文件交付物 size_bytes 无效")
            if not isinstance(mime_type, str) or not mime_type or len(mime_type) > 100:
                raise ValueError("文件交付物 mime_type 无效")
            if (
                not isinstance(content_hash, str)
                or len(content_hash) != 64
                or any(char not in "0123456789abcdef" for char in content_hash)
            ):
                raise ValueError("文件交付物 content_hash 无效")
            if (
                data.get("path") != path
                or data.get("size_bytes") != size_bytes
                or data.get("sha256") != content_hash
            ):
                raise ValueError("文件交付物与工具结果不一致")
            title = candidate.get("title")
            if not isinstance(title, str) or not title.strip():
                title = parsed_path.name
            if tool_name == "workspace.create_file":
                artifact_id = uuid5(
                    NAMESPACE_URL,
                    f"jarvis:artifact:{envelope.run_id}:{tool_call_id}:{index}:{path}",
                )
                storage = "workspace"
                extra_metadata: dict[str, object] = {}
            else:
                artifact_id = uuid5(
                    NAMESPACE_URL,
                    f"jarvis:artifact:{envelope.run_id}:{tool_call_id}:{index}:literature-pdf",
                )
                source_url = data.get("source_url")
                parsed_url = urlparse(source_url) if isinstance(source_url, str) else None
                if (
                    not _is_local_artifact_path(
                        path,
                        run_id=envelope.run_id,
                        artifact_id=artifact_id,
                        suffix=".pdf",
                    )
                    or mime_type != "application/pdf"
                    or parsed_url is None
                    or parsed_url.scheme != "https"
                    or (parsed_url.hostname or "").lower()
                    not in {"arxiv.org", "www.arxiv.org", "export.arxiv.org"}
                    or not isinstance(data.get("arxiv_id"), str)
                ):
                    raise ValueError("文献 Artifact 来源或路径无效")
                storage = "local_file"
                extra_metadata = {
                    "source": "arxiv",
                    "arxiv_id": data["arxiv_id"],
                    "source_url": source_url,
                }
            prepared.append(
                {
                    "id": artifact_id,
                    "tool_call_id": tool_call_id,
                    "title": title[:500],
                    "path": path,
                    "size_bytes": size_bytes,
                    "mime_type": mime_type,
                    "content_hash": content_hash,
                    "storage": storage,
                    "metadata": extra_metadata,
                }
            )
        result["artifact_ids"] = [str(item["id"]) for item in prepared]
        return RuntimeEventEnvelope(
            event_id=envelope.event_id,
            trace_id=envelope.trace_id,
            task_id=envelope.task_id,
            run_id=envelope.run_id,
            event_type=envelope.event_type,
            runtime_event=raw,
            produced_by=envelope.produced_by,
            schema_version=envelope.schema_version,
        ), prepared

    @staticmethod
    async def _build_tool_deliverable_records(
        tx,
        source_envelope: RuntimeEventEnvelope,
        deliverables: list[dict[str, object]],
        *,
        task_id: UUID,
        run_id: UUID,
        step_id: UUID | None,
        trace_id: UUID,
        start_sequence: int,
        created_at: datetime,
    ) -> tuple[list[RuntimeEvent], list[OutboxEvent]]:
        """在 tool.call.finished 同一事务内创建 Artifact、事件与 Outbox。"""
        events: list[RuntimeEvent] = []
        outboxes: list[OutboxEvent] = []
        for index, item in enumerate(deliverables):
            artifact_id = item["id"]
            tool_call_id = item["tool_call_id"]
            if not isinstance(artifact_id, UUID) or not isinstance(tool_call_id, UUID):
                raise ValueError("内部文件交付物标识无效")
            artifact_created_at = created_at + timedelta(microseconds=index + 1)
            storage = str(item.get("storage", "workspace"))
            if storage == "workspace":
                metadata = {
                    "storage": "workspace",
                    "workspace_relative_path": str(item["path"]),
                }
                file_path = None
            elif storage == "local_file":
                metadata = {
                    "storage": "local_file",
                    **dict(item.get("metadata") or {}),
                }
                file_path = str(item["path"])
            else:
                raise ValueError("内部文件交付物 storage 无效")
            artifact = Artifact(
                id=artifact_id,
                task_id=task_id,
                run_id=run_id,
                step_id=step_id,
                kind="file",
                title=str(item["title"]),
                purpose="deliverable",
                producer_type="tool",
                source_tool_call_id=tool_call_id,
                file_path=file_path,
                file_size_bytes=int(item["size_bytes"]),
                mime_type=str(item["mime_type"]),
                content_hash=str(item["content_hash"]),
                metadata=metadata,
                created_at=artifact_created_at,
            )
            await tx.artifacts.create(artifact)
            event_id = uuid5(
                NAMESPACE_URL,
                f"jarvis:event:artifact.created:{artifact_id}",
            )
            artifact_payload = {
                "id": str(artifact_id),
                "task_id": str(task_id),
                "run_id": str(run_id),
                "kind": "file",
                "title": artifact.title,
                "purpose": "deliverable",
                "producer": {
                    "type": "tool",
                    "tool_call_id": str(tool_call_id),
                },
                "file_size_bytes": artifact.file_size_bytes,
                "mime_type": artifact.mime_type,
                "content_hash": artifact.content_hash,
                "metadata": metadata,
                "created_at": artifact_created_at.isoformat(),
            }
            runtime_event = {
                "id": str(event_id),
                "type": "artifact.created",
                "task_id": str(task_id),
                "run_id": str(run_id),
                "timestamp": artifact_created_at.isoformat(),
                "payload": {"artifact": artifact_payload},
            }
            if step_id is not None:
                runtime_event["step_id"] = str(step_id)
            derived_envelope = RuntimeEventEnvelope(
                event_id=str(event_id),
                trace_id=str(trace_id),
                task_id=str(task_id),
                run_id=str(run_id),
                event_type="artifact.created",
                runtime_event=runtime_event,
                produced_by=source_envelope.produced_by,
                schema_version=source_envelope.schema_version,
            )
            derived_envelope.validate()
            events.append(
                RuntimeEvent(
                    id=event_id,
                    event_id=event_id,
                    task_id=task_id,
                    run_id=run_id,
                    step_id=step_id,
                    type="artifact.created",
                    event_sequence=start_sequence + index,
                    payload={"artifact": artifact_payload},
                    created_at=artifact_created_at,
                )
            )
            outboxes.append(
                OutboxEvent(
                    id=new_id(),
                    event_id=event_id,
                    aggregate_type="AgentRun",
                    aggregate_id=run_id,
                    event_type="artifact.created",
                    schema_version=SCHEMA_VERSION,
                    payload=json.loads(derived_envelope.to_payload_json()),
                    trace_id=trace_id,
                    created_at=artifact_created_at,
                )
            )
        return events, outboxes

    def _externalize_large_artifact(
        self,
        envelope: RuntimeEventEnvelope,
        *,
        workspace_id: UUID | None = None,
        workspace_path: str = "",
    ) -> RuntimeEventEnvelope:
        """把超出事件内联上限的文本写入受控文件存储。

        返回的新 envelope 只保留相对引用、大小、MIME 和哈希；绝对路径不会进入
        RuntimeEvent、Outbox 或 Web DTO。
        """
        if envelope.event_type != "artifact.created":
            return envelope
        raw = deepcopy(envelope.runtime_event)
        artifact = (raw.get("payload") or {}).get("artifact")
        if not isinstance(artifact, dict):
            return envelope
        content = artifact.get("content")
        if not isinstance(content, str):
            return envelope
        if len(content.encode("utf-8")) <= self._artifact_inline_max_bytes:
            return envelope
        if self._artifact_file_store is None:
            raise RuntimeError("长 Artifact 缺少文件存储配置")

        kind = str(artifact.get("kind") or "text")
        suffix, mime_type = {
            "markdown": (".md", "text/markdown; charset=utf-8"),
            "json": (".json", "application/json; charset=utf-8"),
            "diff": (".diff", "text/x-diff; charset=utf-8"),
        }.get(kind, (".txt", "text/plain; charset=utf-8"))
        try:
            stored = self._artifact_file_store.write_text(
                UUID(str(artifact.get("id"))),
                content,
                run_id=UUID(envelope.run_id),
                workspace_id=workspace_id,
                workspace_path=workspace_path,
                suffix=suffix,
                mime_type=mime_type,
            )
        except StorageCapacityExceeded as exc:
            metadata = artifact.get("metadata")
            if not isinstance(metadata, dict):
                metadata = {}
                artifact["metadata"] = metadata
            metadata["storage"] = "inline"
            metadata["capacity_fallback"] = exc.code
            return RuntimeEventEnvelope(
                event_id=envelope.event_id,
                trace_id=envelope.trace_id,
                task_id=envelope.task_id,
                run_id=envelope.run_id,
                event_type=envelope.event_type,
                runtime_event=raw,
                produced_by=envelope.produced_by,
                schema_version=envelope.schema_version,
            )
        artifact.pop("content", None)
        artifact["file_path"] = stored.relative_path
        artifact["file_size_bytes"] = stored.size_bytes
        artifact["mime_type"] = stored.mime_type
        artifact["content_hash"] = stored.sha256
        metadata = artifact.get("metadata")
        if not isinstance(metadata, dict):
            metadata = {}
            artifact["metadata"] = metadata
        metadata["storage"] = "local_file"
        metadata["content_encoding"] = "utf-8"
        return RuntimeEventEnvelope(
            event_id=envelope.event_id,
            trace_id=envelope.trace_id,
            task_id=envelope.task_id,
            run_id=envelope.run_id,
            event_type=envelope.event_type,
            runtime_event=raw,
            produced_by=envelope.produced_by,
            schema_version=envelope.schema_version,
        )

    def _bound_large_completed_output(
        self,
        envelope: RuntimeEventEnvelope,
        final_output_artifact_id: UUID | None,
    ) -> RuntimeEventEnvelope:
        """避免最终回复在完成事件中重复占用事件总线。

        投影仍使用原 envelope 写入 Message；公共 RuntimeEvent/Outbox 只保留
        final Artifact 引用。
        """
        if envelope.event_type != "agent.run.completed":
            return envelope
        raw = deepcopy(envelope.runtime_event)
        payload = raw.get("payload")
        if not isinstance(payload, dict):
            return envelope
        output = payload.get("output")
        if (
            not isinstance(output, str)
            or len(output.encode("utf-8")) <= self._artifact_inline_max_bytes
        ):
            return envelope
        if final_output_artifact_id is None:
            raise RuntimeError("长最终输出缺少 final Artifact 引用")
        payload.pop("output", None)
        payload["output_externalized"] = True
        payload["final_output_artifact_id"] = str(final_output_artifact_id)
        return RuntimeEventEnvelope(
            event_id=envelope.event_id,
            trace_id=envelope.trace_id,
            task_id=envelope.task_id,
            run_id=envelope.run_id,
            event_type=envelope.event_type,
            runtime_event=raw,
            produced_by=envelope.produced_by,
            schema_version=envelope.schema_version,
        )

    @staticmethod
    def _extract_internal_checkpoints(
        envelope: RuntimeEventEnvelope,
    ) -> tuple[RuntimeEventEnvelope, dict, dict]:
        """同时剥离 permission 与 run checkpoint，二者都不进入公共事件。"""
        raw = deepcopy(envelope.runtime_event)
        checkpoint: dict = {}
        if envelope.event_type == "permission.required":
            request = (raw.get("payload") or {}).get("request") or {}
            internal = request.pop("_internal_checkpoint", None)
            if isinstance(internal, dict):
                checkpoint = internal
        run_checkpoint = envelope.internal.get("run_checkpoint", {})
        if not isinstance(run_checkpoint, dict):
            run_checkpoint = {}
        if run_checkpoint:
            validate_run_checkpoint(run_checkpoint)
        public_envelope = RuntimeEventEnvelope(
            event_id=envelope.event_id,
            trace_id=envelope.trace_id,
            task_id=envelope.task_id,
            run_id=envelope.run_id,
            event_type=envelope.event_type,
            runtime_event=raw,
            produced_by=envelope.produced_by,
            schema_version=envelope.schema_version,
        )
        return public_envelope, checkpoint, run_checkpoint

    async def _apply_projection(
        self,
        tx,
        envelope,
        run,
        task,
        task_id,
        run_id,
        step_id,
        created_at,
        checkpoint: dict | None = None,
        run_checkpoint: dict | None = None,
    ) -> None:
        event_type = envelope.event_type
        payload = envelope.runtime_event.get("payload", {})

        if run_checkpoint:
            success = await tx.runs.update_with_lock(
                run_id=run.id,
                new_status=run.status.value,
                expected_version=run.version,
                expected_status=run.status.value,
                checkpoint_json=run_checkpoint,
            )
            if not success:
                raise RuntimeError(f"AgentRun checkpoint 并发冲突: {run.id} version={run.version}")
            run.checkpoint = run_checkpoint
            run.version += 1

        if event_type == "permission.required":
            await self._record_permission_required(
                tx,
                payload,
                task_id,
                run_id,
                step_id,
                created_at,
                checkpoint or {},
            )
            if run.status == RunStatus.RUNNING:
                await self._transition(tx, run, RunStatus.WAITING_PERMISSION)
            task.status = TaskStatus.WAITING_FOR_USER
            task.updated_at = created_at
            await tx.tasks.update(task)

        elif event_type == "permission.resolved":
            decision = str(payload.get("decision", ""))
            request_id = payload.get("request_id")
            if request_id:
                req = await tx.permissions.get_request(UUID(str(request_id)))
                if req is not None and req.status == PermissionStatus.PENDING:
                    req.status = (
                        PermissionStatus.DENIED if decision == "deny" else PermissionStatus.APPROVED
                    )
                    req.decision = decision
                    req.decided_at = created_at
                    await tx.permissions.update_request(req)
            if run.status == RunStatus.WAITING_PERMISSION and decision != "deny":
                await self._transition(tx, run, RunStatus.RUNNING)
            if run.status == RunStatus.RUNNING and decision != "deny":
                task.status = TaskStatus.RUNNING
                task.updated_at = created_at
                await tx.tasks.update(task)

        elif event_type == "permission.expired":
            request_id = payload.get("request_id")
            if request_id:
                req = await tx.permissions.get_request(UUID(str(request_id)))
                if req is not None and req.status == PermissionStatus.PENDING:
                    req.status = PermissionStatus.EXPIRED
                    req.decided_at = created_at
                    req.note = str(payload.get("reason") or "timeout")
                    await tx.permissions.update_request(req)
                    await self._mark_tool_call_permission_expired(tx, req)
                    await tx.audits.create(
                        AuditLog(
                            id=new_id(),
                            task_id=task_id,
                            run_id=run_id,
                            step_id=step_id,
                            tool_call_id=req.tool_call_id,
                            event_type="permission.expired",
                            actor="system",
                            risk_level=req.risk_level,
                            action_summary=req.action_summary,
                            details={"request_id": str(req.id), "reason": req.note},
                            result_summary="expired",
                            created_at=created_at,
                        )
                    )

        elif event_type.startswith("tool.call."):
            await self._record_tool_event(
                tx, event_type, payload, task_id, run, step_id, created_at
            )

        elif event_type.startswith("model.call."):
            await self._record_model_event(
                tx, event_type, payload, task_id, run, step_id, created_at
            )

        elif event_type == "artifact.created":
            artifact_data = payload.get("artifact")
            if not isinstance(artifact_data, dict):
                raise ValueError("artifact.created 缺少 artifact")
            artifact_id = UUID(str(artifact_data.get("id")))
            if str(artifact_data.get("task_id")) != str(task_id):
                raise ValueError("artifact.task_id 与 envelope 不一致")
            if str(artifact_data.get("run_id")) != str(run_id):
                raise ValueError("artifact.run_id 与 envelope 不一致")
            metadata = (
                artifact_data.get("metadata")
                if isinstance(artifact_data.get("metadata"), dict)
                else {}
            )
            purpose = artifact_data.get("purpose")
            if purpose is None:
                purpose = (
                    "final_response" if metadata.get("is_final_output") is True else "deliverable"
                )
            if purpose not in {"final_response", "deliverable"}:
                raise ValueError("artifact.purpose 不受支持")
            producer = artifact_data.get("producer")
            if producer is None:
                producer = {"type": "runtime"}
            if not isinstance(producer, dict):
                raise ValueError("artifact.producer 不是对象")
            producer_type = producer.get("type")
            if producer_type not in {"runtime", "tool"}:
                raise ValueError("artifact.producer.type 不受支持")
            raw_tool_call_id = producer.get("tool_call_id")
            if producer_type == "runtime" and raw_tool_call_id is not None:
                raise ValueError("runtime Artifact 不得关联 tool_call_id")
            if producer_type == "tool" and raw_tool_call_id is None:
                raise ValueError("tool Artifact 缺少 tool_call_id")
            source_tool_call_id = (
                UUID(str(raw_tool_call_id)) if raw_tool_call_id is not None else None
            )
            artifact = Artifact(
                id=artifact_id,
                task_id=task_id,
                run_id=run_id,
                step_id=step_id,
                kind=str(artifact_data.get("kind", "text")),
                title=str(artifact_data.get("title", "产物")),
                purpose=str(purpose),
                producer_type=str(producer_type),
                source_tool_call_id=source_tool_call_id,
                content=artifact_data.get("content")
                if isinstance(artifact_data.get("content"), str)
                else None,
                file_path=artifact_data.get("file_path")
                if isinstance(artifact_data.get("file_path"), str)
                else None,
                file_size_bytes=artifact_data.get("file_size_bytes")
                if isinstance(artifact_data.get("file_size_bytes"), int)
                else None,
                mime_type=artifact_data.get("mime_type")
                if isinstance(artifact_data.get("mime_type"), str)
                else None,
                content_hash=artifact_data.get("content_hash")
                if isinstance(artifact_data.get("content_hash"), str)
                else None,
                metadata=metadata,
                created_at=created_at,
            )
            await tx.artifacts.create(artifact)
            if artifact.purpose == "final_response":
                success = await tx.runs.update_with_lock(
                    run_id=run.id,
                    new_status=run.status.value,
                    expected_version=run.version,
                    expected_status=run.status.value,
                    final_output_artifact_id=artifact.id,
                )
                if not success:
                    raise RuntimeError("AgentRun final artifact 并发冲突")
                run.final_output_artifact_id = artifact.id
                run.version += 1

        elif event_type == "agent.run.completed":
            await self._expire_pending_permissions(tx, run_id, created_at, "run_completed")
            await self._transition(
                tx,
                run,
                RunStatus.COMPLETED,
                completed_at=created_at,
                lease_until=None,
                checkpoint_json={},
            )
            task.status = TaskStatus.COMPLETED
            task.completed_at = created_at
            task.updated_at = created_at
            await tx.tasks.update(task)
            output = payload.get("output")
            if isinstance(output, str) and output.strip():
                await tx.messages.create(
                    Message(
                        id=new_id(),
                        conversation_id=task.conversation_id,
                        task_id=task_id,
                        run_id=run_id,
                        role="assistant",
                        content=output,
                        created_at=created_at,
                    )
                )
                if self._memory_extraction_enabled and task.scheduled_execution_id is None:
                    existing_job = await tx.memory_extraction_jobs.get_by_run_policy(
                        run_id, MEMORY_EXTRACTION_POLICY_VERSION
                    )
                    if existing_job is None:
                        await tx.memory_extraction_jobs.create(
                            MemoryExtractionJob(
                                id=uuid5(
                                    NAMESPACE_URL,
                                    "jarvis:memory-extraction:"
                                    f"{run_id}:{MEMORY_EXTRACTION_POLICY_VERSION}",
                                ),
                                source_task_id=task_id,
                                source_run_id=run_id,
                                extraction_policy_version=(MEMORY_EXTRACTION_POLICY_VERSION),
                                created_at=created_at,
                                updated_at=created_at,
                            )
                        )

        elif event_type == "agent.run.failed":
            await self._expire_pending_permissions(tx, run_id, created_at, "run_failed")
            error = payload.get("error") if isinstance(payload.get("error"), dict) else {}
            if error.get("code") == "SOURCE_CHAIN_NAVIGATION_STALLED":
                details = error.get("details") if isinstance(error.get("details"), dict) else {}
                source_navigation = sanitize_source_navigation_guard_details(
                    details.get("source_navigation")
                )
                error = {
                    key: value
                    for key, value in error.items()
                    if key != "details"
                }
                if source_navigation is not None:
                    error["details"] = {"source_navigation": source_navigation}
            await self._fail_open_children(tx, run_id, error, created_at)
            retry_checkpoint = (
                run.checkpoint
                if bool(error.get("recoverable"))
                and is_resumable_run_checkpoint(run.checkpoint)
                and run.checkpoint.get("resume_node") in {"extract_intent", "call_model"}
                else {}
            )
            await self._transition(
                tx,
                run,
                RunStatus.FAILED,
                failed_at=created_at,
                error_json=error,
                lease_until=None,
                checkpoint_json=retry_checkpoint,
            )
            task.status = TaskStatus.FAILED
            task.updated_at = created_at
            await tx.tasks.update(task)

        elif event_type == "agent.run.cancelled":
            await self._expire_pending_permissions(tx, run_id, created_at, "run_cancelled")
            await self._cancel_open_children(tx, run_id, created_at)
            if run.status == RunStatus.CANCEL_REQUESTED:
                run = await self._transition(tx, run, RunStatus.CANCELLING)
            if run.status == RunStatus.CANCELLING:
                await self._transition(
                    tx, run, RunStatus.CANCELLED, lease_until=None, checkpoint_json={}
                )
            elif run.status in (RunStatus.RUNNING, RunStatus.WAITING_PERMISSION):
                # 兼容 Worker 在取消命令与状态投影竞态下的收口。
                run = await self._transition(tx, run, RunStatus.CANCEL_REQUESTED)
                run = await self._transition(tx, run, RunStatus.CANCELLING)
                await self._transition(
                    tx, run, RunStatus.CANCELLED, lease_until=None, checkpoint_json={}
                )
            task.status = TaskStatus.CANCELLED
            task.cancelled_at = created_at
            task.updated_at = created_at
            await tx.tasks.update(task)

        elif event_type == "agent.run.paused":
            if run.status in (RunStatus.RUNNING, RunStatus.PAUSE_REQUESTED):
                await self._transition(
                    tx,
                    run,
                    RunStatus.PAUSED,
                    worker_id=None,
                    lease_until=None,
                )
            elif run.status in (RunStatus.CANCEL_REQUESTED, RunStatus.CANCELLING):
                # cancel supersedes pause；保留事件用于解释安全边界，但不回退状态。
                pass
            elif run.status != RunStatus.PAUSED:
                raise RuntimeError(f"AgentRun 无法确认暂停: {run.id} status={run.status.value}")
            await tx.audits.create(
                AuditLog(
                    id=new_id(),
                    task_id=task_id,
                    run_id=run_id,
                    event_type="run.paused",
                    actor="system",
                    action_summary="运行已在安全检查点暂停",
                    details={"resume_node": (run.checkpoint or {}).get("resume_node", "")},
                    result_summary="paused",
                    created_at=created_at,
                )
            )

        elif event_type == "agent.run.resumed":
            if run.status in (RunStatus.CANCEL_REQUESTED, RunStatus.CANCELLING):
                # 恢复入队与取消并发时，取消拥有更高优先级。
                return
            if run.status != RunStatus.RUNNING:
                raise RuntimeError(
                    f"AgentRun 恢复事件状态不一致: {run.id} status={run.status.value}"
                )
            task.status = TaskStatus.RUNNING
            task.updated_at = created_at
            await tx.tasks.update(task)
            await tx.audits.create(
                AuditLog(
                    id=new_id(),
                    task_id=task_id,
                    run_id=run_id,
                    event_type="run.resumed",
                    actor="system",
                    action_summary="运行已从安全检查点恢复",
                    details={"resume_node": (run.checkpoint or {}).get("resume_node", "")},
                    result_summary="running",
                    created_at=created_at,
                )
            )

    async def _record_model_event(
        self, tx, event_type, payload, task_id, run, step_id, created_at
    ) -> None:
        """把模型调用投影为可审计的 ExecutionStep；缺少 step_id 时兼容旧事件。"""
        if step_id is None:
            return
        step = await tx.steps.get(step_id)
        if step is None:
            step = ExecutionStep(
                id=step_id,
                run_id=run.id,
                task_id=task_id,
                type=StepType.MODEL_CALL,
                status=StepStatus.RUNNING,
                title="模型调用",
                started_at=created_at,
                metadata={
                    "provider": str(payload.get("provider", "unknown")),
                    "model_name": str(payload.get("model_name", "unknown")),
                    "call_id": str(payload.get("call_id", "")),
                    "purpose": str(payload.get("purpose", "agent_action")),
                },
            )
            await self._create_execution_step(tx, run, step)
        else:
            self._validate_execution_step(
                step,
                run,
                task_id,
                StepType.MODEL_CALL,
                identity_field="call_id",
                identity_value=str(payload.get("call_id", "")),
            )
        if event_type == "model.call.started":
            return
        step.completed_at = created_at
        duration = payload.get("duration_ms")
        step.duration_ms = duration if isinstance(duration, int) and duration >= 0 else None
        if event_type == "model.call.completed":
            step.status = StepStatus.COMPLETED
            step.summary = str(payload.get("action_type") or "模型调用完成")
        else:
            step.status = StepStatus.FAILED
            step.summary = "模型调用失败"
            validation = sanitize_final_answer_validation_details(
                payload.get("validation")
            )
            source_navigation = sanitize_source_navigation_guard_details(
                payload.get("navigation_guard")
            )
            safe_details: dict[str, Any] = {}
            if validation is not None:
                safe_details["answer_validation"] = validation
            if source_navigation is not None:
                safe_details["source_navigation"] = source_navigation
            step.error = {
                "code": str(payload.get("error_code") or "MODEL_CALL_FAILED"),
                "message": "模型调用失败",
                "category": "model",
                "recoverable": bool(payload.get("recoverable")),
                **({"details": safe_details} if safe_details else {}),
            }
        await tx.steps.update(step)

    async def _expire_pending_permissions(self, tx, run_id, created_at, reason: str) -> None:
        for req in await tx.permissions.list_pending_by_run(run_id):
            req.status = PermissionStatus.EXPIRED
            req.decided_at = created_at
            req.note = reason
            await tx.permissions.update_request(req)
            await self._mark_tool_call_permission_expired(tx, req)
            await tx.audits.create(
                AuditLog(
                    id=new_id(),
                    task_id=req.task_id,
                    run_id=req.run_id,
                    step_id=req.step_id,
                    tool_call_id=req.tool_call_id,
                    event_type="permission.expired",
                    actor="system",
                    risk_level=req.risk_level,
                    action_summary=req.action_summary,
                    details={"request_id": str(req.id), "reason": reason},
                    result_summary="expired",
                    created_at=created_at,
                )
            )

    @staticmethod
    async def _mark_tool_call_permission_expired(tx, req: PermissionRequest) -> None:
        """Project request expiry without overwriting approved/denied facts."""
        if req.tool_call_id is None:
            return
        tool_call = await tx.tool_calls.get(req.tool_call_id)
        if tool_call is None or tool_call.permission_status != "pending":
            return
        tool_call.permission_status = "expired"
        await tx.tool_calls.update(tool_call)

    @staticmethod
    async def _fail_open_children(tx, run_id, error: dict, created_at) -> None:
        """Close non-terminal Step/ToolCall projections with a failed Run."""
        open_step_statuses = {
            StepStatus.PENDING,
            StepStatus.RUNNING,
            StepStatus.WAITING_FOR_PERMISSION,
        }
        for step in await tx.steps.list_by_run(run_id):
            if step.status not in open_step_statuses:
                continue
            step.status = StepStatus.FAILED
            step.error = deepcopy(error)
            step.completed_at = created_at
            if step.started_at is not None:
                step.duration_ms = max(
                    0,
                    int((created_at - step.started_at).total_seconds() * 1000),
                )
            await tx.steps.update(step)

        for tool_call in await tx.tool_calls.list_by_run(run_id):
            if tool_call.status not in {"pending", "running"}:
                continue
            tool_call.status = "failed"
            tool_call.error = deepcopy(error)
            tool_call.completed_at = created_at
            if tool_call.started_at is not None:
                tool_call.duration_ms = max(
                    0,
                    int((created_at - tool_call.started_at).total_seconds() * 1000),
                )
            await tx.tool_calls.update(tool_call)

    @staticmethod
    async def _cancel_open_children(tx, run_id, created_at) -> None:
        """Close non-terminal Step/ToolCall projections with a cancelled Run."""
        open_step_statuses = {
            StepStatus.PENDING,
            StepStatus.RUNNING,
            StepStatus.WAITING_FOR_PERMISSION,
        }
        for step in await tx.steps.list_by_run(run_id):
            if step.status not in open_step_statuses:
                continue
            step.status = StepStatus.CANCELLED
            step.summary = "运行已取消"
            step.completed_at = created_at
            if step.started_at is not None:
                step.duration_ms = max(
                    0,
                    int((created_at - step.started_at).total_seconds() * 1000),
                )
            await tx.steps.update(step)

        for tool_call in await tx.tool_calls.list_by_run(run_id):
            if tool_call.status not in {"pending", "running"}:
                continue
            tool_call.status = "cancelled"
            tool_call.error = {
                "code": "RUN_CANCELLED",
                "message": "运行已取消",
                "category": "runtime",
                "recoverable": False,
            }
            tool_call.completed_at = created_at
            if tool_call.started_at is not None:
                tool_call.duration_ms = max(
                    0,
                    int((created_at - tool_call.started_at).total_seconds() * 1000),
                )
            await tx.tool_calls.update(tool_call)

    async def _transition(self, tx, run, target: RunStatus, **fields):
        success = await tx.runs.update_with_lock(
            run_id=run.id,
            new_status=target.value,
            expected_version=run.version,
            expected_status=run.status.value,
            **fields,
        )
        if not success:
            raise RuntimeError(
                f"AgentRun 状态并发冲突: {run.id} {run.status.value}->{target.value}"
            )
        run.status = target
        run.version += 1
        for key, value in fields.items():
            domain_key = {
                "error_json": "error",
                "checkpoint_json": "checkpoint",
            }.get(key, key)
            setattr(run, domain_key, value)
        return run

    async def _record_permission_required(
        self,
        tx,
        payload,
        task_id,
        run_id,
        step_id,
        created_at,
        checkpoint: dict | None = None,
    ) -> None:
        data = payload.get("request") or {}
        request_id = UUID(str(data["id"]))
        if await tx.permissions.get_request(request_id) is None:
            req = PermissionRequest(
                id=request_id,
                task_id=task_id,
                run_id=run_id,
                step_id=step_id,
                tool_name=str(data.get("tool_name", "unknown")),
                action_summary=str(data.get("action_summary", "需要用户确认")),
                reason=data.get("reason"),
                risk_level=str(data.get("risk_level", "L3")),
                scope=dict(data.get("scope") or {"type": "once"}),
                arguments_summary=dict(data.get("arguments_summary") or {}),
                allowed_decisions=list(data.get("allowed_decisions") or ["allow_once", "deny"]),
                checkpoint=checkpoint or {},
                tool_call_id=(
                    UUID(str(data["tool_call_id"])) if data.get("tool_call_id") else None
                ),
                status=PermissionStatus.PENDING,
                created_at=created_at,
                expires_at=_parse_timestamp(str(data["expires_at"])),
            )
            await tx.permissions.create_request(req)
            if req.tool_call_id is not None:
                tool_call = await tx.tool_calls.get(req.tool_call_id)
                if tool_call is not None:
                    tool_call.permission_request_id = req.id
                    tool_call.permission_status = "pending"
                    await tx.tool_calls.update(tool_call)
            if step_id is not None:
                step = await tx.steps.get(step_id)
                if step is not None:
                    step.status = StepStatus.WAITING_FOR_PERMISSION
                    await tx.steps.update(step)
            await tx.audits.create(
                AuditLog(
                    id=new_id(),
                    task_id=task_id,
                    run_id=run_id,
                    step_id=step_id,
                    event_type="permission.required",
                    actor="agent",
                    risk_level=req.risk_level,
                    action_summary=req.action_summary,
                    details={"request_id": str(request_id), "tool_name": req.tool_name},
                    created_at=created_at,
                )
            )

    async def _record_tool_event(
        self, tx, event_type, payload, task_id, run, step_id, created_at
    ) -> None:
        data = payload.get("tool_call") or {}
        if not data.get("id") or step_id is None:
            return
        tool_call_id = UUID(str(data["id"]))
        step = await tx.steps.get(step_id)
        if step is None:
            step = ExecutionStep(
                id=step_id,
                run_id=run.id,
                task_id=task_id,
                type=StepType.TOOL_CALL,
                status=StepStatus.RUNNING,
                title=str(data.get("tool_name", "Tool call")),
                started_at=created_at,
                metadata={"tool_call_id": str(tool_call_id)},
            )
            await self._create_execution_step(tx, run, step)
        else:
            self._validate_execution_step(
                step,
                run,
                task_id,
                StepType.TOOL_CALL,
                identity_field="tool_call_id",
                identity_value=str(tool_call_id),
            )

        tool_call = await tx.tool_calls.get(tool_call_id)
        if tool_call is None:
            tool_call = ToolCall(
                id=tool_call_id,
                task_id=task_id,
                run_id=run.id,
                step_id=step_id,
                provider=str(data.get("provider", "native")),
                tool_name=str(data.get("tool_name", "unknown")),
                mcp_server_id=str(data["mcp_server_id"]) if data.get("mcp_server_id") else None,
                risk_level=str(data.get("risk_level", "L0")),
                arguments=dict(data.get("arguments") or data.get("arguments_summary") or {}),
                arguments_summary=dict(data.get("arguments_summary") or {}),
                status="running"
                if event_type == "tool.call.started"
                else str(data.get("status", "failed")),
                started_at=created_at,
                completed_at=created_at if event_type != "tool.call.started" else None,
                result=data.get("result"),
                result_summary=(data.get("result") or {}).get("summary")
                if isinstance(data.get("result"), dict)
                else None,
                error=data.get("error"),
                permission_request_id=(
                    UUID(str(data["permission_request_id"]))
                    if data.get("permission_request_id")
                    else None
                ),
                permission_status=str(data.get("permission_status", "not_required")),
            )
            await tx.tool_calls.create(tool_call)
        elif event_type != "tool.call.started":
            tool_call.status = "completed" if event_type == "tool.call.finished" else "failed"
            if data.get("permission_request_id"):
                tool_call.permission_request_id = UUID(str(data["permission_request_id"]))
            if data.get("permission_status"):
                tool_call.permission_status = str(data["permission_status"])
            tool_call.result = data.get("result")
            result = data.get("result") or {}
            tool_call.result_summary = result.get("summary") if isinstance(result, dict) else None
            tool_call.error = data.get("error")
            tool_call.completed_at = created_at
            if tool_call.started_at is not None:
                tool_call.duration_ms = max(
                    0,
                    int((created_at - tool_call.started_at).total_seconds() * 1000),
                )
            await tx.tool_calls.update(tool_call)

        if event_type != "tool.call.started" and tool_call.permission_request_id is not None:
            req = await tx.permissions.get_request(tool_call.permission_request_id)
            if req is not None and req.status == PermissionStatus.APPROVED:
                req.status = PermissionStatus.CONSUMED
                await tx.permissions.update_request(req)

        if event_type != "tool.call.started":
            step.status = (
                StepStatus.COMPLETED if event_type == "tool.call.finished" else StepStatus.FAILED
            )
            step.completed_at = created_at
            step.output_data = data.get("result")
            step.error = data.get("error")
            await tx.steps.update(step)

        await tx.audits.create(
            AuditLog(
                id=new_id(),
                task_id=task_id,
                run_id=run.id,
                step_id=step_id,
                tool_call_id=tool_call_id,
                event_type=event_type,
                actor="agent",
                risk_level=tool_call.risk_level,
                action_summary=f"{tool_call.tool_name}: {event_type}",
                details={
                    "tool_name": tool_call.tool_name,
                    "provider": tool_call.provider,
                    "arguments_summary": tool_call.arguments_summary or {},
                    "status": tool_call.status,
                    "duration_ms": tool_call.duration_ms,
                },
                result_summary=tool_call.result_summary,
                error=tool_call.error,
                created_at=created_at,
            )
        )

    async def _create_execution_step(self, tx, run, step: ExecutionStep) -> None:
        """Allocate one contiguous order and increment the Run exactly once."""
        persisted_steps = await tx.steps.list_by_run(run.id)
        order_indexes = [item.order_index for item in persisted_steps]
        if (
            run.step_count != len(persisted_steps)
            or order_indexes != list(range(len(persisted_steps)))
            or (
                run.current_step_id is not None
                and run.current_step_id not in {item.id for item in persisted_steps}
            )
        ):
            raise RuntimeError("AgentRun 既有 Step 投影不一致，禁止在原 Run 上继续分配步骤")
        step.order_index = run.step_count
        next_count = run.step_count + 1
        success = await tx.runs.update_with_lock(
            run_id=run.id,
            new_status=run.status.value,
            expected_version=run.version,
            expected_status=run.status.value,
            step_count=next_count,
            current_step_id=step.id,
        )
        if not success:
            raise RuntimeError(f"AgentRun step 投影并发冲突: {run.id} version={run.version}")
        run.step_count = next_count
        run.current_step_id = step.id
        run.version += 1
        await tx.steps.create(step)

    @staticmethod
    def _validate_execution_step(
        step: ExecutionStep,
        run,
        task_id,
        expected_type: StepType,
        *,
        identity_field: str,
        identity_value: str,
    ) -> None:
        """Reject deterministic ID collisions instead of corrupting a Step."""
        if step.run_id != run.id or step.task_id != task_id:
            raise ValueError("ExecutionStep 与 RuntimeEvent 的 Run/Task 不一致")
        if step.type != expected_type:
            raise ValueError(
                "ExecutionStep 类型与 RuntimeEvent 不一致: "
                f"expected={expected_type.value} actual={step.type.value}"
            )
        existing_identity = str(step.metadata.get(identity_field, ""))
        if existing_identity and identity_value and existing_identity != identity_value:
            raise ValueError(f"ExecutionStep {identity_field} 与 RuntimeEvent 不一致")


def _is_local_artifact_path(
    path: str,
    *,
    run_id: str,
    artifact_id: UUID,
    suffix: str,
) -> bool:
    parsed = PurePosixPath(path)
    legacy = (str(artifact_id)[:2], f"{artifact_id}{suffix}")
    if parsed.parts == legacy:
        return True
    if len(parsed.parts) != 5 or parsed.parts[0] != "scoped":
        return False
    workspace_bucket = parsed.parts[1]
    valid_workspace = (
        workspace_bucket == "unscoped"
        or re.fullmatch(r"path-[0-9a-f]{32}", workspace_bucket) is not None
    )
    if workspace_bucket.startswith("id-"):
        try:
            UUID(workspace_bucket[3:])
            valid_workspace = True
        except ValueError:
            valid_workspace = False
    return (
        valid_workspace
        and parsed.parts[2] == run_id
        and parsed.parts[3] == str(artifact_id)[:2]
        and parsed.parts[4] == f"{artifact_id}{suffix}"
    )


def _parse_timestamp(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
