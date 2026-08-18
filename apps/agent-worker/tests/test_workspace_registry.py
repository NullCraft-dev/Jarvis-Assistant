"""Workspace Registry 路径安全、注册语义与 macOS picker 取消测试。"""

import asyncio
from dataclasses import replace

import pytest

from jarvis_worker.shared.errors.application import AppError
from jarvis_worker.runtime.workspaces.workspace_service import (
    WorkspaceApplicationService,
    validate_path_for_registration,
    verify_path_still_valid,
)
from jarvis_worker.runtime.workspaces.workspace_picker_macos import MacOSWorkspacePickerAdapter
from jarvis_worker.shared.domain.models import Workspace, WorkspaceSource, WorkspaceStatus, new_id


def test_registration_allows_explicit_project_below_home(tmp_path, monkeypatch):
    home = tmp_path / "home"
    project = home / "projects" / "jarvis"
    project.mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))

    assert validate_path_for_registration(str(project)) == str(project.resolve())


@pytest.mark.parametrize("path", ["/", "/Users", "/private", "/var", "/tmp", "/Volumes"])
def test_registration_rejects_overbroad_roots(path):
    with pytest.raises(AppError) as exc_info:
        validate_path_for_registration(path)
    assert exc_info.value.code == "WORKSPACE_PATH_FORBIDDEN"


def test_registration_rejects_home_and_sensitive_descendants(tmp_path, monkeypatch):
    home = tmp_path / "home"
    ssh_child = home / ".ssh" / "nested"
    ssh_child.mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))

    for path in (home, ssh_child):
        with pytest.raises(AppError) as exc_info:
            validate_path_for_registration(str(path))
        assert exc_info.value.code == "WORKSPACE_PATH_FORBIDDEN"


def test_registration_rejects_protected_system_tree():
    with pytest.raises(AppError) as exc_info:
        validate_path_for_registration("/etc/ssh")
    assert exc_info.value.code == "WORKSPACE_PATH_FORBIDDEN"


def test_task_revalidation_reapplies_policy_to_legacy_row():
    with pytest.raises(AppError) as exc_info:
        verify_path_still_valid("/Users")
    assert exc_info.value.code == "WORKSPACE_PATH_FORBIDDEN"


def test_task_revalidation_rejects_symlink_replacement(tmp_path):
    workspace = tmp_path / "workspace"
    replacement = tmp_path / "replacement"
    workspace.mkdir()
    replacement.mkdir()
    stored = str(workspace.resolve())
    workspace.rmdir()
    workspace.symlink_to(replacement, target_is_directory=True)

    with pytest.raises(AppError) as exc_info:
        verify_path_still_valid(stored)
    assert exc_info.value.code == "WORKSPACE_PATH_FORBIDDEN"


@pytest.mark.parametrize(
    "stderr",
    [
        b"execution error: User canceled. (-128)",
        "execution error: \u7528\u6237\u53d6\u6d88\u3002 (-128)".encode(),
        b"execution error: \xe7\x94\xa8\xe6\x88\xb7\xe5\x8f\x96\xe6\xb6\x88\xe3\x80\x82",
    ],
)
def test_picker_treats_localized_applescript_cancel_as_noop(stderr):
    result = MacOSWorkspacePickerAdapter._parse_result(1, b"", stderr)

    assert result.cancelled is True
    assert result.error_code is None


def test_picker_preserves_non_cancellation_error():
    result = MacOSWorkspacePickerAdapter._parse_result(
        1,
        b"",
        b"execution error: Not authorized to send Apple events. (-1743)",
    )

    assert result.cancelled is False
    assert result.error_code == "WORKSPACE_PICK_FAILED"


@pytest.mark.asyncio
async def test_picker_cancellation_terminates_process_and_propagates(monkeypatch):
    class FakeProcess:
        returncode = None

        def __init__(self):
            self.terminated = False
            self._done = asyncio.Event()

        async def communicate(self):
            await self._done.wait()
            return b"", b""

        def terminate(self):
            self.terminated = True
            self._done.set()

        def kill(self):
            self.terminated = True
            self._done.set()

        async def wait(self):
            await self._done.wait()
            return 0

    proc = FakeProcess()

    async def fake_create_subprocess_exec(*_args, **_kwargs):
        return proc

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_create_subprocess_exec)
    task = asyncio.create_task(MacOSWorkspacePickerAdapter().pick_directory())
    await asyncio.sleep(0)
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task
    assert proc.terminated is True


@pytest.mark.asyncio
async def test_configured_source_promotes_existing_picker_workspace(tmp_path, monkeypatch):
    project = tmp_path / "project"
    project.mkdir()
    canonical = str(project.resolve())
    configured_path = str(project / ".." / "project")
    existing = Workspace(
        id=new_id(),
        name="project",
        root_path=canonical,
        canonical_path=canonical,
        source=WorkspaceSource.USER_PICKER,
        status=WorkspaceStatus.ACTIVE,
    )

    class FakeWorkspaceRepo:
        def __init__(self):
            self.updated = None

        async def insert_if_absent(self, _workspace):
            return False

        async def get_by_canonical_path_for_update(self, _path):
            return replace(existing)

        async def update(self, workspace):
            self.updated = workspace

    class FakeAuditRepo:
        def __init__(self):
            self.events = []

        async def create(self, audit):
            self.events.append(audit)

    class FakeUow:
        def __init__(self):
            self.workspaces = FakeWorkspaceRepo()
            self.audits = FakeAuditRepo()

        def transaction(self):
            return self

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def commit(self):
            return None

        async def rollback(self):
            return None

    class SessionContext:
        async def __aenter__(self):
            return object()

        async def __aexit__(self, *_args):
            return None

    fake_uow = FakeUow()
    monkeypatch.setattr(
        "jarvis_worker.runtime.workspaces.workspace_service.PostgresUnitOfWork",
        lambda _session: fake_uow,
    )
    service = WorkspaceApplicationService(lambda: lambda: SessionContext())

    result = await service.register_configured(configured_path)

    assert result is not None
    assert result.source == WorkspaceSource.CONFIGURED
    assert result.root_path == configured_path
    assert fake_uow.workspaces.updated.source == WorkspaceSource.CONFIGURED
    assert [event.event_type for event in fake_uow.audits.events] == [
        "workspace.managed_by_config"
    ]
