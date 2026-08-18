"""Redis 连接管理 — 可注入 client，测试用 fakeredis。"""

from __future__ import annotations

from typing import Protocol

import redis


class RedisClientProtocol(Protocol):
    """redis.Redis 的最小接口，用于测试注入。

    只暴露本 worker 需要的操作：XADD / XREADGROUP / XGROUP CREATE / XACK。
    """

    def xadd(self, name: str, fields: dict, id: str = "*") -> str: ...
    def xreadgroup(
        self,
        groupname: str,
        consumername: str,
        streams: dict,
        count: int | None = None,
        block: int | None = None,
    ) -> list: ...
    def xgroup_create(
        self, name: str, groupname: str, id: str = "$", mkstream: bool = False
    ) -> bool: ...
    def xack(self, name: str, groupname: str, *ids: str) -> int: ...
    def xautoclaim(
        self, name: str, groupname: str, consumername: str,
        min_idle_time: int, start_id: str = "0-0", count: int | None = None,
    ) -> list: ...
    def xpending_range(
        self, name: str, groupname: str, min: str, max: str, count: int,
        consumername: str | None = None,
    ) -> list: ...
    def xclaim(
        self, name: str, groupname: str, consumername: str,
        min_idle_time: int, message_ids: list[str],
    ) -> list: ...
    def eval(self, script: str, numkeys: int, *keys_and_args: str): ...
    def ping(self) -> bool: ...
    def close(self) -> None: ...


def create_redis_client(
    redis_addr: str,
    *,
    password: str = "",
    db: int = 0,
) -> redis.Redis:
    """创建真实 Redis client。

    Args:
        redis_addr: Redis 地址，如 "127.0.0.1:6379"

    Returns:
        已连接但未验证的 redis.Redis 实例
    """
    host, _, port_str = redis_addr.partition(":")
    port = int(port_str) if port_str else 6379

    return redis.Redis(
        host=host,
        port=port,
        password=password or None,
        db=db,
        decode_responses=True,
        socket_connect_timeout=3,
        socket_keepalive=True,
    )


def create_async_redis_client(
    redis_addr: str,
    *,
    password: str = "",
    db: int = 0,
):
    """Create the Control Plane client with the same connection semantics."""
    import redis.asyncio as aioredis

    host, _, port_str = redis_addr.partition(":")
    port = int(port_str) if port_str else 6379
    return aioredis.Redis(
        host=host,
        port=port,
        password=password or None,
        db=db,
        decode_responses=True,
        socket_connect_timeout=3,
        socket_keepalive=True,
    )


def ensure_consumer_group(
    client: RedisClientProtocol,
    stream: str,
    group: str,
    start_id: str = "0",
) -> None:
    """幂等创建 consumer group。

    若 group 已存在（BUSYGROUP），忽略错误。
    其他错误原样抛出。

    Args:
        client: Redis client
        stream: stream key
        group: consumer group 名称
        start_id: 起始消费 id，"0" = 从头消费，"$" = 仅新消息
    """
    try:
        client.xgroup_create(stream, group, id=start_id, mkstream=True)
    except redis.ResponseError as e:
        # BUSYGROUP: consumer group 已存在 → 幂等
        if "BUSYGROUP" not in str(e):
            raise
