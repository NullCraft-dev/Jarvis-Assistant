#!/usr/bin/env python3
"""首次启动前的安全、结构化环境自检。"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import socket
import stat
import subprocess
import tempfile
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path

SCHEMA_VERSION = "1.0"
VALID_STATUSES = {"passed", "warning", "failed"}
REQUIRED_IMPORTS = (
    "alembic, asyncpg, fastapi, langchain_core, langchain_deepseek, "
    "langchain_openai, redis, sqlalchemy, uvicorn, jarvis_worker"
)


@dataclass(frozen=True)
class PreflightCheck:
    id: str
    category: str
    status: str
    blocking: bool
    summary: str
    remediation: str | None = None

    def __post_init__(self) -> None:
        if self.status not in VALID_STATUSES:
            raise ValueError(f"非法自检状态: {self.status}")
        if self.status == "failed" and not self.blocking:
            raise ValueError("failed 检查必须阻断启动")


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def utc_stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def run_quiet(command: list[str], *, cwd: Path, timeout: int = 20) -> bool:
    try:
        completed = subprocess.run(
            command,
            cwd=cwd,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return completed.returncode == 0


def parse_dotenv_keys(path: Path) -> dict[str, str]:
    """只读取 key/value presence，不执行插值或 shell 语法。"""
    values: dict[str, str] = {}
    if not path.is_file():
        return values
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return values
    for raw_line in lines:
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if not key or not (key[0].isalpha() or key[0] == "_"):
            continue
        if not all(character.isalnum() or character == "_" for character in key):
            continue
        cleaned = value.strip()
        if len(cleaned) >= 2 and cleaned[0] == cleaned[-1] and cleaned[0] in {'"', "'"}:
            cleaned = cleaned[1:-1]
        values[key] = cleaned
    return values


def effective_value(name: str, dotenv: dict[str, str], default: str = "") -> str:
    return os.environ.get(name, dotenv.get(name, default)).strip()


def port_is_free(host: str, port: int) -> bool:
    family = socket.AF_INET6 if ":" in host else socket.AF_INET
    try:
        with socket.socket(family, socket.SOCK_STREAM) as probe:
            probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            probe.bind((host, port))
    except OSError:
        return False
    return True


def nearest_existing_parent(path: Path) -> Path | None:
    candidate = path
    while not candidate.exists():
        parent = candidate.parent
        if parent == candidate:
            return None
        candidate = parent
    return candidate


class DevPreflight:
    def __init__(self, repo: Path, conda_env: str, *, skip_ports: bool = False) -> None:
        self.repo = repo.resolve()
        self.agent_dir = self.repo / "apps" / "agent-worker"
        self.conda_env = conda_env
        self.skip_ports = skip_ports
        self.dotenv_path = self.agent_dir / ".env"
        self.dotenv = parse_dotenv_keys(self.dotenv_path)
        self.checks: list[PreflightCheck] = []

    def add(
        self,
        check_id: str,
        category: str,
        status: str,
        summary: str,
        remediation: str | None = None,
    ) -> None:
        self.checks.append(
            PreflightCheck(
                id=check_id,
                category=category,
                status=status,
                blocking=status == "failed",
                summary=summary,
                remediation=remediation,
            )
        )

    def check_commands(self) -> bool:
        missing = [
            name for name in ("docker", "conda", "go", "npm", "curl") if not shutil.which(name)
        ]
        if missing:
            self.add(
                "system.commands",
                "system",
                "failed",
                "缺少启动所需的系统工具",
                f"请先安装：{', '.join(missing)}",
            )
            return False
        self.add("system.commands", "system", "passed", "系统工具已就绪")
        return True

    def check_project_files(self) -> None:
        required = (
            "compose.yaml",
            "apps/agent-worker/pyproject.toml",
            "apps/gateway/go.mod",
            "apps/web/package.json",
            "packages/shared/package.json",
        )
        missing = [relative for relative in required if not (self.repo / relative).is_file()]
        if missing:
            self.add(
                "project.manifests",
                "project",
                "failed",
                "项目文件不完整",
                "请恢复缺失的受版本控制文件后重试",
            )
            return
        self.add("project.manifests", "project", "passed", "项目清单文件完整")

    def check_docker(self, commands_ready: bool) -> None:
        if not commands_ready:
            return
        if run_quiet(["docker", "compose", "version"], cwd=self.repo, timeout=10):
            self.add("system.docker_compose", "system", "passed", "Docker Compose 可用")
        else:
            self.add(
                "system.docker_compose",
                "system",
                "failed",
                "Docker Compose 不可用",
                "请启动 Docker Desktop 并确认 docker compose version 可执行",
            )
            return
        if run_quiet(["docker", "info"], cwd=self.repo, timeout=10):
            self.add("system.docker_daemon", "system", "passed", "Docker 服务已启动")
        else:
            self.add(
                "system.docker_daemon",
                "system",
                "failed",
                "Docker 服务未启动或当前用户无法访问",
                "请启动 Docker Desktop 后重试",
            )

    def check_conda(self, commands_ready: bool) -> bool:
        if not commands_ready or not shutil.which("conda"):
            return False
        imports = f"import {REQUIRED_IMPORTS}"
        if run_quiet(
            ["conda", "run", "-n", self.conda_env, "python", "-c", imports],
            cwd=self.agent_dir,
            timeout=30,
        ):
            self.add("runtime.conda", "runtime", "passed", "Python Runtime 依赖已就绪")
            return True
        self.add(
            "runtime.conda",
            "runtime",
            "failed",
            "Python Runtime 环境不存在或依赖不完整",
            "请运行 scripts/dev.sh setup",
        )
        return False

    def check_node_dependencies(self) -> None:
        missing = [
            label
            for label, path in (
                ("Web", self.repo / "apps" / "web" / "node_modules"),
                ("Shared", self.repo / "packages" / "shared" / "node_modules"),
            )
            if not path.is_dir()
        ]
        if missing:
            self.add(
                "runtime.node_dependencies",
                "runtime",
                "failed",
                "JavaScript 依赖尚未安装",
                f"请运行 scripts/dev.sh setup（缺少：{', '.join(missing)}）",
            )
            return
        valid = all(
            run_quiet(["npm", "ls", "--depth=0"], cwd=directory, timeout=30)
            for directory in (
                self.repo / "apps" / "web",
                self.repo / "packages" / "shared",
            )
        )
        if not valid:
            self.add(
                "runtime.node_dependencies",
                "runtime",
                "failed",
                "JavaScript 依赖与锁文件不一致",
                "请运行 scripts/dev.sh setup",
            )
            return
        self.add("runtime.node_dependencies", "runtime", "passed", "JavaScript 依赖已就绪")

    def check_go_dependencies(self, commands_ready: bool) -> None:
        if not commands_ready or not shutil.which("go"):
            return
        if run_quiet(
            ["go", "list", "-mod=readonly", "-m", "all"],
            cwd=self.repo / "apps" / "gateway",
            timeout=30,
        ):
            self.add("runtime.go_dependencies", "runtime", "passed", "Go 依赖已就绪")
        else:
            self.add(
                "runtime.go_dependencies",
                "runtime",
                "failed",
                "Go 依赖不完整或 go.mod/go.sum 不一致",
                "请运行 scripts/dev.sh setup",
            )

    def check_runtime_config(self, conda_ready: bool) -> None:
        if not self.dotenv_path.is_file():
            self.add(
                "config.env_file",
                "config",
                "warning",
                "本地 .env 不存在，将只使用外部环境变量",
                "可运行 scripts/dev.sh setup 创建模板",
            )
        else:
            self.add("config.env_file", "config", "passed", "本地配置文件可读取")
            try:
                permissions = stat.S_IMODE(self.dotenv_path.stat().st_mode)
            except OSError:
                permissions = 0o777
            if permissions & 0o077:
                self.add(
                    "config.env_permissions",
                    "config",
                    "warning",
                    "本地配置文件权限过宽",
                    "建议执行 chmod 600 apps/agent-worker/.env",
                )
            else:
                self.add(
                    "config.env_permissions",
                    "config",
                    "passed",
                    "本地配置文件权限已收紧",
                )

        if not conda_ready:
            return
        validation_code = """
from jarvis_worker.agent.models.provider_config import check_api_key_exists, validate_provider_config
from jarvis_worker.agent.rag.worker.config import RagWorkerConfig
from jarvis_worker.shared.config.env_loader import load_default_local_env
from jarvis_worker.shared.config.settings import WorkerConfig
load_default_local_env()
cfg = WorkerConfig.from_env()
validate_provider_config(cfg.model_base_url, cfg.model_name, cfg.model_api_key_env, cfg.model_max_retries)
check_api_key_exists(cfg.model_api_key_env)
rag = RagWorkerConfig.from_env()
check_api_key_exists(rag.embedding.rag_embedding_api_key_env)
"""
        if run_quiet(
            [
                "conda",
                "run",
                "-n",
                self.conda_env,
                "python",
                "-c",
                validation_code,
            ],
            cwd=self.agent_dir,
            timeout=30,
        ):
            self.add(
                "config.runtime",
                "config",
                "passed",
                "模型与 RAG 配置通过生产规则校验",
            )
        else:
            self.add(
                "config.runtime",
                "config",
                "failed",
                "模型或 RAG 配置不完整",
                "请检查模型名称、Base URL、命名密钥变量及 RAG Embedding 配置；自检不会显示密钥值",
            )

    def check_paths(self) -> None:
        workspace = Path(
            effective_value("JARVIS_WORKSPACE_ROOT", self.dotenv, str(self.repo))
        ).expanduser()
        if not workspace.is_absolute():
            self.add(
                "storage.workspace",
                "storage",
                "failed",
                "默认工作区必须使用绝对路径",
                "请设置 JARVIS_WORKSPACE_ROOT 为可读写的绝对目录",
            )
        elif not workspace.is_dir() or not os.access(workspace, os.R_OK | os.W_OK):
            self.add(
                "storage.workspace",
                "storage",
                "failed",
                "默认工作区不存在或不可读写",
                "请创建目录并授予当前用户读写权限",
            )
        else:
            self.add("storage.workspace", "storage", "passed", "默认工作区可读写")

        allowed_raw = effective_value("JARVIS_ALLOWED_WORKSPACE_PATHS", self.dotenv, str(workspace))
        allowed = [Path(item).expanduser() for item in allowed_raw.split(os.pathsep) if item]
        allowed_valid = 1 <= len(allowed) <= 32 and all(
            path.is_absolute() and path.is_dir() and os.access(path, os.R_OK) for path in allowed
        )
        workspace_allowed = False
        if workspace.is_absolute() and workspace.exists() and allowed_valid:
            workspace_resolved = workspace.resolve()
            for allowed_path in allowed:
                try:
                    if os.path.commonpath((workspace_resolved, allowed_path.resolve())) == str(
                        allowed_path.resolve()
                    ):
                        workspace_allowed = True
                        break
                except ValueError:
                    continue
        if not allowed_valid or not workspace_allowed:
            self.add(
                "storage.allowed_workspaces",
                "storage",
                "failed",
                "允许的工作区范围无效或不包含默认工作区",
                "请设置 1-32 个可读绝对目录，并确保默认工作区位于其中",
            )
        else:
            self.add(
                "storage.allowed_workspaces",
                "storage",
                "passed",
                "工作区允许范围有效",
            )

        for check_id, env_name, default, label in (
            (
                "storage.artifacts",
                "JARVIS_ARTIFACT_ROOT",
                str(self.repo / ".local" / "artifacts"),
                "Artifact 目录",
            ),
            (
                "storage.rag_assets",
                "JARVIS_RAG_ASSET_ROOT",
                str(self.repo / ".local" / "rag-assets"),
                "RAG Asset 目录",
            ),
        ):
            path = Path(effective_value(env_name, self.dotenv, default)).expanduser()
            parent = nearest_existing_parent(path)
            if not path.is_absolute() or parent is None or not os.access(parent, os.W_OK):
                self.add(
                    check_id,
                    "storage",
                    "failed",
                    f"{label}无法安全创建",
                    f"请把 {env_name} 设置为可写绝对路径",
                )
            else:
                self.add(check_id, "storage", "passed", f"{label}可用")

    def check_optional_runtimes(self) -> None:
        runtime_root = self.repo / ".local" / "rag-runtimes"
        vlm_mode = effective_value("JARVIS_LOCAL_VLM_ENABLED", self.dotenv, "auto").lower()
        reranker_mode = effective_value(
            "JARVIS_LOCAL_RERANKER_ENABLED", self.dotenv, "auto"
        ).lower()
        self._check_optional_runtime(
            "optional.mlx_vlm",
            "MLX-VLM",
            vlm_mode,
            (runtime_root / "mlx-vlm" / ".venv" / "bin" / "mlx_vlm.server").is_file(),
            "按 docs/16-dev-runtime-runbook.md 安装本地 MLX-VLM",
        )
        self._check_optional_runtime(
            "optional.reranker",
            "BGE Reranker",
            reranker_mode,
            (runtime_root / "bge-reranker" / ".ready").is_file(),
            "运行 scripts/rag/setup-bge-reranker.sh",
        )

    def _check_optional_runtime(
        self,
        check_id: str,
        label: str,
        mode: str,
        installed: bool,
        setup_instruction: str,
    ) -> None:
        if mode not in {"auto", "true", "false", "1", "0", "yes", "no", "on", "off"}:
            self.add(
                check_id,
                "optional",
                "failed",
                f"{label} 启用模式无效",
                "只允许 auto、true 或 false",
            )
        elif mode in {"true", "1", "yes", "on"} and not installed:
            self.add(
                check_id,
                "optional",
                "failed",
                f"{label} 已要求启用但尚未安装",
                f"请{setup_instruction}，或显式设为 false",
            )
        elif mode == "auto" and not installed:
            self.add(
                check_id,
                "optional",
                "warning",
                f"{label} 未安装，将使用降级能力",
                f"如需该能力，请{setup_instruction}",
            )
        else:
            self.add(check_id, "optional", "passed", f"{label} 配置可用")

    def check_ports(self) -> None:
        if self.skip_ports:
            self.add(
                "network.ports",
                "network",
                "warning",
                "已跳过应用端口占用检查",
            )
            return
        host = effective_value("JARVIS_DEV_HOST", self.dotenv, "127.0.0.1")
        raw_ports = {
            "Control Plane": effective_value("JARVIS_CONTROL_PLANE_PORT", self.dotenv, "8100"),
            "Gateway": "8080",
            "Web": effective_value("JARVIS_WEB_PORT", self.dotenv, "5173"),
        }
        runtime_root = self.repo / ".local" / "rag-runtimes"
        vlm_mode = effective_value("JARVIS_LOCAL_VLM_ENABLED", self.dotenv, "auto").lower()
        reranker_mode = effective_value(
            "JARVIS_LOCAL_RERANKER_ENABLED", self.dotenv, "auto"
        ).lower()
        if vlm_mode not in {"false", "0", "no", "off"} and (
            vlm_mode != "auto"
            or (runtime_root / "mlx-vlm" / ".venv" / "bin" / "mlx_vlm.server").is_file()
        ):
            raw_ports["MLX-VLM"] = "8111"
        if reranker_mode not in {"false", "0", "no", "off"} and (
            reranker_mode != "auto" or (runtime_root / "bge-reranker" / ".ready").is_file()
        ):
            raw_ports["BGE Reranker"] = effective_value(
                "JARVIS_RAG_RERANKER_PORT", self.dotenv, "8121"
            )
        occupied: list[str] = []
        try:
            for label, raw_port in raw_ports.items():
                port = int(raw_port)
                if port < 1 or port > 65535 or not port_is_free(host, port):
                    occupied.append(f"{label}({raw_port})")
        except (ValueError, OSError):
            occupied.append("端口配置")
        if occupied:
            self.add(
                "network.ports",
                "network",
                "failed",
                "应用端口已占用或配置无效",
                f"请停止旧服务或修正配置：{', '.join(occupied)}",
            )
        else:
            self.add("network.ports", "network", "passed", "应用端口可用")

    def run(self) -> dict[str, object]:
        started_at = utc_now()
        commands_ready = self.check_commands()
        self.check_project_files()
        self.check_docker(commands_ready)
        conda_ready = self.check_conda(commands_ready)
        self.check_node_dependencies()
        self.check_go_dependencies(commands_ready)
        self.check_runtime_config(conda_ready)
        self.check_paths()
        self.check_optional_runtimes()
        self.check_ports()
        failed = [check.id for check in self.checks if check.status == "failed"]
        warnings = [check.id for check in self.checks if check.status == "warning"]
        status = "blocked" if failed else ("degraded" if warnings else "ready")
        return {
            "schema_version": SCHEMA_VERSION,
            "report_id": "jarvis-dev-preflight",
            "status": status,
            "started_at": started_at,
            "finished_at": utc_now(),
            "failed_checks": failed,
            "warning_checks": warnings,
            "checks": [asdict(check) for check in self.checks],
        }


def write_report(report: dict[str, object], output: Path) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_suffix(f"{output.suffix}.tmp")
    temporary.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(output)


def print_human(report: dict[str, object], output: Path) -> None:
    symbols = {"passed": "OK", "warning": "WARN", "failed": "FAIL"}
    print(f"[preflight] status={report['status']}")
    for raw_check in report["checks"]:
        check = dict(raw_check)
        print(f"[preflight] {symbols[str(check['status'])]:4} {check['summary']}")
        if check.get("remediation") and check["status"] != "passed":
            print(f"[preflight]      处理：{check['remediation']}")
    print(f"[preflight] report={output}")


def self_test() -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        env_file = root / ".env"
        env_file.write_text(
            "# comment\nSAFE_KEY=value\nSECRET_KEY='not-printed'\nINVALID-KEY=x\n",
            encoding="utf-8",
        )
        parsed = parse_dotenv_keys(env_file)
        if parsed != {"SAFE_KEY": "value", "SECRET_KEY": "not-printed"}:
            raise AssertionError("dotenv parser self-test failed")
        check = PreflightCheck("test", "test", "passed", False, "ok")
        if asdict(check)["status"] != "passed":
            raise AssertionError("check self-test failed")
        report = {
            "schema_version": SCHEMA_VERSION,
            "status": "ready",
            "checks": [asdict(check)],
        }
        serialized = json.dumps(report)
        if "not-printed" in serialized:
            raise AssertionError("preflight report leaked dotenv value")
        preflight = DevPreflight(root, "test")
        preflight._check_optional_runtime(
            "optional.auto", "Optional", "auto", False, "安装 Optional"
        )
        preflight._check_optional_runtime(
            "optional.required", "Required", "true", False, "安装 Required"
        )
        statuses = [item.status for item in preflight.checks]
        if statuses != ["warning", "failed"]:
            raise AssertionError("optional runtime severity self-test failed")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", type=Path, default=Path(__file__).resolve().parent.parent)
    parser.add_argument("--conda-env", default=os.getenv("JARVIS_CONDA_ENV", "jarvis-assistant"))
    parser.add_argument("--output", type=Path)
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--skip-port-check", action="store_true")
    parser.add_argument("--self-test", action="store_true")
    args = parser.parse_args()
    if args.self_test:
        self_test()
        print("dev preflight self-test passed")
        return 0

    output = args.output
    if output is None:
        base = Path(
            os.getenv(
                "JARVIS_PREFLIGHT_OUTPUT_DIR",
                str(args.repo / ".local" / "preflight"),
            )
        )
        output = base / utc_stamp() / "report.json"
    report = DevPreflight(args.repo, args.conda_env, skip_ports=args.skip_port_check).run()
    write_report(report, output)
    if args.json:
        print(json.dumps(report, ensure_ascii=False))
    else:
        print_human(report, output)
    return 1 if report["status"] == "blocked" else 0


if __name__ == "__main__":
    raise SystemExit(main())
