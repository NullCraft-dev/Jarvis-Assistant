"""从受信任的本地目录加载 Jarvis Skill 包。"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any

from jarvis_worker.agent.skills.contracts import (
    SkillDefinition,
    SkillLayerError,
    SkillScriptDefinition,
)

_SKILL_NAME_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
_VERSION_RE = re.compile(r"^\d+\.\d+\.\d+$")
_SCRIPT_NAME_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
_MAX_PACKAGE_FILES = 128
_MAX_CONFIG_BYTES = 64 * 1024
_ALLOWED_RESOURCE_DIRS = frozenset({"agents", "assets", "references", "schemas", "scripts"})


class SkillLoader:
    """严格加载已安装 Skill；格式错误时启动失败而非静默降级。"""

    def __init__(
        self,
        skills_root: str | Path,
        adapters_root: str | Path | None = None,
    ) -> None:
        skills_path = Path(skills_root).expanduser()
        self._root_was_symlink = skills_path.is_symlink()
        self._root = skills_path.resolve()
        adapters_path = (
            Path(adapters_root).expanduser()
            if adapters_root is not None
            else self._root / ".jarvis"
        )
        self._adapters_root_was_symlink = adapters_path.is_symlink()
        self._adapters_root = adapters_path.resolve()

    def load_all(self) -> tuple[SkillDefinition, ...]:
        if not self._root.exists():
            return ()
        if not self._root.is_dir() or self._root_was_symlink:
            raise SkillLayerError("Skill 根路径必须是非符号链接目录")
        definitions: list[SkillDefinition] = []
        for child in sorted(self._root.iterdir(), key=lambda item: item.name):
            if child.name.startswith("."):
                continue
            if child.is_symlink() or not child.is_dir():
                raise SkillLayerError(f"Skill 根目录包含非法条目: {child.name}")
            definitions.append(self.load(child))
        return tuple(definitions)

    def load(self, skill_root: str | Path) -> SkillDefinition:
        skill_path = Path(skill_root)
        root = skill_path.resolve()
        if root.parent != self._root or skill_path.is_symlink() or not root.is_dir():
            raise SkillLayerError("Skill 必须是 Skill 根路径下的直接子目录")
        files = self._package_files(root)
        skill_file = self._safe_file(root, "SKILL.md")
        if skill_file not in files:
            raise SkillLayerError(f"Skill {root.name} 缺少 SKILL.md")

        raw_skill = skill_file.read_text(encoding="utf-8")
        metadata, instructions = _parse_frontmatter(raw_skill)
        skill_id = metadata.get("name", "")
        description = metadata.get("description", "")
        if not _SKILL_NAME_RE.fullmatch(skill_id) or skill_id != root.name:
            raise SkillLayerError("Skill name 必须为 kebab-case 且与目录名一致")
        if not description.strip():
            raise SkillLayerError(f"Skill {skill_id} 缺少 description")

        config_file = self._adapter_file(skill_id)
        if config_file.stat().st_size > _MAX_CONFIG_BYTES:
            raise SkillLayerError(f"Skill {skill_id} 的 Jarvis adapter 过大")
        try:
            config = json.loads(config_file.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise SkillLayerError(f"Skill {skill_id} 的 Jarvis adapter 无效") from exc
        scripts = self._validate_config(
            root,
            config,
            skill_id,
            len(instructions.encode("utf-8")),
        )

        digest = hashlib.sha256()
        for path in files:
            digest.update(path.relative_to(root).as_posix().encode("utf-8"))
            digest.update(b"\0")
            digest.update(path.read_bytes())
            digest.update(b"\0")
        digest.update(b"jarvis-adapter\0")
        digest.update(config_file.read_bytes())
        digest.update(b"\0")
        return SkillDefinition(
            skill_id=skill_id,
            version=str(config["version"]),
            description=description.strip(),
            root=root,
            instructions=instructions.strip(),
            config=config,
            fingerprint=digest.hexdigest(),
            scripts=scripts,
        )

    def read_resource(self, definition: SkillDefinition, relative_path: str) -> str:
        path = self._safe_file(definition.root, relative_path)
        try:
            return path.read_text(encoding="utf-8")
        except UnicodeDecodeError as exc:
            raise SkillLayerError(
                f"Skill {definition.skill_id} 的文本资源不是 UTF-8: {relative_path}"
            ) from exc

    def _package_files(self, root: Path) -> tuple[Path, ...]:
        files: list[Path] = []
        for path in sorted(root.rglob("*")):
            relative = path.relative_to(root)
            if "__pycache__" in relative.parts or path.suffix == ".pyc":
                continue
            if path.is_symlink():
                raise SkillLayerError(f"Skill 不允许符号链接: {relative.as_posix()}")
            if len(relative.parts) > 1 and relative.parts[0] not in _ALLOWED_RESOURCE_DIRS:
                raise SkillLayerError(f"Skill 包含未知资源目录: {relative.parts[0]}")
            if path.is_file():
                files.append(path)
                if len(files) > _MAX_PACKAGE_FILES:
                    raise SkillLayerError("Skill 文件数量超过上限")
        return tuple(files)

    def _validate_config(
        self,
        root: Path,
        config: dict[str, Any],
        skill_id: str,
        instruction_bytes: int,
    ) -> tuple[SkillScriptDefinition, ...]:
        if config.get("schema_version") != "jarvis-skill-adapter-v1":
            raise SkillLayerError(f"Skill {skill_id} schema_version 不受支持")
        version = config.get("version")
        if not isinstance(version, str) or not _VERSION_RE.fullmatch(version):
            raise SkillLayerError(f"Skill {skill_id} version 必须是 x.y.z")
        limits = config.get("limits")
        if not isinstance(limits, dict):
            raise SkillLayerError(f"Skill {skill_id} 缺少 limits")
        max_instruction = _bounded_int(limits, "max_instruction_bytes", 1024, 131072)
        max_reference = _bounded_int(limits, "max_reference_bytes", 1024, 262144)
        _bounded_int(limits, "max_loaded_reference_bytes", max_reference, 524288)
        if instruction_bytes > max_instruction:
            raise SkillLayerError(f"Skill {skill_id} 的 SKILL.md 指令超过上限")

        activation = config.get("activation", {})
        if not isinstance(activation, dict):
            raise SkillLayerError(f"Skill {skill_id} activation 必须是 object")
        for key in ("authorized_tools_any", "source_policy_providers", "goal_phrases"):
            _string_list(activation.get(key, []), f"activation.{key}")
        _string_list(config.get("required_tools", []), "required_tools")
        _string_list(config.get("optional_tools", []), "optional_tools")
        refs = config.get("references", {})
        if not isinstance(refs, dict) or not isinstance(refs.get("by_source", {}), dict):
            raise SkillLayerError(f"Skill {skill_id} references 必须是 object")
        resource_paths = list(_string_list(refs.get("always", []), "references.always"))
        for source, entries in refs.get("by_source", {}).items():
            if not isinstance(source, str) or not source:
                raise SkillLayerError("references.by_source 的键必须是非空字符串")
            resource_paths.extend(_string_list(entries, f"references.by_source.{source}"))

        scripts = config.get("scripts", {})
        if not isinstance(scripts, dict):
            raise SkillLayerError(f"Skill {skill_id} scripts 必须是 object")
        script_definitions: list[SkillScriptDefinition] = []
        for script_name, script in scripts.items():
            if (
                not isinstance(script_name, str)
                or not _SCRIPT_NAME_RE.fullmatch(script_name)
                or not isinstance(script, dict)
            ):
                raise SkillLayerError("scripts 条目无效")
            execution_enabled = script.get("execution_enabled")
            if not isinstance(execution_enabled, bool):
                raise SkillLayerError(f"script {script_name} execution_enabled 必须是 boolean")
            if script.get("runtime") != "python":
                raise SkillLayerError(f"script {script_name} runtime 仅支持 python")
            if script.get("network") is not False:
                raise SkillLayerError(f"script {script_name} v1 必须禁用网络")
            for key in ("path", "input_schema"):
                value = script.get(key)
                if not isinstance(value, str) or not value:
                    raise SkillLayerError(f"script {script_name} 缺少 {key}")
                resource_paths.append(value)
            description = script.get("description")
            if not isinstance(description, str) or not 1 <= len(description.strip()) <= 500:
                raise SkillLayerError(f"script {script_name} description 无效")
            entrypoint_args = _bounded_string_list(
                script.get("entrypoint_args", []),
                f"script {script_name} entrypoint_args",
                maximum_items=8,
                maximum_length=64,
            )
            if "--input" in entrypoint_args:
                raise SkillLayerError(f"script {script_name} entrypoint_args 使用保留参数")
            max_input_bytes = _bounded_int(
                script,
                "max_input_bytes",
                1024,
                262144,
                field_prefix=f"script {script_name}",
            )
            max_output_bytes = _bounded_int(
                script,
                "max_output_bytes",
                1024,
                65536,
                field_prefix=f"script {script_name}",
            )
            timeout_seconds = _bounded_int(
                script,
                "timeout_seconds",
                1,
                30,
                field_prefix=f"script {script_name}",
            )
            script_path = self._safe_file(root, str(script["path"]))
            schema_path = self._safe_file(root, str(script["input_schema"]))
            input_schema = _load_json_object(
                schema_path,
                f"script {script_name} input_schema",
                max_bytes=262144,
            )
            if input_schema.get("type") != "object":
                raise SkillLayerError(f"script {script_name} input_schema 根节点必须是 object")
            if execution_enabled:
                script_definitions.append(
                    SkillScriptDefinition(
                        skill_id=skill_id,
                        skill_version=str(version),
                        script_name=script_name,
                        tool_name=f"skill.{skill_id}.{script_name}",
                        description=description.strip(),
                        path=script_path,
                        entrypoint_args=entrypoint_args,
                        input_schema=input_schema,
                        timeout_seconds=timeout_seconds,
                        max_input_bytes=max_input_bytes,
                        max_output_bytes=max_output_bytes,
                        fingerprint=hashlib.sha256(script_path.read_bytes()).hexdigest(),
                    )
                )

        for relative_path in sorted(set(resource_paths)):
            path = self._safe_file(root, relative_path)
            if path.stat().st_size > max_reference and relative_path.startswith("references/"):
                raise SkillLayerError(f"Skill 引用文件超过单文件上限: {relative_path}")
        return tuple(script_definitions)

    @staticmethod
    def _safe_file(root: Path, relative_path: str) -> Path:
        if not isinstance(relative_path, str) or not relative_path:
            raise SkillLayerError("Skill 资源路径为空")
        candidate = (root / relative_path).resolve()
        if candidate == root or root not in candidate.parents:
            raise SkillLayerError(f"Skill 资源路径越界: {relative_path}")
        if candidate.is_symlink() or not candidate.is_file():
            raise SkillLayerError(f"Skill 资源不存在或不是普通文件: {relative_path}")
        return candidate

    def _adapter_file(self, skill_id: str) -> Path:
        root = self._adapters_root
        if not root.is_dir() or self._adapters_root_was_symlink:
            raise SkillLayerError("Jarvis Skill adapter 根路径不可用")
        candidate = (root / f"{skill_id}.json").resolve()
        if (
            candidate.parent != root
            or candidate.is_symlink()
            or not candidate.is_file()
        ):
            raise SkillLayerError(f"Skill {skill_id} 缺少 Jarvis adapter")
        return candidate


def _parse_frontmatter(content: str) -> tuple[dict[str, str], str]:
    lines = content.splitlines()
    if len(lines) < 4 or lines[0].strip() != "---":
        raise SkillLayerError("SKILL.md 缺少 YAML frontmatter")
    try:
        end = next(index for index in range(1, len(lines)) if lines[index].strip() == "---")
    except StopIteration as exc:
        raise SkillLayerError("SKILL.md frontmatter 未闭合") from exc
    metadata: dict[str, str] = {}
    for line in lines[1:end]:
        if not line.strip():
            continue
        key, separator, value = line.partition(":")
        if not separator or key.strip() not in {"name", "description"}:
            raise SkillLayerError("SKILL.md frontmatter 只允许 name 和 description")
        clean_value = value.strip().strip('"').strip("'")
        if key.strip() in metadata or not clean_value:
            raise SkillLayerError("SKILL.md frontmatter 字段重复或为空")
        metadata[key.strip()] = clean_value
    return metadata, "\n".join(lines[end + 1 :])


def _string_list(value: Any, field: str) -> tuple[str, ...]:
    if not isinstance(value, list) or any(not isinstance(item, str) or not item for item in value):
        raise SkillLayerError(f"{field} 必须是非空字符串数组")
    return tuple(value)


def _bounded_int(
    values: dict[str, Any],
    key: str,
    minimum: int,
    maximum: int,
    *,
    field_prefix: str = "limits",
) -> int:
    value = values.get(key)
    if not isinstance(value, int) or isinstance(value, bool) or not minimum <= value <= maximum:
        raise SkillLayerError(f"{field_prefix}.{key} 超出允许范围")
    return value


def _bounded_string_list(
    value: Any,
    field: str,
    *,
    maximum_items: int,
    maximum_length: int,
) -> tuple[str, ...]:
    if (
        not isinstance(value, list)
        or len(value) > maximum_items
        or any(
            not isinstance(item, str)
            or not item
            or len(item) > maximum_length
            for item in value
        )
    ):
        raise SkillLayerError(f"{field} 无效")
    return tuple(value)


def _load_json_object(path: Path, field: str, *, max_bytes: int) -> dict[str, Any]:
    if path.stat().st_size > max_bytes:
        raise SkillLayerError(f"{field} 超过大小上限")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise SkillLayerError(f"{field} 不是有效 UTF-8 JSON") from exc
    if not isinstance(value, dict):
        raise SkillLayerError(f"{field} 必须是 object")
    return value
