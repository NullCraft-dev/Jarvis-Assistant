"""P6-3 phase service 的架构边界测试。"""

from __future__ import annotations

import ast
import inspect

from jarvis_worker.agent.core import graph, graph_nodes
from jarvis_worker.agent.core.phases import (
    action_validation,
    intent_extraction,
    lifecycle,
    model_call,
    observation,
    tool_execution,
)
from jarvis_worker.agent.core.phases.observation import project_tool_result
from jarvis_worker.agent.tool_gateway.contracts import ToolDeliverable, ToolResult


def _assert_no_effect_capability(module) -> None:
    tree = ast.parse(inspect.getsource(module))
    imported = {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    }
    imported.update(
        node.module or "" for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)
    )

    assert not any(name.endswith("tool_gateway.gateway") for name in imported)
    assert not any("permissions" in name for name in imported)
    assert not any(name.startswith("langchain") for name in imported)
    assert not any(
        isinstance(node, ast.Attribute) and node.attr in {"execute", "assess"}
        for node in ast.walk(tree)
    )


def test_observation_phase_cannot_import_tool_execution_or_permission_owners() -> None:
    _assert_no_effect_capability(observation)


def test_model_call_phase_cannot_import_tool_execution_or_permission_owners() -> None:
    _assert_no_effect_capability(model_call)


def test_lifecycle_phase_has_no_model_tool_or_permission_capability() -> None:
    _assert_no_effect_capability(lifecycle)


def test_action_validation_phase_can_assess_but_cannot_execute_effects() -> None:
    tree = ast.parse(inspect.getsource(action_validation))
    imported = {
        node.module or "" for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)
    }

    assert "jarvis_worker.agent.tool_gateway.gateway" in imported
    assert not any("permissions" in name for name in imported)
    assert not any(name.startswith("langchain") for name in imported)
    attributes = [
        node.attr for node in ast.walk(tree) if isinstance(node, ast.Attribute)
    ]
    assert attributes.count("assess") == 1
    assert "execute" not in attributes


def test_intent_extraction_reads_catalog_but_has_no_effect_capability() -> None:
    tree = ast.parse(inspect.getsource(intent_extraction))
    imported = {
        node.module or "" for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)
    }
    assert "jarvis_worker.agent.tool_gateway.gateway" in imported
    assert not any("permissions" in name for name in imported)
    assert not any(name.startswith("langchain") for name in imported)
    attributes = [
        node.attr for node in ast.walk(tree) if isinstance(node, ast.Attribute)
    ]
    assert "assess" not in attributes
    assert "execute" not in attributes


def test_tool_execution_is_the_only_effect_capable_phase() -> None:
    tree = ast.parse(inspect.getsource(tool_execution))
    imported = {
        node.module or "" for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)
    }

    assert "jarvis_worker.agent.tool_gateway.gateway" in imported
    assert not any("permissions" in name for name in imported)
    assert not any(name.startswith("langchain") for name in imported)
    assert not any("tools." in name or name.endswith(".executor") for name in imported)
    attributes = [
        node.attr for node in ast.walk(tree) if isinstance(node, ast.Attribute)
    ]
    assert attributes.count("execute") == 2
    assert "assess" not in attributes


def test_langgraph_remains_the_control_flow_owner() -> None:
    graph_tree = ast.parse(inspect.getsource(graph))
    graph_calls = [
        node.func.attr
        for node in ast.walk(graph_tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
    ]
    assert graph_calls.count("add_conditional_edges") == 7
    assert graph_calls.count("add_edge") == 1

    nodes_tree = ast.parse(inspect.getsource(graph_nodes))
    route_functions = {
        node.name
        for node in ast.walk(nodes_tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name.startswith("route_")
    }
    assert route_functions == {
        "route_from_start",
        "route_after_initialize",
        "route_after_intent",
        "route_after_model",
        "route_after_validation",
        "route_after_tool_execution",
        "route_after_observation",
    }


def test_langgraph_interrupt_and_second_checkpointer_remain_disabled() -> None:
    source = inspect.getsource(graph)
    tree = ast.parse(source)
    compile_calls = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "compile"
    ]

    assert "interrupt(" not in source
    assert len(compile_calls) == 1
    assert compile_calls[0].args == []
    assert compile_calls[0].keywords == []


def test_tool_result_projection_preserves_public_contract() -> None:
    result = ToolResult(
        ok=True,
        summary="created",
        data={"path": "report.md"},
        artifact_ids=["artifact-1"],
        deliverables=[
            ToolDeliverable(
                kind="file",
                title="Report",
                path="report.md",
                size_bytes=42,
                mime_type="text/markdown",
                content_hash="sha256:example",
            ),
        ],
    )

    assert project_tool_result(result) == {
        "kind": result.kind,
        "summary": "created",
        "data": {"path": "report.md"},
        "artifact_ids": ["artifact-1"],
        "deliverables": [
            {
                "kind": "file",
                "title": "Report",
                "path": "report.md",
                "size_bytes": 42,
                "mime_type": "text/markdown",
                "content_hash": "sha256:example",
            }
        ],
    }


def test_tool_result_projection_redacts_credentials_before_runtime_event() -> None:
    result = ToolResult(
        ok=True,
        summary="search completed",
        data={
            "matches": [
                {
                    "path": "project/src/secrets.ts",
                    "line_number": 1,
                    "preview": 'export const AUTH_TOKEN = "fake-eval-token"',
                }
            ]
        },
    )

    projected = project_tool_result(result)

    assert projected["data"]["matches"][0]["path"] == "project/src/secrets.ts"
    assert "fake-eval-token" not in projected["data"]["matches"][0]["preview"]
    assert "[已隐藏凭据]" in projected["data"]["matches"][0]["preview"]
