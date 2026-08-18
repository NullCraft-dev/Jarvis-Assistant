// Package redis 定义 Redis Runtime Bus 的消息契约、stream key、consumer group 命名、
// 写入 transport adapter、读取/解码/ack adapter 和 go-redis 适配。
//
// 2B-1a：契约骨架 — 消息结构体、序列化 helper、命名常量。
// 2B-1b：写入 transport — RedisStreamClient 窄接口、GoRedisStreamClient、RedisRuntimeTransport、
// ToStreamFields helper、fake client 测试。
// 2B-1c：读取/ack — RedisStreamReader 窄接口、GoRedisStreamReader、RuntimeEventReader、
// payload JSON 解码校验、ack 确认。
//
// # 职责
//
//   - 定义 Go Orchestrator 与 Python Agent Worker 之间的 Redis Streams 消息形状
//   - 定义稳定的 stream key、consumer group 和 message field 命名
//   - 提供消息的 JSON 序列化/反序列化 helper 和类型化 Decode 校验
//   - 提供统一 XADD field shape：schema_version + 标量路由字段 + payload（完整 JSON 字符串）
//   - 提供 go-redis v9 适配（GoRedisStreamClient / GoRedisStreamReader）和可测试的
//     adapter（RedisRuntimeTransport / RuntimeEventReader）
//   - 从 StreamRuntimeEvent 读取消息、从 payload JSON string 解码为 RuntimeEventEnvelope、
//     校验后返回可消费的 envelope 列表
//   - 提供独立的 ack 方法，只对已成功处理的消息确认
//
// # 不负责
//
//   - 真实 Redis 连接管理（GoRedisStreamClient / GoRedisStreamReader 由调用方注入 *redis.Client）
//   - 完整后台 goroutine、事件扇出 / fan-out 到 Web UI（2B-2+ 再做）
//   - Task / Run / Step / ToolCall / Permission / AuditLog 的业务真源
//   - 替换 InMemoryRuntimeBus 或 RuntimeStateStore
//   - 实现 RuntimeStateStore 接口
//
// # 约束
//
// Redis Runtime Bus 是运行时通信层，不是业务数据库。Task / Run / Step / ToolCall /
// Permission / AuditLog 的最终状态必须写入 Storage 层，不能只存在 Redis Streams 中。
//
// 所有 XADD fields 使用标量路由字段 + payload JSON 字符串格式，nested object 不直接作为
// Redis field value。路由字段来自同一 message struct，不存在手写不一致 shape。
// 写入前必须经过对应 Decode* 函数的类型化校验。
// go-redis 类型只在包内部使用，不泄漏到 handler / bus 接口层。
//
// 消费端（RuntimeEventReader）：
//   - nested object 只能来自 payload JSON string，不从 Redis scalar field 拼装 runtime_event
//   - 解码失败不 ack
//   - schema_version 不匹配、payload 缺失/非 string/无效 JSON、event_type 不一致均返回 error
//
// 真源：docs/13-interface-contract.md § Internal Runtime Bus Contract
package redis
