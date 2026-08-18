"""模型连通性测试 Application Service。

拥有 model.test AuditLog 的写入事务边界。
Repository 不得自行 commit；事务边界由本 Service 通过 UnitOfWork 管理。
"""

from __future__ import annotations

from uuid import uuid4

from jarvis_worker.shared.domain.models import AuditLog, utcnow
from jarvis_worker.database.engine import get_session_factory
from jarvis_worker.database.unit_of_work import PostgresUnitOfWork


class ModelTestService:
    """模型测试审计写入服务。"""

    def __init__(self, uow_factory=get_session_factory) -> None:
        self._uow_factory = uow_factory

    async def write_audit(
        self,
        *,
        provider: str,
        model: str,
        safe_host: str,
        timeout_ms: int,
        status: str,
        latency_ms: float,
        error_code: str | None = None,
        error_message: str | None = None,
        error_category: str | None = None,
        error_recoverable: bool = False,
    ) -> None:
        """在当前请求内通过 UnitOfWork 写入一条 model.test AuditLog。"""
        session_factory = self._uow_factory()
        if session_factory is None:
            raise RuntimeError("Session factory 未初始化，无法写入 AuditLog")

        result_summary = f"success: {latency_ms:.0f}ms" if status == "ok" else f"failure: {error_code}"
        audit_error = None
        if error_code:
            audit_error = {
                "code": error_code,
                "message": error_message,
                "category": error_category,
                "recoverable": error_recoverable,
            }

        audit = AuditLog(
            id=uuid4(),
            event_type="model.test",
            actor="system",
            action_summary="模型连通性测试",
            details={
                "provider": provider,
                "model": model,
                "base_url_host": safe_host,
                "timeout_ms": timeout_ms,
            },
            result_summary=result_summary,
            error=audit_error,
            created_at=utcnow(),
        )

        async with session_factory() as session:
            uow = PostgresUnitOfWork(session)
            async with uow.transaction() as tx:
                await tx.audits.create(audit)
                await tx.commit()
