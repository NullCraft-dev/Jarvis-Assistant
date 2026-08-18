// RuntimeBus 工厂：根据配置创建 InMemoryRuntimeBus 或 RedisRuntimeBus。
//
// 职责：
//   - 从环境变量读取 RuntimeBus 配置
//   - 校验配置合法性
//   - 根据配置创建对应的 RuntimeBus + RuntimeStateStore 实现
//   - redis 模式下同时创建 event pump（PumpCloser）
//
// 不负责：
//   - 承载业务语义（本文件只做连线，不做决策）
//   - 启动 Python worker
//   - 管理 pump 生命周期（由 main.go 调用 Start/Close）
//
// 约束：
//   - 默认走 Redis 真实跨进程链路
//   - in-memory 仅在显式配置时用于测试/隔离运行
//   - 配置读取不散落在 handler 或 UI 层
//   - pump 不由工厂启动，由调用方管理生命周期
//
// 真源：docs/13-interface-contract.md § Redis-backed RuntimeBus 接线约定
package orchestrator

import (
	"context"
	"fmt"
	"os"
	"strconv"
	"time"

	runtimeredis "github.com/jarvis-assistant/gateway/internal/redis"
	goredis "github.com/redis/go-redis/v9"
)

// RuntimeBusConfig 是 Gateway runtime bus 的配置。
//
// 字段从环境变量读取；默认连接本地 Redis。
type RuntimeBusConfig struct {
	// BusType 为 runtime bus 类型："inmemory" 或 "redis"。
	// 默认 "redis"。
	BusType string

	// RedisAddr 是 Redis 服务地址。
	// 默认 "127.0.0.1:6379"。
	RedisAddr string

	// RedisPassword 是 Redis 认证密码（可选）。
	RedisPassword string

	// RedisDB 是 Redis 数据库编号（可选）。
	// 默认 0。
	RedisDB int

	// GatewayID 用作 Redis consumer name；多 Gateway 实例必须唯一。
	GatewayID string
}

// RuntimeBusConfigFromEnv 从环境变量读取 RuntimeBus 配置。
//
// 环境变量：
//   - JARVIS_RUNTIME_BUS：默认 "redis"
//   - JARVIS_REDIS_ADDR：默认 "127.0.0.1:6379"
//   - JARVIS_REDIS_PASSWORD：可选
//   - JARVIS_REDIS_DB：可选，默认 0
//   - JARVIS_GATEWAY_ID：可选；默认 hostname + pid，作为唯一 consumer name
func RuntimeBusConfigFromEnv() RuntimeBusConfig {
	cfg := RuntimeBusConfig{
		BusType:   "redis",
		RedisAddr: "127.0.0.1:6379",
		RedisDB:   0,
		GatewayID: defaultGatewayID(),
	}

	if v := os.Getenv("JARVIS_RUNTIME_BUS"); v != "" {
		cfg.BusType = v
	}
	if v := os.Getenv("JARVIS_REDIS_ADDR"); v != "" {
		cfg.RedisAddr = v
	}
	if v := os.Getenv("JARVIS_REDIS_PASSWORD"); v != "" {
		cfg.RedisPassword = v
	}
	if v := os.Getenv("JARVIS_REDIS_DB"); v != "" {
		if db, err := strconv.Atoi(v); err == nil {
			cfg.RedisDB = db
		}
	}
	if v := os.Getenv("JARVIS_GATEWAY_ID"); v != "" {
		cfg.GatewayID = v
	}

	return cfg
}

// Validate 校验配置合法性。
// BusType 仅允许 "inmemory" 或 "redis"，非法值返回明确 error。
func (c RuntimeBusConfig) Validate() error {
	switch c.BusType {
	case "inmemory", "redis":
		return nil
	default:
		return fmt.Errorf("bus: 不支持的 JARVIS_RUNTIME_BUS 值: %q，期望 \"inmemory\" 或 \"redis\"", c.BusType)
	}
}

// NewRuntimeBus 根据配置创建 RuntimeBus、RuntimeStateStore 和 PumpCloser。
//
// RuntimeBus 和 RuntimeStateStore 可能由同一实例实现。
// PumpCloser 在 inmemory 模式下为 nil，redis 模式下非 nil。
//
// inmemory 模式：创建 InMemoryRuntimeBus，不需要 Redis，无 pump。
// redis 模式：创建真实 go-redis client，组合 RedisRuntimeBus + EventPump。
//
// 若配置非法或 Redis client 初始化失败，返回 error。
func NewRuntimeBus(cfg RuntimeBusConfig) (RuntimeBus, RuntimeStateStore, PumpCloser, error) {
	if err := cfg.Validate(); err != nil {
		return nil, nil, nil, err
	}

	switch cfg.BusType {
	case "inmemory":
		b := NewInMemoryRuntimeBus()
		return b, b, nil, nil
	case "redis":
		return newRedisRuntimeBus(cfg)
	default:
		return nil, nil, nil, fmt.Errorf("bus: 不支持的 runtime bus 类型: %s", cfg.BusType)
	}
}

// newRedisRuntimeBus 创建连接真实 Redis 的 RedisRuntimeBus + EventPump。
//
// 内部流程：
//  1. 创建 go-redis *runtimeredis.Client
//  2. 用 PING 验证 Redis 连通性（2 秒超时）
//  3. 包装为 GoRedisStreamClient（写）+ GoRedisStreamReader（读）
//  4. 创建 RuntimeEventReader（解码/校验）
//  5. 创建 RedisRuntimeTransport
//  6. 创建 RedisRuntimeBus（组合 InMemoryRuntimeBus + Transport + EventPump）
//
// PING 失败时返回错误，Gateway 启动应失败。
func newRedisRuntimeBus(cfg RuntimeBusConfig) (RuntimeBus, RuntimeStateStore, PumpCloser, error) {
	rdb := goredis.NewClient(&goredis.Options{
		Addr:     cfg.RedisAddr,
		Password: cfg.RedisPassword,
		DB:       cfg.RedisDB,
	})

	// 验证 Redis 连通性
	ctx, cancel := context.WithTimeout(context.Background(), 2*time.Second)
	defer cancel()
	if err := rdb.Ping(ctx).Err(); err != nil {
		return nil, nil, nil, fmt.Errorf("bus: Redis 连接失败 (%s): %w", cfg.RedisAddr, err)
	}

	// 包装为窄接口：写侧
	streamClient, err := runtimeredis.NewGoRedisStreamClient(rdb)
	if err != nil {
		return nil, nil, nil, fmt.Errorf("bus: 创建 Redis stream client 失败: %w", err)
	}

	// 包装为窄接口：读侧
	streamReader, err := runtimeredis.NewGoRedisStreamReader(rdb)
	if err != nil {
		return nil, nil, nil, fmt.Errorf("bus: 创建 Redis stream reader 失败: %w", err)
	}
	diagnostics, err := runtimeredis.NewGoRedisRuntimeDiagnostics(rdb)
	if err != nil {
		return nil, nil, nil, fmt.Errorf("bus: 创建 Redis diagnostics 失败: %w", err)
	}

	// 创建事件读取/解码器
	eventReader, err := runtimeredis.NewRuntimeEventReader(streamReader)
	if err != nil {
		return nil, nil, nil, fmt.Errorf("bus: 创建 RuntimeEventReader 失败: %w", err)
	}

	runtimeBus, stateStore, pump, err := newRedisRuntimeBusWithComponents(
		streamClient, eventReader, streamReader, cfg.GatewayID,
	)
	if err != nil {
		return nil, nil, nil, err
	}
	if redisBus, ok := runtimeBus.(*RedisRuntimeBus); ok {
		redisBus.SetRuntimeDiagnosticsReader(diagnostics)
	}
	return runtimeBus, stateStore, pump, nil
}

// newRedisRuntimeBusWithComponents 使用已创建好的组件构造 RedisRuntimeBus + EventPump + HeartbeatPump。
//
// 本函数接受窄接口，不关心底层是真实 Redis 还是 fake。
// 生产代码走 newRedisRuntimeBus（真实 go-redis client）；
// 测试可直接注入 fake client/reader 验证工厂逻辑。
func newRedisRuntimeBusWithComponents(
	client runtimeredis.RedisStreamClient,
	eventReader *runtimeredis.RuntimeEventReader,
	streamReader runtimeredis.RedisStreamReader,
	consumerNames ...string,
) (RuntimeBus, RuntimeStateStore, PumpCloser, error) {
	transport, err := runtimeredis.NewRedisRuntimeTransport(client)
	if err != nil {
		return nil, nil, nil, fmt.Errorf("bus: 创建 Redis transport 失败: %w", err)
	}

	// 3B: 同时创建 heartbeat reader（复用同一个 streamReader）
	// 仅当 streamReader 非 nil 时创建（测试可能传 nil）
	var heartbeatReader *runtimeredis.HeartbeatReader
	if streamReader != nil {
		var err error
		heartbeatReader, err = runtimeredis.NewHeartbeatReader(streamReader)
		if err != nil {
			return nil, nil, nil, fmt.Errorf("bus: 创建 HeartbeatReader 失败: %w", err)
		}
	}

	backoff := NewExponentialBackoff(100*time.Millisecond, 5*time.Second)
	rb, err := NewRedisRuntimeBus(
		transport,
		eventReader,
		streamReader,
		backoff,
		heartbeatReader,
		DefaultStaleTimeout,
		consumerNames...,
	)
	if err != nil {
		return nil, nil, nil, fmt.Errorf("bus: 创建 RedisRuntimeBus 失败: %w", err)
	}

	return rb, rb, rb, nil
}

func defaultGatewayID() string {
	hostname, err := os.Hostname()
	if err != nil || hostname == "" {
		hostname = "local"
	}
	return fmt.Sprintf("gateway-%s-%d", hostname, os.Getpid())
}
