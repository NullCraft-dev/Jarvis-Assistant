"""Runtime event producer — 将 RuntimeEventEnvelope 写入 Redis runtime event stream。"""

from __future__ import annotations

from jarvis_worker.runtime_bus.messages import (
    STREAM_RUNTIME_EVENT,
    RuntimeEventEnvelope,
)
from jarvis_worker.runtime_bus import RedisClientProtocol


class RuntimeEventProducer:
    """将 RuntimeEventEnvelope 写入 Redis StreamRuntimeEvent。

    对齐 Go 侧 RedisRuntimeTransport.PublishRuntimeEvent。
    XADD fields 使用 RuntimeEventEnvelope.to_xadd_fields()，
    格式：schema_version + payload（完整 JSON）+ 冗余标量路由字段。
    """

    def __init__(self, client: RedisClientProtocol):
        self._client = client

    def publish(self, envelope: RuntimeEventEnvelope) -> str:
        """写入 RuntimeEventEnvelope 到 runtime event stream。

        先校验 envelope 一致性，校验通过后 XADD。

        Args:
            envelope: 已构造的 RuntimeEventEnvelope

        Returns:
            Redis 返回的消息 id

        Raises:
            ValueError: envelope 校验失败
            RuntimeError: XADD 失败
        """
        # 先校验
        envelope.validate()

        fields = envelope.to_xadd_fields()

        try:
            msg_id = self._client.xadd(STREAM_RUNTIME_EVENT, fields, id="*")
        except Exception as e:
            raise RuntimeError(
                f"XADD {STREAM_RUNTIME_EVENT} 失败: {e}"
            ) from e

        return msg_id
