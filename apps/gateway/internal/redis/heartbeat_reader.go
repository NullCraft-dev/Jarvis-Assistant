// HeartbeatReader 从 StreamWorkerHeartbeat 读取心跳消息并解码校验。
//
// 职责：
//   - 通过 RedisStreamReader 从 StreamWorkerHeartbeat 读取新消息（含 consumer group）
//   - 从 payload JSON string 解码为 WorkerHeartbeatMessage
//   - 调用 DecodeWorkerHeartbeatMessage 做类型化校验
//   - 提供独立的 Ack 方法确认已成功处理的消息
//   - 支持幂等创建 consumer group (XGroupCreateMkStream)
//
// 不负责：
//   - 消息写入（由 RedisRuntimeTransport 负责）
//   - Worker 状态业务判断（由 WorkerStatusView 负责）
//   - 成为 Worker 状态业务真源
//
// 约束：
//   - nested object 只能来自 payload JSON string
//   - 解码失败不 ack
//   - heartbeat 不校验 trace_id
package redis

import (
	"context"
	"encoding/json"
	"fmt"
)

// HeartbeatReader 从 StreamWorkerHeartbeat 读取、解码、校验、ack 心跳消息。
type HeartbeatReader struct {
	reader RedisStreamReader
}

// NewHeartbeatReader 创建 HeartbeatReader。
// 若 reader 为 nil 则返回 error。
func NewHeartbeatReader(reader RedisStreamReader) (*HeartbeatReader, error) {
	if reader == nil {
		return nil, fmt.Errorf("redisruntime: cannot create HeartbeatReader with nil RedisStreamReader")
	}
	return &HeartbeatReader{reader: reader}, nil
}

// ReadHeartbeats 从 StreamWorkerHeartbeat 读取新消息并解码为 WorkerHeartbeatMessage 列表。
//
// 使用 consumer group 模式读取，id 固定为 ">"（仅新消息）。
// 返回三个值：
//   - heartbeats：成功解码的 WorkerHeartbeatMessage 切片
//   - msgIDs：对应的 Redis 消息 id 切片（用于后续 Ack）
//   - error：读取或解码失败时返回
//
// 解码失败时：返回 error 且不 ack 任何消息。
// 空读取（stream 中无新消息）返回空切片和 nil error。
func (r *HeartbeatReader) ReadHeartbeats(ctx context.Context, group, consumer string, count int64) ([]WorkerHeartbeatMessage, []string, error) {
	msgs, err := r.reader.XReadGroup(ctx, group, consumer, StreamWorkerHeartbeat, ">", count)
	if err != nil {
		return nil, nil, fmt.Errorf("redisruntime: read heartbeats from %s: %w", StreamWorkerHeartbeat, err)
	}

	if len(msgs) == 0 {
		return nil, nil, nil
	}

	heartbeats := make([]WorkerHeartbeatMessage, 0, len(msgs))
	msgIDs := make([]string, 0, len(msgs))

	for _, msg := range msgs {
		hb, err := decodeHeartbeatFromStreamMsg(msg)
		if err != nil {
			return nil, nil, fmt.Errorf("redisruntime: read heartbeats: %w", err)
		}
		heartbeats = append(heartbeats, hb)
		msgIDs = append(msgIDs, msg.ID)
	}

	return heartbeats, msgIDs, nil
}

// AckHeartbeats 确认已成功处理的心跳消息。
func (r *HeartbeatReader) AckHeartbeats(ctx context.Context, group string, ids ...string) error {
	if len(ids) == 0 {
		return nil
	}
	if err := r.reader.XAck(ctx, StreamWorkerHeartbeat, group, ids...); err != nil {
		return fmt.Errorf("redisruntime: ack heartbeats: %w", err)
	}
	return nil
}

// CreateGroupIfNotExists 幂等创建消费者组（XGroupCreateMkStream）。
func (r *HeartbeatReader) CreateGroupIfNotExists(ctx context.Context, group, startID string) error {
	return r.reader.XGroupCreateMkStream(ctx, StreamWorkerHeartbeat, group, startID)
}

// decodeHeartbeatFromStreamMsg 从 StreamMessage 解码并校验 WorkerHeartbeatMessage。
func decodeHeartbeatFromStreamMsg(msg StreamMessage) (WorkerHeartbeatMessage, error) {
	payloadRaw, ok := msg.Values[FieldPayload]
	if !ok {
		return WorkerHeartbeatMessage{}, fmt.Errorf("redisruntime: heartbeat message %s missing field %s", msg.ID, FieldPayload)
	}
	payloadStr, ok := payloadRaw.(string)
	if !ok {
		return WorkerHeartbeatMessage{}, fmt.Errorf("redisruntime: heartbeat message %s field %s is not a string, got %T", msg.ID, FieldPayload, payloadRaw)
	}

	var payloadMap map[string]interface{}
	if err := json.Unmarshal([]byte(payloadStr), &payloadMap); err != nil {
		return WorkerHeartbeatMessage{}, fmt.Errorf("redisruntime: heartbeat message %s invalid payload JSON: %w", msg.ID, err)
	}

	hb, err := DecodeWorkerHeartbeatMessage(payloadMap)
	if err != nil {
		return WorkerHeartbeatMessage{}, fmt.Errorf("redisruntime: heartbeat message %s decode: %w", msg.ID, err)
	}

	return hb, nil
}
