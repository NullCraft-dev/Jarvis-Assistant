"""模型 Provider 启动配置与密钥读取。

该模块是 direct HTTP 与 LangChain adapter 共用的唯一配置校验边界。
只保存 API key 环境变量名；密钥值按调用需要读取，不进入 repr、日志或错误链。
"""

from __future__ import annotations

import os
import re
from urllib.parse import urlparse

from jarvis_worker.agent.models.errors import model_config_error


def validate_provider_config(
    base_url: str,
    model: str,
    api_key_env: str,
    max_retries: int,
) -> None:
    """在启动阶段校验所有模型 adapter 共用的配置。"""
    if not base_url:
        raise model_config_error("JARVIS_MODEL_BASE_URL 缺失或为空")
    parsed = urlparse(base_url)
    if parsed.scheme not in ("http", "https"):
        raise model_config_error(
            f"JARVIS_MODEL_BASE_URL 必须是 http/https，当前: {parsed.scheme!r}"
        )
    if not parsed.hostname:
        raise model_config_error(
            f"JARVIS_MODEL_BASE_URL 缺少 hostname: {base_url!r}"
        )
    if parsed.username or parsed.password:
        raise model_config_error("JARVIS_MODEL_BASE_URL 不允许包含用户名或密码")
    if parsed.query or parsed.fragment:
        raise model_config_error("JARVIS_MODEL_BASE_URL 不允许包含 query 或 fragment")

    if not model:
        raise model_config_error("JARVIS_MODEL_NAME 缺失或为空")

    if not api_key_env:
        raise model_config_error("JARVIS_MODEL_API_KEY_ENV 缺失或为空")
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", api_key_env):
        raise model_config_error(
            f"JARVIS_MODEL_API_KEY_ENV 不是合法环境变量名: {api_key_env!r}"
        )

    if not isinstance(max_retries, int) or not (0 <= max_retries <= 2):
        raise model_config_error(
            f"JARVIS_MODEL_MAX_RETRIES 必须在 0-2，当前: {max_retries}"
        )


def check_api_key_exists(env_var: str) -> None:
    """检查 API key 环境变量存在且非空，不保存其值。"""
    read_api_key(env_var)


def read_api_key(env_var: str) -> str:
    """按调用需要读取 API key；错误只包含环境变量名。"""
    key = os.environ.get(env_var, "")
    if not key or not key.strip():
        raise model_config_error(f"API key 环境变量 {env_var} 未设置或为空")
    return key.strip()
