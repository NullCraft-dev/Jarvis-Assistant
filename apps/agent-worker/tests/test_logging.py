"""统一日志系统测试 — formatter / 脱敏 / 无颜色文件 / 上下文 / 降级。"""

from __future__ import annotations

import logging
import os
import re
import tempfile
from pathlib import Path

import pytest

from jarvis_worker.shared.observability.logging import (
    JarvisFormatter,
    clear_log_context,
    normalize_trace_id,
    sanitize_extra,
    sanitize_message,
    set_log_context,
    setup_logging,
    shutdown_logging,
    _use_color,
)


# ── helpers ───────────────────────────────────────────────────────

def _make_record(
    name: str = "jarvis_worker.test",
    level: int = logging.INFO,
    msg: str = "测试消息",
    extra: dict | None = None,
) -> logging.LogRecord:
    record = logging.LogRecord(
        name=name,
        level=level,
        pathname="/fake/path/test.py",
        lineno=42,
        msg=msg,
        args=(),
        exc_info=None,
        func="test_func",
    )
    if extra:
        record.extra = extra
    return record


# ── Formatter: 固定列格式 ──────────────────────────────────────────

class TestJarvisFormatter:
    """JarvisFormatter 格式输出测试。"""

    def test_info_line_has_fixed_columns(self):
        """INFO 行包含全部 7 个固定列。"""
        fmt = JarvisFormatter(use_color=False, service_instance="agent-worker/worker-01")
        record = _make_record()
        line = fmt.format(record)

        parts = line.split(" | ")
        assert len(parts) == 7, f"期望 7 列，实际 {len(parts)}: {line}"
        # 时间列
        assert re.match(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\.\d{3}", parts[0])
        # 级别列（固定宽度 5）
        assert parts[1] == "INFO "
        # 服务/实例列
        assert parts[2] == "agent-worker/worker-01"
        # 调用位置列
        assert "test_func" in parts[4]
        # 消息列
        assert "测试消息" in parts[6]

    def test_error_level_correct(self):
        """ERROR 级别正确显示。"""
        fmt = JarvisFormatter(use_color=False)
        record = _make_record(level=logging.ERROR, msg="故障")
        line = fmt.format(record)
        parts = line.split(" | ")
        assert parts[1] == "ERROR"

    def test_warn_level_correct(self):
        """WARN 级别正确显示。"""
        fmt = JarvisFormatter(use_color=False)
        record = _make_record(level=logging.WARNING, msg="警告")
        line = fmt.format(record)
        parts = line.split(" | ")
        assert parts[1] == "WARN "

    def test_debug_level_correct(self):
        """DEBUG 级别正确显示。"""
        fmt = JarvisFormatter(use_color=False)
        record = _make_record(level=logging.DEBUG, msg="调试")
        line = fmt.format(record)
        parts = line.split(" | ")
        assert parts[1] == "DEBUG"

    def test_missing_context_shows_dash(self):
        """缺失关联上下文时显示 '-'。"""
        clear_log_context()
        fmt = JarvisFormatter(use_color=False)
        record = _make_record()
        line = fmt.format(record)
        parts = line.split(" | ")
        ctx = parts[5]
        assert "trace=-" in ctx
        assert "request=-" in ctx
        assert "task=-" in ctx
        assert "run=-" in ctx
        assert "step=-" in ctx

    def test_context_from_extra(self):
        """extra 中的上下文被正确拼接。"""
        fmt = JarvisFormatter(use_color=False)
        record = _make_record(extra={
            "trace_id": "tr_abc",
            "run_id": "run_xyz",
        })
        line = fmt.format(record)
        parts = line.split(" | ")
        ctx = parts[5]
        assert "trace=tr_abc" in ctx
        assert "run=run_xyz" in ctx
        assert "task=-" in ctx
        assert "step=-" in ctx

    def test_standard_logging_extra_is_used(self):
        """标准 logger extra 会写入 LogRecord 的属性，而非 record.extra。"""
        record = logging.getLogger("jarvis_worker.test").makeRecord(
            "jarvis_worker.test", logging.INFO, __file__, 42, "测试", (), None,
            extra={"trace_id": "tr_standard", "run_id": "run_standard"},
        )
        line = JarvisFormatter(use_color=False).format(record)
        assert "trace=tr_standard" in line
        assert "run=run_standard" in line

    def test_context_from_contextvars(self):
        """contextvars 中的上下文被正确拼接。"""
        set_log_context(trace_id="tr_cv", request_id="req_1", step_id="st_1")
        try:
            fmt = JarvisFormatter(use_color=False)
            record = _make_record()
            line = fmt.format(record)
            parts = line.split(" | ")
            ctx = parts[5]
            assert "trace=tr_cv" in ctx
            assert "request=req_1" in ctx
            assert "step=st_1" in ctx
        finally:
            clear_log_context()

    def test_extra_overrides_contextvars(self):
        """extra 字段优先于 contextvars。"""
        set_log_context(trace_id="tr_cv")
        try:
            fmt = JarvisFormatter(use_color=False)
            record = _make_record(extra={"trace_id": "tr_extra"})
            line = fmt.format(record)
            parts = line.split(" | ")
            ctx = parts[5]
            assert "trace=tr_extra" in ctx
        finally:
            clear_log_context()

    def test_multiline_message_flattened(self):
        """多行消息被单行化。"""
        fmt = JarvisFormatter(use_color=False)
        record = _make_record(msg="第一行\n第二行\r\n第三行")
        line = fmt.format(record)
        assert "\n" not in line.split(" | ")[6]
        assert "第一行 第二行 第三行" in line

    def test_no_ansi_in_non_color_mode(self):
        """use_color=False 时不含 ANSI escape sequence。"""
        fmt = JarvisFormatter(use_color=False)
        record = _make_record()
        line = fmt.format(record)
        assert "\033[" not in line

    def test_ansi_in_color_mode(self):
        """use_color=True 时包含 ANSI escape sequence。"""
        fmt = JarvisFormatter(use_color=True)
        record = _make_record()
        line = fmt.format(record)
        assert "\033[" in line

    def test_service_field_uses_stable_service_color(self):
        """并行服务输出时，服务字段需要具有稳定且不同的颜色。"""
        record = _make_record()
        gateway = JarvisFormatter(use_color=True, service_instance="gateway/gateway-01").format(record)
        worker = JarvisFormatter(use_color=True, service_instance="agent-worker/worker-01").format(record)

        assert "\033[34mgateway/gateway-01\033[0m" in gateway
        assert "\033[35magent-worker/worker-01\033[0m" in worker


# ── 脱敏 ───────────────────────────────────────────────────────────

class TestSanitize:
    """脱敏功能测试。"""

    def test_api_key_in_message(self):
        """消息中的 api_key=xxx 被脱敏。"""
        result = sanitize_message("请求失败 api_key=sk-abc123def456")
        assert "sk-abc123def456" not in result
        assert "api_key=***" in result

    def test_token_in_message(self):
        """消息中的 token=xxx 被脱敏。"""
        result = sanitize_message("使用 token=secret123 认证")
        assert "secret123" not in result
        assert "token=***" in result

    def test_password_in_message(self):
        """消息中的 password=xxx 被脱敏。"""
        result = sanitize_message("连接数据库 password=mypass")
        assert "mypass" not in result
        assert "password=***" in result

    def test_bearer_token(self):
        """Bearer token 被脱敏。"""
        result = sanitize_message("Authorization: Bearer eyJhbGciOiJIUzI1NiJ9.xxx")
        assert "Bearer ***" in result

    def test_sk_prefix_key(self):
        """sk- 前缀密钥被脱敏。"""
        result = sanitize_message("使用密钥 sk-proj-abc123def456ghijkl")
        assert "sk-***" in result

    def test_quoted_sensitive_value(self):
        """引号中的敏感值被脱敏。"""
        result = sanitize_message('设置 api_key="my-secret-key-123"')
        assert "my-secret-key-123" not in result
        assert 'api_key="***"' in result

    def test_normal_message_unchanged(self):
        """普通消息不被修改。"""
        msg = "任务已创建 task_id=abc run_id=xyz"
        result = sanitize_message(msg)
        assert result == msg

    def test_pipe_cannot_create_a_new_log_column(self):
        result = sanitize_message("外部输入 | ERROR | forged")
        assert " | " not in result
        assert "¦" in result

    def test_trace_id_validation(self):
        assert normalize_trace_id("trace-01:abc") == "trace-01:abc"
        assert normalize_trace_id("trace | ERROR | forged") is None

    def test_sanitize_extra_dict(self):
        """extra 字典中的敏感值被脱敏。"""
        extra = {"api_key": "sk-secret", "name": "test", "nested": {"token": "abc"}}
        result = sanitize_extra(extra)
        assert result["api_key"] == "***"
        assert result["name"] == "test"
        assert result["nested"]["token"] == "***"


# ── 文件输出降级 ───────────────────────────────────────────────────

class TestFileOutputDegradation:
    """文件输出降级测试。"""

    def test_unwritable_dir_does_not_crash(self, monkeypatch):
        """日志目录不可写不会导致崩溃。"""
        # 指向一个不存在且不可创建的路径
        monkeypatch.setenv("JARVIS_LOG_DIR", "/dev/null/logs")
        # 不应该抛异常
        try:
            setup_logging(level=logging.INFO)
        finally:
            shutdown_logging()

    def test_logging_still_works_without_file(self, monkeypatch, capsys):
        """文件不可用时终端日志仍正常。"""
        monkeypatch.setenv("JARVIS_LOG_DIR", "/dev/null/logs")
        try:
            setup_logging(level=logging.INFO)
            log = logging.getLogger("jarvis_worker.test")
            log.warning("文件不可用测试")
        finally:
            shutdown_logging()
        # 消息应该出现在 stderr
        captured = capsys.readouterr()
        assert "文件不可用测试" in captured.err


# ── 日志级别 ───────────────────────────────────────────────────────

class TestLogLevel:
    """日志级别控制测试。"""

    def test_default_level_info(self, monkeypatch):
        """默认日志级别为 INFO。"""
        monkeypatch.delenv("LOG_LEVEL", raising=False)
        from jarvis_worker.shared.observability.logging import _resolve_log_level
        assert _resolve_log_level() == logging.INFO

    def test_debug_from_env(self, monkeypatch):
        """LOG_LEVEL=DEBUG 生效。"""
        monkeypatch.setenv("LOG_LEVEL", "DEBUG")
        from jarvis_worker.shared.observability.logging import _resolve_log_level
        assert _resolve_log_level() == logging.DEBUG

    def test_error_from_env(self, monkeypatch):
        """LOG_LEVEL=ERROR 生效。"""
        monkeypatch.setenv("LOG_LEVEL", "ERROR")
        from jarvis_worker.shared.observability.logging import _resolve_log_level
        assert _resolve_log_level() == logging.ERROR


class TestColorConfiguration:
    def test_force_color_works_when_stderr_is_piped(self, monkeypatch):
        monkeypatch.delenv("NO_COLOR", raising=False)
        monkeypatch.setenv("JARVIS_LOG_COLOR", "always")
        assert _use_color() is True

    def test_no_color_overrides_force_color(self, monkeypatch):
        monkeypatch.setenv("JARVIS_LOG_COLOR", "always")
        monkeypatch.setenv("NO_COLOR", "1")
        assert _use_color() is False

    def test_never_disables_color(self, monkeypatch):
        monkeypatch.delenv("NO_COLOR", raising=False)
        monkeypatch.setenv("JARVIS_LOG_COLOR", "never")
        assert _use_color() is False


class TestLogConfigurationResolution:
    def test_worker_id_is_used_when_instance_id_missing(self, monkeypatch):
        from jarvis_worker.shared.observability.logging import _resolve_instance_id
        monkeypatch.delenv("JARVIS_INSTANCE_ID", raising=False)
        monkeypatch.setenv("JARVIS_WORKER_ID", "worker-02")
        assert _resolve_instance_id() == "worker-02"

    def test_control_plane_has_service_specific_default_instance(self, monkeypatch):
        from jarvis_worker.shared.observability.logging import _resolve_instance_id
        monkeypatch.delenv("JARVIS_INSTANCE_ID", raising=False)
        monkeypatch.setenv("JARVIS_WORKER_ID", "worker-02")
        assert _resolve_instance_id("control-plane") == "control-plane-01"

    def test_log_dir_defaults_to_project_root(self, monkeypatch):
        from jarvis_worker.shared.observability.logging import _resolve_log_dir
        monkeypatch.delenv("JARVIS_LOG_DIR", raising=False)
        log_dir = _resolve_log_dir()
        assert log_dir.name == "logs"
        assert log_dir.parent.name == ".local"
        assert (log_dir.parent.parent / "compose.yaml").is_file()


# ── 上下文 ─────────────────────────────────────────────────────────

class TestLogContext:
    """日志上下文字段测试。"""

    def test_set_and_clear(self):
        """set_log_context 和 clear_log_context 正常工作。"""
        set_log_context(trace_id="tr_1", request_id="req_1", run_id="run_1")
        from jarvis_worker.shared.observability.logging import _get_log_context
        ctx = _get_log_context()
        assert ctx["trace_id"] == "tr_1"
        assert ctx["request_id"] == "req_1"
        assert ctx["run_id"] == "run_1"

        clear_log_context()
        ctx2 = _get_log_context()
        assert ctx2["trace_id"] == "-"
        assert ctx2["request_id"] == "-"
        assert ctx2["run_id"] == "-"


# ── 集成测试 ───────────────────────────────────────────────────────

class TestSetupLoggingIntegration:
    """集成测试：完整日志流。"""

    def test_setup_logging_creates_handlers(self, monkeypatch, tmp_path):
        """setup_logging 创建 handler。"""
        monkeypatch.setenv("JARVIS_LOG_DIR", str(tmp_path))
        try:
            setup_logging(level=logging.DEBUG)
            root = logging.getLogger("jarvis_worker")
            assert len(root.handlers) >= 1
            assert root.level == logging.DEBUG
        finally:
            shutdown_logging()

    def test_setup_logging_idempotent(self, monkeypatch, tmp_path):
        """setup_logging 多次调用不重复添加 handler。"""
        monkeypatch.setenv("JARVIS_LOG_DIR", str(tmp_path))
        try:
            setup_logging(level=logging.INFO)
            handler_count = len(logging.getLogger("jarvis_worker").handlers)
            setup_logging(level=logging.INFO)
            assert len(logging.getLogger("jarvis_worker").handlers) == handler_count
        finally:
            shutdown_logging()

    def test_file_output_is_plain_text(self, monkeypatch, tmp_path):
        monkeypatch.setenv("JARVIS_LOG_DIR", str(tmp_path))
        monkeypatch.setenv("JARVIS_INSTANCE_ID", "worker-01")
        monkeypatch.setenv("NO_COLOR", "1")
        try:
            setup_logging(level=logging.INFO)
            logging.getLogger("jarvis_worker.test").info("写入文件", extra={"trace_id": "tr_file"})
        finally:
            shutdown_logging()

        content = (tmp_path / "worker-worker-01.log").read_text(encoding="utf-8")
        assert "\033[" not in content
        assert "trace=tr_file" in content

    def test_uvicorn_logs_use_jarvis_formatter_and_access_is_quiet(
        self, monkeypatch, tmp_path
    ):
        monkeypatch.setenv("JARVIS_LOG_DIR", str(tmp_path))
        monkeypatch.setenv("NO_COLOR", "1")
        try:
            setup_logging(
                level=logging.INFO,
                service_name="control-plane",
                log_basename="control-plane.log",
            )
            logging.getLogger("uvicorn.error").info("server ready")
            logging.getLogger("uvicorn.access").info("GET /internal/health 200")
        finally:
            shutdown_logging()

        content = (tmp_path / "control-plane.log").read_text(encoding="utf-8")
        assert "control-plane/control-plane-01" in content
        assert "server ready" in content
        assert "GET /internal/health 200" not in content
