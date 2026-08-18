package redis

import (
	"context"
	"fmt"
)

// RedisRuntimeTransport 是 Redis Runtime Bus 的写入 adapter。
//
// 职责：
//   - 将已验证的消息转换为 stream fields 并写入 Redis Streams
//   - 在写入前对每条消息做类型化校验（通过 Decode* 函数）
//   - 使用 *ToStreamFields helper 生成统一 XADD field shape：
//     schema_version + payload（完整 JSON 字符串）+ 冗余标量路由字段
//
// 不负责：
//   - Task / Run / Step / ToolCall / Permission / AuditLog 的业务状态真源
//   - Agent loop / LLM / 工具执行
//   - 从 Redis 读取消息（2B-1c 再做 read/ack/fan-out）
//   - 实现 RuntimeStateStore
//   - 替换 InMemoryRuntimeBus 的默认路径
//
// 约束：
//
//	Redis 是运行时通信层，不是业务数据库。
//	所有 XADD fields 来自同一 message struct，nested object 不直接作为 Redis field value。
type RedisRuntimeTransport struct {
	client RedisStreamClient
}

// NewRedisRuntimeTransport 创建 transport，注入 RedisStreamClient。
// 测试中用 fakeStreamClient；生产中用 GoRedisStreamClient（go-redis v9 适配）。
// 若 client 为 nil 则返回 error，防止 nil panic。
func NewRedisRuntimeTransport(client RedisStreamClient) (*RedisRuntimeTransport, error) {
	if client == nil {
		return nil, fmt.Errorf("redisruntime: cannot create RedisRuntimeTransport with nil RedisStreamClient")
	}
	return &RedisRuntimeTransport{client: client}, nil
}

// EnqueueRunJob 校验 RunJobMessage 并写入 StreamRunQueue。
// 校验失败或 XAdd 失败均返回 error。
// XADD fields 使用 RunJobToStreamFields 生成。
func (t *RedisRuntimeTransport) EnqueueRunJob(ctx context.Context, msg RunJobMessage) error {
	if err := validateRunJob(msg); err != nil {
		return fmt.Errorf("redisruntime: enqueue run job: %w", err)
	}
	fields, err := RunJobToStreamFields(msg)
	if err != nil {
		return fmt.Errorf("redisruntime: enqueue run job: %w", err)
	}
	if err := t.client.XAdd(ctx, StreamRunQueue, fields); err != nil {
		return fmt.Errorf("redisruntime: enqueue run job: xadd: %w", err)
	}
	return nil
}

// PublishPermissionDecision 校验 PermissionDecisionCommand 并写入 StreamWorkerCommand。
// 校验失败或 XAdd 失败均返回 error。
// XADD fields 使用 PermissionDecisionToStreamFields 生成。
func (t *RedisRuntimeTransport) PublishPermissionDecision(ctx context.Context, cmd PermissionDecisionCommand) error {
	if err := validatePermissionDecision(cmd); err != nil {
		return fmt.Errorf("redisruntime: publish permission decision: %w", err)
	}
	fields, err := PermissionDecisionToStreamFields(cmd)
	if err != nil {
		return fmt.Errorf("redisruntime: publish permission decision: %w", err)
	}
	if err := t.client.XAdd(ctx, StreamWorkerCommand, fields); err != nil {
		return fmt.Errorf("redisruntime: publish permission decision: xadd: %w", err)
	}
	return nil
}

// PublishRuntimeEvent 校验 RuntimeEventEnvelope 并写入 StreamRuntimeEvent。
// 校验包括 schema_version 精确匹配、envelope 与内层一致性等。
// 校验失败或 XAdd 失败均返回 error。
// XADD fields 使用 RuntimeEventToStreamFields 生成。
func (t *RedisRuntimeTransport) PublishRuntimeEvent(ctx context.Context, env RuntimeEventEnvelope) error {
	if err := validateRuntimeEvent(env); err != nil {
		return fmt.Errorf("redisruntime: publish runtime event: %w", err)
	}
	fields, err := RuntimeEventToStreamFields(env)
	if err != nil {
		return fmt.Errorf("redisruntime: publish runtime event: %w", err)
	}
	if err := t.client.XAdd(ctx, StreamRuntimeEvent, fields); err != nil {
		return fmt.Errorf("redisruntime: publish runtime event: xadd: %w", err)
	}
	return nil
}

// PublishRunCancel 校验 RunCancelCommand 并写入 StreamWorkerCommand。
//
// 用于 Gateway 向 Python worker 发送取消运行命令（3C cancel）。
// 校验失败或 XAdd 失败均返回 error。
// XADD fields 使用 RunCancelToStreamFields 生成。
func (t *RedisRuntimeTransport) PublishRunCancel(ctx context.Context, cmd RunCancelCommand) error {
	if err := validateRunCancel(cmd); err != nil {
		return fmt.Errorf("redisruntime: publish run cancel: %w", err)
	}
	fields, err := RunCancelToStreamFields(cmd)
	if err != nil {
		return fmt.Errorf("redisruntime: publish run cancel: %w", err)
	}
	if err := t.client.XAdd(ctx, StreamWorkerCommand, fields); err != nil {
		return fmt.Errorf("redisruntime: publish run cancel: xadd: %w", err)
	}
	return nil
}

func (t *RedisRuntimeTransport) PublishMcpDiscoveryRefresh(
	ctx context.Context, cmd McpDiscoveryRefreshCommand,
) error {
	if err := validateMcpDiscoveryRefresh(cmd); err != nil {
		return fmt.Errorf("redisruntime: publish MCP discovery refresh: %w", err)
	}
	fields, err := McpDiscoveryRefreshToStreamFields(cmd)
	if err != nil {
		return fmt.Errorf("redisruntime: publish MCP discovery refresh: %w", err)
	}
	if err := t.client.XAdd(ctx, StreamWorkerCommand, fields); err != nil {
		return fmt.Errorf("redisruntime: publish MCP discovery refresh: xadd: %w", err)
	}
	return nil
}

// PublishWorkerHeartbeat 校验 WorkerHeartbeatMessage 并写入 StreamWorkerHeartbeat。
//
// 心跳是状态探针，不要求 trace_id。
// 校验失败或 XAdd 失败均返回 error。
// XADD fields 使用 WorkerHeartbeatToStreamFields 生成。
func (t *RedisRuntimeTransport) PublishWorkerHeartbeat(ctx context.Context, hb WorkerHeartbeatMessage) error {
	if err := validateHeartbeat(hb); err != nil {
		return fmt.Errorf("redisruntime: publish worker heartbeat: %w", err)
	}
	fields, err := WorkerHeartbeatToStreamFields(hb)
	if err != nil {
		return fmt.Errorf("redisruntime: publish worker heartbeat: %w", err)
	}
	if err := t.client.XAdd(ctx, StreamWorkerHeartbeat, fields); err != nil {
		return fmt.Errorf("redisruntime: publish worker heartbeat: xadd: %w", err)
	}
	return nil
}

// -- 校验 helper（复用 Decode*，避免 struct→map→struct 往返） --

func validateRunJob(msg RunJobMessage) error {
	m, err := ToMap(msg)
	if err != nil {
		return err
	}
	_, err = DecodeRunJobMessage(m)
	return err
}

func validateRuntimeEvent(env RuntimeEventEnvelope) error {
	m, err := ToMap(env)
	if err != nil {
		return err
	}
	_, err = DecodeRuntimeEventEnvelope(m)
	return err
}

func validatePermissionDecision(cmd PermissionDecisionCommand) error {
	m, err := ToMap(cmd)
	if err != nil {
		return err
	}
	_, err = DecodePermissionDecisionCommand(m)
	return err
}

func validateRunCancel(cmd RunCancelCommand) error {
	m, err := ToMap(cmd)
	if err != nil {
		return err
	}
	_, err = DecodeRunCancelCommand(m)
	return err
}

func validateMcpDiscoveryRefresh(cmd McpDiscoveryRefreshCommand) error {
	m, err := ToMap(cmd)
	if err != nil {
		return err
	}
	_, err = DecodeMcpDiscoveryRefreshCommand(m)
	return err
}

func validateHeartbeat(hb WorkerHeartbeatMessage) error {
	m, err := ToMap(hb)
	if err != nil {
		return err
	}
	_, err = DecodeWorkerHeartbeatMessage(m)
	return err
}
