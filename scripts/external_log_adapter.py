#!/usr/bin/env python3
"""把无法接入项目 logger 的外部服务 stdout 转为 Jarvis 统一日志格式。"""

from __future__ import annotations

import argparse
import logging
import logging.handlers
import os
import re
import sys
from pathlib import Path

ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")
LEVEL_RE = re.compile(r"\b(CRITICAL|ERROR|WARNING|WARN|INFO|DEBUG)\b", re.IGNORECASE)
SUCCESS_ACCESS_RE = re.compile(r'"GET\s+/(?:openapi\.json|health|v1/models)[^\"]*"\s+2\d\d\b')
SENSITIVE_KEY_RE = re.compile(
    r"(?i)\b(key|api_key|apikey|token|secret|password|cookie|credential|passwd|pwd|"
    r"access_key|secret_key|private_key|api_secret)(\s*[:=]\s*)"
    r"(?:\"[^\"]*\"|'[^']*'|[^\s,;]+)"
)
SENSITIVE_REPLACEMENTS = (
    (re.compile(r"(?i)(bearer\s+)[A-Za-z0-9._~+\-/]+=*"), r"\1***"),
    (re.compile(r"\bsk-[A-Za-z0-9_-]{20,}\b"), "sk-***"),
    (re.compile(r"\beyJ[A-Za-z0-9_-]+\.eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\b"), "***"),
)

COLORS = {
    "reset": "\033[0m",
    "green": "\033[32m",
    "grey": "\033[90m",
    "cyan": "\033[36m",
    "yellow": "\033[33m",
    "red": "\033[31m",
    "blue": "\033[34m",
    "magenta": "\033[35m",
}


class ExternalFormatter(logging.Formatter):
    def __init__(self, service_instance: str, *, use_color: bool = False) -> None:
        super().__init__()
        self._service_instance = service_instance
        self._use_color = use_color

    def format(self, record: logging.LogRecord) -> str:
        timestamp = self.formatTime(record, "%Y-%m-%d %H:%M:%S")
        timestamp += f".{int(record.msecs):03d}"
        level = "WARN" if record.levelno == logging.WARNING else record.levelname
        level = level[:5].ljust(5)
        message = sanitize(record.getMessage())
        line = (
            f"{timestamp} | {level} | {self._service_instance} | - | "
            "external.process:0 | trace=- request=- task=- run=- step=- | "
            f"{message}"
        )
        if not self._use_color:
            return line

        parts = line.split(" | ", 6)
        parts[0] = f"{COLORS['green']}{parts[0]}{COLORS['reset']}"
        if record.levelno >= logging.ERROR:
            level_color = COLORS["red"]
        elif record.levelno >= logging.WARNING:
            level_color = COLORS["yellow"]
        elif record.levelno >= logging.INFO:
            level_color = COLORS["cyan"]
        else:
            level_color = COLORS["grey"]
        parts[1] = f"{level_color}{parts[1]}{COLORS['reset']}"
        parts[2] = f"{COLORS['magenta']}{parts[2]}{COLORS['reset']}"
        parts[3] = f"{COLORS['grey']}{parts[3]}{COLORS['reset']}"
        parts[4] = f"{COLORS['blue']}{parts[4]}{COLORS['reset']}"
        parts[5] = f"{COLORS['grey']}{parts[5]}{COLORS['reset']}"
        return " | ".join(parts)


def sanitize(value: str) -> str:
    message = ANSI_RE.sub("", value).replace("\r", " ").replace("\n", " ")
    message = " ".join(message.split()).replace(" | ", " ¦ ")
    for pattern, replacement in SENSITIVE_REPLACEMENTS:
        message = pattern.sub(replacement, message)
    message = SENSITIVE_KEY_RE.sub(r"\1\2***", message)
    return message[:4096]


def classify(line: str) -> int:
    if SUCCESS_ACCESS_RE.search(line):
        return logging.DEBUG
    match = LEVEL_RE.search(line)
    if not match:
        return logging.INFO
    level = match.group(1).upper()
    if level in {"CRITICAL", "ERROR"}:
        return logging.ERROR
    if level in {"WARNING", "WARN"}:
        return logging.WARNING
    if level == "DEBUG":
        return logging.DEBUG
    return logging.INFO


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--service", required=True)
    parser.add_argument("--instance", required=True)
    parser.add_argument("--log-file", type=Path, required=True)
    return parser.parse_args()


def resolve_level() -> int:
    return {
        "DEBUG": logging.DEBUG,
        "WARN": logging.WARNING,
        "WARNING": logging.WARNING,
        "ERROR": logging.ERROR,
    }.get(os.getenv("LOG_LEVEL", "INFO").strip().upper(), logging.INFO)


def use_console_color() -> bool:
    if os.getenv("NO_COLOR"):
        return False
    mode = os.getenv("JARVIS_LOG_COLOR", "auto").strip().lower()
    if mode in {"always", "force", "1", "true"}:
        return True
    if mode in {"never", "0", "false"}:
        return False
    return sys.stderr.isatty()


def build_logger(args: argparse.Namespace) -> logging.Logger:
    logger = logging.getLogger("jarvis.external")
    logger.handlers.clear()
    logger.propagate = False
    logger.setLevel(resolve_level())
    service_instance = f"{args.service}/{args.instance}"

    console = logging.StreamHandler(sys.stderr)
    console.setFormatter(
        ExternalFormatter(service_instance, use_color=use_console_color())
    )
    logger.addHandler(console)

    try:
        args.log_file.parent.mkdir(parents=True, exist_ok=True)
        file_handler = logging.handlers.RotatingFileHandler(
            args.log_file,
            maxBytes=20 * 1024 * 1024,
            backupCount=10,
            encoding="utf-8",
        )
        file_handler.setFormatter(ExternalFormatter(service_instance, use_color=False))
        logger.addHandler(file_handler)
    except OSError as exc:
        print(
            f"[observability] WARNING: 无法创建日志文件 {args.log_file}: {exc}",
            file=sys.stderr,
        )
    return logger


def main() -> int:
    args = parse_args()
    logger = build_logger(args)
    logger.info("外部服务日志适配器已启动")
    for raw_line in sys.stdin:
        line = raw_line.strip()
        if line:
            logger.log(classify(line), line)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
