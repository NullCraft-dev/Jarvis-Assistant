package redis

import (
	"context"
	"fmt"

	"github.com/redis/go-redis/v9"
)

// GoRedisStreamClient 是 RedisStreamClient 的 go-redis v9 窄接口适配器。
//
// 内部持有 *redis.Client，实现窄接口 XAdd，不泄漏 go-redis 类型到 handler / bus 层。
//
// 生产路径：Gateway factory 在 JARVIS_RUNTIME_BUS=redis 模式下注入真实 *redis.Client，
// 本 adapter 只负责 XAdd 调用，连接创建和 PING 验证属于 bus factory 职责。
// JARVIS_RUNTIME_BUS=inmemory 显式测试模式不创建本 adapter。
type GoRedisStreamClient struct {
	client *redis.Client
}

// 编译期断言 GoRedisStreamClient 实现 RedisStreamClient 接口。
var _ RedisStreamClient = (*GoRedisStreamClient)(nil)

// NewGoRedisStreamClient 创建 go-redis 适配的 stream client。
// client 由调用方（如 bus factory）创建并注入。
// 若 client 为 nil 则返回 error，防止 nil panic。
func NewGoRedisStreamClient(client *redis.Client) (*GoRedisStreamClient, error) {
	if client == nil {
		return nil, fmt.Errorf("redisruntime: cannot create GoRedisStreamClient with nil *redis.Client")
	}
	return &GoRedisStreamClient{client: client}, nil
}

// XAdd 调用 go-redis v9 的 XAdd，将 values 作为 field-value 对写入 stream。
// 若内部 client 为 nil（如构造后未正确初始化）则返回 error，防止 panic。
func (g *GoRedisStreamClient) XAdd(ctx context.Context, stream string, values map[string]interface{}) error {
	if g.client == nil {
		return fmt.Errorf("redisruntime: GoRedisStreamClient.client is nil")
	}
	return g.client.XAdd(ctx, &redis.XAddArgs{
		Stream: stream,
		Values: values,
	}).Err()
}
