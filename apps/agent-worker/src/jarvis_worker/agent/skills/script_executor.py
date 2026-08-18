"""通用 Skill 脚本执行器；只能作为 ToolGateway executor 使用。"""

from __future__ import annotations

import hashlib
import json
import os
import signal
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

from jarvis_worker.agent.skills.contracts import SkillScriptDefinition
from jarvis_worker.agent.tool_gateway.contracts import ToolRequest, ToolResult


class SkillScriptExecutor:
    """执行已由 SkillLoader 校验、固定参数与固定哈希的 Python 脚本。"""

    def __init__(self, definition: SkillScriptDefinition) -> None:
        self._definition = definition
        self._bootstrap = Path(__file__).with_name("_script_bootstrap.py").resolve()

    def __call__(self, request: ToolRequest) -> ToolResult:
        input_bytes = self._serialize_input(request.arguments)
        if input_bytes is None:
            return self._error(
                "SKILL_SCRIPT_INPUT_INVALID",
                "Skill 脚本输入不是有效的有界 JSON",
                recoverable=True,
            )
        definition = self._definition
        if len(input_bytes) > definition.max_input_bytes:
            return self._error(
                "SKILL_SCRIPT_INPUT_TOO_LARGE",
                "Skill 脚本输入超过大小上限",
                recoverable=True,
            )
        if not self._script_integrity_matches():
            return self._error(
                "SKILL_SCRIPT_INTEGRITY_MISMATCH",
                "Skill 脚本与启动时校验的版本不一致，需要重启 Worker",
                recoverable=False,
            )

        with tempfile.TemporaryDirectory(prefix="jarvis-skill-script-") as temp_dir:
            input_path = Path(temp_dir) / "input.json"
            input_path.write_bytes(input_bytes)
            command = [
                sys.executable,
                "-I",
                str(self._bootstrap),
                "--script",
                str(definition.path),
                "--",
                *definition.entrypoint_args,
                "--input",
                str(input_path),
            ]
            try:
                process = subprocess.Popen(
                    command,
                    cwd=temp_dir,
                    env=_isolated_environment(),
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    start_new_session=True,
                )
                stdout, _stderr = process.communicate(
                    timeout=definition.timeout_seconds
                )
            except subprocess.TimeoutExpired:
                _terminate_process_group(process)
                process.communicate()
                return self._error(
                    "SKILL_SCRIPT_TIMEOUT",
                    "Skill 脚本执行超时",
                    recoverable=True,
                )
            except OSError:
                return self._error(
                    "SKILL_SCRIPT_START_FAILED",
                    "Skill 脚本无法启动",
                    recoverable=True,
                )

        if len(stdout) > definition.max_output_bytes:
            return self._error(
                "SKILL_SCRIPT_OUTPUT_TOO_LARGE",
                "Skill 脚本输出超过大小上限",
                recoverable=False,
            )
        if process.returncode != 0:
            return self._error(
                "SKILL_SCRIPT_FAILED",
                "Skill 脚本执行失败",
                recoverable=True,
            )
        output = _parse_json_object(stdout)
        if output is None:
            return self._error(
                "SKILL_SCRIPT_OUTPUT_INVALID",
                "Skill 脚本没有返回有效的 JSON object",
                recoverable=False,
            )
        if "_skill_script" in output:
            return self._error(
                "SKILL_SCRIPT_OUTPUT_INVALID",
                "Skill 脚本输出使用了 Runtime 保留字段",
                recoverable=False,
            )
        output["_skill_script"] = {
            "skill_id": definition.skill_id,
            "skill_version": definition.skill_version,
            "script_name": definition.script_name,
            "fingerprint": definition.fingerprint,
        }
        if len(_serialize_output(output)) > definition.max_output_bytes:
            return self._error(
                "SKILL_SCRIPT_OUTPUT_TOO_LARGE",
                "Skill 脚本输出超过大小上限",
                recoverable=False,
            )
        valid = output.get("valid")
        summary = (
            "Skill 结构校验通过"
            if valid is True
            else "Skill 结构校验未通过"
            if valid is False
            else "Skill 脚本执行完成"
        )
        return ToolResult(
            ok=True,
            kind="json",
            summary=summary,
            data=output,
            metadata={
                "skill_id": definition.skill_id,
                "skill_version": definition.skill_version,
                "script_name": definition.script_name,
                "script_fingerprint": definition.fingerprint,
            },
        )

    def _script_integrity_matches(self) -> bool:
        path = self._definition.path
        try:
            if path.is_symlink() or not path.is_file():
                return False
            current = hashlib.sha256(path.read_bytes()).hexdigest()
        except OSError:
            return False
        return current == self._definition.fingerprint

    @staticmethod
    def _serialize_input(arguments: dict[str, Any]) -> bytes | None:
        try:
            return json.dumps(
                arguments,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            ).encode("utf-8")
        except (TypeError, ValueError, UnicodeError):
            return None

    @staticmethod
    def _error(code: str, message: str, *, recoverable: bool) -> ToolResult:
        return ToolResult(
            ok=False,
            kind="empty",
            summary=message,
            error={
                "code": code,
                "message": message,
                "category": "tool",
                "recoverable": recoverable,
            },
        )


def _isolated_environment() -> dict[str, str]:
    return {
        "PATH": os.defpath,
        "PYTHONIOENCODING": "utf-8",
        "PYTHONUTF8": "1",
        "JARVIS_SKILL_NETWORK": "disabled",
    }


def _terminate_process_group(process: subprocess.Popen[bytes]) -> None:
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass


def _parse_json_object(raw: bytes) -> dict[str, Any] | None:
    def reject_constant(_value: str) -> None:
        raise ValueError("non-finite JSON number")

    def reject_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate JSON field")
            result[key] = value
        return result

    try:
        value = json.loads(
            raw.decode("utf-8"),
            parse_constant=reject_constant,
            object_pairs_hook=reject_duplicates,
        )
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def _serialize_output(value: dict[str, Any]) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
