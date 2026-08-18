#!/usr/bin/env python3
"""Jarvis local PostgreSQL migration, backup, restore drill and upgrade entrypoint."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import socket
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlsplit

SCHEMA_VERSION = "1.0"
IDENTIFIER_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
REVISION_RE = re.compile(r"^[A-Za-z0-9_]+$")
APP_PORTS = (8100, 8080, 5173)


class OperationError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class Settings:
    repo: Path
    agent_dir: Path
    output_dir: Path
    conda_env: str
    db_user: str
    source_db: str
    db_password: str
    stamp: str

    @property
    def database_url(self) -> str:
        return (
            "postgresql+asyncpg://"
            f"{self.db_user}:{self.db_password}@127.0.0.1:5432/{self.source_db}"
        )


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def iso(value: datetime) -> str:
    return value.isoformat().replace("+00:00", "Z")


def run(
    command: list[str],
    *,
    cwd: Path,
    env: dict[str, str] | None = None,
    stdin: Any = None,
    stdout: Any = subprocess.PIPE,
    label: str,
) -> subprocess.CompletedProcess[Any]:
    try:
        return subprocess.run(
            command,
            cwd=cwd,
            env=env,
            stdin=stdin,
            stdout=stdout,
            stderr=subprocess.PIPE,
            check=True,
            text=stdout == subprocess.PIPE and stdin is None,
        )
    except subprocess.CalledProcessError as exc:
        raise OperationError("command_failed", f"{label}失败") from exc


def compose(settings: Settings, args: list[str], **kwargs: Any) -> subprocess.CompletedProcess[Any]:
    return run(
        ["docker", "compose", "-f", str(settings.repo / "compose.yaml"), *args],
        cwd=settings.repo,
        **kwargs,
    )


def compose_exec(
    settings: Settings, args: list[str], **kwargs: Any
) -> subprocess.CompletedProcess[Any]:
    return compose(settings, ["exec", "-T", "postgres", *args], **kwargs)


def query(settings: Settings, database: str, sql: str) -> str:
    result = compose_exec(
        settings,
        [
            "psql",
            "-X",
            "-v",
            "ON_ERROR_STOP=1",
            "-U",
            settings.db_user,
            "-d",
            database,
            "-Atqc",
            sql,
        ],
        label="读取 PostgreSQL 状态",
    )
    return str(result.stdout).strip().replace("\r", "")


def require_commands(names: tuple[str, ...]) -> None:
    missing = [name for name in names if shutil.which(name) is None]
    if missing:
        raise OperationError("missing_dependency", "缺少必要的本机命令")


def validate_identifier(value: str, label: str) -> None:
    if not IDENTIFIER_RE.fullmatch(value):
        raise OperationError("invalid_identifier", f"{label}不是安全的 PostgreSQL 标识符")


def database_identity_from_url(value: str) -> tuple[str, str, str]:
    """Extract the local Compose identity without exposing it in reports."""
    try:
        parsed = urlsplit(value)
        port = parsed.port or 5432
    except ValueError as exc:
        raise OperationError("database_url_invalid", "JARVIS_DATABASE_URL 无效") from exc
    if (
        parsed.scheme not in {"postgresql", "postgresql+asyncpg"}
        or parsed.hostname not in {"127.0.0.1", "localhost"}
        or port != 5432
    ):
        raise OperationError(
            "database_url_not_local_compose",
            "data-lifecycle 只管理本地 Compose PostgreSQL",
        )
    db_user = unquote(parsed.username or "jarvis")
    db_password = unquote(parsed.password or "jarvis")
    source_db = unquote(parsed.path.lstrip("/"))
    if not source_db:
        raise OperationError("database_url_invalid", "JARVIS_DATABASE_URL 缺少数据库名")
    return db_user, source_db, db_password


def ensure_postgres_ready(settings: Settings) -> None:
    result = compose(
        settings,
        ["ps", "--status", "running", "-q", "postgres"],
        label="检查 PostgreSQL 容器",
    )
    if not str(result.stdout).strip():
        raise OperationError("postgres_not_running", "PostgreSQL 容器未运行")
    compose_exec(
        settings,
        ["pg_isready", "-U", settings.db_user, "-d", settings.source_db],
        label="检查 PostgreSQL 就绪状态",
    )


def open_app_ports() -> list[int]:
    opened: list[int] = []
    for port in APP_PORTS:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.settimeout(0.2)
            if sock.connect_ex(("127.0.0.1", port)) == 0:
                opened.append(port)
    return opened


def ensure_apps_stopped() -> None:
    opened = open_app_ports()
    if opened:
        joined = ", ".join(str(port) for port in opened)
        raise OperationError(
            "application_running",
            f"应用端口仍在使用（{joined}）；请先停止服务再执行恢复对账或升级",
        )


def alembic(settings: Settings, args: list[str], label: str) -> str:
    env = os.environ.copy()
    env["JARVIS_DATABASE_URL"] = settings.database_url
    result = run(
        [
            "conda",
            "run",
            "--no-capture-output",
            "-n",
            settings.conda_env,
            "python",
            "-m",
            "alembic",
            *args,
        ],
        cwd=settings.agent_dir,
        env=env,
        label=label,
    )
    return str(result.stdout)


def code_head(settings: Settings) -> str:
    output = alembic(settings, ["heads"], "读取 Alembic code head")
    heads = [line.split()[0] for line in output.splitlines() if line.strip().endswith("(head)")]
    if len(heads) != 1 or not REVISION_RE.fullmatch(heads[0]):
        raise OperationError("migration_heads_invalid", "Alembic 必须且只能有一个合法 head")
    return heads[0]


def database_head(settings: Settings, database: str | None = None) -> str:
    selected_database = database or settings.source_db
    version_table = query(
        settings,
        selected_database,
        "SELECT to_regclass('public.alembic_version');",
    )
    if not version_table:
        return "base"
    value = query(
        settings,
        selected_database,
        "SELECT version_num FROM alembic_version;",
    )
    revisions = [item for item in value.splitlines() if item]
    if len(revisions) != 1 or not REVISION_RE.fullmatch(revisions[0]):
        raise OperationError(
            "database_revision_invalid",
            "数据库必须且只能记录一个合法 migration revision",
        )
    return revisions[0]


def git_revision(settings: Settings) -> str:
    return str(
        run(
            ["git", "rev-parse", "HEAD"],
            cwd=settings.repo,
            label="读取 Git revision",
        ).stdout
    ).strip()


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def create_backup(settings: Settings, report: dict[str, Any]) -> Path:
    backup = settings.output_dir / f"jarvis-{settings.stamp}.dump"
    with backup.open("wb") as handle:
        os.chmod(backup, 0o600)
        compose_exec(
            settings,
            [
                "pg_dump",
                "-U",
                settings.db_user,
                "-d",
                settings.source_db,
                "-Fc",
                "--no-owner",
                "--no-privileges",
            ],
            stdout=handle,
            label="创建 PostgreSQL 备份",
        )
    if backup.stat().st_size <= 0:
        raise OperationError("backup_empty", "PostgreSQL 备份文件为空")
    with backup.open("rb") as handle:
        compose_exec(
            settings,
            ["pg_restore", "--list"],
            stdin=handle,
            stdout=subprocess.DEVNULL,
            label="校验 PostgreSQL 备份目录",
        )
    report["backup"] = {
        "file": backup.name,
        "bytes": backup.stat().st_size,
        "sha256": sha256(backup),
        "mode": "0600",
        "catalog_readable": True,
    }
    return backup


def list_tables(settings: Settings, database: str) -> list[str]:
    output = query(
        settings,
        database,
        "SELECT tablename FROM pg_tables WHERE schemaname = 'public' ORDER BY tablename;",
    )
    return [line for line in output.splitlines() if line]


def count_rows(settings: Settings, database: str, table: str) -> int:
    escaped = table.replace('"', '""')
    value = query(settings, database, f'SELECT count(*) FROM public."{escaped}";')
    return int(value)


def restore_and_verify(settings: Settings, backup: Path, report: dict[str, Any]) -> None:
    restore_db = f"jarvis_restore_{settings.stamp.lower()}_{os.getpid()}"
    validate_identifier(restore_db, "临时恢复数据库")
    if restore_db == settings.source_db:
        raise OperationError("unsafe_restore_target", "恢复目标不得等于源数据库")
    created = False
    try:
        compose_exec(
            settings,
            ["createdb", "-U", settings.db_user, restore_db],
            label="创建隔离恢复数据库",
        )
        created = True
        with backup.open("rb") as handle:
            compose_exec(
                settings,
                [
                    "pg_restore",
                    "-U",
                    settings.db_user,
                    "-d",
                    restore_db,
                    "--no-owner",
                    "--no-privileges",
                    "--exit-on-error",
                ],
                stdin=handle,
                stdout=subprocess.DEVNULL,
                label="恢复 PostgreSQL 备份",
            )
        source_revision = database_head(settings)
        restored_revision = database_head(settings, restore_db)
        if source_revision != restored_revision:
            raise OperationError("restore_revision_mismatch", "恢复库 migration revision 不一致")
        source_tables = list_tables(settings, settings.source_db)
        restored_tables = list_tables(settings, restore_db)
        if source_tables != restored_tables:
            raise OperationError("restore_schema_mismatch", "恢复库 public 表集合不一致")
        mismatches: list[str] = []
        for table in source_tables:
            if count_rows(settings, settings.source_db, table) != count_rows(
                settings, restore_db, table
            ):
                mismatches.append(table)
        if mismatches:
            raise OperationError("restore_count_mismatch", "恢复库存在表行数不一致")
        report["restore_verification"] = {
            "isolated_database": True,
            "temporary_database_removed": False,
            "migration_revision_match": True,
            "public_table_set_match": True,
            "compared_table_count": len(source_tables),
            "row_counts_match": True,
        }
    finally:
        if created:
            compose_exec(
                settings,
                ["dropdb", "-U", settings.db_user, "--if-exists", restore_db],
                stdout=subprocess.DEVNULL,
                label="清理隔离恢复数据库",
            )
    if report["restore_verification"] is not None:
        report["restore_verification"]["temporary_database_removed"] = True


def write_report(settings: Settings, report: dict[str, Any]) -> Path:
    path = settings.output_dir / "report.json"
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.chmod(path, 0o600)
    return path


def make_settings(args: argparse.Namespace) -> Settings:
    repo = Path(args.repo).resolve()
    stamp = utc_now().strftime("%Y%m%dT%H%M%S%fZ")
    output_root = Path(
        args.output or os.getenv("JARVIS_DATA_LIFECYCLE_OUTPUT_DIR", repo / ".local/data-lifecycle")
    ).resolve()
    output_dir = output_root / stamp
    output_dir.mkdir(parents=True, exist_ok=False)
    os.chmod(output_dir, 0o700)
    runtime_database_url = os.getenv("JARVIS_DATABASE_URL", "").strip()
    default_user, default_source_db, default_password = (
        database_identity_from_url(runtime_database_url)
        if runtime_database_url
        else ("jarvis", "jarvis", "jarvis")
    )
    db_user = os.getenv("JARVIS_DATA_DB_USER", default_user)
    source_db = os.getenv("JARVIS_DATA_SOURCE_DB", default_source_db)
    validate_identifier(db_user, "数据库用户")
    validate_identifier(source_db, "源数据库")
    return Settings(
        repo=repo,
        agent_dir=repo / "apps/agent-worker",
        output_dir=output_dir,
        conda_env=args.conda_env,
        db_user=db_user,
        source_db=source_db,
        db_password=os.getenv("JARVIS_DATA_DB_PASSWORD", default_password),
        stamp=stamp,
    )


def self_test() -> int:
    validate_identifier("jarvis_restore_20260803t000000z_123", "测试数据库")
    try:
        validate_identifier("jarvis;drop database", "测试数据库")
    except OperationError as exc:
        assert exc.code == "invalid_identifier"
    else:
        raise AssertionError("unsafe identifier was accepted")
    assert database_identity_from_url(
        "postgresql+asyncpg://fixture:p%40ss@127.0.0.1:5432/jarvis_rc_a59fef8"
    ) == ("fixture", "jarvis_rc_a59fef8", "p@ss")
    try:
        database_identity_from_url(
            "postgresql+asyncpg://fixture:secret@database.example:5432/jarvis"
        )
    except OperationError as exc:
        assert exc.code == "database_url_not_local_compose"
    else:
        raise AssertionError("remote database URL was accepted by local lifecycle")
    safe_report = {
        "schema_version": SCHEMA_VERSION,
        "operation": "backup",
        "status": "failed",
        "error": {"code": "command_failed", "message": "创建 PostgreSQL 备份失败"},
    }
    rendered = json.dumps(safe_report)
    for forbidden in ("postgresql://", "asyncpg://", "password", "/Users/"):
        assert forbidden not in rendered
    print("[data-lifecycle] self-test passed")
    return 0


def execute(args: argparse.Namespace) -> int:
    settings = make_settings(args)
    started = utc_now()
    report: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "operation": args.operation,
        "status": "running",
        "revision": None,
        "started_at": iso(started),
        "finished_at": None,
        "code_head": None,
        "database_head_before": None,
        "database_head_after": None,
        "backup": None,
        "restore_verification": None,
        "error": None,
    }
    try:
        require_commands(("docker", "conda", "git"))
        ensure_postgres_ready(settings)
        report["revision"] = git_revision(settings)
        report["code_head"] = code_head(settings)
        report["database_head_before"] = database_head(settings)

        if args.operation == "status":
            if report["code_head"] != report["database_head_before"]:
                raise OperationError("migration_pending", "数据库 migration 尚未到达当前 code head")
        elif args.operation == "backup":
            create_backup(settings, report)
        elif args.operation == "restore-drill":
            ensure_apps_stopped()
            backup = create_backup(settings, report)
            restore_and_verify(settings, backup, report)
        elif args.operation == "upgrade":
            if not args.confirm:
                raise OperationError("confirmation_required", "升级必须显式传入 --confirm")
            ensure_apps_stopped()
            backup = create_backup(settings, report)
            restore_and_verify(settings, backup, report)
            alembic(settings, ["upgrade", "head"], "执行 Alembic migration")
            report["database_head_after"] = database_head(settings)
            if report["database_head_after"] != report["code_head"]:
                raise OperationError("upgrade_incomplete", "升级后数据库未到达当前 code head")
        else:
            raise OperationError("unsupported_operation", "不支持的数据操作")

        if report["database_head_after"] is None:
            report["database_head_after"] = database_head(settings)
        report["status"] = "passed"
        report["finished_at"] = iso(utc_now())
        path = write_report(settings, report)
        print(f"[data-lifecycle] PASSED operation={args.operation}")
        print(f"[data-lifecycle] report={path}")
        return 0
    except OperationError as exc:
        report["status"] = "failed"
        report["finished_at"] = iso(utc_now())
        report["error"] = {"code": exc.code, "message": str(exc)}
        path = write_report(settings, report)
        print(f"[data-lifecycle] FAILED: {exc}", file=sys.stderr)
        print(f"[data-lifecycle] report={path}", file=sys.stderr)
        return 1


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument(
        "operation", choices=("status", "backup", "restore-drill", "upgrade"), nargs="?"
    )
    result.add_argument("--repo", default=Path(__file__).resolve().parent.parent)
    result.add_argument("--conda-env", default=os.getenv("JARVIS_CONDA_ENV", "jarvis-assistant"))
    result.add_argument("--output")
    result.add_argument("--confirm", action="store_true")
    result.add_argument("--self-test", action="store_true")
    return result


def main() -> int:
    args = parser().parse_args()
    if args.self_test:
        return self_test()
    if args.operation is None:
        parser().error("operation is required unless --self-test is used")
    return execute(args)


if __name__ == "__main__":
    raise SystemExit(main())
