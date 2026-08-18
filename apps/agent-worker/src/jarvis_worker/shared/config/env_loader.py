""".env 加载 —— 仅在工作区启动边界调用。

Phase 6B-1 收口修复：
- .env 不存在 → 允许继续。
- .env 存在但加载失败 → 抛出异常，启动失败。
- 可注入 _loader 用于测试。
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Callable

log = logging.getLogger("jarvis_worker.env_loader")

# Worker 与 Control Plane 共用的本地配置路径。不要由各个入口重复根据
# 自己所在目录计算，否则会出现加载到 apps/agent-worker/src/.env 的漂移。
DEFAULT_LOCAL_ENV_PATH = Path(__file__).resolve().parents[4] / ".env"


def _default_dotenv_loader(dotenv_path: str) -> bool:
    """默认 loader：使用 python-dotenv。"""
    from dotenv import load_dotenv as _load
    return _load(dotenv_path=dotenv_path, override=False)


def load_local_env(
    env_path: str | Path,
    *,
    _loader: Callable[[str], bool] | None = None,
) -> None:
    """加载本地 .env 文件。

    Args:
        env_path: .env 文件路径。
        _loader: 可注入的 loader 函数（测试用），签名 dotenv_path→loaded。

    Raises:
        ImportError: python-dotenv 未安装且 .env 存在。
        OSError: .env 存在但无法读取。
    """
    loader = _loader or _default_dotenv_loader
    path = Path(env_path).resolve()

    if not path.is_file():
        log.debug("本地 .env 不存在，跳过加载: %s", path)
        return

    loaded = loader(str(path))
    if loaded:
        log.info("已加载本地配置: %s", path)
    else:
        log.debug("本地 .env 已存在但未加载任何新变量: %s", path)


def load_default_local_env() -> None:
    """加载应用默认 `.env`，且保留外部注入环境变量优先级。"""
    load_local_env(DEFAULT_LOCAL_ENV_PATH)
