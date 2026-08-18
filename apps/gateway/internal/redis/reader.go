package redis

import (
	"context"
	"crypto/sha256"
	"encoding/json"
	"fmt"
	"strings"
	"time"
)

// RedisStreamReader 是 Redis Streams 读取操作的最小接口。
//
// 本接口只暴露 RuntimeEvent 消费所需的 XReadGroup 和 XAck 方法，
// 不泄漏 go-redis 类型到 handler / bus 接口层。
//
// 真实实现：GoRedisStreamReader（go-redis v9 适配）。
// 测试实现：fakeStreamReader（定义在 reader_test.go）。
type RedisStreamReader interface {
	// XReadGroup 从指定 stream 的 consumer group 中读取消息。
	//
	// group 是 consumer group 名称。
	// consumer 是 consumer 实例名称。
	// stream 是目标 stream key。
	// id 是读取起始 id：">" 表示只读新消息，"0" 或具体 id 表示读取 pending。
	// count 是单次读取的最大消息数。
	//
	// 返回的 []StreamMessage 是项目内部类型，不包含 go-redis 类型。
	// 若 stream 中无新消息且不 block，返回空切片（nil error）。
	XReadGroup(ctx context.Context, group, consumer, stream, id string, count int64) ([]StreamMessage, error)

	// XAck 确认已成功处理的消息，从 consumer group pending 列表中移除。
	//
	// 只对已成功解码并处理的消息调用 ack。
	// ack 失败返回 error。
	XAck(ctx context.Context, stream, group string, ids ...string) error

	// XGroupCreateMkStream 创建 consumer group（若 stream 不存在则创建 stream）。
	//
	// startID 是 consumer group 的起始消费 ID：
	//   - "0" 表示从 stream 开头消费所有已有消息
	//   - "$" 表示仅消费创建之后的新消息
	//
	// 若 group 已存在（Redis BUSYGROUP 错误），实现应返回 nil（幂等）。
	// 其他错误原样返回。
	//
	// 由 EventPump 在 Start 时调用，确保 consumer group 存在后再进入读取循环。
	XGroupCreateMkStream(ctx context.Context, stream, group, startID string) error
}

// PendingStreamEntry 是 Redis PEL 的项目内表示，不泄漏 go-redis 类型。
type PendingStreamEntry struct {
	ID         string
	Idle       time.Duration
	Deliveries int64
}

// RedisStreamReliability 是 pending 接管与 DLQ 所需的可选窄接口。
// Heartbeat 等只读消费者无需实现；EventPump 在运行时按能力检测。
type RedisStreamReliability interface {
	XPending(ctx context.Context, stream, group, start string, count int64) ([]PendingStreamEntry, error)
	XClaim(ctx context.Context, stream, group, consumer string, minIdle time.Duration, ids ...string) ([]StreamMessage, error)
	MoveToDeadLetter(
		ctx context.Context,
		sourceStream, deadLetterStream, group, messageID, dedupeKey string,
		dedupeTTL time.Duration,
		maxLen int64,
		fields map[string]interface{},
	) (string, error)
}

// StreamMessage 是 Redis Stream 消息的项目内部表示。
//
// 不直接暴露 go-redis 的 XMessage 类型。
// Values 是 XADD 时写入的 field-value 对，
// 统一格式为：schema_version + payload（完整 JSON 字符串）+ 冗余标量路由字段。
type StreamMessage struct {
	// ID 是 Redis 分配的消息 id（如 "1234567890123-0"）。
	ID string
	// Values 是消息的 field-value 对。
	Values map[string]interface{}
}

// RuntimeEventDelivery 保留单条消息的解码结果与 PEL 元数据。
// 解码错误按消息隔离，避免一条 poison event 阻塞同批正常事件。
type RuntimeEventDelivery struct {
	MessageID     string
	Fields        map[string]interface{}
	Envelope      RuntimeEventEnvelope
	DeliveryCount int64
	Reclaimed     bool
	ErrorCode     string
	ErrorMessage  string
}

func (d RuntimeEventDelivery) Valid() bool {
	return d.ErrorCode == ""
}

// RuntimeEventReader 从 StreamRuntimeEvent 读取消息、解码校验并 ack。
//
// 职责：
//   - 通过 RedisStreamReader 从 StreamRuntimeEvent 读取消息
//   - 从 payload JSON string 解码为 RuntimeEventEnvelope
//   - 调用 DecodeRuntimeEventEnvelope 做类型化校验
//   - 提供独立的 AckEvents 方法确认已成功处理的消息
//
// 不负责：
//   - 消息写入（由 RedisRuntimeTransport 负责）
//   - 事件扇出 / fan-out 到 Web UI
//   - Task / Run / Step / ToolCall / Permission / AuditLog 的业务状态
//   - 替换 InMemoryRuntimeBus
//
// 约束：
//
//	nested object 只能来自 payload JSON string，不从 Redis scalar field 拼装 runtime_event。
//	解码失败不 ack。
//	schema_version 不匹配、payload 缺失/非 string/无效 JSON、event_type 不一致均返回 error。
type RuntimeEventReader struct {
	reader           RedisStreamReader
	pendingScanStart string
}

// NewRuntimeEventReader 创建 RuntimeEventReader，注入 RedisStreamReader。
//
// 测试中用 fakeStreamReader；生产中用 GoRedisStreamReader。
// 若 reader 为 nil 则返回 error，防止 nil panic。
func NewRuntimeEventReader(reader RedisStreamReader) (*RuntimeEventReader, error) {
	if reader == nil {
		return nil, fmt.Errorf("redisruntime: cannot create RuntimeEventReader with nil RedisStreamReader")
	}
	return &RuntimeEventReader{reader: reader, pendingScanStart: "-"}, nil
}

// ReadDeliveries 逐条解码新消息；单条格式错误不会丢弃同批其他消息。
func (r *RuntimeEventReader) ReadDeliveries(
	ctx context.Context, group, consumer string, count int64,
) ([]RuntimeEventDelivery, error) {
	msgs, err := r.reader.XReadGroup(ctx, group, consumer, StreamRuntimeEvent, ">", count)
	if err != nil {
		return nil, fmt.Errorf("redisruntime: read events from %s: %w", StreamRuntimeEvent, err)
	}
	deliveries := make([]RuntimeEventDelivery, 0, len(msgs))
	for _, msg := range msgs {
		deliveries = append(deliveries, decodeRuntimeEventDelivery(msg, 1, false))
	}
	return deliveries, nil
}

// ClaimStaleDelivery 有界扫描并接管一条 stale runtime event。
func (r *RuntimeEventReader) ClaimStaleDelivery(
	ctx context.Context, group, consumer string, baseIdle time.Duration,
) (*RuntimeEventDelivery, error) {
	reliable, ok := r.reader.(RedisStreamReliability)
	if !ok {
		return nil, nil
	}
	pending, err := reliable.XPending(
		ctx, StreamRuntimeEvent, group, r.pendingScanStart, 20,
	)
	if err != nil {
		return nil, fmt.Errorf("redisruntime: xpending runtime-event: %w", err)
	}
	if len(pending) == 0 {
		r.pendingScanStart = "-"
		return nil, nil
	}
	for _, entry := range pending {
		requiredIdle := runtimeEventRetryIdle(baseIdle, entry.Deliveries)
		if entry.ID == "" || entry.Idle < requiredIdle {
			continue
		}
		messages, claimErr := reliable.XClaim(
			ctx, StreamRuntimeEvent, group, consumer, requiredIdle, entry.ID,
		)
		if claimErr != nil {
			return nil, fmt.Errorf("redisruntime: xclaim runtime-event %s: %w", entry.ID, claimErr)
		}
		if len(messages) == 0 {
			continue
		}
		if len(pending) < 20 {
			r.pendingScanStart = "-"
		} else {
			r.pendingScanStart = "(" + entry.ID
		}
		delivery := decodeRuntimeEventDelivery(messages[0], entry.Deliveries+1, true)
		return &delivery, nil
	}
	lastID := pending[len(pending)-1].ID
	if len(pending) < 20 || lastID == "" {
		r.pendingScanStart = "-"
	} else {
		r.pendingScanStart = "(" + lastID
	}
	return nil, nil
}

func runtimeEventRetryIdle(base time.Duration, deliveries int64) time.Duration {
	if base <= 0 {
		base = 5 * time.Second
	}
	exponent := deliveries - 1
	if exponent < 0 {
		exponent = 0
	}
	if exponent > 2 {
		exponent = 2
	}
	return base * time.Duration(1<<exponent)
}

// DeadLetter 原子写入脱敏 runtime-event DLQ 并 ACK 原消息。
func (r *RuntimeEventReader) DeadLetter(
	ctx context.Context, group string, delivery RuntimeEventDelivery,
) (string, error) {
	reliable, ok := r.reader.(RedisStreamReliability)
	if !ok {
		return "", fmt.Errorf("redisruntime: stream reader does not support dead-letter")
	}
	payload, _ := delivery.Fields[FieldPayload].(string)
	digest := sha256.Sum256([]byte(payload))
	fields := map[string]interface{}{
		FieldSchemaVersion:    delivery.Fields[FieldSchemaVersion],
		"type":                "runtime.event.dead_letter",
		"original_stream":     StreamRuntimeEvent,
		"original_message_id": delivery.MessageID,
		"consumer_group":      group,
		"delivery_count":      delivery.DeliveryCount,
		"reclaimed":           delivery.Reclaimed,
		"error_code":          delivery.ErrorCode,
		"error_message":       sanitizeDiagnostic(delivery.ErrorMessage),
		"failed_at":           time.Now().UTC().Format(time.RFC3339Nano),
		"payload_sha256":      fmt.Sprintf("%x", digest),
		"payload_size_bytes":  len([]byte(payload)),
	}
	if fields[FieldSchemaVersion] == nil || fields[FieldSchemaVersion] == "" {
		fields[FieldSchemaVersion] = SchemaVersion
	}
	for _, key := range []string{FieldEventID, FieldTraceID, FieldTaskID, FieldRunID} {
		if value, exists := delivery.Fields[key]; exists && value != "" {
			fields[key] = value
		}
	}
	dedupeKey := fmt.Sprintf(
		"jarvis:runtime-event-dlq:dedupe:%s:%s", group, delivery.MessageID,
	)
	return reliable.MoveToDeadLetter(
		ctx, StreamRuntimeEvent, StreamRuntimeEventDeadLetter, group,
		delivery.MessageID, dedupeKey, 7*24*time.Hour, 10_000, fields,
	)
}

func sanitizeDiagnostic(message string) string {
	clean := strings.Join(strings.Fields(message), " ")
	runes := []rune(clean)
	if len(runes) > 300 {
		runes = runes[:300]
	}
	return string(runes)
}

func decodeRuntimeEventDelivery(
	msg StreamMessage, deliveryCount int64, reclaimed bool,
) RuntimeEventDelivery {
	delivery := RuntimeEventDelivery{
		MessageID: msg.ID, Fields: msg.Values,
		DeliveryCount: deliveryCount, Reclaimed: reclaimed,
	}
	outerVersion, ok := msg.Values[FieldSchemaVersion].(string)
	if !ok || outerVersion != SchemaVersion {
		delivery.ErrorCode = "RUNTIME_EVENT_SCHEMA_MISMATCH"
		delivery.ErrorMessage = fmt.Sprintf(
			"runtime-event outer schema_version 不匹配: %v", msg.Values[FieldSchemaVersion],
		)
		return delivery
	}
	env, err := decodeEnvelopeFromStreamMsg(msg)
	if err != nil {
		delivery.ErrorCode = "RUNTIME_EVENT_MALFORMED"
		delivery.ErrorMessage = err.Error()
		return delivery
	}
	routing := map[string]string{
		FieldEventID: string(env.EventID),
		FieldTraceID: string(env.TraceID),
		FieldTaskID:  string(env.TaskID),
		FieldRunID:   string(env.RunID),
		"type":       env.EventType,
	}
	for key, expected := range routing {
		actual, actualOK := msg.Values[key].(string)
		if !actualOK || actual != expected {
			delivery.ErrorCode = "RUNTIME_EVENT_ROUTING_MISMATCH"
			delivery.ErrorMessage = fmt.Sprintf(
				"runtime-event %s outer/payload 不一致", key,
			)
			return delivery
		}
	}
	delivery.Envelope = env
	return delivery
}

// ReadEvents 从 StreamRuntimeEvent 读取新消息并解码为 RuntimeEventEnvelope 列表。
//
// 使用 consumer group 模式读取，id 固定为 ">"（仅新消息）。
// 返回三个值：
//   - envelopes：成功解码的 RuntimeEventEnvelope 切片
//   - msgIDs：对应的 Redis 消息 id 切片（用于后续 AckEvents）
//   - error：读取或解码失败时返回
//
// 解码失败时：返回 error 且不 ack 任何消息。
// 空读取（stream 中无新消息）返回空切片和 nil error。
func (r *RuntimeEventReader) ReadEvents(ctx context.Context, group, consumer string, count int64) ([]RuntimeEventEnvelope, []string, error) {
	msgs, err := r.reader.XReadGroup(ctx, group, consumer, StreamRuntimeEvent, ">", count)
	if err != nil {
		return nil, nil, fmt.Errorf("redisruntime: read events from %s: %w", StreamRuntimeEvent, err)
	}

	if len(msgs) == 0 {
		return nil, nil, nil
	}

	envelopes := make([]RuntimeEventEnvelope, 0, len(msgs))
	msgIDs := make([]string, 0, len(msgs))

	for _, msg := range msgs {
		env, err := decodeEnvelopeFromStreamMsg(msg)
		if err != nil {
			// 解码失败不 ack，返回 error
			return nil, nil, fmt.Errorf("redisruntime: read events: %w", err)
		}
		envelopes = append(envelopes, env)
		msgIDs = append(msgIDs, msg.ID)
	}

	return envelopes, msgIDs, nil
}

// AckEvents 确认已成功处理的事件消息。
//
// 只对已成功解码并处理的消息 id 调用 ack。
// ids 为空时直接返回 nil（无操作）。
// ack 失败返回 error。
func (r *RuntimeEventReader) AckEvents(ctx context.Context, group string, ids ...string) error {
	if len(ids) == 0 {
		return nil
	}
	if err := r.reader.XAck(ctx, StreamRuntimeEvent, group, ids...); err != nil {
		return fmt.Errorf("redisruntime: ack events: %w", err)
	}
	return nil
}

// decodeEnvelopeFromStreamMsg 从 StreamMessage 解码并校验 RuntimeEventEnvelope。
//
// 解码流程：
//  1. 从 Values 中提取 payload 字段，校验存在且为 string
//  2. JSON decode payload 字符串到 map[string]interface{}
//  3. 调用 DecodeRuntimeEventEnvelope 做类型化校验
//
// nested object 只来自 payload JSON string，不从 Redis scalar field 拼装 runtime_event。
func decodeEnvelopeFromStreamMsg(msg StreamMessage) (RuntimeEventEnvelope, error) {
	// 1. 检查 payload 字段存在且为 string
	payloadRaw, ok := msg.Values[FieldPayload]
	if !ok {
		return RuntimeEventEnvelope{}, fmt.Errorf("redisruntime: stream message %s missing field %s", msg.ID, FieldPayload)
	}
	payloadStr, ok := payloadRaw.(string)
	if !ok {
		return RuntimeEventEnvelope{}, fmt.Errorf("redisruntime: stream message %s field %s is not a string, got %T", msg.ID, FieldPayload, payloadRaw)
	}

	// 2. JSON decode payload 到 map
	var payloadMap map[string]interface{}
	if err := json.Unmarshal([]byte(payloadStr), &payloadMap); err != nil {
		return RuntimeEventEnvelope{}, fmt.Errorf("redisruntime: stream message %s invalid payload JSON: %w", msg.ID, err)
	}

	// 3. 类型化校验（schema_version 精确匹配、字段一致性等）
	env, err := DecodeRuntimeEventEnvelope(payloadMap)
	if err != nil {
		return RuntimeEventEnvelope{}, fmt.Errorf("redisruntime: stream message %s decode envelope: %w", msg.ID, err)
	}

	return env, nil
}
