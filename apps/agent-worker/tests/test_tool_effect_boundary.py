from __future__ import annotations

import json
import multiprocessing
import time
from pathlib import Path

import pytest

from jarvis_worker.agent.permissions.manager import PermissionManager
from jarvis_worker.agent.tool_gateway.contracts import PermissionApproval, ToolRequest
from jarvis_worker.agent.tool_gateway.effect_boundary import FileToolEffectBarrier
from jarvis_worker.agent.tool_gateway.gateway import ToolGateway
from jarvis_worker.agent.tools.workspace.create_file import _supports_safe_dir_fd
from jarvis_worker.bootstrap.tool_registry import create_tool_registry
from jarvis_worker.shared.config.settings import WorkerConfig


def _execute_approved_create_file(barrier_root: str, workspace_root: str) -> None:
    gateway = ToolGateway(
        create_tool_registry(),
        PermissionManager(),
        effect_boundary=FileToolEffectBarrier(
            Path(barrier_root),
            timeout_seconds=30,
            poll_interval_seconds=0.01,
        ),
    )
    result = gateway.execute(
        ToolRequest(
            task_id="rec-07-task",
            run_id="rec-07-run",
            step_id="rec-07-step",
            tool_name="workspace.create_file",
            arguments={
                "workspace_root": workspace_root,
                "path": "crash-window.txt",
                "content": "EFFECT_GUARD_OK",
            },
        ),
        approval=PermissionApproval(
            request_id="rec-07-permission",
            decision="allow_once",
        ),
    )
    if not result.ok:
        raise RuntimeError(result.error["code"] if result.error else result.summary)


def _wait_for_marker(root: Path, timeout_seconds: float = 5) -> Path:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        markers = list(root.glob("*.reached.json"))
        if markers:
            return markers[0]
        time.sleep(0.01)
    raise AssertionError("未在超时内到达 Tool effect barrier")


def test_worker_can_be_killed_after_approval_before_workspace_effect(tmp_path):
    if not _supports_safe_dir_fd():
        pytest.skip("当前平台不支持 workspace.create_file 安全 dir-fd")
    barrier_root = tmp_path / "effect-barrier"
    workspace_root = tmp_path / "workspace"
    barrier_root.mkdir()
    workspace_root.mkdir()
    target = workspace_root / "crash-window.txt"
    context = multiprocessing.get_context("spawn")

    crashed = context.Process(
        target=_execute_approved_create_file,
        args=(str(barrier_root), str(workspace_root)),
    )
    crashed.start()
    marker_path = _wait_for_marker(barrier_root)
    marker = json.loads(marker_path.read_text(encoding="utf-8"))
    assert marker["boundary"] == "tool.before_effect"
    assert marker["tool_name"] == "workspace.create_file"
    assert target.exists() is False

    crashed.kill()
    crashed.join(timeout=5)
    assert crashed.exitcode is not None and crashed.exitcode != 0
    assert target.exists() is False

    (barrier_root / marker["release_file"]).touch()
    replacement = context.Process(
        target=_execute_approved_create_file,
        args=(str(barrier_root), str(workspace_root)),
    )
    replacement.start()
    replacement.join(timeout=10)

    assert replacement.exitcode == 0
    assert target.read_text(encoding="utf-8") == "EFFECT_GUARD_OK"


def test_effect_barrier_timeout_fails_closed_before_executor(tmp_path):
    gateway = ToolGateway(
        create_tool_registry(),
        PermissionManager(),
        effect_boundary=FileToolEffectBarrier(
            tmp_path.resolve(),
            timeout_seconds=0.02,
            poll_interval_seconds=0.005,
        ),
    )

    result = gateway.execute(
        ToolRequest(
            task_id="task",
            run_id="run",
            step_id="step",
            tool_name="workspace.create_file",
            arguments={
                "workspace_root": str(tmp_path),
                "path": "must-not-exist.txt",
                "content": "blocked",
            },
        ),
        approval=PermissionApproval(request_id="permission", decision="allow_once"),
    )

    assert result.ok is False
    assert result.error is not None
    assert result.error["code"] == "TOOL_EFFECT_BARRIER_TIMEOUT"
    assert (tmp_path / "must-not-exist.txt").exists() is False


def test_fault_injection_config_requires_explicit_paired_opt_in(tmp_path, monkeypatch):
    monkeypatch.setenv("JARVIS_TEST_TOOL_EFFECT_BARRIER_ROOT", str(tmp_path.resolve()))
    with pytest.raises(ValueError, match="必须显式启用"):
        WorkerConfig.from_env()

    monkeypatch.setenv("JARVIS_TEST_FAULT_INJECTION_ENABLED", "true")
    config = WorkerConfig.from_env()

    assert config.test_fault_injection_enabled is True
    assert config.test_tool_effect_barrier_root == str(tmp_path.resolve())
