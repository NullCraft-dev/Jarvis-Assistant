"""模型配置投影与连通性测试。

- build_model_config(): 从环境变量读取 WorkerConfig，投影为安全 DTO
- sanitize_base_url(): URL 脱敏（移除 userinfo/query/fragment）
- test_model_connection(): 短超时连通性测试 + AuditLog 写入（通过 Application Service）

安全约束：
- 绝不返回/记录 API key、Authorization header、原始 HTTP body、prompt 或模型原始响应。
- base_url_display 不包含 userinfo、query、fragment；非法值返回安全错误标记。
- 测试请求超时 5s、不重试、固定最小 prompt（max_tokens=1）。
- AuditLog 只记录安全摘要（provider/model/latency/error_code）。
- Provider 身份由 WorkerConfig/Provider Registry 归一化，协议实现不泄漏为供应商名。
"""

from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass
from typing import Callable
from urllib.parse import urlparse

import httpx

from jarvis_worker.shared.config.env_loader import load_default_local_env
from jarvis_worker.shared.config.settings import WorkerConfig
from jarvis_worker.agent.models.registry import get_provider_spec

log = logging.getLogger(__name__)

# 连通性测试硬编码常量
TEST_TIMEOUT_SECONDS = 5.0
TEST_MAX_TOKENS = 1

@dataclass
class ModelConfigProjection:
    """模型配置安全投影。"""
    provider: str
    protocol: str
    model_name: str
    base_url_display: str
    api_key_configured: bool
    timeout_seconds: int
    max_retries: int
    max_tokens: int
    thinking_mode: str
    worker_status: str = "unknown"
    last_heartbeat_at: str | None = None
    last_error_code: str | None = None


@dataclass
class ModelTestResult:
    """连通性测试结果。"""
    provider: str
    model: str
    latency_ms: float
    tested_at: str
    status: str  # "ok" | "failed"
    error_code: str | None = None
    error_message: str | None = None
    error_category: str | None = None
    error_recoverable: bool = False


# ── 共享配置加载边界 ────────────────────────────────────────────────


def ensure_config_loaded() -> None:
    """确保 .env 已加载，使 Control Plane 与 Worker 使用同一套配置源。

    幂等：多次调用不会覆盖已有环境变量（override=False）。
    外部注入优先于 .env。
    """
    load_default_local_env()


# ── URL 校验（复用生产 ModelProvider 规则）─────────────────────────


def _validate_model_base_url(raw_url: str) -> str:
    """校验并规范化 base URL。复用 OpenAiCompatibleModelProvider 规则。

    Returns:
        规范化后的 base URL（已 strip + rstrip "/"）。

    Raises:
        ValueError: 非法 URL（含 userinfo/query/fragment、非 http/https、无 hostname）。
    """
    if not raw_url or not raw_url.strip():
        raise ValueError("JARVIS_MODEL_BASE_URL 缺失或为空")

    cleaned = raw_url.strip()
    parsed = urlparse(cleaned)

    if parsed.scheme not in ("http", "https"):
        raise ValueError(f"JARVIS_MODEL_BASE_URL 必须是 http/https，当前: {parsed.scheme!r}")

    if not parsed.hostname:
        raise ValueError(f"JARVIS_MODEL_BASE_URL 缺少 hostname: {cleaned!r}")

    if parsed.username or parsed.password:
        raise ValueError("JARVIS_MODEL_BASE_URL 不允许包含用户名或密码")

    if parsed.query or parsed.fragment:
        raise ValueError("JARVIS_MODEL_BASE_URL 不允许包含 query 或 fragment")

    # 端口校验：若提供了端口，必须为合法整数且范围 1-65535
    try:
        port_val = parsed.port
    except ValueError as e:
        raise ValueError(f"JARVIS_MODEL_BASE_URL 端口非法: {e}") from None
    if port_val is not None:
        if not isinstance(port_val, int) or port_val < 1 or port_val > 65535:
            raise ValueError(f"JARVIS_MODEL_BASE_URL 端口非法: {port_val}")

    return cleaned.rstrip("/")


def validate_model_config_for_test() -> tuple[str, str, str]:
    """校验并返回 (normalized_base_url, model_name, api_key)。

    复用生产 _validate_model_base_url 规则。
    外部环境变量优先于 .env（ensure_config_loaded 已调用）。

    Raises:
        SystemExit: 配置不合法时直接退出（与 Worker 行为一致，startup fail-closed）。
    """
    cfg = WorkerConfig.from_env()

    try:
        normalized_url = _validate_model_base_url(cfg.model_base_url)
    except ValueError:
        raise

    if not cfg.model_name or not cfg.model_name.strip():
        raise ValueError("JARVIS_MODEL_NAME 缺失或为空")

    api_key_env = cfg.model_api_key_env.strip()
    if not api_key_env:
        raise ValueError("JARVIS_MODEL_API_KEY_ENV 缺失或为空")

    api_key = os.environ.get(api_key_env, "").strip()
    if not api_key:
        raise ValueError(f"API key 环境变量 {api_key_env} 未设置或为空")

    return normalized_url, cfg.model_name.strip(), api_key


# ── URL 脱敏 ─────────────────────────────────────────────────────


def sanitize_base_url(raw: str) -> str:
    """返回安全的 base URL 展示值。

    移除 userinfo、query、fragment，只保留 scheme://host[:port][/path]。
    非法 scheme、无 hostname 或非法端口时返回安全错误标记，不抛异常。
    """
    if not raw or not raw.strip():
        return ""
    cleaned = raw.strip()
    try:
        parsed = urlparse(cleaned)
    except Exception:
        return "<invalid-url>"

    if parsed.scheme not in ("http", "https"):
        return "<invalid-scheme>"

    host = parsed.hostname or ""
    if not host:
        return "<invalid-url>"

    # 端口校验：非法端口返回安全标记
    try:
        port_val = parsed.port
    except ValueError:
        return "<invalid-port>"

    if port_val is not None:
        try:
            port_int = int(port_val)
            if port_int < 1 or port_int > 65535:
                return "<invalid-port>"
        except (ValueError, TypeError):
            return "<invalid-port>"

    port = f":{port_val}" if port_val else ""
    path = parsed.path.rstrip("/") if parsed.path not in ("", "/") else ""

    return f"{parsed.scheme}://{host}{port}{path}"


def _safe_base_url_for_log(raw: str) -> str:
    """供 AuditLog 使用的脱敏 base URL（仅保留 host）。"""
    if not raw or not raw.strip():
        return ""
    cleaned = raw.strip()
    try:
        parsed = urlparse(cleaned)
    except Exception:
        return "<invalid-url>"
    return parsed.hostname or "<invalid-url>"


# ── 配置投影 ─────────────────────────────────────────────────────


def build_model_config(
    worker_status: str = "unknown",
    last_heartbeat_at: str | None = None,
    last_error_code: str | None = None,
) -> ModelConfigProjection:
    """从环境变量构建模型配置安全投影。

    API key 状态仅返回 boolean，不返回 key 值或环境变量名。
    URL 经过 sanitize_base_url 脱敏。
    配置只在进程启动边界加载，避免请求时重复读取 `.env`。
    """
    try:
        cfg = WorkerConfig.from_env()
    except ValueError as e:
        log.warning("WorkerConfig 解析失败: %s", e)
        return ModelConfigProjection(
            provider="",
            protocol="",
            model_name="",
            base_url_display="",
            api_key_configured=False,
            timeout_seconds=0,
            max_retries=0,
            max_tokens=0,
            thinking_mode="",
            worker_status=worker_status,
            last_heartbeat_at=last_heartbeat_at,
            last_error_code=str(e)[:200],
        )

    # API key 仅检查是否已配置（环境变量名非空 且 对应环境变量值非空）
    api_key_ok = bool(
        cfg.model_api_key_env
        and os.environ.get(cfg.model_api_key_env, "").strip()
    )

    base_url_display = sanitize_base_url(cfg.model_base_url) if cfg.model_base_url else ""

    return ModelConfigProjection(
        provider=cfg.model_provider,
        protocol=get_provider_spec(cfg.model_provider).protocol,
        model_name=cfg.model_name,
        base_url_display=base_url_display,
        api_key_configured=api_key_ok,
        timeout_seconds=cfg.model_timeout_seconds,
        max_retries=cfg.model_max_retries,
        max_tokens=cfg.model_max_tokens,
        thinking_mode=cfg.model_thinking_mode,
        worker_status=worker_status,
        last_heartbeat_at=last_heartbeat_at,
        last_error_code=last_error_code,
    )


# ── 连通性测试 + AuditLog (通过 Application Service) ────────────────


async def _write_audit_via_service(
    provider: str,
    model: str,
    safe_host: str,
    timeout_ms: int,
    status: str,
    latency_ms: float,
    error_code: str | None,
    error_message: str | None,
    error_category: str | None,
    error_recoverable: bool,
) -> None:
    """通过 ModelTestService + UoW 写入审计日志。"""
    from jarvis_worker.agent.models.test_service import ModelTestService
    try:
        await ModelTestService().write_audit(
            provider=provider,
            model=model,
            safe_host=safe_host,
            timeout_ms=timeout_ms,
            status=status,
            latency_ms=latency_ms,
            error_code=error_code,
            error_message=error_message,
            error_category=error_category,
            error_recoverable=error_recoverable,
        )
    except Exception:
        log.exception("AuditLog 写入失败")
        raise


async def test_model_connection(
    *,
    _client_factory: Callable[[], httpx.AsyncClient] | None = None,
) -> ModelTestResult:
    """短超时连通性测试 + AuditLog 写入。

    使用固定最小 prompt（max_tokens=1），5 秒超时，不重试。
    测试完成后通过 Application Service → UoW → Repository 写入 AuditLog。

    Args:
        _client_factory: 可注入的 httpx.AsyncClient 工厂（测试用）。

    Security:
        - URL 经过 _validate_model_base_url 校验（与 Worker 同规则）
        - API key 仅在内部读取、不记录/返回
        - 不走 AgentRunner 长任务热路径
    """
    tested_at = _now_iso()
    cfg = WorkerConfig.from_env()

    # 校验配置（复用生产规则）
    try:
        normalized_base_url, model_name, api_key = validate_model_config_for_test()
    except ValueError as e:
        error_code = "MODEL_CONFIG_ERROR"
        error_msg = str(e)
        await _write_audit_via_service(
            provider=cfg.model_provider,
            model=cfg.model_name,
            safe_host=_safe_base_url_for_log(cfg.model_base_url),
            timeout_ms=int(TEST_TIMEOUT_SECONDS * 1000),
            status="failed",
            latency_ms=0,
            error_code=error_code,
            error_message=error_msg,
            error_category="model",
            error_recoverable=True,
        )
        return ModelTestResult(
            provider=cfg.model_provider,
            model=cfg.model_name,
            latency_ms=0,
            tested_at=tested_at,
            status="failed",
            error_code=error_code,
            error_message=error_msg,
            error_category="model",
            error_recoverable=True,
        )

    # 使用校验后的 URL 拼接（安全：已确保无 userinfo/query/fragment）
    url = f"{normalized_base_url}/chat/completions"
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {api_key}",
    }
    body = {
        "model": model_name,
        "messages": [{"role": "user", "content": "ping"}],
        "max_tokens": TEST_MAX_TOKENS,
        "stream": False,
    }

    start = time.monotonic()
    error_code = None
    error_msg = None
    error_category = None
    error_recoverable = False
    status = "failed"

    client_factory = _client_factory or (lambda: httpx.AsyncClient(timeout=TEST_TIMEOUT_SECONDS))

    try:
        async with client_factory() as client:
            resp = await client.post(url, headers=headers, json=body)
            latency = (time.monotonic() - start) * 1000

            if resp.status_code == 200:
                status = "ok"
                log.info(
                    "模型连通性测试成功: provider=%s model=%s latency=%.0fms",
                    cfg.model_provider, model_name, latency,
                )
            elif resp.status_code in (401, 403):
                error_code = "MODEL_AUTH_ERROR"
                error_msg = "认证失败（401/403），请检查 API key 是否正确"
                error_category = "model"
                error_recoverable = True
            elif resp.status_code == 404:
                error_code = "MODEL_HTTP_ERROR"
                error_msg = "模型端点未找到（404），请检查 base URL 和 model name"
                error_category = "model"
                error_recoverable = True
            elif resp.status_code >= 500:
                error_code = "MODEL_HTTP_ERROR"
                error_msg = f"模型服务返回服务器错误（{resp.status_code}）"
                error_category = "model"
                error_recoverable = True
            else:
                error_code = "MODEL_HTTP_ERROR"
                error_msg = f"模型服务返回意外状态码 {resp.status_code}"
                error_category = "model"
                error_recoverable = True

    except httpx.TimeoutException:
        latency = (time.monotonic() - start) * 1000
        error_code = "MODEL_TIMEOUT"
        error_msg = f"连接超时（{TEST_TIMEOUT_SECONDS:.0f}s）"
        error_category = "model"
        error_recoverable = True
    except httpx.ConnectError:
        latency = (time.monotonic() - start) * 1000
        error_code = "MODEL_HTTP_ERROR"
        error_msg = "无法连接到模型服务，请检查 base URL 和网络"
        error_category = "model"
        error_recoverable = True
    except Exception:
        latency = (time.monotonic() - start) * 1000
        error_code = "MODEL_HTTP_ERROR"
        error_msg = "模型连通性测试异常"
        error_category = "model"
        error_recoverable = True
        log.exception("模型连通性测试未知错误")

    # 写入 AuditLog（通过 Application Service → UoW）
    await _write_audit_via_service(
        provider=cfg.model_provider,
        model=model_name,
        safe_host=_safe_base_url_for_log(normalized_base_url),
        timeout_ms=int(TEST_TIMEOUT_SECONDS * 1000),
        status=status,
        latency_ms=latency,
        error_code=error_code,
        error_message=error_msg,
        error_category=error_category,
        error_recoverable=error_recoverable,
    )

    return ModelTestResult(
        provider=cfg.model_provider,
        model=model_name,
        latency_ms=round(latency, 1),
        tested_at=tested_at,
        status=status,
        error_code=error_code,
        error_message=error_msg,
        error_category=error_category,
        error_recoverable=error_recoverable,
    )


def _now_iso() -> str:
    """返回 UTC ISO 时间字符串。"""
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat()
