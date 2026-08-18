from __future__ import annotations

import json
from pathlib import Path

import pytest

from jarvis_worker.agent.permissions.manager import PermissionManager
from jarvis_worker.agent.skills import SkillLayerError, SkillLoader
from jarvis_worker.agent.skills.script_executor import SkillScriptExecutor
from jarvis_worker.agent.skills.script_module import (
    create_skill_script_capability_modules,
)
from jarvis_worker.agent.tool_gateway.catalog import install_capability_modules
from jarvis_worker.agent.tool_gateway.contracts import ToolRequest
from jarvis_worker.agent.tool_gateway.gateway import ToolGateway
from jarvis_worker.agent.tool_gateway.registry import ToolRegistry

TOOL_NAME = "skill.sample-transform.check-input"


def _request(arguments: dict) -> ToolRequest:
    return ToolRequest(
        task_id="task-1",
        run_id="run-1",
        tool_name=TOOL_NAME,
        arguments=arguments,
        reason="generic Skill script test",
    )


def _loader(
    tmp_path: Path,
    script_body: str | None = None,
    *,
    max_output_bytes: int = 65536,
    timeout_seconds: int = 10,
    network: bool = False,
) -> SkillLoader:
    skills_root = tmp_path / "skills"
    skill_root = skills_root / "sample-transform"
    scripts_root = skill_root / "scripts"
    schemas_root = skill_root / "schemas"
    adapter_root = skills_root / ".jarvis"
    scripts_root.mkdir(parents=True)
    schemas_root.mkdir()
    adapter_root.mkdir()
    (skill_root / "SKILL.md").write_text(
        """---
name: sample-transform
description: Generic deterministic script fixture for runtime safety tests.
---

# Sample Transform

Use the deterministic check capability when explicitly requested.
""",
        encoding="utf-8",
    )
    default_script = """import argparse
import json
from pathlib import Path

parser = argparse.ArgumentParser()
parser.add_argument("command")
parser.add_argument("--input", required=True)
args = parser.parse_args()
value = json.loads(Path(args.input).read_text(encoding="utf-8"))
print(json.dumps({"valid": isinstance(value, dict), "keys": sorted(value)}))
"""
    (scripts_root / "check_input.py").write_text(
        script_body if script_body is not None else default_script,
        encoding="utf-8",
    )
    schema = {"type": "object", "additionalProperties": True}
    (schemas_root / "input.schema.json").write_text(
        json.dumps(schema), encoding="utf-8"
    )
    config = {
        "schema_version": "jarvis-skill-adapter-v1",
        "version": "1.0.0",
        "activation": {
            "authorized_tools_any": [],
            "source_policy_providers": [],
            "goal_phrases": ["sample transform"],
        },
        "required_tools": [TOOL_NAME],
        "optional_tools": [],
        "references": {"always": [], "by_source": {}},
        "source_hints": {},
        "scripts": {
            "check-input": {
                "path": "scripts/check_input.py",
                "runtime": "python",
                "network": network,
                "execution_enabled": True,
                "description": "Validate a generic JSON object without external effects.",
                "entrypoint_args": ["check"],
                "input_schema": "schemas/input.schema.json",
                "max_input_bytes": 256000,
                "max_output_bytes": max_output_bytes,
                "timeout_seconds": timeout_seconds,
            }
        },
        "limits": {
            "max_instruction_bytes": 32768,
            "max_reference_bytes": 65536,
            "max_loaded_reference_bytes": 131072,
        },
    }
    (adapter_root / "sample-transform.json").write_text(
        json.dumps(config), encoding="utf-8"
    )
    return SkillLoader(skills_root)


def _gateway(loader: SkillLoader) -> ToolGateway:
    registry = ToolRegistry()
    install_capability_modules(
        registry,
        create_skill_script_capability_modules(loader.load_all()),
    )
    return ToolGateway(registry, PermissionManager())


def test_enabled_generic_skill_script_registers_as_l1_system_tool(tmp_path: Path):
    gateway = _gateway(_loader(tmp_path))

    manifest = gateway.registry.get_manifest(TOOL_NAME)
    result = gateway.execute(_request({"alpha": 1}))

    assert manifest is not None
    assert manifest.provider == "system"
    assert manifest.risk_level_default == "L1"
    assert result.ok is True
    assert result.data["valid"] is True
    assert result.data["keys"] == ["alpha"]
    assert result.data["_skill_script"]["skill_id"] == "sample-transform"


def test_script_change_after_load_fails_integrity_check(tmp_path: Path):
    loader = _loader(tmp_path)
    definition = loader.load_all()[0].scripts[0]
    definition.path.write_text("print('{}')\n", encoding="utf-8")

    result = SkillScriptExecutor(definition)(_request({"alpha": 1}))

    assert result.ok is False
    assert result.error["code"] == "SKILL_SCRIPT_INTEGRITY_MISMATCH"


@pytest.mark.parametrize(
    ("script", "code"),
    [
        ("import socket\nsocket.getaddrinfo('example.com', 443)\n", "SKILL_SCRIPT_FAILED"),
        ("from pathlib import Path\nPath('side-effect.txt').write_text('x')\n", "SKILL_SCRIPT_FAILED"),
        ("print('not-json')\n", "SKILL_SCRIPT_OUTPUT_INVALID"),
    ],
)
def test_script_executor_fails_closed_for_forbidden_or_invalid_behavior(
    tmp_path: Path, script: str, code: str
):
    result = _gateway(_loader(tmp_path, script)).execute(_request({"alpha": 1}))
    assert result.ok is False
    assert result.error["code"] == code


def test_script_output_limit_and_timeout_fail_closed(tmp_path: Path):
    output = _gateway(
        _loader(
            tmp_path / "output",
            "import json\nprint(json.dumps({'blob': 'x' * 2048}))\n",
            max_output_bytes=1024,
        )
    ).execute(_request({}))
    timeout = _gateway(
        _loader(
            tmp_path / "timeout",
            "import time\ntime.sleep(2)\n",
            timeout_seconds=1,
        )
    ).execute(_request({}))

    assert output.error["code"] == "SKILL_SCRIPT_OUTPUT_TOO_LARGE"
    assert timeout.error["code"] == "SKILL_SCRIPT_TIMEOUT"


def test_loader_rejects_network_enabled_script(tmp_path: Path):
    with pytest.raises(SkillLayerError, match="必须禁用网络"):
        _loader(tmp_path, network=True).load_all()


def test_loader_ignores_generated_python_cache_in_fingerprint(tmp_path: Path):
    loader = _loader(tmp_path)
    before = loader.load_all()[0].fingerprint
    cache = tmp_path / "skills" / "sample-transform" / "scripts" / "__pycache__"
    cache.mkdir()
    (cache / "generated.pyc").write_bytes(b"generated")

    assert loader.load_all()[0].fingerprint == before
