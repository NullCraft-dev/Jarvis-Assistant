from __future__ import annotations

import os

import pytest

from jarvis_worker.shared.errors.application import AppError
from jarvis_worker.runtime.workspaces.workspace_policy import WorkspacePolicy


def test_workspace_policy_uses_canonical_default_and_allowed_roots(tmp_path, monkeypatch):
    first = tmp_path / "first"
    second = tmp_path / "second"
    first.mkdir()
    second.mkdir()
    monkeypatch.setenv("JARVIS_WORKSPACE_ROOT", str(first))
    monkeypatch.setenv(
        "JARVIS_ALLOWED_WORKSPACE_PATHS",
        os.pathsep.join((str(first), str(second))),
    )

    policy = WorkspacePolicy.from_env()

    assert policy.resolve(None) == str(first.resolve())
    assert policy.resolve(str(second)) == str(second.resolve())
    assert policy.allowed_workspace_paths == (str(first.resolve()), str(second.resolve()))


def test_workspace_policy_allows_a_narrower_directory_inside_allowed_root(tmp_path):
    child = tmp_path / "project"
    child.mkdir()
    policy = WorkspacePolicy(str(tmp_path), (str(tmp_path),))

    assert policy.resolve(str(child)) == str(child.resolve())


def test_workspace_policy_rejects_escape_and_symlink_escape(tmp_path):
    allowed = tmp_path / "allowed"
    outside = tmp_path / "outside"
    allowed.mkdir()
    outside.mkdir()
    link = allowed / "outside-link"
    link.symlink_to(outside, target_is_directory=True)
    policy = WorkspacePolicy(str(allowed), (str(allowed.resolve()),))

    for path in (outside, link):
        with pytest.raises(AppError) as exc:
            policy.resolve(str(path))
        assert exc.value.code == "WORKSPACE_ACCESS_DENIED"
        assert exc.value.category == "permission"


def test_workspace_policy_rejects_missing_directory(tmp_path):
    policy = WorkspacePolicy(str(tmp_path), (str(tmp_path.resolve()),))

    with pytest.raises(AppError) as exc:
        policy.resolve(str(tmp_path / "missing"))

    assert exc.value.code == "WORKSPACE_NOT_FOUND"
    assert exc.value.category == "validation"


def test_workspace_policy_fails_closed_without_server_allowlist(tmp_path):
    policy = WorkspacePolicy(None, ())

    with pytest.raises(AppError) as exc:
        policy.resolve(str(tmp_path))

    assert exc.value.code == "WORKSPACE_ACCESS_DENIED"
