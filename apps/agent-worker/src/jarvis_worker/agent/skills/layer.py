"""Skill 选择与渐进式上下文加载。"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable

from jarvis_worker.agent.skills.contracts import (
    SkillContext,
    SkillDefinition,
    SkillLayerError,
)
from jarvis_worker.agent.skills.loader import SkillLoader
from jarvis_worker.agent.tool_gateway.registry import ToolRegistry
from jarvis_worker.runtime_bus.messages import RunJobMessage


class SkillLayer:
    """AgentRunner 与 Skill 包之间的唯一运行时边界。"""

    def __init__(
        self,
        loader: SkillLoader,
        tool_registry: ToolRegistry,
        *,
        definitions: tuple[SkillDefinition, ...] | None = None,
    ) -> None:
        self._loader = loader
        self._registry = tool_registry
        self._definitions = definitions if definitions is not None else loader.load_all()
        for definition in self._definitions:
            missing = [
                name
                for name in definition.config.get("required_tools", [])
                if not tool_registry.has(name)
            ]
            if missing:
                raise SkillLayerError(
                    f"Skill {definition.skill_id} 缺少必需工具: {', '.join(missing)}"
                )

    @property
    def definitions(self) -> tuple[SkillDefinition, ...]:
        return self._definitions

    def resolve(self, job: RunJobMessage) -> SkillContext | None:
        matches: list[tuple[int, SkillDefinition]] = []
        for definition in self._definitions:
            score = _activation_score(definition, job)
            if score > 0:
                matches.append((score, definition))
        if not matches:
            return None
        matches.sort(key=lambda item: (-item[0], item[1].skill_id))
        if len(matches) > 1 and matches[0][0] == matches[1][0]:
            raise SkillLayerError("多个 Skill 具有相同激活优先级，拒绝猜测")
        definition = matches[0][1]
        reference_paths = self._reference_paths(definition, job)
        limits = definition.config["limits"]
        max_total = int(limits["max_loaded_reference_bytes"])
        total = 0
        references: list[tuple[str, str]] = []
        for path in reference_paths:
            content = self._loader.read_resource(definition, path)
            total += len(content.encode("utf-8"))
            if total > max_total:
                raise SkillLayerError(
                    f"Skill {definition.skill_id} 的本轮引用超过总上限"
                )
            references.append((path, content))
        fingerprint = _context_fingerprint(
            definition,
            reference_paths,
        )
        return SkillContext(
            skill_id=definition.skill_id,
            version=definition.version,
            description=definition.description,
            instructions=definition.instructions,
            references=tuple(references),
            fingerprint=fingerprint,
        )

    def _reference_paths(
        self, definition: SkillDefinition, job: RunJobMessage
    ) -> tuple[str, ...]:
        config = definition.config
        references = config.get("references", {})
        selected = list(references.get("always", []))
        source = str(job.source_policy.get("provider", "")).strip().lower()
        if not source:
            goal = job.user_goal.casefold()
            for candidate, hints in config.get("source_hints", {}).items():
                if any(str(hint).casefold() in goal for hint in hints):
                    source = str(candidate)
                    break
        selected.extend(references.get("by_source", {}).get(source, []))
        return tuple(_deduplicate(selected))


def _activation_score(definition: SkillDefinition, job: RunJobMessage) -> int:
    activation = definition.config.get("activation", {})
    score = 0
    authorized = set(job.authorized_tools)
    configured_tools = set(activation.get("authorized_tools_any", []))
    if authorized & configured_tools:
        score += 100
    provider = str(job.source_policy.get("provider", "")).strip().lower()
    if provider and provider in activation.get("source_policy_providers", []):
        score += 50
    goal = job.user_goal.casefold()
    if any(str(phrase).casefold() in goal for phrase in activation.get("goal_phrases", [])):
        score += 10
    return score


def _deduplicate(values: Iterable[str]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        if value not in seen:
            seen.add(value)
            result.append(value)
    return result


def _context_fingerprint(
    definition: SkillDefinition,
    reference_paths: tuple[str, ...],
) -> str:
    payload = json.dumps(
        {
            "package": definition.fingerprint,
            "references": reference_paths,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()
