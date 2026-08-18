"""ToolCall permission expired migration contract."""

from importlib import import_module


def test_permission_expired_migration_extends_current_head(monkeypatch):
    migration = import_module(
        "jarvis_worker.migrations.versions.024_tool_call_permission_expired"
    )
    calls = []
    monkeypatch.setattr(
        migration.op,
        "drop_constraint",
        lambda *args, **kwargs: calls.append(("drop", args, kwargs)),
    )
    monkeypatch.setattr(
        migration.op,
        "create_check_constraint",
        lambda *args, **kwargs: calls.append(("create", args, kwargs)),
    )
    monkeypatch.setattr(
        migration.op,
        "execute",
        lambda statement: calls.append(("execute", statement)),
    )

    assert migration.down_revision == "023_rag_quality_issues"
    assert migration.revision == "024_tool_permission_expired"

    migration.upgrade()
    assert calls[0][0] == "drop"
    assert calls[1][0] == "create"
    assert "'expired'" in calls[1][1][2]

    calls.clear()
    migration.downgrade()
    assert calls[0][0] == "execute"
    assert "permission_status = 'pending'" in calls[0][1]
    assert calls[1][0] == "drop"
    assert calls[2][0] == "create"
    assert "'expired'" not in calls[2][1][2]
