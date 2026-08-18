"""Worker 配置 — 全部通过环境变量读取，无文件配置依赖。

环境变量：
  JARVIS_REDIS_ADDR               Redis 地址，默认 127.0.0.1:6379
  JARVIS_REDIS_PASSWORD           Redis 密码，可选；不得写入日志
  JARVIS_REDIS_DB                 Redis logical DB，默认 0；与 Gateway/Control Plane/RAG Worker 一致
  JARVIS_WORKER_ID                Worker 标识，默认 worker-01
  JARVIS_WORKER_GROUP             Consumer group，默认 jarvis:group:worker-pool
  JARVIS_WORKER_CONSUMER          Consumer 名称，默认与 JARVIS_WORKER_ID 相同
  JARVIS_WORKER_HEARTBEAT_INTERVAL_MS 心跳间隔毫秒，默认 3000
  JARVIS_RUN_QUEUE_RECLAIM_IDLE_MS RunJob 首次接管最小 idle，最低 65000
  JARVIS_RUN_QUEUE_RECLAIM_INTERVAL_MS 空闲 Worker 扫描 pending 的间隔
  JARVIS_RUN_QUEUE_MAX_DELIVERIES RunJob 最大投递次数，默认 3
  JARVIS_COMMAND_RECLAIM_IDLE_MS Worker command 首次接管最小 idle，默认 5000
  JARVIS_COMMAND_RECLAIM_INTERVAL_MS Worker command pending 扫描间隔
  JARVIS_WORKSPACE_ROOT           默认 workspace 根目录（ToolGateway MVP），默认空
  JARVIS_SKILLS_ROOT              已安装 Skill 包根目录，默认由启动装配定位仓库 skills/
  JARVIS_SKILL_ADAPTERS_ROOT      Jarvis Skill adapter 根目录，默认 <skills>/.jarvis
  JARVIS_ARTIFACT_ROOT            大产物文件根目录，默认 <workspace>/.local/artifacts
  JARVIS_ARTIFACT_INLINE_MAX_BYTES RuntimeEvent 内联正文上限，默认 8192
  JARVIS_ARTIFACT_MAX_FILE_BYTES  单个 Artifact 文件上限，默认 52428800
  JARVIS_ARTIFACT_MAX_RUN_BYTES   单 Run 本地 Artifact 上限，默认 262144000
  JARVIS_ARTIFACT_MAX_WORKSPACE_BYTES 单 Workspace 上限，默认 2147483648
  JARVIS_ARTIFACT_MAX_TOTAL_BYTES Artifact 本地总量上限，默认 10737418240
  JARVIS_MEMORY_EXTRACTION_ENABLED 是否在成功任务后异步提取候选，默认 true
  JARVIS_MEMORY_EXTRACTION_POLL_INTERVAL_MS PostgreSQL 作业轮询间隔，默认 1000
  JARVIS_MEMORY_EXTRACTION_MAX_ATTEMPTS 最大提取次数，默认 3
  JARVIS_MEMORY_EXTRACTION_STALE_SECONDS running 作业超时回收秒数，默认 300
  JARVIS_MODEL_PROVIDER           模型提供者: deepseek | custom_openai_compatible
  JARVIS_MODEL_BASE_URL           API base URL（DeepSeek 可省略并使用官方默认值）
  JARVIS_MODEL_NAME               模型名称
  JARVIS_MODEL_API_KEY_ENV        存放 API key 的环境变量名
                                  注意：WorkerConfig 只保存环境变量名，不保存密钥值
  JARVIS_MODEL_TIMEOUT_SECONDS    请求超时秒数，默认 120
  JARVIS_MODEL_MAX_RETRIES        最大重试次数（0-2），默认 1
  JARVIS_MODEL_MAX_TOKENS         max_tokens 参数，默认 4096
  JARVIS_MODEL_CONTEXT_WINDOW_TOKENS 模型上下文窗口，默认值 131072
  JARVIS_MODEL_THINKING_MODE      thinking 模式: "" | "disabled"，默认 ""（不发送扩展字段）
  JARVIS_AGENT_MAX_ITERATIONS     单个 Run 的工具调用预算，范围 1-20，默认 14
  JARVIS_TEST_FAULT_INJECTION_ENABLED 隔离验收专用故障注入总开关，默认 false
  JARVIS_TEST_TOOL_EFFECT_BARRIER_ROOT 获批工具 effect 前屏障目录，必须为绝对路径
  JARVIS_TEST_TOOL_EFFECT_BARRIER_TIMEOUT_SECONDS 屏障等待 release 的超时，默认 120 秒
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field

from jarvis_worker.agent.models.registry import (
    DEEPSEEK_PROVIDER,
    get_provider_spec,
    normalize_provider_id,
    resolve_base_url,
)
from jarvis_worker.shared.config.redis import redis_db_from_env, redis_password_from_env


@dataclass
class WorkerConfig:
    """Worker 运行时配置。"""

    redis_addr: str = "127.0.0.1:6379"
    redis_password: str = field(default="", repr=False)
    redis_db: int = 0
    worker_id: str = "worker-01"
    worker_group: str = "jarvis:group:worker-pool"
    worker_consumer: str = "worker-01"
    heartbeat_interval_ms: int = 3000
    run_queue_reclaim_idle_ms: int = 65_000
    run_queue_reclaim_interval_ms: int = 5_000
    run_queue_max_deliveries: int = 3
    command_reclaim_idle_ms: int = 5_000
    command_reclaim_interval_ms: int = 1_000
    workspace_root: str = ""  # 默认 workspace 根目录
    skills_root: str = ""
    skill_adapters_root: str = ""
    artifact_root: str = ""
    artifact_inline_max_bytes: int = 8 * 1024
    artifact_max_file_bytes: int = 50 * 1024 * 1024
    artifact_max_run_bytes: int = 250 * 1024 * 1024
    artifact_max_workspace_bytes: int = 2 * 1024 * 1024 * 1024
    artifact_max_total_bytes: int = 10 * 1024 * 1024 * 1024
    memory_extraction_enabled: bool = True
    memory_extraction_poll_interval_ms: int = 1_000
    memory_extraction_max_attempts: int = 3
    memory_extraction_stale_seconds: int = 300
    memory_candidate_expiry_poll_interval_ms: int = 60_000
    # -- 模型提供者配置（Phase 6B-1） --
    model_adapter: str = "langchain"  # langchain | direct（回退）
    model_provider: str = DEEPSEEK_PROVIDER
    model_base_url: str = ""
    model_name: str = ""
    model_api_key_env: str = ""  # 保存 API key 的环境变量名，不保存密钥值
    model_timeout_seconds: int = 120
    model_max_retries: int = 1  # 0-2
    model_max_tokens: int = 4096
    model_context_window_tokens: int = 131_072
    model_thinking_mode: str = ""  # "" | "disabled"
    agent_max_iterations: int = 14
    agent_max_run_seconds: int = 900
    test_fault_injection_enabled: bool = False
    test_tool_effect_barrier_root: str = ""
    test_tool_effect_barrier_timeout_seconds: int = 120

    @classmethod
    def from_env(cls) -> "WorkerConfig":
        """从环境变量加载配置。

        新增强制校验：模型数值配置出现非整数或超出范围时明确失败。
        兼容已有配置（Redis/heartbeat）的宽松行为不变。
        """
        hb_interval = _parse_int_lenient("JARVIS_WORKER_HEARTBEAT_INTERVAL_MS", 3000, min_val=100)
        timeout = _parse_int_strict("JARVIS_MODEL_TIMEOUT_SECONDS", 120, min_val=1, max_val=600)
        max_retries = _parse_int_strict("JARVIS_MODEL_MAX_RETRIES", 1, min_val=0, max_val=2)
        max_tokens = _parse_int_strict("JARVIS_MODEL_MAX_TOKENS", 4096, min_val=1, max_val=131_072)
        context_window_tokens = _parse_int_strict(
            "JARVIS_MODEL_CONTEXT_WINDOW_TOKENS",
            131_072,
            min_val=2_048,
            max_val=2_000_000,
        )
        agent_max_iterations = _parse_int_strict(
            "JARVIS_AGENT_MAX_ITERATIONS", 14, min_val=1, max_val=20
        )
        agent_max_run_seconds = _parse_int_strict(
            "JARVIS_AGENT_MAX_RUN_SECONDS", 900, min_val=30, max_val=86_400
        )
        test_fault_injection_enabled = _parse_bool_strict(
            "JARVIS_TEST_FAULT_INJECTION_ENABLED", False
        )
        test_tool_effect_barrier_root = os.getenv(
            "JARVIS_TEST_TOOL_EFFECT_BARRIER_ROOT", ""
        ).strip()
        if test_fault_injection_enabled:
            if not test_tool_effect_barrier_root:
                raise ValueError(
                    "JARVIS_TEST_FAULT_INJECTION_ENABLED=true 时必须设置 "
                    "JARVIS_TEST_TOOL_EFFECT_BARRIER_ROOT"
                )
            if not os.path.isabs(test_tool_effect_barrier_root):
                raise ValueError("JARVIS_TEST_TOOL_EFFECT_BARRIER_ROOT 必须是绝对路径")
        elif test_tool_effect_barrier_root:
            raise ValueError(
                "设置 JARVIS_TEST_TOOL_EFFECT_BARRIER_ROOT 前必须显式启用 "
                "JARVIS_TEST_FAULT_INJECTION_ENABLED"
            )
        if context_window_tokens <= max_tokens + 1024:
            raise ValueError(
                "JARVIS_MODEL_CONTEXT_WINDOW_TOKENS 必须大于 JARVIS_MODEL_MAX_TOKENS + 1024"
            )
        artifact_max_file_bytes = _parse_int_strict(
            "JARVIS_ARTIFACT_MAX_FILE_BYTES",
            50 * 1024 * 1024,
            min_val=1_024,
            max_val=100 * 1024 * 1024,
        )
        artifact_max_run_bytes = _parse_int_strict(
            "JARVIS_ARTIFACT_MAX_RUN_BYTES",
            250 * 1024 * 1024,
            min_val=1_024,
            max_val=10 * 1024 * 1024 * 1024,
        )
        artifact_max_workspace_bytes = _parse_int_strict(
            "JARVIS_ARTIFACT_MAX_WORKSPACE_BYTES",
            2 * 1024 * 1024 * 1024,
            min_val=1_024,
            max_val=100 * 1024 * 1024 * 1024,
        )
        artifact_max_total_bytes = _parse_int_strict(
            "JARVIS_ARTIFACT_MAX_TOTAL_BYTES",
            10 * 1024 * 1024 * 1024,
            min_val=1_024,
            max_val=500 * 1024 * 1024 * 1024,
        )
        if not (
            artifact_max_file_bytes
            <= artifact_max_run_bytes
            <= artifact_max_workspace_bytes
            <= artifact_max_total_bytes
        ):
            raise ValueError("Artifact 容量必须满足 file <= run <= workspace <= total")

        worker_id = os.getenv("JARVIS_WORKER_ID", "worker-01")
        configured_base_url = os.getenv("JARVIS_MODEL_BASE_URL", "")
        model_name = os.getenv("JARVIS_MODEL_NAME", "")
        provider = _validate_model_provider(
            os.getenv("JARVIS_MODEL_PROVIDER", DEEPSEEK_PROVIDER),
            base_url=configured_base_url,
            model_name=model_name,
        )
        model_adapter = _validate_model_adapter(os.getenv("JARVIS_MODEL_ADAPTER", "langchain"))
        thinking_mode = _validate_thinking_mode(os.getenv("JARVIS_MODEL_THINKING_MODE", ""))
        _validate_provider_options(provider, thinking_mode)
        return cls(
            redis_addr=os.getenv("JARVIS_REDIS_ADDR", "127.0.0.1:6379"),
            redis_password=redis_password_from_env(),
            redis_db=redis_db_from_env(),
            worker_id=worker_id,
            worker_group=os.getenv("JARVIS_WORKER_GROUP", "jarvis:group:worker-pool"),
            worker_consumer=os.getenv("JARVIS_WORKER_CONSUMER", worker_id),
            heartbeat_interval_ms=hb_interval,
            run_queue_reclaim_idle_ms=_parse_int_lenient(
                "JARVIS_RUN_QUEUE_RECLAIM_IDLE_MS",
                65_000,
                min_val=65_000,
                max_val=3_600_000,
            ),
            run_queue_reclaim_interval_ms=_parse_int_lenient(
                "JARVIS_RUN_QUEUE_RECLAIM_INTERVAL_MS",
                5_000,
                min_val=1_000,
                max_val=60_000,
            ),
            run_queue_max_deliveries=_parse_int_lenient(
                "JARVIS_RUN_QUEUE_MAX_DELIVERIES",
                3,
                min_val=1,
                max_val=10,
            ),
            command_reclaim_idle_ms=_parse_int_lenient(
                "JARVIS_COMMAND_RECLAIM_IDLE_MS",
                5_000,
                min_val=1_000,
                max_val=300_000,
            ),
            command_reclaim_interval_ms=_parse_int_lenient(
                "JARVIS_COMMAND_RECLAIM_INTERVAL_MS",
                1_000,
                min_val=500,
                max_val=60_000,
            ),
            workspace_root=os.getenv("JARVIS_WORKSPACE_ROOT", ""),
            skills_root=os.getenv("JARVIS_SKILLS_ROOT", ""),
            skill_adapters_root=os.getenv("JARVIS_SKILL_ADAPTERS_ROOT", ""),
            artifact_root=os.getenv("JARVIS_ARTIFACT_ROOT", ""),
            artifact_inline_max_bytes=_parse_int_strict(
                "JARVIS_ARTIFACT_INLINE_MAX_BYTES",
                8 * 1024,
                min_val=1,
                max_val=1024 * 1024,
            ),
            artifact_max_file_bytes=artifact_max_file_bytes,
            artifact_max_run_bytes=artifact_max_run_bytes,
            artifact_max_workspace_bytes=artifact_max_workspace_bytes,
            artifact_max_total_bytes=artifact_max_total_bytes,
            memory_extraction_enabled=_parse_bool_strict("JARVIS_MEMORY_EXTRACTION_ENABLED", True),
            memory_extraction_poll_interval_ms=_parse_int_strict(
                "JARVIS_MEMORY_EXTRACTION_POLL_INTERVAL_MS",
                1_000,
                min_val=250,
                max_val=60_000,
            ),
            memory_extraction_max_attempts=_parse_int_strict(
                "JARVIS_MEMORY_EXTRACTION_MAX_ATTEMPTS",
                3,
                min_val=1,
                max_val=10,
            ),
            memory_extraction_stale_seconds=_parse_int_strict(
                "JARVIS_MEMORY_EXTRACTION_STALE_SECONDS",
                300,
                min_val=60,
                max_val=3_600,
            ),
            memory_candidate_expiry_poll_interval_ms=_parse_int_strict(
                "JARVIS_MEMORY_CANDIDATE_EXPIRY_POLL_INTERVAL_MS",
                60_000,
                min_val=1_000,
                max_val=3_600_000,
            ),
            model_adapter=model_adapter,
            model_provider=provider,
            model_base_url=resolve_base_url(provider, configured_base_url),
            model_name=model_name,
            model_api_key_env=os.getenv("JARVIS_MODEL_API_KEY_ENV", ""),
            model_timeout_seconds=timeout,
            model_max_retries=max_retries,
            model_max_tokens=max_tokens,
            model_context_window_tokens=context_window_tokens,
            model_thinking_mode=thinking_mode,
            agent_max_iterations=agent_max_iterations,
            agent_max_run_seconds=agent_max_run_seconds,
            test_fault_injection_enabled=test_fault_injection_enabled,
            test_tool_effect_barrier_root=test_tool_effect_barrier_root,
            test_tool_effect_barrier_timeout_seconds=_parse_int_strict(
                "JARVIS_TEST_TOOL_EFFECT_BARRIER_TIMEOUT_SECONDS",
                120,
                min_val=1,
                max_val=600,
            ),
        )


def _parse_int_lenient(
    env_var: str,
    default: int,
    min_val: int | None = None,
    max_val: int | None = None,
) -> int:
    """宽松解析整数环境变量（兼容已有 Redis/heartbeat 配置）。"""
    v = os.getenv(env_var)
    if v is None:
        return default
    try:
        val = int(v)
    except ValueError:
        return default
    if min_val is not None and val < min_val:
        val = min_val
    if max_val is not None and val > max_val:
        val = max_val
    return val


def _parse_bool_strict(env_var: str, default: bool) -> bool:
    value = os.getenv(env_var)
    if value is None:
        return default
    normalized = value.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"{env_var} 必须是 true 或 false，当前: {value!r}")


def _parse_int_strict(
    env_var: str,
    default: int,
    min_val: int | None = None,
    max_val: int | None = None,
) -> int:
    """严格解析整数环境变量（新增模型配置）。

    非整数或超出范围时明确失败，不静默回退。
    """
    v = os.getenv(env_var)
    if v is None:
        return default
    try:
        val = int(v)
    except (ValueError, TypeError):
        raise ValueError(f"{env_var} 必须是整数，当前: {v!r}") from None
    if min_val is not None and val < min_val:
        raise ValueError(f"{env_var} 不能小于 {min_val}，当前: {val}")
    if max_val is not None and val > max_val:
        raise ValueError(f"{env_var} 不能大于 {max_val}，当前: {val}")
    return val


def _validate_model_provider(
    raw: str,
    *,
    base_url: str = "",
    model_name: str = "",
) -> str:
    """校验并归一化 JARVIS_MODEL_PROVIDER。"""
    return normalize_provider_id(
        raw,
        base_url=base_url,
        model_name=model_name,
    )


def _validate_model_adapter(raw: str) -> str:
    """校验模型调用实现；direct 只作为迁移回退路径。"""
    cleaned = raw.strip().lower()
    allowed = {"langchain", "direct"}
    if cleaned not in allowed:
        raise ValueError(f"JARVIS_MODEL_ADAPTER 非法值: {raw!r}，允许: {sorted(allowed)}")
    return cleaned


def _validate_thinking_mode(raw: str) -> str:
    """校验 JARVIS_MODEL_THINKING_MODE。"""
    cleaned = raw.strip().lower()
    allowed = {"", "disabled"}
    if cleaned not in allowed:
        raise ValueError(f"JARVIS_MODEL_THINKING_MODE 非法值: {raw!r}，允许: {sorted(allowed)}")
    return cleaned


def _validate_provider_options(provider: str, thinking_mode: str) -> None:
    """供应商扩展不得泄漏到通用协议 Provider。"""
    if thinking_mode and not get_provider_spec(provider).supports_thinking_mode:
        raise ValueError("JARVIS_MODEL_THINKING_MODE 当前仅支持 deepseek provider")
