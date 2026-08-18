"""把已启用 Skill 脚本映射为 ToolGateway capability modules。"""

from __future__ import annotations

from collections.abc import Iterable

from jarvis_worker.agent.skills.contracts import SkillDefinition
from jarvis_worker.agent.skills.script_executor import SkillScriptExecutor
from jarvis_worker.agent.tool_gateway.contracts import ToolManifest
from jarvis_worker.agent.tool_gateway.modules import CapabilityModule, ToolBinding


def create_skill_script_capability_modules(
    definitions: Iterable[SkillDefinition],
) -> tuple[CapabilityModule, ...]:
    """为每个包含已启用脚本的 Skill 创建一个显式 capability module。"""
    modules: list[CapabilityModule] = []
    for definition in definitions:
        bindings = tuple(
            ToolBinding(
                manifest=ToolManifest(
                    name=script.tool_name,
                    provider="system",
                    description=script.description,
                    risk_level_default="L1",
                    permission_scope="skill_script",
                    input_schema=script.input_schema,
                    metadata={
                        "capability": {
                            "id": f"skill-script:{definition.skill_id}",
                            "version": definition.version,
                        },
                        "skill_script": {
                            "skill_id": definition.skill_id,
                            "skill_version": definition.version,
                            "script_name": script.script_name,
                            "fingerprint": script.fingerprint,
                            "network": False,
                        },
                    },
                ),
                executor=SkillScriptExecutor(script),
            )
            for script in definition.scripts
        )
        if bindings:
            modules.append(
                CapabilityModule(
                    capability_id=f"skill-script:{definition.skill_id}",
                    version=definition.version,
                    tool_bindings=bindings,
                )
            )
    return tuple(modules)
