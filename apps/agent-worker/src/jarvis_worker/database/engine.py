"""PostgreSQL 异步引擎工厂。

使用 asyncpg + SQLAlchemy 2.x async engine。
数据库配置来自 JARVIS_DATABASE_URL 环境变量。
"""

import logging
from typing import Optional

from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine

from jarvis_worker.shared.config.database import DatabaseConfig

logger = logging.getLogger(__name__)

# 全局引擎实例（单例）
_engine: Optional[AsyncEngine] = None
_session_factory: Optional[async_sessionmaker[AsyncSession]] = None


def create_engine(cfg: Optional[DatabaseConfig] = None) -> AsyncEngine:
    """创建或返回全局异步引擎。

    Args:
        cfg: 数据库配置；为 None 时从环境变量读取。

    Returns:
        SQLAlchemy AsyncEngine 实例。

    Raises:
        RuntimeError: 配置缺失或连接失败。
    """
    global _engine, _session_factory

    if _engine is not None:
        return _engine

    if cfg is None:
        cfg = DatabaseConfig.from_env()

    logger.info("创建 PostgreSQL 引擎: %s", _mask_url(cfg.url))
    _engine = create_async_engine(
        cfg.url,
        echo=False,
        pool_size=10,
        max_overflow=10,
        pool_pre_ping=True,
        pool_recycle=3600,
        connect_args={
            "timeout": 10,
            "command_timeout": 30,
        },
    )
    _session_factory = async_sessionmaker(
        _engine,
        class_=AsyncSession,
        expire_on_commit=False,
    )
    return _engine


def get_session_factory() -> async_sessionmaker[AsyncSession]:
    """获取全局 session factory。"""
    if _session_factory is None:
        create_engine()
    return _session_factory


async def check_connection() -> bool:
    """测试数据库连接。

    Returns:
        True 表示连接成功。
    """
    try:
        factory = get_session_factory()
        async with factory() as session:
            await session.execute(
                __import__("sqlalchemy").text("SELECT 1")
            )
        return True
    except Exception as e:
        logger.error("PostgreSQL 连接失败: %s", e)
        return False


async def dispose_engine() -> None:
    """关闭全局引擎。"""
    global _engine, _session_factory
    if _engine is not None:
        await _engine.dispose()
        _engine = None
        _session_factory = None
        logger.info("PostgreSQL 引擎已关闭")


def _mask_url(url: str) -> str:
    """脱敏 URL 用于日志。"""
    if "@" in url:
        return url.split("@")[0].rsplit(":", 1)[0] + ":***@" + url.split("@")[1]
    return url
