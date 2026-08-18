"""Alembic migration 环境配置。

使用 SQLAlchemy 异步引擎 + asyncpg。
"""

import asyncio
import os
from logging.config import fileConfig

from alembic import context
from sqlalchemy import pool
from sqlalchemy.ext.asyncio import async_engine_from_config

from jarvis_worker.database.models import Base

# Alembic Config 对象
config = context.config

# 设置日志
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# 目标 metadata（所有 ORM 模型）
target_metadata = Base.metadata

# 从环境变量获取数据库 URL
DB_URL = os.getenv("JARVIS_DATABASE_URL", "")


def get_url() -> str:
    """获取数据库连接串。"""
    if DB_URL:
        return DB_URL
    # 尝试从 alembic.ini 读取
    url = config.get_main_option("sqlalchemy.url", "")
    if url:
        return url
    raise RuntimeError(
        "JARVIS_DATABASE_URL 未设置。请设置环境变量或在 alembic.ini 中配置。"
    )


def run_migrations_offline() -> None:
    """离线 migration（生成 SQL 脚本，不连接数据库）。"""
    url = get_url()
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection):
    context.configure(connection=connection, target_metadata=target_metadata)
    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    """在线 migration（连接数据库并执行）。"""
    configuration = config.get_section(config.config_ini_section, {})
    configuration["sqlalchemy.url"] = get_url()  # type: ignore
    connectable = async_engine_from_config(
        configuration,
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)
    await connectable.dispose()


def run_migrations_online() -> None:
    """在线 migration 入口。"""
    asyncio.run(run_async_migrations())


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
