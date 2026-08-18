"""Skill Layer 的运行时契约。"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


class SkillLayerError(ValueError):
    """已安装 Skill 无法被安全加载或解析。"""

    code = "SKILL_CONTEXT_INVALID"


@dataclass(frozen=True)
class SkillScriptDefinition:
    """启动时校验并固定的 Skill 脚本执行契约。"""

    skill_id: str
    skill_version: str
    script_name: str
    tool_name: str
    description: str
    path: Path
    entrypoint_args: tuple[str, ...]
    input_schema: dict[str, Any]
    timeout_seconds: int
    max_input_bytes: int
    max_output_bytes: int
    fingerprint: str


@dataclass(frozen=True)
class SkillDefinition:
    """启动时完成校验的 Skill 定义。"""

    skill_id: str
    version: str
    description: str
    root: Path
    instructions: str
    config: dict[str, Any]
    fingerprint: str
    scripts: tuple[SkillScriptDefinition, ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class SkillContext:
    """单次 AgentRun 选中的、有界且可 checkpoint 的 Skill 上下文。"""

    skill_id: str
    version: str
    description: str
    instructions: str
    references: tuple[tuple[str, str], ...] = field(default_factory=tuple)
    fingerprint: str = ""

    def to_state_dict(self) -> dict[str, Any]:
        return {
            "skill_id": self.skill_id,
            "version": self.version,
            "description": self.description,
            "instructions": self.instructions,
            "references": [
                {"path": path, "content": content}
                for path, content in self.references
            ],
            "fingerprint": self.fingerprint,
        }
