from __future__ import annotations

import csv
import hashlib
import io
import json
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from jarvis_worker.runtime.audit.service import (
    _EXPORT_FIELDS,
    AUDIT_RETENTION_TOOL_NAME,
    AuditQueryApplicationService,
    _decode_cursor,
    _to_safe_item,
)
from jarvis_worker.shared.domain.models import AuditLog, PermissionStatus


def _log(**overrides) -> AuditLog:
    values = {
        "id": uuid4(), "event_type": "tool.executed", "actor": "agent",
        "action_summary": "读取安全文件", "details": {},
        "created_at": datetime(2026, 7, 20, 12, 0, tzinfo=UTC),
    }
    values.update(overrides)
    return AuditLog(**values)


def test_safe_item_redacts_sensitive_details_and_never_exposes_error_message():
    item = _to_safe_item(_log(
        details={"api_key": "sk-secret", "nested": {"token": "abc", "path": "notes.txt"}, "content": "private body"},
        error={"code": "TOOL_FAILED", "message": "raw traceback and secret"},
    ))
    assert item.details_summary["api_key"] == "[已脱敏]"
    assert item.details_summary["content"] == "[已脱敏]"
    assert item.details_summary["nested"]["token"] == "[已脱敏]"
    assert item.details_summary["nested"]["path"] == "notes.txt"
    assert item.error_code == "TOOL_FAILED"
    assert "message" not in item.__dict__


def test_safe_item_redacts_sensitive_values_hidden_under_ordinary_keys():
    bearer = "Bearer p2-super-secret-token"
    jwt = "eyJheader.eyJpayload.signature"
    item = _to_safe_item(
        _log(
            action_summary=f"request failed with {bearer}",
            details={
                "note": f"auth={bearer}",
                "endpoint": "https://alice:password@example.com/v1",
                "nested": [{"message": jwt}, {"private_key_hint": "secret"}],
            },
        )
    )

    encoded = str(item)
    assert "p2-super-secret-token" not in encoded
    assert "alice" not in encoded
    assert "password@example.com" not in encoded
    assert jwt not in encoded
    assert item.details_summary["nested"][1]["private_key_hint"] == "[已脱敏]"
    assert "Bearer ***" in item.action_summary
    assert item.details_summary["endpoint"] == "https://***:***@example.com/v1"


@pytest.mark.asyncio
async def test_list_page_uses_filters_and_stable_cursor(monkeypatch):
    first, second = _log(), _log(created_at=datetime(2026, 7, 20, 11, 0, tzinfo=UTC))
    captured = {}

    class FakeAudits:
        async def list_page(self, **kwargs):
            captured.update(kwargs)
            return [first, second]

    class FakeUow:
        def __init__(self, _session): self.audits = FakeAudits()

    class FakeSession:
        async def __aenter__(self): return object()
        async def __aexit__(self, *_): return None

    monkeypatch.setattr("jarvis_worker.runtime.audit.service.PostgresUnitOfWork", FakeUow)
    service = AuditQueryApplicationService(lambda: lambda: FakeSession())
    result = await service.list_audit_logs(limit=1, event_type="tool.executed", actor="agent")

    assert len(result["audit_logs"]) == 1
    assert captured["limit"] == 2
    assert captured["event_type"] == "tool.executed"
    assert captured["actor"] == "agent"
    cursor_ts, cursor_id = _decode_cursor(result["next_cursor"])
    assert cursor_ts == first.created_at and cursor_id == first.id


class _FakeSession:
    async def __aenter__(self):
        return object()

    async def __aexit__(self, *_):
        return None


class _FakeTransaction:
    def __init__(self, uow):
        self._uow = uow

    async def __aenter__(self):
        return self._uow

    async def __aexit__(self, *_):
        return None


@pytest.mark.asyncio
async def test_jsonl_export_is_paginated_bounded_redacted_and_audited(monkeypatch):
    source_logs = [
        _log(
            created_at=datetime(2026, 7, 20, 12, 0, tzinfo=UTC)
            - timedelta(seconds=index),
            action_summary=f"row {index} Bearer p2-hidden-token",
            details={"note": "api_key=hidden-value"},
        )
        for index in range(102)
    ]
    source_logs.sort(key=lambda log: (log.created_at, log.id), reverse=True)
    created_audits = []
    page_calls = []

    class FakeAudits:
        async def list_page(self, **kwargs):
            page_calls.append(kwargs)
            candidates = source_logs
            before_created_at = kwargs["before_created_at"]
            before_id = kwargs["before_id"]
            if before_created_at and before_id:
                candidates = [
                    log
                    for log in candidates
                    if (log.created_at, log.id) < (before_created_at, before_id)
                ]
            return candidates[: kwargs["limit"]]

        async def create(self, audit):
            created_audits.append(audit)
            return audit

    class FakeUow:
        def __init__(self, _session):
            self.audits = FakeAudits()

        def transaction(self):
            return _FakeTransaction(self)

        async def commit(self):
            return None

    monkeypatch.setattr(
        "jarvis_worker.runtime.audit.service.PostgresUnitOfWork", FakeUow
    )
    service = AuditQueryApplicationService(lambda: lambda: _FakeSession())
    chunks = [
        chunk
        async for chunk in service.export_audit_logs(
            max_rows=101,
            max_bytes=256 * 1024,
            event_type="tool.executed",
        )
    ]
    body = b"".join(chunks)
    records = [json.loads(line) for line in body.decode().splitlines()]

    assert len(records) == 101
    assert list(records[0]) == list(_EXPORT_FIELDS)
    assert len(page_calls) == 2
    assert all(call["limit"] == 101 for call in page_calls)
    assert "p2-hidden-token" not in body.decode()
    assert "hidden-value" not in body.decode()
    assert created_audits[-1].event_type == "audit.export.completed"
    assert created_audits[-1].details["row_count"] == 101
    assert created_audits[-1].details["byte_count"] == len(body)
    assert created_audits[-1].details["truncated"] is True
    assert created_audits[-1].details["sha256"] == hashlib.sha256(body).hexdigest()
    assert "export_body" not in created_audits[-1].details


@pytest.mark.asyncio
async def test_csv_export_has_fixed_header_and_neutralizes_formulas(monkeypatch):
    created_audits = []
    formula_log = _log(
        event_type="=CMD()",
        action_summary="+SUM(1,1)",
        details={"note": "@danger"},
    )

    class FakeAudits:
        async def list_page(self, **_kwargs):
            return [formula_log]

        async def create(self, audit):
            created_audits.append(audit)
            return audit

    class FakeUow:
        def __init__(self, _session):
            self.audits = FakeAudits()

        def transaction(self):
            return _FakeTransaction(self)

        async def commit(self):
            return None

    monkeypatch.setattr(
        "jarvis_worker.runtime.audit.service.PostgresUnitOfWork", FakeUow
    )
    service = AuditQueryApplicationService(lambda: lambda: _FakeSession())
    body = b"".join(
        [
            chunk
            async for chunk in service.export_audit_logs(
                export_format="csv",
                max_rows=1,
                max_bytes=4_096,
            )
        ]
    )
    rows = list(csv.reader(io.StringIO(body.decode())))

    assert rows[0] == list(_EXPORT_FIELDS)
    assert rows[1][1] == "'=CMD()"
    assert rows[1][3] == "'+SUM(1,1)"
    assert created_audits[-1].details["truncated"] is False


@pytest.mark.asyncio
async def test_export_byte_budget_stops_before_oversized_row(monkeypatch):
    created_audits = []

    class FakeAudits:
        async def list_page(self, **_kwargs):
            return [_log(action_summary="x" * 240) for _ in range(20)]

        async def create(self, audit):
            created_audits.append(audit)
            return audit

    class FakeUow:
        def __init__(self, _session):
            self.audits = FakeAudits()

        def transaction(self):
            return _FakeTransaction(self)

        async def commit(self):
            return None

    monkeypatch.setattr(
        "jarvis_worker.runtime.audit.service.PostgresUnitOfWork", FakeUow
    )
    service = AuditQueryApplicationService(lambda: lambda: _FakeSession())
    body = b"".join(
        [
            chunk
            async for chunk in service.export_audit_logs(
                max_rows=100,
                max_bytes=1_024,
            )
        ]
    )

    assert len(body) <= 1_024
    assert created_audits[-1].details["byte_count"] == len(body)
    assert created_audits[-1].details["truncated"] is True


@pytest.mark.asyncio
async def test_interrupted_export_is_audited_without_exception_text(monkeypatch):
    created_audits = []

    class FakeAudits:
        async def list_page(self, **_kwargs):
            return [_log(), _log()]

        async def create(self, audit):
            created_audits.append(audit)
            return audit

    class FakeUow:
        def __init__(self, _session):
            self.audits = FakeAudits()

        def transaction(self):
            return _FakeTransaction(self)

        async def commit(self):
            return None

    monkeypatch.setattr(
        "jarvis_worker.runtime.audit.service.PostgresUnitOfWork", FakeUow
    )
    service = AuditQueryApplicationService(lambda: lambda: _FakeSession())
    stream = service.export_audit_logs(max_rows=100, max_bytes=4_096)

    await anext(stream)
    await stream.aclose()

    failed = created_audits[-1]
    assert failed.event_type == "audit.export.failed"
    assert failed.error == {
        "code": "AUDIT_EXPORT_INTERRUPTED",
        "category": "runtime",
        "recoverable": True,
    }
    assert "message" not in failed.error


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"export_format": "xml"}, "jsonl 或 csv"),
        ({"max_rows": 0}, "max_rows"),
        ({"max_rows": 10_001}, "max_rows"),
        ({"max_bytes": 1_023}, "max_bytes"),
        ({"max_bytes": 10 * 1024 * 1024 + 1}, "max_bytes"),
    ],
)
def test_export_rejects_out_of_range_budgets_before_streaming(kwargs, message):
    service = AuditQueryApplicationService(lambda: None)
    with pytest.raises(Exception, match=message):
        service.export_audit_logs(**kwargs)


@pytest.mark.asyncio
async def test_retention_preview_classifies_without_deleting_or_exposing_ids(
    monkeypatch,
):
    now = datetime(2026, 7, 31, 12, 0, tzinfo=UTC)
    source_logs = [
        _log(created_at=now - timedelta(days=100)),
        _log(created_at=now - timedelta(days=100), risk_level="L3"),
        _log(created_at=now - timedelta(days=400), risk_level="L3"),
        _log(created_at=now - timedelta(days=400), risk_level="L4"),
        _log(
            created_at=now - timedelta(days=400),
            event_type="memory.deleted",
        ),
        _log(
            created_at=now - timedelta(days=400),
            event_type="runtime.recovery.completed",
        ),
        _log(
            created_at=now - timedelta(days=100),
            event_type="permission.resolved",
        ),
        _log(created_at=now - timedelta(days=400)),
        _log(created_at=now - timedelta(days=10)),
    ]
    source_logs.sort(key=lambda log: (log.created_at, log.id))
    created_audits = []
    calls = []

    class FakeAudits:
        async def list_oldest_page(self, **kwargs):
            calls.append(kwargs)
            candidates = [
                log
                for log in source_logs
                if log.created_at < kwargs["created_before"]
            ]
            after_created_at = kwargs["after_created_at"]
            after_id = kwargs["after_id"]
            if after_created_at and after_id:
                candidates = [
                    log
                    for log in candidates
                    if (log.created_at, log.id) > (after_created_at, after_id)
                ]
            return candidates[: kwargs["limit"]]

        async def create(self, audit):
            created_audits.append(audit)
            return audit

    class FakeUow:
        def __init__(self, _session):
            self.audits = FakeAudits()

        def transaction(self):
            return _FakeTransaction(self)

        async def commit(self):
            return None

    monkeypatch.setattr(
        "jarvis_worker.runtime.audit.service.PostgresUnitOfWork", FakeUow
    )
    service = AuditQueryApplicationService(lambda: lambda: _FakeSession())
    preview = await service.preview_retention(
        standard_days=90,
        extended_days=365,
        max_scan=100,
        max_candidates=100,
        now=now,
    )

    assert preview.dry_run is True
    assert preview.scanned_records == 8
    assert preview.candidate_records == 3
    assert preview.protected_records == 3
    assert preview.extended_retained_records == 2
    assert preview.has_more is False
    assert calls[0]["limit"] == 101
    result_audit = created_audits[-1]
    assert result_audit.event_type == "audit.retention.previewed"
    assert result_audit.details["candidate_records"] == 3
    assert "candidate_ids" not in result_audit.details
    assert len(source_logs) == 9


@pytest.mark.asyncio
async def test_retention_preview_stops_at_candidate_budget(monkeypatch):
    now = datetime(2026, 7, 31, 12, 0, tzinfo=UTC)
    source_logs = [
        _log(created_at=now - timedelta(days=200, seconds=index))
        for index in range(5)
    ]
    source_logs.sort(key=lambda log: (log.created_at, log.id))
    created_audits = []

    class FakeAudits:
        async def list_oldest_page(self, **kwargs):
            return source_logs[: kwargs["limit"]]

        async def create(self, audit):
            created_audits.append(audit)
            return audit

    class FakeUow:
        def __init__(self, _session):
            self.audits = FakeAudits()

        def transaction(self):
            return _FakeTransaction(self)

        async def commit(self):
            return None

    monkeypatch.setattr(
        "jarvis_worker.runtime.audit.service.PostgresUnitOfWork", FakeUow
    )
    service = AuditQueryApplicationService(lambda: lambda: _FakeSession())
    preview = await service.preview_retention(
        max_scan=5,
        max_candidates=2,
        now=now,
    )

    assert preview.scanned_records == 2
    assert preview.candidate_records == 2
    assert preview.has_more is True
    assert created_audits[-1].details["has_more"] is True


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"standard_days": 29}, "standard_days"),
        ({"extended_days": 3_651}, "extended_days"),
        ({"standard_days": 365, "extended_days": 365}, "大于"),
        ({"max_scan": 0}, "max_scan"),
        ({"max_scan": 10_001}, "max_scan"),
        ({"max_candidates": 0}, "max_candidates"),
        ({"max_candidates": 1_001}, "max_candidates"),
    ],
)
@pytest.mark.asyncio
async def test_retention_preview_rejects_unsafe_bounds(kwargs, message):
    service = AuditQueryApplicationService(lambda: None)
    with pytest.raises(Exception, match=message):
        await service.preview_retention(**kwargs)


class _RetentionFakeAudits:
    def __init__(self, source_logs):
        self.source_logs = source_logs
        self.created = []
        self.deleted_ids = []
        self.lock_count = 0

    async def acquire_retention_execution_lock(self):
        self.lock_count += 1

    async def list_oldest_page(self, **kwargs):
        candidates = [
            log
            for log in self.source_logs
            if log.created_at < kwargs["created_before"]
        ]
        after_created_at = kwargs["after_created_at"]
        after_id = kwargs["after_id"]
        if after_created_at and after_id:
            candidates = [
                log
                for log in candidates
                if (log.created_at, log.id) > (after_created_at, after_id)
            ]
        candidates.sort(key=lambda log: (log.created_at, log.id))
        return candidates[: kwargs["limit"]]

    async def create(self, audit):
        self.created.append(audit)
        return audit

    async def delete_by_ids(self, audit_log_ids):
        audit_log_ids = set(audit_log_ids)
        before = len(self.source_logs)
        self.source_logs[:] = [
            log for log in self.source_logs if log.id not in audit_log_ids
        ]
        self.deleted_ids.extend(audit_log_ids)
        return before - len(self.source_logs)


class _RetentionFakePermissions:
    def __init__(self):
        self.requests = {}

    async def get_request(self, request_id):
        return self.requests.get(request_id)

    async def get_request_for_update(self, request_id):
        return self.requests.get(request_id)

    async def create_request(self, request):
        self.requests[request.id] = request
        return request

    async def update_request(self, request):
        self.requests[request.id] = request


class _RetentionEntityRepo:
    def __init__(self):
        self.entities = {}

    async def get(self, entity_id):
        return self.entities.get(entity_id)

    async def create(self, entity):
        self.entities[entity.id] = entity
        return entity

    async def update(self, entity):
        self.entities[entity.id] = entity


class _RetentionFakeUow:
    def __init__(self, source_logs):
        self.audits = _RetentionFakeAudits(source_logs)
        self.permissions = _RetentionFakePermissions()
        self.conversations = _RetentionEntityRepo()
        self.tasks = _RetentionEntityRepo()
        self.runs = _RetentionEntityRepo()

    def transaction(self):
        return _FakeTransaction(self)

    async def flush(self):
        return None

    async def commit(self):
        return None


def _retention_service(monkeypatch, source_logs):
    fake_uow = _RetentionFakeUow(source_logs)
    monkeypatch.setattr(
        "jarvis_worker.runtime.audit.service.PostgresUnitOfWork",
        lambda _session: fake_uow,
    )
    return AuditQueryApplicationService(lambda: lambda: _FakeSession()), fake_uow


@pytest.mark.asyncio
async def test_retention_execution_requires_l4_once_and_rechecks_before_delete(
    monkeypatch,
):
    now = datetime.now(UTC)
    ordinary = [
        _log(created_at=now - timedelta(days=400, seconds=index))
        for index in range(3)
    ]
    permanent = _log(
        created_at=now - timedelta(days=500),
        risk_level="L4",
        event_type="permission.denied",
    )
    source_logs = [*ordinary, permanent]
    service, fake_uow = _retention_service(monkeypatch, source_logs)

    request = await service.create_retention_request(
        standard_days=90,
        extended_days=365,
        max_scan=100,
        max_candidates=100,
    )

    assert request.tool_name == AUDIT_RETENTION_TOOL_NAME
    assert request.risk_level == "L4"
    assert request.allowed_decisions == ["allow_once", "deny"]
    assert request.arguments_summary["candidate_records"] == 3
    assert fake_uow.audits.deleted_ids == []
    assert all("candidate_ids" not in audit.details for audit in fake_uow.audits.created)

    result = await service.resolve_retention_request(
        request.id,
        "allow_once",
        "已核对预演结果",
    )

    assert result.request.status is PermissionStatus.CONSUMED
    assert result.deleted_records == 3
    assert set(fake_uow.audits.deleted_ids) == {log.id for log in ordinary}
    assert source_logs == [permanent]
    assert fake_uow.audits.lock_count == 2
    assert fake_uow.audits.created[-1].event_type == "audit.retention.applied"
    assert fake_uow.audits.created[-1].risk_level == "L4"


@pytest.mark.asyncio
async def test_retention_denial_is_terminal_audited_and_deletes_nothing(monkeypatch):
    source_logs = [_log(created_at=datetime.now(UTC) - timedelta(days=400))]
    service, fake_uow = _retention_service(monkeypatch, source_logs)
    request = await service.create_retention_request()

    result = await service.resolve_retention_request(request.id, "deny", "暂不清理")

    assert result.request.status is PermissionStatus.DENIED
    assert result.deleted_records == 0
    assert fake_uow.audits.deleted_ids == []
    assert fake_uow.audits.created[-1].event_type == (
        "audit.retention.permission_decision"
    )
    assert fake_uow.audits.created[-1].permission_decision == "deny"

    replacement = await service.create_retention_request()
    assert replacement.id != request.id
    assert replacement.status is PermissionStatus.PENDING
    assert fake_uow.audits.deleted_ids == []


@pytest.mark.asyncio
async def test_retention_snapshot_change_aborts_without_delete(monkeypatch):
    source_logs = [
        _log(created_at=datetime.now(UTC) - timedelta(days=400, seconds=index))
        for index in range(2)
    ]
    service, fake_uow = _retention_service(monkeypatch, source_logs)
    request = await service.create_retention_request()
    source_logs.pop()

    with pytest.raises(Exception, match="候选已变化"):
        await service.resolve_retention_request(request.id, "allow_once")

    assert fake_uow.audits.deleted_ids == []
    assert request.status is PermissionStatus.PENDING
