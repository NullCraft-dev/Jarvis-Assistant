from jarvis_worker.agent.core.actions import AgentAction
from jarvis_worker.agent.core.state import AgentState
from jarvis_worker.agent.core.workspace_listing_projection import (
    WorkspaceListingProjectionValidator,
    project_workspace_listing_observations,
)


def _state() -> AgentState:
    return AgentState(
        user_goal="只告诉我一级目录",
        intent={
            "workspace": {
                "evidence": "metadata",
                "action": "read",
                "ambiguity": "clear",
                "listing_entry_types": ["dir"],
            }
        },
        observations=[
            {
                "tool_name": "workspace.list_files",
                "ok": True,
                "data": {
                    "entries": [
                        {"name": "apps", "type": "dir"},
                        {"name": "draft.md", "type": "file"},
                        {"name": "external-link", "type": "symlink"},
                    ]
                },
            }
        ],
    )


def test_projection_filters_model_view_but_preserves_raw_observation():
    state = _state()

    projected = project_workspace_listing_observations(
        state.observations,
        state.intent,
    )

    assert projected[0]["data"]["entries"] == [{"name": "apps", "type": "dir"}]
    assert len(state.observations[0]["data"]["entries"]) == 3


def test_validator_rejects_excluded_entry_names_and_accepts_allowed_only():
    state = _state()
    validator = WorkspaceListingProjectionValidator()

    rejected = validator.validate(
        action=AgentAction.finish("目录有 apps；另外还有 draft.md 和 external-link。"),
        state=state,
    )
    accepted = validator.validate(
        action=AgentAction.finish("一级目录只有 apps。"),
        state=state,
    )

    assert rejected.accepted is False
    assert rejected.reason_code == "WORKSPACE_LISTING_OUTPUT_SCOPE"
    assert "draft.md" not in rejected.feedback
    assert accepted.accepted is True


def test_projection_is_inactive_for_unrestricted_listing():
    state = _state()
    state.intent["workspace"]["listing_entry_types"] = []

    projected = project_workspace_listing_observations(
        state.observations,
        state.intent,
    )
    validation = WorkspaceListingProjectionValidator().validate(
        action=AgentAction.finish("apps、draft.md、external-link"),
        state=state,
    )

    assert projected is state.observations
    assert validation.accepted is True
