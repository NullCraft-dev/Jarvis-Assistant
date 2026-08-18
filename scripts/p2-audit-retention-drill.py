#!/usr/bin/env python3
"""在一次性 PostgreSQL 中验证审计保留执行器的真实事务语义。"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import subprocess
from dataclasses import asdict
from datetime import timedelta
from pathlib import Path
from typing import Any

from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from jarvis_worker.database.models import AuditLogModel, PermissionRequestModel
from jarvis_worker.database.unit_of_work import PostgresUnitOfWork
from jarvis_worker.runtime.audit.service import AuditQueryApplicationService
from jarvis_worker.shared.domain.models import AuditLog, PermissionStatus, new_id, utcnow

REPO_DIR = Path(__file__).resolve().parent.parent
AGENT_DIR = REPO_DIR / "apps" / "agent-worker"


class DrillError(RuntimeError):
    pass


def source_state() -> dict[str, Any]:
    """为本次实际执行的源码生成包含未跟踪文件的稳定摘要。"""
    revision = subprocess.check_output(
        ["git", "rev-parse", "HEAD"], cwd=REPO_DIR, text=True
    ).strip()
    cached = subprocess.check_output(
        [
            "git",
            "diff",
            "--cached",
            "--binary",
            "HEAD",
            "--",
            "apps",
            "packages",
            "scripts",
        ],
        cwd=REPO_DIR,
    )
    unstaged = subprocess.check_output(
        [
            "git",
            "diff",
            "--binary",
            "HEAD",
            "--",
            "apps",
            "packages",
            "scripts",
        ],
        cwd=REPO_DIR,
    )
    untracked_output = subprocess.check_output(
        [
            "git",
            "ls-files",
            "--others",
            "--exclude-standard",
            "--",
            "apps",
            "packages",
            "scripts",
        ],
        cwd=REPO_DIR,
        text=True,
    )
    untracked = sorted(filter(None, untracked_output.splitlines()))
    digest = hashlib.sha256()
    digest.update(f"revision:{revision}\n".encode())
    digest.update(b"cached\0")
    digest.update(cached)
    digest.update(b"unstaged\0")
    digest.update(unstaged)
    for relative_path in untracked:
        digest.update(b"untracked\0")
        digest.update(relative_path.encode())
        digest.update(b"\0")
        digest.update((REPO_DIR / relative_path).read_bytes())
    return {
        "revision": revision,
        "worktree_dirty": bool(cached or unstaged or untracked),
        "source_state_sha256": digest.hexdigest(),
        "untracked_source_file_count": len(untracked),
    }


def require(condition: bool, message: str) -> None:
    if not condition:
        raise DrillError(message)


async def count_ids(session_factory, ids: list) -> int:
    async with session_factory() as session:
        result = await session.execute(
            select(func.count()).select_from(AuditLogModel).where(
                AuditLogModel.id.in_(ids)
            )
        )
        return int(result.scalar_one())


async def event_count(session_factory, event_type: str) -> int:
    async with session_factory() as session:
        result = await session.execute(
            select(func.count()).select_from(AuditLogModel).where(
                AuditLogModel.event_type == event_type
            )
        )
        return int(result.scalar_one())


async def run_drill(database_url: str, expected_database: str) -> dict[str, Any]:
    engine = create_async_engine(database_url, pool_pre_ping=True)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with session_factory() as session:
            database_name = (
                await session.execute(text("SELECT current_database()"))
            ).scalar_one()
        require(
            database_name == expected_database
            and database_name.startswith("jarvis_p2_audit_retention"),
            "数据库安全前缀或名称不匹配，拒绝执行",
        )

        now = utcnow()
        ordinary = [
            AuditLog(
                id=new_id(),
                event_type="tool.executed",
                actor="agent",
                action_summary="隔离演练普通过期记录",
                created_at=now - timedelta(days=400, seconds=index),
            )
            for index in range(3)
        ]
        extended_expired = AuditLog(
            id=new_id(),
            event_type="permission.reviewed",
            actor="user",
            risk_level="L3",
            action_summary="隔离演练延长保留但已过期记录",
            created_at=now - timedelta(days=500),
        )
        extended_retained = AuditLog(
            id=new_id(),
            event_type="permission.reviewed",
            actor="user",
            risk_level="L3",
            action_summary="隔离演练延长保留记录",
            created_at=now - timedelta(days=100),
        )
        permanent = [
            AuditLog(
                id=new_id(),
                event_type="permission.denied",
                actor="user",
                risk_level="L4",
                action_summary="隔离演练永久权限记录",
                created_at=now - timedelta(days=500),
            ),
            AuditLog(
                id=new_id(),
                event_type="runtime.cleanup.completed",
                actor="system",
                risk_level="L0",
                action_summary="隔离演练永久清理记录",
                created_at=now - timedelta(days=500),
            ),
        ]
        recent = AuditLog(
            id=new_id(),
            event_type="tool.executed",
            actor="agent",
            action_summary="隔离演练近期记录",
            created_at=now - timedelta(days=10),
        )
        candidate_ids = [log.id for log in [*ordinary, extended_expired]]
        protected_ids = [extended_retained.id, *(log.id for log in permanent), recent.id]

        async with session_factory() as session:
            async with PostgresUnitOfWork(session).transaction() as tx:
                for audit in [
                    *ordinary,
                    extended_expired,
                    extended_retained,
                    *permanent,
                    recent,
                ]:
                    await tx.audits.create(audit)
                await tx.commit()

        service = AuditQueryApplicationService(lambda: session_factory)
        preview = await service.preview_retention(
            standard_days=90,
            extended_days=365,
            max_scan=100,
            max_candidates=100,
            now=now,
        )
        require(preview.candidate_records == 4, "预演候选数量不符合预期")
        require(preview.protected_records == 2, "永久保护数量不符合预期")
        require(preview.extended_retained_records == 1, "延长保留数量不符合预期")

        first_request = await service.create_retention_request(
            standard_days=90,
            extended_days=365,
            max_scan=100,
            max_candidates=100,
        )
        require(first_request.risk_level == "L4", "清理请求不是 L4")
        require(
            first_request.allowed_decisions == ["allow_once", "deny"],
            "清理请求允许了长期授权",
        )
        require(
            await count_ids(session_factory, candidate_ids) == 4,
            "创建确认请求阶段发生了删除",
        )

        denied = await service.resolve_retention_request(
            first_request.id,
            "deny",
            "隔离演练先拒绝",
        )
        require(denied.request.status is PermissionStatus.DENIED, "拒绝状态未持久化")
        require(
            await count_ids(session_factory, candidate_ids) == 4,
            "拒绝决策错误删除了记录",
        )

        second_request = await service.create_retention_request(
            standard_days=90,
            extended_days=365,
            max_scan=100,
            max_candidates=100,
        )
        require(second_request.id != first_request.id, "拒绝后未创建新的单次确认")
        require(
            second_request.status is PermissionStatus.PENDING,
            "重新申请未进入 pending",
        )

        applied = await service.resolve_retention_request(
            second_request.id,
            "allow_once",
            "隔离演练批准单批清理",
        )
        require(applied.request.status is PermissionStatus.CONSUMED, "批准未原子消费")
        require(applied.deleted_records == 4, "实际删除数量不符合预期")
        require(
            await count_ids(session_factory, candidate_ids) == 0,
            "过期候选没有全部删除",
        )
        require(
            await count_ids(session_factory, protected_ids) == len(protected_ids),
            "延长、永久或近期记录被错误删除",
        )

        repeated = await service.resolve_retention_request(
            second_request.id,
            "allow_once",
            "隔离演练幂等重试",
        )
        require(repeated.deleted_records == 4, "批准幂等结果漂移")
        require(
            await event_count(session_factory, "audit.retention.applied") == 1,
            "幂等重试重复写入应用审计",
        )
        require(
            await event_count(
                session_factory, "audit.retention.permission_decision"
            )
            == 1,
            "拒绝审计数量不符合预期",
        )

        async with session_factory() as session:
            requests = (
                await session.execute(
                    select(PermissionRequestModel).order_by(
                        PermissionRequestModel.created_at.asc()
                    )
                )
            ).scalars().all()
            retention_audits = (
                await session.execute(
                    select(AuditLogModel).where(
                        AuditLogModel.event_type.like("audit.retention.%")
                    )
                )
            ).scalars().all()
        require(len(requests) == 2, "权限请求数量不符合预期")
        require(
            all("candidate_ids" not in (request.checkpoint_json or {}) for request in requests),
            "权限检查点泄漏候选 ID",
        )
        require(
            all("candidate_ids" not in (audit.details_json or {}) for audit in retention_audits),
            "结果审计泄漏候选 ID",
        )

        return {
            "status": "passed",
            "database_safety_check": "passed",
            "migration_head": (
                await _migration_head(session_factory)
            ),
            "preview": asdict(preview),
            "permission_requests": {
                "count": 2,
                "first_status": denied.request.status.value,
                "second_status": applied.request.status.value,
                "allowed_decisions": second_request.allowed_decisions,
            },
            "deletion": {
                "candidate_records_before": 4,
                "deleted_records": applied.deleted_records,
                "candidate_records_after": 0,
                "protected_records_after": len(protected_ids),
                "idempotent_repeat_applied_audit_count": 1,
            },
            "audit": {
                "permission_decision_records": 1,
                "applied_records": 1,
                "candidate_ids_exposed": False,
            },
            "source": source_state(),
        }
    finally:
        await engine.dispose()


async def _migration_head(session_factory) -> str:
    async with session_factory() as session:
        return str(
            (
                await session.execute(text("SELECT version_num FROM alembic_version"))
            ).scalar_one()
        )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--database-url", required=True)
    parser.add_argument("--expected-database", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    try:
        summary = asyncio.run(
            run_drill(args.database_url, args.expected_database)
        )
    except Exception as exc:
        summary = {
            "status": "failed",
            "error_type": type(exc).__name__,
            "error": str(exc)[:500],
            "source": source_state(),
        }
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(
            json.dumps(summary, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        raise
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(summary, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
