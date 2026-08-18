package redis

import (
	"context"
	"errors"
	"fmt"
	"strings"
	"time"

	"github.com/redis/go-redis/v9"
)

// GoRedisStreamReader 是 RedisStreamReader 的 go-redis v9 实现。
//
// 内部持有 *redis.Client，实现窄接口 XReadGroup、XAck 和 XGroupCreateMkStream，
// 不泄漏 go-redis 类型到 handler / bus 层。
//
// 真实连接由 factory（bus/factory.go）在 redis 模式下创建并注入；
// adapter 本身不管理连接生命周期。
type GoRedisStreamReader struct {
	client *redis.Client
}

// 编译期断言 GoRedisStreamReader 实现 RedisStreamReader 接口。
var _ RedisStreamReader = (*GoRedisStreamReader)(nil)
var _ RedisStreamReliability = (*GoRedisStreamReader)(nil)

const atomicDeadLetterScript = `
local source_stream = KEYS[1]
local dead_letter_stream = KEYS[2]
local dedupe_key = KEYS[3]
local group = ARGV[1]
local message_id = ARGV[2]
local ttl = tonumber(ARGV[3])
local maxlen = tonumber(ARGV[4])
if redis.call("EXISTS", dedupe_key) == 1 then
    redis.call("XACK", source_stream, group, message_id)
    return "0"
end
local fields = {}
for i = 5, #ARGV, 2 do
    table.insert(fields, ARGV[i])
    table.insert(fields, ARGV[i + 1])
end
local dead_letter_id = redis.call(
    "XADD", dead_letter_stream, "MAXLEN", "~", maxlen, "*", unpack(fields)
)
redis.call("SET", dedupe_key, "1", "EX", ttl)
redis.call("XACK", source_stream, group, message_id)
return dead_letter_id
`

// NewGoRedisStreamReader 创建 go-redis 适配的 stream reader。
//
// client 由调用方创建并注入（如 redis.NewClient(&redis.Options{Addr: "localhost:6379"})）。
// 若 client 为 nil 则返回 error，防止 nil panic。
// 真实 *redis.Client 由 factory 在 redis 模式下创建。
func NewGoRedisStreamReader(client *redis.Client) (*GoRedisStreamReader, error) {
	if client == nil {
		return nil, fmt.Errorf("redisruntime: cannot create GoRedisStreamReader with nil *redis.Client")
	}
	return &GoRedisStreamReader{client: client}, nil
}

// XReadGroup 调用 go-redis v9 的 XReadGroup，从指定 stream 的 consumer group 非阻塞读取消息。
//
// 内部调用 client.XReadGroup，Block 设为 -1（不发送 BLOCK 参数，非阻塞读取）。
// 将 go-redis 返回的 XMessage 转换为项目内部 StreamMessage。
// 若 stream 中无新消息，返回空切片 + nil error（redis.Nil 被归一化为空切片）。
// 若内部 client 为 nil（如构造后未正确初始化）则返回 error，防止 panic。
func (g *GoRedisStreamReader) XReadGroup(ctx context.Context, group, consumer, stream, id string, count int64) ([]StreamMessage, error) {
	if g.client == nil {
		return nil, fmt.Errorf("redisruntime: GoRedisStreamReader.client is nil")
	}

	result, err := g.client.XReadGroup(ctx, &redis.XReadGroupArgs{
		Group:    group,
		Consumer: consumer,
		Streams:  []string{stream, id},
		Count:    count,
		Block:    -1, // 负数跳过 BLOCK 参数 → 非阻塞读取；0 会导致 BLOCK 0（无限阻塞）
	}).Result()
	if err != nil {
		// redis.Nil 表示无新消息（非阻塞读取超时），归一化为空切片
		if errors.Is(err, redis.Nil) {
			return []StreamMessage{}, nil
		}
		return nil, fmt.Errorf("redisruntime: xreadgroup %s > %s: %w", stream, group, err)
	}

	// 将 go-redis 类型转换为项目内部 StreamMessage
	var msgs []StreamMessage
	for _, streamResult := range result {
		for _, msg := range streamResult.Messages {
			msgs = append(msgs, StreamMessage{
				ID:     msg.ID,
				Values: msg.Values,
			})
		}
	}
	return msgs, nil
}

// XAck 调用 go-redis v9 的 XAck，确认已成功处理的消息。
//
// 若内部 client 为 nil 则返回 error，防止 panic。
func (g *GoRedisStreamReader) XAck(ctx context.Context, stream, group string, ids ...string) error {
	if g.client == nil {
		return fmt.Errorf("redisruntime: GoRedisStreamReader.client is nil")
	}

	if err := g.client.XAck(ctx, stream, group, ids...).Err(); err != nil {
		return fmt.Errorf("redisruntime: xack %s > %s: %w", stream, group, err)
	}
	return nil
}

// XGroupCreateMkStream 调用 go-redis v9 的 XGroupCreateMkStream，幂等创建 consumer group。
//
// 若 group 已存在（Redis 返回 BUSYGROUP），视为成功返回 nil。
// 若内部 client 为 nil 则返回 error。
// startID 为 consumer group 起始消费 ID（如 "0" 从开头消费，"$" 仅新消息）。
func (g *GoRedisStreamReader) XGroupCreateMkStream(ctx context.Context, stream, group, startID string) error {
	if g.client == nil {
		return fmt.Errorf("redisruntime: GoRedisStreamReader.client is nil")
	}

	err := g.client.XGroupCreateMkStream(ctx, stream, group, startID).Err()
	if err != nil && strings.Contains(err.Error(), "BUSYGROUP") {
		// group 已存在 → 幂等，不报错
		return nil
	}
	if err != nil {
		return fmt.Errorf("redisruntime: xgroup create %s on %s: %w", group, stream, err)
	}
	return nil
}

func (g *GoRedisStreamReader) XPending(
	ctx context.Context, stream, group, start string, count int64,
) ([]PendingStreamEntry, error) {
	if g.client == nil {
		return nil, fmt.Errorf("redisruntime: GoRedisStreamReader.client is nil")
	}
	entries, err := g.client.XPendingExt(ctx, &redis.XPendingExtArgs{
		Stream: stream, Group: group, Start: start, End: "+", Count: count,
	}).Result()
	if err != nil {
		return nil, fmt.Errorf("redisruntime: xpending %s > %s: %w", stream, group, err)
	}
	result := make([]PendingStreamEntry, 0, len(entries))
	for _, entry := range entries {
		result = append(result, PendingStreamEntry{
			ID: entry.ID, Idle: entry.Idle, Deliveries: entry.RetryCount,
		})
	}
	return result, nil
}

func (g *GoRedisStreamReader) XClaim(
	ctx context.Context, stream, group, consumer string,
	minIdle time.Duration, ids ...string,
) ([]StreamMessage, error) {
	if g.client == nil {
		return nil, fmt.Errorf("redisruntime: GoRedisStreamReader.client is nil")
	}
	messages, err := g.client.XClaim(ctx, &redis.XClaimArgs{
		Stream: stream, Group: group, Consumer: consumer,
		MinIdle: minIdle, Messages: ids,
	}).Result()
	if err != nil {
		return nil, fmt.Errorf("redisruntime: xclaim %s > %s: %w", stream, group, err)
	}
	result := make([]StreamMessage, 0, len(messages))
	for _, message := range messages {
		result = append(result, StreamMessage{ID: message.ID, Values: message.Values})
	}
	return result, nil
}

func (g *GoRedisStreamReader) MoveToDeadLetter(
	ctx context.Context,
	sourceStream, deadLetterStream, group, messageID, dedupeKey string,
	dedupeTTL time.Duration,
	maxLen int64,
	fields map[string]interface{},
) (string, error) {
	if g.client == nil {
		return "", fmt.Errorf("redisruntime: GoRedisStreamReader.client is nil")
	}
	args := []interface{}{group, messageID, int64(dedupeTTL.Seconds()), maxLen}
	for key, value := range fields {
		args = append(args, key, value)
	}
	result, err := g.client.Eval(
		ctx, atomicDeadLetterScript,
		[]string{sourceStream, deadLetterStream, dedupeKey}, args...,
	).Result()
	if err != nil {
		return "", fmt.Errorf("redisruntime: dead-letter %s: %w", messageID, err)
	}
	return fmt.Sprint(result), nil
}
