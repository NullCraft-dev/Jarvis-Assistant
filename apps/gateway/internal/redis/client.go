package redis

import "context"

// RedisStreamClient 是 Redis Streams 写入操作的最小接口。
//
// 本接口只暴露 RedisRuntimeTransport 需要的 XAdd 方法，
// 不泄漏 go-redis 类型到 handler / bus 接口层。
//
// 真实实现：GoRedisStreamClient（go-redis v9 适配）。
// 测试实现：fakeStreamClient（定义在 transport_test.go）。
type RedisStreamClient interface {
	// XAdd 向指定 stream 写入一条消息。
	//
	// values 是 XADD field-value pairs，统一格式为：
	//   - schema_version：当前契约版本号
	//   - payload：完整 message 的 JSON 字符串
	//   - 冗余标量路由字段（如 job_id / trace_id / task_id 等）
	//
	// 所有字段必须来自同一 message struct 的 *ToStreamFields helper，
	// nested object 不直接作为 Redis field value。
	XAdd(ctx context.Context, stream string, values map[string]interface{}) error
}
