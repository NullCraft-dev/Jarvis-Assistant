from __future__ import annotations

import json
from pathlib import Path

import pytest

from jarvis_worker.agent.skills import SkillLayer, SkillLayerError, SkillLoader
from jarvis_worker.agent.tool_gateway.contracts import ToolManifest, ToolResult
from jarvis_worker.agent.tool_gateway.registry import ToolRegistry
from jarvis_worker.runtime_bus.messages import RunJobMessage

REPO_ROOT = Path(__file__).resolve().parents[3]
SKILLS_ROOT = REPO_ROOT / "skills"


def _job(goal: str) -> RunJobMessage:
    return RunJobMessage(
        job_id="job-1",
        trace_id="trace-1",
        task_id="task-1",
        run_id="run-1",
        user_goal=goal,
        created_at="2026-07-30T00:00:00Z",
    )


def _create_instruction_skill(
    tmp_path: Path,
    *,
    required_tools: list[str] | None = None,
) -> SkillLoader:
    skills_root = tmp_path / "skills"
    skill_root = skills_root / "sample-advisor"
    adapter_root = skills_root / ".jarvis"
    skill_root.mkdir(parents=True)
    adapter_root.mkdir()
    (skill_root / "SKILL.md").write_text(
        """---
name: sample-advisor
description: Domain advice used only by generic Skill Layer tests.
---

# Sample Advisor

Apply the bounded domain checklist. Never grant permissions or execute effects.
""",
        encoding="utf-8",
    )
    config = {
        "schema_version": "jarvis-skill-adapter-v1",
        "version": "1.0.0",
        "activation": {
            "authorized_tools_any": [],
            "source_policy_providers": [],
            "goal_phrases": ["use sample advice"],
        },
        "required_tools": required_tools or [],
        "optional_tools": [],
        "references": {"always": [], "by_source": {}},
        "source_hints": {},
        "scripts": {},
        "limits": {
            "max_instruction_bytes": 32768,
            "max_reference_bytes": 65536,
            "max_loaded_reference_bytes": 131072,
        },
    }
    (adapter_root / "sample-advisor.json").write_text(
        json.dumps(config), encoding="utf-8"
    )
    return SkillLoader(skills_root)


def test_product_runtime_has_no_bundled_knowledge_workflow_skill():
    assert SkillLoader(SKILLS_ROOT).load_all() == ()


def test_generic_instruction_skill_resolves_only_for_matching_goal(tmp_path: Path):
    loader = _create_instruction_skill(tmp_path)
    registry = ToolRegistry()
    layer = SkillLayer(loader, registry)

    context = layer.resolve(_job("Please use sample advice for this review"))

    assert context is not None
    assert context.skill_id == "sample-advisor"
    assert "bounded domain checklist" in context.instructions
    assert layer.resolve(_job("ordinary conversation")) is None


def test_generic_skill_context_fingerprint_is_stable(tmp_path: Path):
    loader = _create_instruction_skill(tmp_path)
    layer = SkillLayer(loader, ToolRegistry())

    first = layer.resolve(_job("use sample advice"))
    second = layer.resolve(_job("use sample advice"))

    assert first is not None and second is not None
    assert len(first.fingerprint) == 64
    assert first.fingerprint == second.fingerprint


def test_skill_layer_rejects_missing_declared_host_capability(tmp_path: Path):
    loader = _create_instruction_skill(
        tmp_path, required_tools=["sample.required_tool"]
    )

    with pytest.raises(SkillLayerError, match="缺少必需工具"):
        SkillLayer(loader, ToolRegistry())

    registry = ToolRegistry()
    registry.register(
        ToolManifest(name="sample.required_tool"),
        lambda _request: ToolResult(ok=True),
    )
    assert SkillLayer(loader, registry).definitions[0].skill_id == "sample-advisor"
