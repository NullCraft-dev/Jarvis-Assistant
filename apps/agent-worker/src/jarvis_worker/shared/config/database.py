"""数据库配置。

从环境变量 JARVIS_DATABASE_URL 读取 PostgreSQL 连接串。
缺失或连接失败时明确失败，不静默降级到 SQLite 或内存。
"""

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class DatabaseConfig:
    """PostgreSQL 数据库配置。"""

    url: str  # postgresql+asyncpg://user:pass@host:port/db

    @classmethod
    def from_env(cls) -> "DatabaseConfig":
        """从环境变量读取数据库配置。

        Raises:
            RuntimeError: JARVIS_DATABASE_URL 缺失或为空。
        """
        url = os.getenv("JARVIS_DATABASE_URL", "").strip()
        if not url:
            raise RuntimeError(
                "JARVIS_DATABASE_URL 未设置。PostgreSQL 是唯一持久化真相，"
                "缺少数据库连接串时无法启动。"
                "请设置环境变量，例如："
                "JARVIS_DATABASE_URL=postgresql+asyncpg://jarvis:jarvis@127.0.0.1:5432/jarvis"
            )
        if not url.startswith("postgresql"):
            raise RuntimeError(
                f"JARVIS_DATABASE_URL 必须以 postgresql 开头，当前值: {url[:50]}..."
            )
        return cls(url=url)
