#!/usr/bin/env python3
"""Generate bounded runtime diagnostics and a redacted Jarvis support bundle."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

SCHEMA_VERSION = "1.0"
ALLOWED_HOSTS = {"127.0.0.1", "localhost", "::1"}
ALLOWED_BUNDLE_FILES = {
    "environment.json",
    "health.json",
    "log-summary.json",
    "operations-summary.json",
    "report.json",
    "manifest.json",
}
LOG_PATTERN = re.compile(r"^[A-Za-z0-9_.-]+\.log(?:\.\d+)?$")
LOG_LEVELS = ("DEBUG", "INFO", "WARN", "ERROR")
MAX_LOG_SCAN_BYTES = 5 * 1024 * 1024
MAX_LOG_FILES = 50
MAX_API_BYTES = 1024 * 1024
MAX_OPERATION_REPORT_BYTES = 1024 * 1024
MAX_CAPACITY_FILES = 100_000


@dataclass(frozen=True)
class DiagnosticCheck:
    id: str
    category: str
    status: str
    summary: str
    remediation: str | None = None


class SafeFailure(RuntimeError):
    pass


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def iso(value: datetime) -> str:
    return value.isoformat().replace("+00:00", "Z")


def stamp() -> str:
    return utc_now().strftime("%Y%m%dT%H%M%S%fZ")


def write_json(path: Path, value: Any) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.chmod(path, 0o600)


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def validate_gateway_url(value: str) -> str:
    parsed = urllib.parse.urlsplit(value.rstrip("/"))
    if parsed.scheme != "http" or parsed.hostname not in ALLOWED_HOSTS:
        raise SafeFailure("Gateway URL 只允许本机 HTTP 地址")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise SafeFailure("Gateway URL 不允许凭据、query 或 fragment")
    if not parsed.path.endswith("/api"):
        raise SafeFailure("Gateway URL 必须以 /api 结尾")
    return value.rstrip("/")


def fetch_api(base_url: str, relative: str, timeout: float = 8.0) -> dict[str, Any]:
    request = urllib.request.Request(
        f"{base_url}{relative}", headers={"Accept": "application/json"}
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            if response.status != 200:
                raise SafeFailure("诊断接口返回非成功状态")
            body = response.read(MAX_API_BYTES + 1)
            if len(body) > MAX_API_BYTES:
                raise SafeFailure("诊断接口响应超过容量上限")
            payload = json.loads(body)
    except (OSError, ValueError, urllib.error.URLError) as exc:
        raise SafeFailure("诊断接口暂不可用") from exc
    if not isinstance(payload, dict) or payload.get("ok") is not True:
        raise SafeFailure("诊断接口未返回有效 ApiResult")
    data = payload.get("data")
    if not isinstance(data, dict):
        raise SafeFailure("诊断接口缺少结构化 data")
    return data


def project_gateway_health(data: dict[str, Any]) -> dict[str, Any]:
    return {
        "status": str(data.get("status", "unknown")),
        "runtime_bus": str(data.get("runtime_bus", "unknown")),
        "persistence_backend": str(data.get("persistence_backend", "unknown")),
        "persistence_status": str(data.get("persistence_status", "unknown")),
        "control_plane_status": str(data.get("control_plane_status", "unknown")),
    }


def project_runtime_health(data: dict[str, Any]) -> dict[str, Any]:
    workers = data.get("workers") if isinstance(data.get("workers"), dict) else {}
    streams: list[dict[str, Any]] = []
    for item in data.get("streams", []):
        if not isinstance(item, dict):
            continue
        streams.append(
            {
                "name": str(item.get("name", "unknown")),
                "available": bool(item.get("available", False)),
                "lag": int(item.get("lag", -1)),
                "pending": int(item.get("pending", 0)),
                "consumers": int(item.get("consumers", 0)),
                "oldest_pending_ms": int(item.get("oldest_pending_ms", 0)),
                "error_code": str(item.get("error_code", "")),
            }
        )
    dead_letters: list[dict[str, Any]] = []
    for item in data.get("dead_letters", []):
        if isinstance(item, dict):
            dead_letters.append(
                {
                    "name": str(item.get("name", "unknown")),
                    "count": int(item.get("count", 0)),
                }
            )
    counters = data.get("counters") if isinstance(data.get("counters"), dict) else {}
    safe_counters = {
        str(key): int(value)
        for key, value in counters.items()
        if isinstance(key, str) and isinstance(value, int)
    }
    return {
        "status": str(data.get("status", "unknown")),
        "runtime_bus": str(data.get("runtime_bus", "unknown")),
        "workers": {
            "total": int(workers.get("total", 0)),
            "online": int(workers.get("online", 0)),
            "busy": int(workers.get("busy", 0)),
            "stale": int(workers.get("stale", 0)),
        },
        "streams": streams,
        "dead_letters": dead_letters,
        "counters": safe_counters,
        "warning_count": len(data.get("warnings", [])),
    }


def project_workers(data: dict[str, Any]) -> dict[str, Any]:
    workers = data.get("workers") if isinstance(data.get("workers"), list) else []
    kinds: Counter[str] = Counter()
    statuses: Counter[str] = Counter()
    stale = 0
    model_configured = 0
    for worker in workers:
        if not isinstance(worker, dict):
            continue
        kinds[str(worker.get("worker_kind", "unknown"))] += 1
        statuses[str(worker.get("status", "unknown"))] += 1
        stale += int(bool(worker.get("is_stale", False)))
        model = worker.get("model")
        if isinstance(model, dict) and model.get("status") == "configured":
            model_configured += 1
    return {
        "total": len(workers),
        "by_kind": dict(sorted(kinds.items())),
        "by_status": dict(sorted(statuses.items())),
        "stale": stale,
        "model_configured": model_configured,
    }


def project_reconciliation(data: dict[str, Any]) -> dict[str, Any]:
    return {
        "status": str(data.get("status", "unknown")),
        "scanned_runs": int(data.get("scanned_runs", 0)),
        "scanned_events": int(data.get("scanned_events", 0)),
        "scanned_steps": int(data.get("scanned_steps", 0)),
        "scanned_artifacts": int(data.get("scanned_artifacts", 0)),
        "issue_count": int(data.get("issue_count", 0)),
        "truncated": bool(data.get("truncated", False)),
    }


def directory_usage(path: Path) -> dict[str, Any]:
    total_bytes = 0
    files = 0
    truncated = False
    if path.is_dir():
        for root, directories, filenames in os.walk(path, followlinks=False):
            directories[:] = [name for name in directories if not (Path(root) / name).is_symlink()]
            for name in filenames:
                candidate = Path(root) / name
                if candidate.is_symlink():
                    continue
                try:
                    total_bytes += candidate.stat().st_size
                except OSError:
                    continue
                files += 1
                if files >= MAX_CAPACITY_FILES:
                    truncated = True
                    break
            if truncated:
                break
    return {"bytes": total_bytes, "files": files, "truncated": truncated}


def capacity_summary(repo: Path) -> dict[str, Any]:
    usage = shutil.disk_usage(repo)
    local = repo / ".local"
    categories = {}
    for name in ("logs", "artifacts", "rag-assets", "release-gate", "data-lifecycle"):
        categories[name] = directory_usage(local / name)
    return {
        "filesystem": {
            "total_bytes": usage.total,
            "used_bytes": usage.used,
            "free_bytes": usage.free,
            "free_percent": round((usage.free / usage.total) * 100, 2) if usage.total else 0,
        },
        "categories": categories,
    }


def tail_bytes(path: Path, limit: int) -> bytes:
    size = path.stat().st_size
    with path.open("rb") as handle:
        if size > limit:
            handle.seek(size - limit)
            handle.readline()
        return handle.read(limit)


def summarize_log(path: Path) -> dict[str, Any]:
    levels: Counter[str] = Counter()
    failure_locations: Counter[str] = Counter()
    scanned = tail_bytes(path, MAX_LOG_SCAN_BYTES).decode("utf-8", errors="replace")
    for line in scanned.splitlines():
        columns = line.split(" | ", 6)
        if len(columns) != 7:
            continue
        level = columns[1].strip()
        if level in LOG_LEVELS:
            levels[level] += 1
        if level in {"WARN", "ERROR"}:
            location = columns[4].strip()
            if (
                not location.startswith("/")
                and ".." not in location
                and re.fullmatch(r"[A-Za-z0-9_./()*:-]{1,200}", location)
            ):
                failure_locations[location] += 1
    stat = path.stat()
    return {
        "bytes": stat.st_size,
        "scanned_bytes": min(stat.st_size, MAX_LOG_SCAN_BYTES),
        "modified_at": iso(datetime.fromtimestamp(stat.st_mtime, timezone.utc)),
        "levels": {level: levels[level] for level in LOG_LEVELS},
        "failure_locations": [
            {"location": location, "count": count}
            for location, count in failure_locations.most_common(10)
        ],
    }


def log_service(name: str) -> str:
    base = re.sub(r"\.\d+$", "", name)
    if base == "gateway.log":
        return "gateway"
    if base == "control-plane.log":
        return "control-plane"
    if base == "mlx-vlm.log":
        return "mlx-vlm"
    if base.startswith("rag-worker-"):
        return "rag-worker"
    if base.startswith("worker-"):
        return "agent-worker"
    return "dev-runtime"


def log_summary(repo: Path) -> dict[str, Any]:
    log_dir = repo / ".local/logs"
    services: dict[str, dict[str, Any]] = {}
    scanned_files = 0
    truncated = False
    if log_dir.is_dir():
        for candidate in sorted(log_dir.iterdir(), key=lambda path: path.name):
            if (
                candidate.is_file()
                and not candidate.is_symlink()
                and LOG_PATTERN.fullmatch(candidate.name)
            ):
                if scanned_files >= MAX_LOG_FILES:
                    truncated = True
                    break
                scanned_files += 1
                item = summarize_log(candidate)
                service = log_service(candidate.name)
                aggregate = services.setdefault(
                    service,
                    {
                        "service": service,
                        "segments": 0,
                        "bytes": 0,
                        "scanned_bytes": 0,
                        "latest_modified_at": item["modified_at"],
                        "levels": {level: 0 for level in LOG_LEVELS},
                        "failure_locations": Counter(),
                    },
                )
                aggregate["segments"] += 1
                aggregate["bytes"] += item["bytes"]
                aggregate["scanned_bytes"] += item["scanned_bytes"]
                aggregate["latest_modified_at"] = max(
                    aggregate["latest_modified_at"], item["modified_at"]
                )
                for level in LOG_LEVELS:
                    aggregate["levels"][level] += item["levels"][level]
                for location in item["failure_locations"]:
                    aggregate["failure_locations"][location["location"]] += location["count"]
    safe_services = []
    for service in sorted(services):
        aggregate = services[service]
        locations = aggregate.pop("failure_locations")
        aggregate["failure_locations"] = [
            {"location": location, "count": count} for location, count in locations.most_common(10)
        ]
        safe_services.append(aggregate)
    return {
        "raw_messages_included": False,
        "raw_file_names_included": False,
        "max_scanned_bytes_per_file": MAX_LOG_SCAN_BYTES,
        "max_files": MAX_LOG_FILES,
        "truncated": truncated,
        "services": safe_services,
    }


def latest_report(root: Path) -> dict[str, Any] | None:
    candidates = sorted(root.glob("*/report.json")) if root.is_dir() else []
    for path in reversed(candidates):
        try:
            if path.stat().st_size > MAX_OPERATION_REPORT_BYTES:
                continue
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(value, dict):
            return value
    return None


def operations_summary(repo: Path) -> dict[str, Any]:
    local = repo / ".local"
    release = latest_report(local / "release-gate")
    preflight = latest_report(local / "preflight")
    data = latest_report(local / "data-lifecycle")
    return {
        "release_gate": (
            {
                "mode": str(release.get("mode", "unknown")),
                "status": str(release.get("status", "unknown")),
                "revision": str(release.get("revision", "")),
                "passed_steps": int(release.get("passed_steps", 0)),
                "failed_steps": [str(item) for item in release.get("failed_steps", [])],
            }
            if release
            else None
        ),
        "preflight": (
            {
                "status": str(preflight.get("status", "unknown")),
                "check_count": len(preflight.get("checks", [])),
                "failed_count": sum(
                    1
                    for item in preflight.get("checks", [])
                    if isinstance(item, dict) and item.get("status") == "failed"
                ),
                "warning_count": sum(
                    1
                    for item in preflight.get("checks", [])
                    if isinstance(item, dict) and item.get("status") == "warning"
                ),
            }
            if preflight
            else None
        ),
        "data_lifecycle": (
            {
                "operation": str(data.get("operation", "unknown")),
                "status": str(data.get("status", "unknown")),
                "code_head": str(data.get("code_head", "")),
                "database_head_after": str(data.get("database_head_after") or ""),
                "error_code": str((data.get("error") or {}).get("code", "")),
            }
            if data
            else None
        ),
    }


def safe_version(command: list[str], cwd: Path) -> str | None:
    try:
        result = subprocess.run(
            command,
            cwd=cwd,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=8,
            check=False,
            text=True,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    first = result.stdout.splitlines()[0].strip() if result.stdout else ""
    if not re.fullmatch(r"[A-Za-z0-9 ._+()/-]{1,200}", first):
        return None
    return first


def git_state(repo: Path) -> dict[str, Any]:
    revision = safe_version(["git", "rev-parse", "HEAD"], repo) or "unknown"
    try:
        status = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=repo,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=8,
            check=False,
            text=True,
        )
        dirty = bool(status.stdout.strip())
    except (OSError, subprocess.TimeoutExpired):
        dirty = True
    return {"revision": revision, "worktree_dirty": dirty}


def environment_summary(repo: Path) -> dict[str, Any]:
    return {
        "os": platform.system(),
        "architecture": platform.machine(),
        "python": platform.python_version(),
        "git": git_state(repo),
        "tool_versions": {
            "docker_compose": safe_version(["docker", "compose", "version"], repo),
            "conda": safe_version(["conda", "--version"], repo),
            "go": safe_version(["go", "version"], repo),
            "node": safe_version(["node", "--version"], repo),
            "npm": safe_version(["npm", "--version"], repo),
        },
        "local_paths_included": False,
        "environment_values_included": False,
    }


def build_health(base_url: str, checks: list[DiagnosticCheck]) -> dict[str, Any]:
    result: dict[str, Any] = {
        "gateway": None,
        "runtime": None,
        "workers": None,
        "storage_reconciliation": None,
    }
    try:
        gateway = project_gateway_health(fetch_api(base_url, "/health"))
        result["gateway"] = gateway
        status = "passed" if gateway["status"] == "healthy" else "warning"
        checks.append(
            DiagnosticCheck("gateway.health", "runtime", status, "Gateway 健康投影已读取")
        )
    except SafeFailure:
        checks.append(
            DiagnosticCheck(
                "gateway.health", "runtime", "warning", "Gateway 暂不可用", "先确认服务是否已启动"
            )
        )
        return result

    try:
        runtime = project_runtime_health(fetch_api(base_url, "/runtime/health"))
        result["runtime"] = runtime
        degraded = runtime["status"] != "healthy" or runtime["warning_count"] > 0
        checks.append(
            DiagnosticCheck(
                "runtime.bus",
                "runtime",
                "warning" if degraded else "passed",
                "Runtime Bus 诊断已读取",
            )
        )
        if sum(item["count"] for item in runtime["dead_letters"]) > 0:
            checks.append(
                DiagnosticCheck(
                    "runtime.dead_letters",
                    "runtime",
                    "warning",
                    "Runtime DLQ 中存在历史诊断记录",
                    "在 Runtime Health 页面按 PostgreSQL 真源核对后再处置",
                )
            )
        else:
            checks.append(
                DiagnosticCheck("runtime.dead_letters", "runtime", "passed", "Runtime DLQ 为空")
            )
    except SafeFailure:
        checks.append(
            DiagnosticCheck("runtime.bus", "runtime", "warning", "Runtime Bus 诊断暂不可用")
        )

    try:
        workers = project_workers(fetch_api(base_url, "/runtime/workers"))
        result["workers"] = workers
        healthy = workers["total"] > 0 and workers["stale"] == 0
        checks.append(
            DiagnosticCheck(
                "runtime.workers",
                "runtime",
                "passed" if healthy else "warning",
                "Worker 心跳聚合已读取",
                None if healthy else "检查 Agent/RAG Worker 进程和 Redis heartbeat",
            )
        )
    except SafeFailure:
        checks.append(
            DiagnosticCheck("runtime.workers", "runtime", "warning", "Worker 心跳暂不可用")
        )

    try:
        reconciliation = project_reconciliation(
            fetch_api(base_url, "/runtime/storage-reconciliation?limit=20", timeout=20)
        )
        result["storage_reconciliation"] = reconciliation
        healthy = reconciliation["status"] == "healthy" and reconciliation["issue_count"] == 0
        checks.append(
            DiagnosticCheck(
                "storage.reconciliation",
                "storage",
                "passed" if healthy else "warning",
                "PostgreSQL 业务真源对账已完成",
                None if healthy else "在 Runtime Health 页面检查对账问题，不要直接修改 Redis",
            )
        )
    except SafeFailure:
        checks.append(
            DiagnosticCheck("storage.reconciliation", "storage", "warning", "业务真源对账暂不可用")
        )
    return result


def build_manifest(directory: Path) -> dict[str, Any]:
    files = []
    for path in sorted(directory.iterdir(), key=lambda item: item.name):
        if path.name == "manifest.json" or not path.is_file():
            continue
        files.append({"file": path.name, "bytes": path.stat().st_size, "sha256": sha256(path)})
    return {
        "schema_version": SCHEMA_VERSION,
        "bundle_id": directory.name,
        "files": files,
        "excluded": [
            "raw_logs",
            "audit_logs",
            "database_backups",
            "artifacts",
            "rag_documents",
            "environment_files",
            "task_and_model_content",
        ],
    }


def create_archive(directory: Path) -> Path:
    archive = directory.parent / f"{directory.name}.tar.gz"
    with tarfile.open(archive, "w:gz") as handle:
        for name in sorted(ALLOWED_BUNDLE_FILES):
            path = directory / name
            if not path.is_file():
                raise SafeFailure("支持包缺少预期文件")
            handle.add(path, arcname=name, recursive=False)
    os.chmod(archive, 0o600)
    with tarfile.open(archive, "r:gz") as handle:
        members = {member.name for member in handle.getmembers() if member.isfile()}
    if members != ALLOWED_BUNDLE_FILES:
        raise SafeFailure("支持包文件白名单校验失败")
    return archive


def execute(args: argparse.Namespace) -> int:
    repo = Path(args.repo).resolve()
    base_url = validate_gateway_url(args.gateway_url)
    output_root = Path(
        args.output or os.getenv("JARVIS_SUPPORT_OUTPUT_DIR", repo / ".local/support-bundles")
    ).resolve()
    directory = output_root / stamp()
    directory.mkdir(parents=True, exist_ok=False)
    os.chmod(directory, 0o700)
    started = utc_now()
    checks: list[DiagnosticCheck] = []

    environment = environment_summary(repo)
    health = build_health(base_url, checks)
    capacity = capacity_summary(repo)
    free_percent = capacity["filesystem"]["free_percent"]
    checks.append(
        DiagnosticCheck(
            "capacity.filesystem",
            "capacity",
            "passed" if free_percent >= 10 else "warning",
            "本地文件系统容量已检查",
            None if free_percent >= 10 else "清理过期本地产物并保留可恢复备份",
        )
    )
    logs = log_summary(repo)
    checks.append(
        DiagnosticCheck(
            "logs.summary",
            "logs",
            "passed" if logs["services"] else "warning",
            "应用日志只生成聚合摘要",
            None if logs["services"] else "确认 JARVIS_LOG_DIR 和日志写入权限",
        )
    )
    operations = operations_summary(repo)
    checks.append(
        DiagnosticCheck("operations.evidence", "operations", "passed", "最近操作证据已汇总")
    )

    write_json(directory / "environment.json", environment)
    write_json(directory / "health.json", health)
    write_json(directory / "log-summary.json", {"capacity": capacity, "logs": logs})
    write_json(directory / "operations-summary.json", operations)
    status = "degraded" if any(item.status == "warning" for item in checks) else "healthy"
    report = {
        "schema_version": SCHEMA_VERSION,
        "diagnostic_id": "jarvis-runtime-support",
        "mode": args.mode,
        "status": status,
        "started_at": iso(started),
        "finished_at": iso(utc_now()),
        "checks": [asdict(item) for item in checks],
        "raw_logs_included": False,
        "business_data_included": False,
        "archive": args.mode == "bundle",
    }
    write_json(directory / "report.json", report)
    write_json(directory / "manifest.json", build_manifest(directory))
    archive = create_archive(directory) if args.mode == "bundle" else None
    print(f"[runtime-support] status={status}")
    print(f"[runtime-support] report={directory / 'report.json'}")
    if archive:
        print(f"[runtime-support] bundle={archive}")
    return 0


def self_test() -> int:
    with tempfile.TemporaryDirectory() as temp:
        repo = Path(temp)
        log_dir = repo / ".local/logs"
        log_dir.mkdir(parents=True)
        secret = "sk-test-secret-value"
        (log_dir / "gateway.log").write_text(
            "2026-08-03 10:00:00.000 | ERROR | gateway/gateway-01 | - | app/run:42 | "
            f"trace=abc request=- task=- run=- step=- | api_key={secret} user text\n",
            encoding="utf-8",
        )
        summary = log_summary(repo)
        rendered = json.dumps(summary)
        assert secret not in rendered
        assert "user text" not in rendered
        assert summary["services"][0]["levels"]["ERROR"] == 1
        bundle = repo / "bundle"
        bundle.mkdir()
        for name in ALLOWED_BUNDLE_FILES:
            write_json(bundle / name, {"safe": True})
        archive = create_archive(bundle)
        with tarfile.open(archive, "r:gz") as handle:
            assert {item.name for item in handle.getmembers()} == ALLOWED_BUNDLE_FILES
        for unsafe in (
            "https://example.com/api",
            "http://user:pass@127.0.0.1:8080/api",
        ):
            try:
                validate_gateway_url(unsafe)
            except SafeFailure:
                pass
            else:
                raise AssertionError("unsafe Gateway URL was accepted")
    print("[runtime-support] self-test passed")
    return 0


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("mode", choices=("check", "bundle"), nargs="?")
    result.add_argument("--repo", default=Path(__file__).resolve().parent.parent)
    result.add_argument(
        "--gateway-url", default=os.getenv("JARVIS_GATEWAY_URL", "http://127.0.0.1:8080/api")
    )
    result.add_argument("--output")
    result.add_argument("--self-test", action="store_true")
    return result


def main() -> int:
    args = parser().parse_args()
    if args.self_test:
        return self_test()
    if args.mode is None:
        parser().error("mode is required unless --self-test is used")
    try:
        return execute(args)
    except SafeFailure as exc:
        print(f"[runtime-support] FAILED: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
