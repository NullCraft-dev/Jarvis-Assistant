from jarvis_worker.runtime_bus import create_async_redis_client, create_redis_client


def test_sync_redis_client_receives_password_and_logical_db() -> None:
    client = create_redis_client(
        "redis.example:6380",
        password="redis-secret",
        db=9,
    )

    options = client.connection_pool.connection_kwargs
    assert options["host"] == "redis.example"
    assert options["port"] == 6380
    assert options["password"] == "redis-secret"
    assert options["db"] == 9


def test_async_redis_client_receives_password_and_logical_db() -> None:
    client = create_async_redis_client(
        "redis.example:6380",
        password="redis-secret",
        db=9,
    )

    options = client.connection_pool.connection_kwargs
    assert options["host"] == "redis.example"
    assert options["port"] == 6380
    assert options["password"] == "redis-secret"
    assert options["db"] == 9
