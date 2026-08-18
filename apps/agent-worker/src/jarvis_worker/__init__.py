"""Jarvis Python Agent Worker Runtime.

消费 Redis run queue 中的 RunJobMessage，执行 deterministic mock runner，
产出 RuntimeEventEnvelope 写入 Redis runtime event stream，
Go Gateway 通过 EventPump 读取并扇出到 SSE/UI。
"""

__version__ = "1.0.0"
