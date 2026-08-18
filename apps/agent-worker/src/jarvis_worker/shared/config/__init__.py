"""Worker 配置包。

对外导出 WorkerConfig。
内部实现在 settings.py，保持旧导入兼容。
"""

from jarvis_worker.shared.config.settings import WorkerConfig  # noqa: F401

__all__ = ["WorkerConfig"]
