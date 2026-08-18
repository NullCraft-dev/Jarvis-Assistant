#!/usr/bin/env python3
"""P2 Phase 8 process restart and SSE recovery drill.

This script owns a minimal local runtime so each process can be restarted in
isolation. It never flushes Redis, drops PostgreSQL data, or executes a DLQ
payload. Evidence contains only identifiers, event types, counts, and timing.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

REPO_DIR = Path(__file__).resolve().parent.parent
AGENT_DIR = REPO_DIR / "apps" / "agent-worker"
GATEWAY_DIR = REPO_DIR / "apps" / "gateway"
RUNTIME_DIR = REPO_DIR / ".cache" / "p2-hardening-runtime"
GATEWAY_BIN = RUNTIME_DIR / "bin" / "jarvis-gateway"
DEFAULT_OUTPUT = REPO_DIR / ".local" / "p2-hardening"
HOST = "127.0.0.1"
GATEWAY_PORT = 8080
CONTROL_PLANE_PORT = 8100
GATEWAY_API = f"http://{HOST}:{GATEWAY_PORT}/api"
CONTROL_PLANE_URL = f"http://{HOST}:{CONTROL_PLANE_PORT}"
MAX_SSE_EVENTS = 256


class DrillError(RuntimeError):
    pass


@dataclass
class ManagedProcess:
    name: str
    command: list[str]
    cwd: Path
    env: dict[str, str]
    log_path: Path
    process: subprocess.Popen[bytes] | None = None

    def start(self) -> None:
        if self.process is not None and self.process.poll() is None:
            raise DrillError(f"{self.name} is already running")
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        log_file = self.log_path.open("ab")
        try:
            self.process = subprocess.Popen(
                self.command,
                cwd=self.cwd,
                env=self.env,
                stdout=log_file,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
        finally:
            log_file.close()

    def assert_running(self) -> None:
        if self.process is None:
            raise DrillError(f"{self.name} was not started")
        code = self.process.poll()
        if code is not None:
            raise DrillError(
                f"{self.name} exited unexpectedly with code {code}; log={self.log_path}"
            )

    def stop(self, timeout: float = 15.0) -> None:
        process = self.process
        if process is None or process.poll() is not None:
            self.process = None
            return
        os.killpg(process.pid, signal.SIGTERM)
        try:
            process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGKILL)
            process.wait(timeout=5)
        finally:
            self.process = None

    def restart(self) -> None:
        self.stop()
        self.start()

    def kill(self) -> None:
        """立即终止进程组，用于验证 lease 驱动的崩溃恢复。"""
        process = self.process
        if process is None or process.poll() is not None:
            self.process = None
            return
        os.killpg(process.pid, signal.SIGKILL)
        process.wait(timeout=5)
        self.process = None


def utc_stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def source_state() -> dict[str, Any]:
    """为实际执行的 apps/packages/scripts 状态生成可复核摘要。"""
    revision = subprocess.check_output(
        ["git", "rev-parse", "HEAD"], cwd=REPO_DIR, text=True
    ).strip()
    cached = subprocess.check_output(
        ["git", "diff", "--cached", "--binary", "HEAD", "--", "apps", "packages", "scripts"],
        cwd=REPO_DIR,
    )
    unstaged = subprocess.check_output(
        ["git", "diff", "--binary", "HEAD", "--", "apps", "packages", "scripts"],
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
    digest.update(f"revision:{revision}\n".encode("utf-8"))
    digest.update(b"cached\0")
    digest.update(cached)
    digest.update(b"unstaged\0")
    digest.update(unstaged)
    for relative_path in untracked:
        digest.update(b"untracked\0")
        digest.update(relative_path.encode("utf-8"))
        digest.update(b"\0")
        digest.update((REPO_DIR / relative_path).read_bytes())
    return {
        "revision": revision,
        "worktree_dirty": bool(cached or unstaged or untracked),
        "source_state_sha256": digest.hexdigest(),
        "untracked_source_file_count": len(untracked),
    }


def run_checked(
    command: list[str], *, cwd: Path, env: dict[str, str], log_path: Path
) -> None:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("ab") as log_file:
        result = subprocess.run(
            command,
            cwd=cwd,
            env=env,
            stdout=log_file,
            stderr=subprocess.STDOUT,
            check=False,
        )
    if result.returncode != 0:
        raise DrillError(
            f"command failed ({result.returncode}): {command[0]}; log={log_path}"
        )


def require_commands(names: tuple[str, ...]) -> None:
    missing = [name for name in names if shutil.which(name) is None]
    if missing:
        raise DrillError("missing commands: " + ", ".join(missing))


def require_free_port(port: int) -> None:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.2)
        if sock.connect_ex((HOST, port)) == 0:
            raise DrillError(
                f"port {port} is already in use; stop the existing runtime first"
            )


def request_json(
    url: str,
    *,
    method: str = "GET",
    payload: dict[str, Any] | None = None,
    timeout: float = 5.0,
) -> dict[str, Any]:
    data = None if payload is None else json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(url, data=data, method=method)
    request.add_header("Accept", "application/json")
    if data is not None:
        request.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(request, timeout=timeout) as response:
        parsed = json.loads(response.read().decode("utf-8"))
    if not isinstance(parsed, dict) or parsed.get("ok") is not True:
        raise DrillError(f"endpoint returned non-success ApiResult: {url}")
    return parsed


def wait_json(
    url: str, *, contains: str | None = None, timeout: float = 60.0
) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    last_error = "not attempted"
    while time.monotonic() < deadline:
        try:
            result = request_json(url, timeout=2.0)
            encoded = json.dumps(result, ensure_ascii=False)
            if contains is None or contains in encoded:
                return result
            last_error = f"response did not contain {contains!r}"
        except (OSError, ValueError, urllib.error.URLError, DrillError) as exc:
            last_error = str(exc)
        time.sleep(0.5)
    raise DrillError(f"timed out waiting for {url}: {last_error}")


def wait_redis(timeout: float = 60.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        result = subprocess.run(
            ["docker", "compose", "exec", "-T", "redis", "redis-cli", "ping"],
            cwd=REPO_DIR,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            check=False,
        )
        if result.returncode == 0 and result.stdout.strip() == "PONG":
            return
        time.sleep(0.5)
    raise DrillError("Redis did not become ready")


def wait_postgres(timeout: float = 60.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        result = subprocess.run(
            [
                "docker",
                "compose",
                "exec",
                "-T",
                "postgres",
                "pg_isready",
                "-U",
                "jarvis",
                "-d",
                "jarvis",
            ],
            cwd=REPO_DIR,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
        if result.returncode == 0:
            return
        time.sleep(0.5)
    raise DrillError("PostgreSQL did not become ready")


def postgres_reconciliation_snapshot() -> dict[str, Any]:
    sql = """
WITH recent_runs AS (
    SELECT id, status
    FROM agent_runs
    ORDER BY created_at DESC
    LIMIT 20
),
outbox_counts AS (
    SELECT COALESCE(jsonb_object_agg(status, count_value), '{}'::jsonb) AS value
    FROM (
        SELECT status, count(*) AS count_value
        FROM outbox_events
        GROUP BY status
    ) grouped
),
inbox_counts AS (
    SELECT COALESCE(jsonb_object_agg(status, count_value), '{}'::jsonb) AS value
    FROM (
        SELECT status, count(*) AS count_value
        FROM inbox_events
        GROUP BY status
    ) grouped
),
terminal_event_counts AS (
    SELECT
        run.id,
        run.status,
        count(event.id) FILTER (
            WHERE event.type = CASE run.status
                WHEN 'completed' THEN 'agent.run.completed'
                WHEN 'failed' THEN 'agent.run.failed'
                WHEN 'cancelled' THEN 'agent.run.cancelled'
            END
        ) AS expected_count,
        count(event.id) FILTER (
            WHERE event.type IN (
                'agent.run.completed', 'agent.run.failed', 'agent.run.cancelled'
            )
        ) AS all_terminal_count
    FROM recent_runs run
    LEFT JOIN runtime_events event ON event.run_id = run.id
    WHERE run.status IN ('completed', 'failed', 'cancelled')
    GROUP BY run.id, run.status
)
SELECT jsonb_build_object(
    'recent_run_count', (SELECT count(*) FROM recent_runs),
    'outbox_status_counts', (SELECT value FROM outbox_counts),
    'inbox_status_counts', (SELECT value FROM inbox_counts),
    'active_outbox_count', (
        SELECT count(*) FROM outbox_events
        WHERE status IN ('pending', 'dispatching', 'failed')
    ),
    'processing_inbox_count', (
        SELECT count(*) FROM inbox_events WHERE status = 'processing'
    ),
    'terminal_event_violation_count', (
        SELECT count(*) FROM terminal_event_counts
        WHERE expected_count <> 1 OR all_terminal_count <> 1
    ),
    'nonterminal_step_on_terminal_run_count', (
        SELECT count(*)
        FROM execution_steps step
        JOIN recent_runs run ON run.id = step.run_id
        WHERE run.status IN ('completed', 'failed', 'cancelled')
          AND step.status IN ('pending', 'running', 'waiting_for_permission')
    ),
    'nonterminal_tool_on_terminal_run_count', (
        SELECT count(*)
        FROM tool_calls tool
        JOIN recent_runs run ON run.id = tool.run_id
        WHERE run.status IN ('completed', 'failed', 'cancelled')
          AND tool.status IN ('pending', 'running')
    ),
    'pending_permission_on_terminal_run_count', (
        SELECT count(*)
        FROM permission_requests permission
        JOIN recent_runs run ON run.id = permission.run_id
        WHERE run.status IN ('completed', 'failed', 'cancelled')
          AND permission.status = 'pending'
    )
)::text;
"""
    result = subprocess.run(
        [
            "docker",
            "compose",
            "exec",
            "-T",
            "postgres",
            "psql",
            "-U",
            "jarvis",
            "-d",
            "jarvis",
            "-At",
            "-v",
            "ON_ERROR_STOP=1",
            "-c",
            sql,
        ],
        cwd=REPO_DIR,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise DrillError("failed to collect PostgreSQL reconciliation snapshot")
    try:
        parsed = json.loads(result.stdout.strip())
    except (json.JSONDecodeError, ValueError) as exc:
        raise DrillError("PostgreSQL reconciliation snapshot is not JSON") from exc
    if not isinstance(parsed, dict):
        raise DrillError("PostgreSQL reconciliation snapshot is not an object")
    return parsed


def redis_xadd(stream: str, fields: dict[str, str]) -> str:
    command = [
        "docker",
        "compose",
        "exec",
        "-T",
        "redis",
        "redis-cli",
        "--raw",
        "XADD",
        stream,
        "*",
    ]
    for key, value in fields.items():
        command.extend([key, value])
    result = subprocess.run(
        command,
        cwd=REPO_DIR,
        capture_output=True,
        text=True,
        check=False,
    )
    message_id = result.stdout.strip()
    if result.returncode != 0 or not message_id or "-" not in message_id:
        raise DrillError(f"failed to inject poison message into {stream}")
    return message_id


def _read_redis_response(reader) -> Any:
    prefix = reader.read(1)
    if not prefix:
        raise DrillError("Redis closed the diagnostic connection")
    line = reader.readline()
    if not line.endswith(b"\r\n"):
        raise DrillError("Redis returned a truncated diagnostic response")
    value = line[:-2]
    if prefix == b"+":
        return value.decode("utf-8")
    if prefix == b"-":
        raise DrillError(
            "Redis diagnostic command failed: "
            + value.decode("utf-8", errors="replace")[:200]
        )
    if prefix == b":":
        return int(value)
    if prefix == b"$":
        size = int(value)
        if size == -1:
            return None
        data = reader.read(size)
        if len(data) != size or reader.read(2) != b"\r\n":
            raise DrillError("Redis returned a truncated bulk response")
        return data.decode("utf-8")
    if prefix == b"*":
        count = int(value)
        if count == -1:
            return None
        return [_read_redis_response(reader) for _ in range(count)]
    raise DrillError(f"Redis returned unsupported response prefix: {prefix!r}")


def redis_command(*parts: str) -> Any:
    encoded = [part.encode("utf-8") for part in parts]
    request = [f"*{len(encoded)}\r\n".encode("ascii")]
    for part in encoded:
        request.extend(
            [
                f"${len(part)}\r\n".encode("ascii"),
                part,
                b"\r\n",
            ]
        )
    with socket.create_connection((HOST, 6379), timeout=3.0) as client:
        client.sendall(b"".join(request))
        with client.makefile("rb") as reader:
            return _read_redis_response(reader)


def _stream_fields(raw_fields: Any) -> dict[str, str]:
    if not isinstance(raw_fields, list) or len(raw_fields) % 2:
        raise DrillError("Redis stream entry fields are malformed")
    return {
        str(raw_fields[index]): str(raw_fields[index + 1])
        for index in range(0, len(raw_fields), 2)
    }


def wait_run_queue_message(run_id: str, timeout: float = 20.0) -> str:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        entries = redis_command(
            "XRANGE", "jarvis:stream:run-queue", "-", "+", "COUNT", "200"
        )
        if not isinstance(entries, list):
            raise DrillError("Redis XRANGE response is not a list")
        matches = []
        for entry in entries:
            if not isinstance(entry, list) or len(entry) != 2:
                raise DrillError("Redis XRANGE entry is malformed")
            fields = _stream_fields(entry[1])
            if fields.get("run_id") == run_id:
                matches.append(str(entry[0]))
        if len(matches) == 1:
            return matches[0]
        if len(matches) > 1:
            raise DrillError(f"queued run has duplicate source messages: {run_id}")
        time.sleep(0.25)
    raise DrillError(f"run job was not published to Redis: {run_id}")


def consume_and_ack_isolated_run_job(expected_message_id: str) -> None:
    result = redis_command(
        "XREADGROUP",
        "GROUP",
        "jarvis:group:worker-pool",
        "p2-drill-setup",
        "COUNT",
        "1",
        "STREAMS",
        "jarvis:stream:run-queue",
        ">",
    )
    consumed_message_id = ""
    if result:
        try:
            consumed_message_id = str(result[0][1][0][0])
        except (IndexError, TypeError) as exc:
            raise DrillError(
                "isolated source delivery returned a malformed response"
            ) from exc
        if consumed_message_id != expected_message_id:
            raise DrillError(
                "isolated source delivery order changed: "
                f"got {consumed_message_id}, expected {expected_message_id}"
            )
    else:
        pending = redis_command(
            "XPENDING",
            "jarvis:stream:run-queue",
            "jarvis:group:worker-pool",
            expected_message_id,
            expected_message_id,
            "1",
        )
        if (
            not isinstance(pending, list)
            or len(pending) != 1
            or str(pending[0][0]) != expected_message_id
        ):
            raise DrillError("isolated source message is neither unread nor pending")
    acked = redis_command(
        "XACK",
        "jarvis:stream:run-queue",
        "jarvis:group:worker-pool",
        expected_message_id,
    )
    if acked != 1:
        raise DrillError("failed to acknowledge the isolated task's source message")


def wait_pending_delivery(
    message_id: str, *, minimum_deliveries: int, timeout: float = 20.0
) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        entries = redis_command(
            "XPENDING",
            "jarvis:stream:run-queue",
            "jarvis:group:worker-pool",
            "-",
            "+",
            "20",
        )
        if not isinstance(entries, list):
            raise DrillError("Redis XPENDING response is not a list")
        for entry in entries:
            if not isinstance(entry, list) or len(entry) != 4:
                raise DrillError("Redis XPENDING entry is malformed")
            deliveries = int(entry[3])
            if str(entry[0]) == message_id and deliveries >= minimum_deliveries:
                return {
                    "message_id": str(entry[0]),
                    "consumer": str(entry[1]),
                    "idle_ms": int(entry[2]),
                    "delivery_count": deliveries,
                }
        time.sleep(0.25)
    raise DrillError(
        f"pending delivery did not reach attempt {minimum_deliveries}: {message_id}"
    )


def force_pending_idle(message_id: str, consumer: str) -> None:
    result = redis_command(
        "XCLAIM",
        "jarvis:stream:run-queue",
        "jarvis:group:worker-pool",
        consumer,
        "0",
        message_id,
        "IDLE",
        "300000",
        "JUSTID",
    )
    if result != [message_id]:
        raise DrillError(f"failed to accelerate pending retry: {message_id}")


def wait_dead_letter(
    *,
    source: str,
    original_message_id: str,
    payload: str,
    expected_error_code: str,
    secret_marker: str,
    expected_delivery_count: int = 1,
    timeout: float = 30.0,
) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        response = request_json(
            f"{GATEWAY_API}/runtime/dead-letters?source={source}&limit=10"
        )
        if secret_marker in json.dumps(response, ensure_ascii=False):
            raise DrillError(f"{source} DLQ diagnostic leaked poison payload")
        records = (response.get("data") or {}).get("records") or []
        matches = [
            item
            for item in records
            if item.get("original_message_id") == original_message_id
        ]
        if len(matches) == 1:
            record = matches[0]
            expected_hash = hashlib.sha256(payload.encode("utf-8")).hexdigest()
            if (
                record.get("error_code") != expected_error_code
                or record.get("payload_sha256") != expected_hash
                or record.get("payload_size_bytes") != len(payload.encode("utf-8"))
                or record.get("delivery_count") != expected_delivery_count
            ):
                raise DrillError(f"{source} DLQ safe projection is inconsistent")
            if {"payload", "arguments", "content", "checkpoint"}.intersection(record):
                raise DrillError(f"{source} DLQ projection contains unsafe fields")
            return record
        if len(matches) > 1:
            raise DrillError(f"{source} poison message created duplicate DLQ records")
        time.sleep(0.5)
    raise DrillError(f"{source} poison message did not reach DLQ")


def exercise_run_queue_retry_exhaustion(
    *, stamp: str, processes: dict[str, ManagedProcess]
) -> dict[str, Any]:
    worker_before = wait_worker("worker-01")
    processes["worker"].stop()

    user_goal = "P2 retry exhaustion isolation task. Do not execute tools."
    task_id, run_id = create_task(user_goal)
    original_message_id = wait_run_queue_message(run_id)
    consume_and_ack_isolated_run_job(original_message_id)

    job_id = f"p2-retry-{stamp}-" + ("x" * 220)
    payload = json.dumps(
        {
            "job_id": job_id,
            "trace_id": str(uuid4()),
            "task_id": task_id,
            "run_id": run_id,
            "user_goal": user_goal,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "schema_version": "2B-1a.1",
        },
        separators=(",", ":"),
    )
    crafted_message_id = redis_xadd(
        "jarvis:stream:run-queue",
        {
            "schema_version": "2B-1a.1",
            "type": "run.job",
            "payload": payload,
            "job_id": job_id,
            "trace_id": str(uuid4()),
            "task_id": task_id,
            "run_id": run_id,
            "created_at": datetime.now(timezone.utc).isoformat(),
        },
    )

    processes["worker"].start()
    wait_worker(
        "worker-01",
        previous_reported_at=worker_before.get("reported_at"),
    )
    first = wait_pending_delivery(
        crafted_message_id, minimum_deliveries=1
    )
    force_pending_idle(crafted_message_id, first["consumer"])
    second = wait_pending_delivery(
        crafted_message_id, minimum_deliveries=2
    )
    force_pending_idle(crafted_message_id, second["consumer"])

    record = wait_dead_letter(
        source="run_queue",
        original_message_id=crafted_message_id,
        payload=payload,
        expected_error_code="RUN_QUEUE_RETRY_EXHAUSTED",
        secret_marker=job_id,
        expected_delivery_count=3,
        timeout=30.0,
    )
    events = read_sse(run_id, timeout=30.0)
    validate_event_history(events, require_terminal=True)
    if events[-1].get("type") != "agent.run.failed":
        raise DrillError("retry-exhausted Run did not end in agent.run.failed")

    return {
        "task_id": task_id,
        "run_id": run_id,
        "original_message_id": original_message_id,
        "original_message_disposition": "controlled_ack",
        "crafted_message_id": crafted_message_id,
        "delivery_count": record.get("delivery_count"),
        "error_code": record.get("error_code"),
        "dlq_record_id": record.get("id"),
        "payload_size_bytes": record.get("payload_size_bytes"),
        "payload_sha256": record.get("payload_sha256"),
        "terminal_type": events[-1].get("type"),
        "terminal_event_count": sum(
            event.get("type")
            in {
                "agent.run.completed",
                "agent.run.failed",
                "agent.run.cancelled",
            }
            for event in events
        ),
        "production_reclaim_idle_unchanged": True,
    }


def inject_poison_messages(stamp: str) -> list[dict[str, Any]]:
    secret_marker = f"Bearer-p2-secret-{stamp}"
    payload = json.dumps({"token": secret_marker}, separators=(",", ":"))
    targets = (
        (
            "run_queue",
            "jarvis:stream:run-queue",
            {"schema_version": "2B-1a.1", "type": "run.job", "payload": payload},
            "RUN_QUEUE_MALFORMED",
        ),
        (
            "worker_command",
            "jarvis:stream:worker-command",
            {
                "schema_version": "2B-1a.1",
                "type": "run.cancel",
                "payload": payload,
            },
            "WORKER_COMMAND_MALFORMED",
        ),
        (
            "runtime_event",
            "jarvis:stream:runtime-event",
            {
                "schema_version": "2B-1a.1",
                "type": "runtime.event",
                "payload": payload,
            },
            "RUNTIME_EVENT_MALFORMED",
        ),
    )
    injected = [
        (source, redis_xadd(stream, fields), error_code)
        for source, stream, fields, error_code in targets
    ]
    records: list[dict[str, Any]] = []
    for source, original_message_id, error_code in injected:
        record = wait_dead_letter(
            source=source,
            original_message_id=original_message_id,
            payload=payload,
            expected_error_code=error_code,
            secret_marker=secret_marker,
        )
        records.append(
            {
                "source": source,
                "record_id": record.get("id"),
                "original_message_id": original_message_id,
                "error_code": error_code,
                "delivery_count": record.get("delivery_count"),
                "payload_size_bytes": record.get("payload_size_bytes"),
                "payload_sha256": record.get("payload_sha256"),
            }
        )
    return records


def wait_reconciliation(timeout: float = 45.0) -> tuple[dict[str, Any], dict[str, Any]]:
    deadline = time.monotonic() + timeout
    last_reason = "not attempted"
    while time.monotonic() < deadline:
        runtime = request_json(f"{GATEWAY_API}/runtime/health")
        storage = request_json(
            f"{GATEWAY_API}/runtime/storage-reconciliation?limit=20",
            timeout=12.0,
        )
        runtime_data = runtime.get("data") or {}
        storage_data = storage.get("data") or {}
        streams = runtime_data.get("streams") or []
        stream_clean = len(streams) == 3 and all(
            item.get("available") is True
            and item.get("pending") == 0
            and item.get("lag") == 0
            for item in streams
        )
        if (
            runtime_data.get("status") == "healthy"
            and stream_clean
            and storage_data.get("status") == "healthy"
            and storage_data.get("issue_count") == 0
        ):
            return runtime_data, storage_data
        last_reason = (
            f"runtime={runtime_data.get('status')} "
            f"stream_clean={stream_clean} "
            f"storage={storage_data.get('status')} "
            f"issues={storage_data.get('issue_count')}"
        )
        time.sleep(1.0)
    raise DrillError(f"runtime/storage reconciliation did not settle: {last_reason}")


def wait_worker(
    worker_id: str,
    timeout: float = 30.0,
    *,
    previous_reported_at: str | None = None,
) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        response = wait_json(f"{GATEWAY_API}/runtime/workers", timeout=5.0)
        workers = (response.get("data") or {}).get("workers") or []
        for worker in workers:
            if (
                worker.get("worker_id") == worker_id
                and worker.get("is_stale") is False
                and (
                    previous_reported_at is None
                    or worker.get("reported_at") != previous_reported_at
                )
            ):
                return worker
        time.sleep(0.5)
    raise DrillError(f"worker heartbeat did not recover: {worker_id}")


def wait_pending_permission(run_id: str, timeout: float = 90.0) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        response = request_json(f"{GATEWAY_API}/runs/{run_id}/permissions")
        requests = (response.get("data") or {}).get("requests") or []
        pending = [item for item in requests if item.get("status") == "pending"]
        if len(pending) == 1:
            return pending[0]
        if len(pending) > 1:
            raise DrillError(f"expected one pending permission, got {len(pending)}")
        time.sleep(0.5)
    raise DrillError(f"run did not reach permission wait: {run_id}")


def create_task(
    goal: str, *, workspace_id: str | None = None
) -> tuple[str, str]:
    payload: dict[str, Any] = {"user_goal": goal}
    if workspace_id is not None:
        payload["workspace_id"] = workspace_id
    response = request_json(
        f"{GATEWAY_API}/tasks",
        method="POST",
        payload=payload,
        timeout=10.0,
    )
    data = response.get("data") or {}
    task_id = (data.get("task") or {}).get("id")
    run_id = (data.get("run") or {}).get("id")
    if (
        not isinstance(task_id, str)
        or not isinstance(run_id, str)
        or not task_id
        or not run_id
    ):
        raise DrillError("create task response is missing task/run id")
    return task_id, run_id


def configured_workspace_id() -> str:
    response = request_json(f"{GATEWAY_API}/workspaces")
    workspaces = (response.get("data") or {}).get("workspaces") or []
    expected = str(REPO_DIR.resolve())
    matches = [
        workspace
        for workspace in workspaces
        if isinstance(workspace, dict)
        and workspace.get("canonical_path") == expected
        and workspace.get("status") == "active"
    ]
    if len(matches) != 1:
        raise DrillError(
            "expected exactly one active configured workspace for the repository"
        )
    try:
        return str(UUID(str(matches[0].get("id"))))
    except (TypeError, ValueError):
        raise DrillError("configured workspace has an invalid id") from None


def get_task_detail(task_id: str) -> dict[str, Any]:
    response = request_json(f"{GATEWAY_API}/tasks/{task_id}")
    data = response.get("data") or {}
    task = data.get("task")
    if not isinstance(task, dict):
        raise DrillError(f"task detail is missing for {task_id}")
    return data


def require_nonterminal_run(task_id: str, run_id: str) -> str:
    data = get_task_detail(task_id)
    active_run = data.get("active_run") or {}
    if not isinstance(active_run, dict) or active_run.get("id") != run_id:
        raise DrillError("task detail does not reference the expected active run")
    status = active_run.get("status")
    if status in {"completed", "failed", "cancelled"}:
        raise DrillError(
            f"run became terminal before the SSE disconnect: {status}"
        )
    if not isinstance(status, str) or not status:
        raise DrillError("active run status is missing")
    return status


def task_workspace_id(task_id: str) -> str:
    task = get_task_detail(task_id).get("task") or {}
    workspace_id = task.get("workspace_id")
    try:
        return str(UUID(str(workspace_id)))
    except (TypeError, ValueError):
        raise DrillError("task does not have a valid workspace_id") from None


def read_sse(
    run_id: str,
    *,
    stop_after: int | None = None,
    stop_on_types: frozenset[str] = frozenset(),
    last_event_id: str | None = None,
    timeout: float = 45.0,
) -> list[dict[str, Any]]:
    request = urllib.request.Request(f"{GATEWAY_API}/runs/{run_id}/events")
    request.add_header("Accept", "text/event-stream")
    if last_event_id is not None:
        request.add_header("Last-Event-ID", last_event_id)
    events: list[dict[str, Any]] = []
    with urllib.request.urlopen(request, timeout=timeout) as response:
        for raw_line in response:
            line = raw_line.decode("utf-8", errors="strict").strip()
            if not line.startswith("data:"):
                continue
            event = json.loads(line[5:].strip())
            if not isinstance(event, dict):
                raise DrillError("SSE event is not an object")
            events.append(event)
            if len(events) > MAX_SSE_EVENTS:
                raise DrillError(
                    f"SSE exceeded bounded event budget ({MAX_SSE_EVENTS})"
                )
            if stop_after is not None and len(events) >= stop_after:
                break
            if event.get("type") in stop_on_types:
                break
            if event.get("type") in {
                "agent.run.completed",
                "agent.run.failed",
                "agent.run.cancelled",
            }:
                break
    return events


def build_pdf_fixture(*, marker: str, page_count: int = 32) -> bytes:
    if not 1 <= page_count <= 256:
        raise ValueError("PDF fixture page_count must be between 1 and 256")
    objects: list[bytes] = []
    font_id = 3 + page_count * 2
    page_ids = [3 + index * 2 for index in range(page_count)]
    objects.append(b"<< /Type /Catalog /Pages 2 0 R >>")
    kids = " ".join(f"{page_id} 0 R" for page_id in page_ids)
    objects.append(
        f"<< /Type /Pages /Kids [{kids}] /Count {page_count} >>".encode("ascii")
    )
    safe_marker = "".join(
        character if character.isalnum() or character in "-_" else "-"
        for character in marker
    )[:80]
    for index, page_id in enumerate(page_ids, start=1):
        content_id = page_id + 1
        objects.append(
            (
                "<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
                f"/Resources << /Font << /F1 {font_id} 0 R >> >> "
                f"/Contents {content_id} 0 R >>"
            ).encode("ascii")
        )
        lines = [
            (
                f"Jarvis P2 RAG restart {safe_marker} page {index} line {line}. "
                "This document validates durable ingestion lease recovery, "
                "bounded retry, atomic chunks, and vector index consistency."
            )
            for line in range(1, 13)
        ]
        operators = ["BT /F1 9 Tf 48 744 Td"]
        for line in lines:
            operators.extend([f"({line}) Tj", "0 -18 Td"])
        operators.append("ET")
        stream = "\n".join(operators).encode("ascii")
        objects.append(
            f"<< /Length {len(stream)} >>\nstream\n".encode("ascii")
            + stream
            + b"\nendstream"
        )
    objects.append(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")

    output = bytearray(b"%PDF-1.7\n%\xe2\xe3\xcf\xd3\n")
    offsets = [0]
    for object_id, body in enumerate(objects, start=1):
        offsets.append(len(output))
        output.extend(f"{object_id} 0 obj\n".encode("ascii"))
        output.extend(body)
        output.extend(b"\nendobj\n")
    xref_offset = len(output)
    output.extend(f"xref\n0 {len(objects) + 1}\n".encode("ascii"))
    output.extend(b"0000000000 65535 f \n")
    for offset in offsets[1:]:
        output.extend(f"{offset:010d} 00000 n \n".encode("ascii"))
    output.extend(
        (
            f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\n"
            f"startxref\n{xref_offset}\n%%EOF\n"
        ).encode("ascii")
    )
    return bytes(output)


def upload_rag_pdf(
    *, workspace_id: str, filename: str, content: bytes
) -> dict[str, Any]:
    boundary = f"jarvis-p2-{uuid4().hex}"
    body = bytearray()

    def append(value: str | bytes) -> None:
        body.extend(value.encode("utf-8") if isinstance(value, str) else value)

    append(f"--{boundary}\r\n")
    append('Content-Disposition: form-data; name="workspace_id"\r\n\r\n')
    append(workspace_id)
    append("\r\n")
    append(f"--{boundary}\r\n")
    append(
        'Content-Disposition: form-data; name="file"; '
        f'filename="{filename}"\r\n'
    )
    append("Content-Type: application/pdf\r\n\r\n")
    append(content)
    append("\r\n")
    append(f"--{boundary}--\r\n")
    request = urllib.request.Request(
        f"{GATEWAY_API}/rag/documents",
        data=bytes(body),
        method="POST",
    )
    request.add_header("Accept", "application/json")
    request.add_header(
        "Content-Type", f"multipart/form-data; boundary={boundary}"
    )
    with urllib.request.urlopen(request, timeout=30.0) as response:
        parsed = json.loads(response.read().decode("utf-8"))
    if not isinstance(parsed, dict) or parsed.get("ok") is not True:
        raise DrillError("RAG upload returned non-success ApiResult")
    data = parsed.get("data")
    if not isinstance(data, dict):
        raise DrillError("RAG upload response is missing data")
    return data


def get_rag_document(
    *, workspace_id: str, document_id: str
) -> dict[str, Any]:
    response = request_json(
        f"{GATEWAY_API}/rag/documents?workspace_id={workspace_id}"
    )
    documents = (response.get("data") or {}).get("documents") or []
    matches = [
        document
        for document in documents
        if isinstance(document, dict) and document.get("id") == document_id
    ]
    if len(matches) != 1:
        raise DrillError(
            f"expected one RAG document {document_id}, got {len(matches)}"
        )
    return matches[0]


def wait_rag_job_status(
    *,
    workspace_id: str,
    document_id: str,
    statuses: frozenset[str],
    timeout: float,
) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    last_status = "missing"
    while time.monotonic() < deadline:
        document = get_rag_document(
            workspace_id=workspace_id,
            document_id=document_id,
        )
        job = document.get("latest_job") or {}
        last_status = str(job.get("status") or "missing")
        if last_status in statuses:
            return document
        if last_status in {"completed", "failed", "cancelled"}:
            error_code = job.get("error_code")
            raise DrillError(
                "RAG job reached an unexpected terminal status while waiting "
                f"for {sorted(statuses)}: {last_status} "
                f"error_code={error_code or 'none'}"
            )
        time.sleep(0.05)
    raise DrillError(
        f"RAG job did not reach {sorted(statuses)}; last={last_status}"
    )


def rag_integrity_snapshot(document_id: str) -> dict[str, Any]:
    normalized_id = str(UUID(document_id))
    sql = f"""
SELECT jsonb_build_object(
    'document_status', (
        SELECT status FROM rag_documents WHERE id = '{normalized_id}'::uuid
    ),
    'job_status', (
        SELECT status FROM rag_ingestion_jobs
        WHERE document_id = '{normalized_id}'::uuid
        ORDER BY created_at DESC LIMIT 1
    ),
    'chunk_count', (
        SELECT count(*) FROM rag_chunks
        WHERE document_id = '{normalized_id}'::uuid
    ),
    'embedding_count', (
        SELECT count(*)
        FROM rag_chunk_embeddings embedding
        JOIN rag_chunks chunk ON chunk.id = embedding.chunk_id
        WHERE chunk.document_id = '{normalized_id}'::uuid
    )
)::text;
"""
    result = subprocess.run(
        [
            "docker",
            "compose",
            "exec",
            "-T",
            "postgres",
            "psql",
            "-U",
            "jarvis",
            "-d",
            "jarvis",
            "-At",
            "-v",
            "ON_ERROR_STOP=1",
            "-c",
            sql,
        ],
        cwd=REPO_DIR,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise DrillError("failed to collect RAG integrity snapshot")
    try:
        snapshot = json.loads(result.stdout.strip())
    except (json.JSONDecodeError, ValueError) as exc:
        raise DrillError("RAG integrity snapshot is not JSON") from exc
    if not isinstance(snapshot, dict):
        raise DrillError("RAG integrity snapshot is not an object")
    return snapshot


def wait_rag_integrity(
    *,
    workspace_id: str,
    document_id: str,
    timeout: float = 10.0,
) -> tuple[dict[str, Any], dict[str, Any]]:
    deadline = time.monotonic() + timeout
    last_document: dict[str, Any] = {}
    last_integrity: dict[str, Any] = {}
    while time.monotonic() < deadline:
        last_document = get_rag_document(
            workspace_id=workspace_id,
            document_id=document_id,
        )
        last_integrity = rag_integrity_snapshot(document_id)
        chunk_count = last_integrity.get("chunk_count")
        if (
            last_document.get("status") == "ready"
            and last_integrity.get("document_status") == "ready"
            and last_integrity.get("job_status") == "completed"
            and isinstance(chunk_count, int)
            and chunk_count >= 1
            and last_integrity.get("embedding_count") == chunk_count
        ):
            return last_document, last_integrity
        time.sleep(0.25)
    raise DrillError(
        "RAG restart recovery left an inconsistent index: "
        f"api_document_status={last_document.get('status')} "
        f"db_document_status={last_integrity.get('document_status')} "
        f"job_status={last_integrity.get('job_status')} "
        f"chunk_count={last_integrity.get('chunk_count')} "
        f"embedding_count={last_integrity.get('embedding_count')}"
    )


def validate_event_history(
    events: list[dict[str, Any]], *, require_terminal: bool
) -> None:
    if not events:
        raise DrillError("SSE returned no events")
    event_ids = [event.get("id") for event in events]
    if any(not isinstance(event_id, str) or not event_id for event_id in event_ids):
        raise DrillError("SSE event is missing id")
    if len(set(event_ids)) != len(event_ids):
        raise DrillError("SSE history contains duplicate event ids")
    sequences = [
        event.get("sequence")
        for event in events
        if isinstance(event.get("sequence"), int)
    ]
    if sequences != sorted(sequences) or len(set(sequences)) != len(sequences):
        raise DrillError("SSE event sequence is not strictly monotonic")
    terminal = [
        event
        for event in events
        if event.get("type")
        in {"agent.run.completed", "agent.run.failed", "agent.run.cancelled"}
    ]
    if require_terminal and len(terminal) != 1:
        raise DrillError(f"expected exactly one terminal event, got {len(terminal)}")


def event_tool_name(event: dict[str, Any]) -> str | None:
    payload = event.get("payload") or {}
    if not isinstance(payload, dict):
        return None
    direct = payload.get("tool_name")
    if isinstance(direct, str):
        return direct
    tool_call = payload.get("tool_call") or {}
    if isinstance(tool_call, dict) and isinstance(tool_call.get("tool_name"), str):
        return tool_call["tool_name"]
    return None


def process_env() -> dict[str, str]:
    env = os.environ.copy()
    env.update(
        {
            "JARVIS_DATABASE_URL": "postgresql+asyncpg://jarvis:jarvis@127.0.0.1:5432/jarvis",
            "JARVIS_REDIS_ADDR": "127.0.0.1:6379",
            "JARVIS_WORKSPACE_ROOT": str(REPO_DIR),
            "JARVIS_ALLOWED_WORKSPACE_PATHS": str(REPO_DIR),
            "JARVIS_ARTIFACT_ROOT": str(REPO_DIR / ".local" / "artifacts"),
            "JARVIS_RAG_ASSET_ROOT": str(REPO_DIR / ".local" / "rag-assets"),
            "JARVIS_LOG_DIR": str(REPO_DIR / ".local" / "logs"),
            "JARVIS_LOG_COLOR": "never",
            "JARVIS_RUNTIME_BUS": "redis",
            "JARVIS_CONTROL_PLANE_URL": CONTROL_PLANE_URL,
            "JARVIS_RAG_STRUCTURE_PROVIDER": "native-only",
            "JARVIS_RAG_RERANKER_ENABLED": "false",
            "JARVIS_RAG_JOB_LEASE_SECONDS": "10",
            "JARVIS_WORKER_ID": "worker-01",
            "JARVIS_RAG_WORKER_ID": "rag-worker-01",
        }
    )
    return env


def build_processes(
    env: dict[str, str], output_dir: Path, conda_env: str
) -> dict[str, ManagedProcess]:
    python_executable = subprocess.check_output(
        [
            "conda",
            "run",
            "--no-capture-output",
            "-n",
            conda_env,
            "python",
            "-c",
            "import sys; print(sys.executable)",
        ],
        cwd=REPO_DIR,
        env=env,
        text=True,
    ).strip()
    if not python_executable or not Path(python_executable).is_file():
        raise DrillError(f"could not resolve Python for conda env: {conda_env}")
    return {
        "control-plane": ManagedProcess(
            "control-plane",
            [
                python_executable,
                "-m",
                "jarvis_worker.control_plane.main",
                "--host",
                HOST,
                "--port",
                str(CONTROL_PLANE_PORT),
            ],
            AGENT_DIR,
            env,
            output_dir / "control-plane.log",
        ),
        "gateway": ManagedProcess(
            "gateway", [str(GATEWAY_BIN)], GATEWAY_DIR, env, output_dir / "gateway.log"
        ),
        "worker": ManagedProcess(
            "worker",
            [python_executable, "-m", "jarvis_worker.main"],
            AGENT_DIR,
            env,
            output_dir / "worker.log",
        ),
        "rag-worker": ManagedProcess(
            "rag-worker",
            [python_executable, "-m", "jarvis_worker.agent.rag.worker"],
            AGENT_DIR,
            env,
            output_dir / "rag-worker.log",
        ),
    }


def drill(args: argparse.Namespace) -> Path:
    require_commands(("docker", "conda", "go", "git"))
    require_free_port(GATEWAY_PORT)
    require_free_port(CONTROL_PLANE_PORT)
    stamp = utc_stamp()
    output_dir = Path(args.output_dir).resolve() / stamp
    output_dir.mkdir(parents=True, exist_ok=False)
    env = process_env()
    processes = build_processes(env, output_dir, args.conda_env)
    evidence: dict[str, Any] = {
        **source_state(),
        "started_at": stamp,
        "status": "running",
        "checks": [],
    }

    def record(name: str, **details: Any) -> None:
        evidence["checks"].append({"name": name, "status": "passed", **details})

    try:
        run_checked(
            ["docker", "compose", "up", "-d", "postgres", "redis"],
            cwd=REPO_DIR,
            env=env,
            log_path=output_dir / "infra.log",
        )
        wait_postgres()
        wait_redis()
        RUNTIME_DIR.joinpath("bin").mkdir(parents=True, exist_ok=True)
        run_checked(
            [
                "conda",
                "run",
                "--no-capture-output",
                "-n",
                args.conda_env,
                "python",
                "-m",
                "alembic",
                "upgrade",
                "head",
            ],
            cwd=AGENT_DIR,
            env=env,
            log_path=output_dir / "migration.log",
        )
        run_checked(
            ["go", "build", "-o", str(GATEWAY_BIN), "./cmd/gateway"],
            cwd=GATEWAY_DIR,
            env={**env, "GOCACHE": str(REPO_DIR / ".cache" / "go-build")},
            log_path=output_dir / "gateway-build.log",
        )

        processes["control-plane"].start()
        wait_json(f"{CONTROL_PLANE_URL}/internal/health")
        processes["gateway"].start()
        wait_json(f"{GATEWAY_API}/health", contains="healthy")
        processes["worker"].start()
        processes["rag-worker"].start()
        wait_worker("worker-01")
        wait_worker("rag-worker-01")
        record("baseline_runtime", workers=["worker-01", "rag-worker-01"])

        workspace_id = configured_workspace_id()
        task_id, run_id = create_task(
            "P2 SSE reconnect drill. Reply briefly without using tools.",
            workspace_id=workspace_id,
        )
        initial = read_sse(
            run_id,
            stop_on_types=frozenset({"model.call.started"}),
            timeout=20.0,
        )
        validate_event_history(initial, require_terminal=False)
        if initial[-1].get("type") != "model.call.started":
            raise DrillError(
                "SSE disconnect drill did not reach model.call.started"
            )
        disconnected_status = require_nonterminal_run(task_id, run_id)
        last_event_id = str(initial[-1]["id"])
        time.sleep(1.0)
        processes["gateway"].restart()
        wait_json(f"{GATEWAY_API}/health", contains="healthy")
        resumed = read_sse(
            run_id,
            last_event_id=last_event_id,
            timeout=float(args.sse_timeout),
        )
        validate_event_history(resumed, require_terminal=True)
        if {event.get("id") for event in initial}.intersection(
            event.get("id") for event in resumed
        ):
            raise DrillError("Last-Event-ID reconnect replayed an acknowledged event")
        recovered = read_sse(run_id, timeout=float(args.sse_timeout))
        validate_event_history(recovered, require_terminal=True)
        if not {event.get("id") for event in initial}.issubset(
            event.get("id") for event in recovered
        ):
            raise DrillError(
                "Gateway restart did not restore the persisted SSE history"
            )
        record(
            "gateway_restart_sse_recovery",
            task_id=task_id,
            run_id=run_id,
            disconnected_at_event_id=last_event_id,
            disconnected_at_type=initial[-1].get("type"),
            disconnected_run_status=disconnected_status,
            resumed_event_count=len(resumed),
            recovered_event_count=len(recovered),
            terminal_type=recovered[-1].get("type"),
        )

        worker_before_restart = wait_worker("worker-01")
        processes["worker"].restart()
        wait_worker(
            "worker-01",
            previous_reported_at=worker_before_restart.get("reported_at"),
        )
        worker_task, worker_run = create_task(
            "P2 Agent Worker restart smoke. Reply briefly without using tools."
        )
        worker_events = read_sse(worker_run, timeout=float(args.sse_timeout))
        validate_event_history(worker_events, require_terminal=True)
        record(
            "agent_worker_restart",
            task_id=worker_task,
            run_id=worker_run,
            event_count=len(worker_events),
        )

        rag_before_restart = wait_worker("rag-worker-01")
        processes["rag-worker"].stop()
        if task_workspace_id(task_id) != workspace_id:
            raise DrillError("task lost its configured workspace binding")
        upload = upload_rag_pdf(
            workspace_id=workspace_id,
            filename=f"p2-rag-restart-{stamp}.pdf",
            content=build_pdf_fixture(marker=stamp),
        )
        document_id = str(upload.get("document_id") or "")
        job_id = str(upload.get("job_id") or "")
        if not document_id or not job_id or upload.get("status") != "queued":
            raise DrillError("RAG restart fixture was not enqueued")
        queued = wait_rag_job_status(
            workspace_id=workspace_id,
            document_id=document_id,
            statuses=frozenset({"queued"}),
            timeout=5.0,
        )
        processes["rag-worker"].start()
        wait_worker(
            "rag-worker-01",
            previous_reported_at=rag_before_restart.get("reported_at"),
        )
        active = wait_rag_job_status(
            workspace_id=workspace_id,
            document_id=document_id,
            statuses=frozenset({"parsing", "chunking", "embedding"}),
            timeout=20.0,
        )
        active_job = active.get("latest_job") or {}
        active_status = str(active_job.get("status"))
        rag_active_worker = wait_worker("rag-worker-01")
        processes["rag-worker"].kill()
        time.sleep(1.0)
        processes["rag-worker"].start()
        wait_worker(
            "rag-worker-01",
            previous_reported_at=rag_active_worker.get("reported_at"),
        )
        recovered_rag = wait_rag_job_status(
            workspace_id=workspace_id,
            document_id=document_id,
            statuses=frozenset({"completed"}),
            timeout=float(args.sse_timeout),
        )
        recovered_job = recovered_rag.get("latest_job") or {}
        recovered_rag, integrity = wait_rag_integrity(
            workspace_id=workspace_id,
            document_id=document_id,
        )
        record(
            "rag_worker_restart_nonterminal_ingestion",
            worker_id="rag-worker-01",
            workspace_id=workspace_id,
            document_id=document_id,
            job_id=job_id,
            queued_status=(queued.get("latest_job") or {}).get("status"),
            interrupted_status=active_status,
            recovered_status=recovered_job.get("status"),
            document_status=recovered_rag.get("status"),
            chunk_count=integrity.get("chunk_count"),
            embedding_count=integrity.get("embedding_count"),
        )

        marker = f"p2-redis-permission-{stamp}"
        relative_path = Path("tmp") / "p2-hardening" / f"{marker}.txt"
        artifact_path = REPO_DIR / relative_path
        if artifact_path.exists():
            raise DrillError(f"permission drill target already exists: {relative_path}")
        artifact_path.parent.mkdir(parents=True, exist_ok=True)
        permission_task, permission_run = create_task(
            "请只调用 workspace.create_file 创建文件 "
            f"{relative_path.as_posix()}，文件内容必须精确为 {marker}，"
            "不要调用其他工具。完成后简短回复。"
        )
        permission = wait_pending_permission(permission_run)
        if permission.get("tool_name") != "workspace.create_file":
            raise DrillError(
                "permission drill requested unexpected tool: "
                f"{permission.get('tool_name')!r}"
            )

        worker_before_redis = wait_worker("worker-01")
        rag_before_redis = wait_worker("rag-worker-01")
        run_checked(
            ["docker", "compose", "restart", "redis"],
            cwd=REPO_DIR,
            env=env,
            log_path=output_dir / "redis-restart.log",
        )
        wait_redis()
        wait_json(f"{GATEWAY_API}/health", contains="healthy", timeout=60.0)
        wait_worker(
            "worker-01",
            timeout=60.0,
            previous_reported_at=worker_before_redis.get("reported_at"),
        )
        wait_worker(
            "rag-worker-01",
            timeout=60.0,
            previous_reported_at=rag_before_redis.get("reported_at"),
        )
        request_json(
            f"{GATEWAY_API}/permissions/resolve",
            method="POST",
            payload={
                "request_id": permission.get("id"),
                "decision": "allow_once",
                "note": "P2 isolated Redis restart drill",
            },
            timeout=10.0,
        )
        permission_events = read_sse(permission_run, timeout=float(args.sse_timeout))
        validate_event_history(permission_events, require_terminal=True)
        matching_tool_finishes = [
            event
            for event in permission_events
            if event.get("type") == "tool.call.finished"
            and event_tool_name(event) == "workspace.create_file"
        ]
        if len(matching_tool_finishes) != 1:
            raise DrillError(
                "permission drill expected exactly one successful create_file, got "
                f"{len(matching_tool_finishes)}"
            )
        if not artifact_path.is_file():
            raise DrillError("permission drill target file was not created")
        artifact_bytes = artifact_path.read_bytes()
        if artifact_bytes != marker.encode("utf-8"):
            raise DrillError("permission drill target content is not exact")
        remaining_permissions = request_json(
            f"{GATEWAY_API}/runs/{permission_run}/permissions"
        )
        if (remaining_permissions.get("data") or {}).get("requests"):
            raise DrillError("permission drill left a pending permission")
        record(
            "redis_restart_permission_resume",
            task_id=permission_task,
            run_id=permission_run,
            permission_request_id=permission.get("id"),
            tool_name="workspace.create_file",
            tool_finish_count=1,
            target_path=relative_path.as_posix(),
            target_bytes=len(artifact_bytes),
            target_sha256=hashlib.sha256(artifact_bytes).hexdigest(),
            terminal_type=permission_events[-1].get("type"),
        )

        redis_task, redis_run = create_task(
            "P2 Redis restart smoke. Reply briefly without using tools."
        )
        redis_events = read_sse(redis_run, timeout=float(args.sse_timeout))
        validate_event_history(redis_events, require_terminal=True)
        record(
            "redis_restart",
            task_id=redis_task,
            run_id=redis_run,
            event_count=len(redis_events),
        )

        retry_exhaustion = exercise_run_queue_retry_exhaustion(
            stamp=stamp,
            processes=processes,
        )
        record(
            "run_queue_retry_exhaustion",
            **retry_exhaustion,
        )

        poison_records = inject_poison_messages(stamp)
        record(
            "poison_message_atomic_dlq_ack",
            records=poison_records,
            payload_redacted=True,
            source_pending_expected=0,
        )

        runtime_snapshot, storage_snapshot = wait_reconciliation()
        postgres_snapshot = postgres_reconciliation_snapshot()
        required_zero = (
            "active_outbox_count",
            "processing_inbox_count",
            "terminal_event_violation_count",
            "nonterminal_step_on_terminal_run_count",
            "nonterminal_tool_on_terminal_run_count",
            "pending_permission_on_terminal_run_count",
        )
        violations = {
            key: postgres_snapshot.get(key)
            for key in required_zero
            if postgres_snapshot.get(key) != 0
        }
        if violations:
            raise DrillError(
                "PostgreSQL terminal reconciliation violations: "
                + json.dumps(violations, sort_keys=True)
            )
        record(
            "runtime_terminal_reconciliation",
            redis_status=runtime_snapshot.get("status"),
            redis_streams=[
                {
                    "name": stream.get("name"),
                    "available": stream.get("available"),
                    "pending": stream.get("pending"),
                    "lag": stream.get("lag"),
                }
                for stream in runtime_snapshot.get("streams") or []
            ],
            redis_dead_letters=[
                {
                    "name": item.get("name"),
                    "count": item.get("count"),
                }
                for item in runtime_snapshot.get("dead_letters") or []
            ],
            storage_status=storage_snapshot.get("status"),
            storage_scanned_runs=storage_snapshot.get("scanned_runs"),
            storage_issue_count=storage_snapshot.get("issue_count"),
            postgres=postgres_snapshot,
        )

        evidence["status"] = "passed"
        evidence["finished_at"] = utc_stamp()
        return output_dir
    except KeyboardInterrupt:
        evidence["status"] = "interrupted"
        evidence["finished_at"] = utc_stamp()
        evidence["error"] = {
            "type": "KeyboardInterrupt",
            "message": "drill interrupted by operator",
        }
        raise
    except Exception as exc:
        evidence["status"] = "failed"
        evidence["finished_at"] = utc_stamp()
        evidence["error"] = {"type": type(exc).__name__, "message": str(exc)[:500]}
        raise
    finally:
        for name in ("rag-worker", "worker", "gateway", "control-plane"):
            processes[name].stop()
        if not args.keep_infra:
            subprocess.run(
                ["docker", "compose", "down"],
                cwd=REPO_DIR,
                env=env,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
            )
        (output_dir / "summary.json").write_text(
            json.dumps(evidence, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run isolated P2 process restart and SSE recovery drills"
    )
    parser.add_argument(
        "--conda-env", default=os.environ.get("JARVIS_CONDA_ENV", "jarvis-assistant")
    )
    parser.add_argument(
        "--output-dir", default=str(DEFAULT_OUTPUT / "runtime-fault-drill")
    )
    parser.add_argument("--sse-timeout", type=int, default=90)
    parser.add_argument(
        "--keep-infra",
        action="store_true",
        help="leave PostgreSQL and Redis running after the drill",
    )
    args = parser.parse_args()
    if not 15 <= args.sse_timeout <= 300:
        parser.error("--sse-timeout must be between 15 and 300 seconds")
    return args


def main() -> int:
    try:
        output_dir = drill(parse_args())
    except KeyboardInterrupt:
        print("[p2-runtime-drill] INTERRUPTED", file=sys.stderr)
        return 130
    except (DrillError, OSError, ValueError, urllib.error.URLError) as exc:
        print(f"[p2-runtime-drill] FAILED: {exc}", file=sys.stderr)
        return 1
    print(f"[p2-runtime-drill] PASSED: {output_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
