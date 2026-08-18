"""统一应用日志系统 — JarvisFormatter + 彩色终端 + 无颜色滚动文件。

职责边界（参见 docs/18-observability-logging-design.md）：
  应用日志 → 开发与运行排障（本模块）
  RuntimeEvent → 任务进度、工具调用、权限和产物的用户可见状态
  AuditLog → 权限、安全与本地影响操作的持久化审计

格式：
  时间 | 级别 | 服务/实例 | 执行上下文 | 调用位置 | 关联上下文 | 消息

用法：
  from jarvis_worker.shared.observability import setup_logging, get_logger
  setup_logging()                        # 进程入口最早调用
  log = get_logger(__name__)
  log.info("Worker 启动")
"""

from __future__ import annotations

import contextvars
import logging
import logging.handlers
import os
import re
import sys
import threading
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Optional

# ── 常量 ──────────────────────────────────────────────────────────

_SERVICE_NAME = "agent-worker"
_MAX_FILE_BYTES = 20 * 1024 * 1024  # 20 MiB
_MAX_BACKUP_COUNT = 10
_DATE_FORMAT = "%Y-%m-%d %H:%M:%S"
_MAX_MESSAGE_LENGTH = 4096
_MAX_CONTEXT_VALUE_LENGTH = 128
_TRACE_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")

# 敏感键名（大小写不敏感匹配）
_SENSITIVE_KEYS = {
    "key", "api_key", "apikey", "token", "secret", "password",
    "cookie", "credential", "passwd", "pwd",
    "access_key", "secret_key", "private_key", "api_secret",
}

# 敏感值正则（Bearer token、sk- 前缀密钥、JWT）
_SENSITIVE_VALUE_PATTERNS = [
    re.compile(r'(?:bearer\s+)([a-zA-Z0-9\-._~+/]+=*)', re.IGNORECASE),
    re.compile(r'(?:sk-)[a-zA-Z0-9\-_]{20,}'),
    re.compile(r'(?:eyJ)[a-zA-Z0-9\-_]+\.(?:eyJ)[a-zA-Z0-9\-_]+\.(?:[a-zA-Z0-9\-_]+)'),
]

# ANSI 颜色代码（仅 TTY 且 NO_COLOR 未设置时使用）
_COLORS: Dict[str, str] = {
    "reset": "\033[0m",
    "green": "\033[32m",
    "grey": "\033[90m",
    "cyan": "\033[36m",
    "yellow": "\033[33m",
    "red": "\033[31m",
    "blue": "\033[34m",
    "magenta": "\033[35m",
}

# ── 日志上下文 (contextvars) ──────────────────────────────────────

_ctx_trace: contextvars.ContextVar[Optional[str]] = contextvars.ContextVar(
    "log_trace_id", default=None
)
_ctx_request: contextvars.ContextVar[Optional[str]] = contextvars.ContextVar(
    "log_request_id", default=None
)
_ctx_task: contextvars.ContextVar[Optional[str]] = contextvars.ContextVar(
    "log_task_id", default=None
)
_ctx_run: contextvars.ContextVar[Optional[str]] = contextvars.ContextVar(
    "log_run_id", default=None
)
_ctx_step: contextvars.ContextVar[Optional[str]] = contextvars.ContextVar(
    "log_step_id", default=None
)


def set_log_context(
    trace_id: Optional[str] = None,
    request_id: Optional[str] = None,
    task_id: Optional[str] = None,
    run_id: Optional[str] = None,
    step_id: Optional[str] = None,
) -> None:
    """设置当前协程/线程的日志上下文字段。"""
    if trace_id is not None:
        _ctx_trace.set(trace_id)
    if request_id is not None:
        _ctx_request.set(request_id)
    if task_id is not None:
        _ctx_task.set(task_id)
    if run_id is not None:
        _ctx_run.set(run_id)
    if step_id is not None:
        _ctx_step.set(step_id)


def clear_log_context() -> None:
    """清除当前协程/线程的日志上下文。"""
    _ctx_trace.set(None)
    _ctx_request.set(None)
    _ctx_task.set(None)
    _ctx_run.set(None)
    _ctx_step.set(None)


def _get_log_context() -> Dict[str, str]:
    """获取当前日志上下文，缺失字段填 '-'。"""
    return {
        "trace_id": _safe_context_value(_ctx_trace.get()),
        "request_id": _safe_context_value(_ctx_request.get()),
        "task_id": _safe_context_value(_ctx_task.get()),
        "run_id": _safe_context_value(_ctx_run.get()),
        "step_id": _safe_context_value(_ctx_step.get()),
    }


# ── 脱敏 ──────────────────────────────────────────────────────────

def sanitize_message(message: str) -> str:
    """对日志消息中的敏感值脱敏。

    规则：
    - Bearer token → Bearer ***
    - sk- 前缀密钥 → sk-***
    - JWT token → ***
    - 敏感 key=value / key:value 模式 → value 替换为 ***
    """
    # 先处理敏感值模式（Bearer / sk- / JWT），避免被 key=value 规则先捕获
    message = _SENSITIVE_VALUE_PATTERNS[0].sub(r'Bearer ***', message)
    message = _SENSITIVE_VALUE_PATTERNS[1].sub(r'sk-***', message)
    message = _SENSITIVE_VALUE_PATTERNS[2].sub(r'***', message)

    for key in _SENSITIVE_KEYS:
        # key="value" 或 key='value'（先匹配引号形式）
        message = re.sub(
            rf'({key}[_\w]*)\s*[=:]\s*["\']([^"\']+)["\']',
            r'\1="***"',
            message,
            flags=re.IGNORECASE,
        )
        # key=value（无引号，值不以 " 开头，匹配到空格/逗号/行尾）
        message = re.sub(
            rf'({key}[_\w]*)\s*[=:]\s*([^\s,}}"\'][^\s,}}]*)',
            r'\1=***',
            message,
            flags=re.IGNORECASE,
        )

    return _single_line(message, _MAX_MESSAGE_LENGTH)


def normalize_trace_id(value: Optional[str]) -> Optional[str]:
    """校验外部 trace id，拒绝会破坏日志格式或关联性的值。"""
    if value is None:
        return None
    candidate = value.strip()
    if _TRACE_ID_PATTERN.fullmatch(candidate) is None:
        return None
    return candidate


def _single_line(value: object, max_length: int) -> str:
    """归一化不可信文本，保持一条日志始终为单行且不破坏列分隔符。"""
    text = str(value).replace("\n", " ").replace("\r", " ")
    text = re.sub(r"\s+", " ", text).strip().replace("|", "¦")
    if len(text) > max_length:
        return text[: max_length - 1] + "…"
    return text


def _safe_context_value(value: object) -> str:
    if value is None or value == "":
        return "-"
    return _single_line(value, _MAX_CONTEXT_VALUE_LENGTH)


def sanitize_extra(extra: Dict[str, Any]) -> Dict[str, Any]:
    """对 extra 字典中的敏感值脱敏。"""
    if not extra:
        return extra
    sanitized: Dict[str, Any] = {}
    for k, v in extra.items():
        if isinstance(v, str):
            if k.lower() in _SENSITIVE_KEYS:
                sanitized[k] = "***"
            else:
                sanitized[k] = sanitize_message(v)
        elif isinstance(v, dict):
            sanitized[k] = sanitize_extra(v)
        else:
            sanitized[k] = v
    return sanitized


# ── Formatter ─────────────────────────────────────────────────────

class JarvisFormatter(logging.Formatter):
    """统一日志格式化器。

    输出格式：
      时间 | 级别 | 服务/实例 | 执行上下文 | 调用位置 | 关联上下文 | 消息

    终端可着色（根据 TTY + NO_COLOR 判断），文件永不包含 ANSI 控制字符。
    """

    def __init__(
        self,
        use_color: bool = False,
        service_instance: str = "agent-worker/worker-01",
    ):
        super().__init__()
        self._use_color = use_color
        self._service_instance = service_instance

    def format(self, record: logging.LogRecord) -> str:
        # ── 时间（本地时间 + 毫秒）──
        now = datetime.fromtimestamp(record.created)
        timestamp = now.strftime(_DATE_FORMAT) + f".{int(record.msecs):03d}"

        # ── 级别（固定宽度 5，归一化 WARNING → WARN）──
        raw_level = record.levelname
        if raw_level == "WARNING":
            raw_level = "WARN"
        level = raw_level.ljust(5)

        # ── 执行上下文（Python thread name）──
        exec_ctx = threading.current_thread().name

        # ── 调用位置 ──
        caller = f"{record.name}.{record.funcName}:{record.lineno}"

        # ── 关联上下文 ──
        ctx = self._build_context(record)
        ctx_str = (
            f"trace={ctx['trace_id']} "
            f"request={ctx['request_id']} "
            f"task={ctx['task_id']} "
            f"run={ctx['run_id']} "
            f"step={ctx['step_id']}"
        )

        # ── 消息（单行化 + 合并多余空白 + 脱敏）──
        msg = sanitize_message(record.getMessage())

        # ── 组装 ──
        line = (
            f"{timestamp} | {level} | {self._service_instance} | "
            f"{exec_ctx} | {caller} | {ctx_str} | {msg}"
        )

        # ── 异常信息 ──
        if record.exc_info and record.exc_info[1]:
            exc_msg = sanitize_message(str(record.exc_info[1]))
            line += f" error={exc_msg}"

        if self._use_color:
            line = self._apply_color(line, record.levelno)

        return line

    def _build_context(self, record: logging.LogRecord) -> Dict[str, str]:
        """合并 extra 字段和 contextvars 上下文。"""
        ctx = _get_log_context()
        # Python logging 会把 logger.info(..., extra={...}) 的字段直接写入
        # LogRecord，而不是 record.extra；同时兼容测试或旧调用手动设置的 record.extra。
        legacy_extra = getattr(record, "extra", None)
        for field in ("trace_id", "request_id", "task_id", "run_id", "step_id"):
            value = getattr(record, field, None)
            if value is None and isinstance(legacy_extra, dict):
                value = legacy_extra.get(field)
            if value:
                ctx[field] = _safe_context_value(value)
        return ctx

    def _apply_color(self, line: str, levelno: int) -> str:
        """应用终端颜色。"""
        c = _COLORS
        parts = line.split(" | ", 6)

        if len(parts) >= 7:
            # 时间 — 绿色
            parts[0] = f"{c['green']}{parts[0]}{c['reset']}"
            # 级别 — 按严重程度着色
            if levelno >= logging.ERROR:
                parts[1] = f"{c['red']}{parts[1]}{c['reset']}"
            elif levelno >= logging.WARNING:
                parts[1] = f"{c['yellow']}{parts[1]}{c['reset']}"
            elif levelno >= logging.INFO:
                parts[1] = f"{c['cyan']}{parts[1]}{c['reset']}"
            else:
                parts[1] = f"{c['grey']}{parts[1]}{c['reset']}"
            # 服务/实例 — 固定服务色；其余辅助字段使用灰色。
            parts[2] = f"{self._service_color()}{parts[2]}{c['reset']}"
            parts[3] = f"{c['grey']}{parts[3]}{c['reset']}"
            parts[5] = f"{c['grey']}{parts[5]}{c['reset']}"
            # 调用位置 — 蓝色
            parts[4] = f"{c['blue']}{parts[4]}{c['reset']}"

        return " | ".join(parts)

    def _service_color(self) -> str:
        """为服务字段提供稳定颜色，便于多服务并行输出时快速扫读。"""
        service_name = self._service_instance.split("/", 1)[0]
        return {
            "gateway": _COLORS["blue"],
            "control-plane": _COLORS["cyan"],
            "agent-worker": _COLORS["magenta"],
            "rag-worker": _COLORS["magenta"],
        }.get(service_name, _COLORS["grey"])


# ── Logger 获取 ───────────────────────────────────────────────────

def get_logger(name: str) -> logging.Logger:
    """获取 jarvis_worker 命名空间下的 logger。

    等价于 logging.getLogger(f"jarvis_worker.{name}")。
    """
    qualified_name = name if name == "jarvis_worker" or name.startswith("jarvis_worker.") else f"jarvis_worker.{name}"
    return logging.getLogger(qualified_name)


# ── 内部 helper ───────────────────────────────────────────────────

def _use_color() -> bool:
    """是否启用终端颜色。

    ``JARVIS_LOG_COLOR`` 支持 ``auto``（默认）、``always`` 和 ``never``。
    ``always`` 用于 ``scripts/dev.sh`` 将子进程输出经管道汇总回真实终端的场景；
    ``NO_COLOR`` 始终拥有最高优先级。
    """
    if os.environ.get("NO_COLOR"):
        return False

    mode = os.environ.get("JARVIS_LOG_COLOR", "auto").strip().lower()
    if mode in {"always", "force", "1", "true"}:
        return True
    if mode in {"never", "0", "false"}:
        return False
    return sys.stderr.isatty()


def _resolve_instance_id(service_name: str = _SERVICE_NAME) -> str:
    """解析服务实例 ID。

    优先级：JARVIS_INSTANCE_ID > Worker 场景的 JARVIS_WORKER_ID > 服务默认值。
    """
    configured = os.environ.get("JARVIS_INSTANCE_ID")
    if configured:
        return configured
    if service_name == "agent-worker":
        return os.environ.get("JARVIS_WORKER_ID", "worker-01")
    return f"{service_name}-01"


def _resolve_log_dir() -> Path:
    """解析日志目录。

    优先级：JARVIS_LOG_DIR > <项目根目录>/.local/logs
    """
    env_dir = os.environ.get("JARVIS_LOG_DIR")
    if env_dir:
        return Path(env_dir)
    # editable 本地开发时从当前源码向上定位 compose.yaml，确保无论从
    # apps/agent-worker 还是项目根目录启动，三个后端服务都写到同一目录。
    for candidate in Path(__file__).resolve().parents:
        if (candidate / "compose.yaml").is_file() and (candidate / "apps").is_dir():
            return candidate / ".local" / "logs"
    # 非仓库安装环境没有项目根目录时才回退到当前目录。
    return Path.cwd() / ".local" / "logs"


def _resolve_log_level() -> int:
    """从 LOG_LEVEL 环境变量解析日志级别。"""
    raw = os.environ.get("LOG_LEVEL", "INFO").strip().upper()
    level_map = {
        "DEBUG": logging.DEBUG,
        "INFO": logging.INFO,
        "WARN": logging.WARNING,
        "WARNING": logging.WARNING,
        "ERROR": logging.ERROR,
    }
    return level_map.get(raw, logging.INFO)


def _create_file_handler(
    log_name: str,
    level: int,
    service_instance: str,
) -> Optional[logging.Handler]:
    """创建滚动文件 handler。

    返回 None 表示文件输出不可用（不阻塞启动）。
    """
    log_dir = _resolve_log_dir()
    log_file = log_dir / log_name

    try:
        log_dir.mkdir(parents=True, exist_ok=True)
        handler = logging.handlers.RotatingFileHandler(
            filename=str(log_file),
            maxBytes=_MAX_FILE_BYTES,
            backupCount=_MAX_BACKUP_COUNT,
            encoding="utf-8",
        )
        handler.setLevel(level)
        handler.setFormatter(JarvisFormatter(
            use_color=False,
            service_instance=service_instance,
        ))
        return handler
    except (OSError, PermissionError) as e:
        # 文件不可用：stderr 告警，不阻塞启动，不递归触发日志故障
        print(
            f"[observability] WARNING: 无法创建日志文件 {log_file}: {e}",
            file=sys.stderr,
        )
        return None


def _quiet_third_party_loggers() -> None:
    """抑制第三方库日志噪音。"""
    noisy = [
        "sqlalchemy", "sqlalchemy.engine", "sqlalchemy.pool",
        "redis", "redis.asyncio",
        "httpx", "httpcore",
        "asyncio", "aiosqlite",
    ]
    for name in noisy:
        logging.getLogger(name).setLevel(logging.WARNING)


def _route_external_loggers(handlers: list[logging.Handler]) -> None:
    """让进程级服务日志复用统一 formatter，并压制重复 access log。

    Uvicorn 在 FastAPI lifespan 之前安装自己的 handler；若只配置
    ``jarvis_worker`` namespace，它的启动、错误和访问日志会继续使用默认格式。
    """
    for name in ("uvicorn", "uvicorn.error"):
        external = logging.getLogger(name)
        external.handlers = list(handlers)
        external.propagate = False
        external.setLevel(logging.INFO)

    access = logging.getLogger("uvicorn.access")
    access.handlers = list(handlers)
    access.propagate = False
    access.setLevel(logging.WARNING)


# ── 初始化 ─────────────────────────────────────────────────────────

def setup_logging(
    level: int | None = None,
    service_name: str = "agent-worker",
    log_basename: str | None = None,
) -> None:
    """配置全局日志系统。

    同时输出到：
    - 彩色终端（stderr，TTY + 无 NO_COLOR 时着色）
    - 无颜色滚动文件（JARVIS_LOG_DIR/<log_basename>，20 MiB × 10）

    参数：
      level：日志级别，默认从 LOG_LEVEL 环境变量读取，fallback INFO
      service_name：服务名（agent-worker / control-plane）
      log_basename：日志文件名，默认 worker-<instance_id>.log
    """
    if level is None:
        level = _resolve_log_level()

    instance_id = _resolve_instance_id(service_name)
    service_instance = f"{service_name}/{instance_id}"

    if log_basename is None:
        log_basename = f"worker-{instance_id}.log"

    root = logging.getLogger("jarvis_worker")
    root.setLevel(level)
    root.propagate = False

    # 避免重复添加（幂等）
    if root.handlers:
        return

    # ── 控制台 handler（彩色，stderr）──
    console = logging.StreamHandler(sys.stderr)
    console.setLevel(level)
    console.setFormatter(JarvisFormatter(
        use_color=_use_color(),
        service_instance=service_instance,
    ))
    root.addHandler(console)

    # ── 文件 handler（无颜色，滚动）──
    file_handler = _create_file_handler(log_basename, level, service_instance)
    if file_handler is not None:
        root.addHandler(file_handler)

    # Uvicorn 等服务级 logger 也必须使用相同的 7 列格式。正常 HTTP
    # 请求由应用自己的中间件按 DEBUG/INFO/ERROR 记录，不重复输出 access log。
    _route_external_loggers(root.handlers)

    # ── 抑制第三方库日志噪音 ──
    _quiet_third_party_loggers()

    # 初始化完成
    root.info(
        "日志系统已初始化: level=%s instance=%s console_color=%s file=%s",
        logging.getLevelName(level),
        instance_id,
        _use_color(),
        _resolve_log_dir() / log_basename if file_handler else "disabled",
    )


def shutdown_logging() -> None:
    """安全关闭日志系统（刷新并关闭所有 handler）。"""
    root = logging.getLogger("jarvis_worker")
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        external = logging.getLogger(name)
        external.handlers.clear()
    for handler in root.handlers[:]:
        handler.flush()
        handler.close()
        root.removeHandler(handler)
