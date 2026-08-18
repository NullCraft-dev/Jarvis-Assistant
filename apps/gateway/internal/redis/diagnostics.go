package redis

import (
	"context"
	"fmt"
	"strconv"
	"strings"

	"github.com/redis/go-redis/v9"
)

// StreamDiagnostics 是 Redis Stream consumer group 的只读运行时快照。
// 它只包含队列治理元数据，不包含消息 payload。
type StreamDiagnostics struct {
	Name            string `json:"name"`
	Stream          string `json:"stream"`
	ConsumerGroup   string `json:"consumer_group"`
	Available       bool   `json:"available"`
	Lag             int64  `json:"lag"`
	Pending         int64  `json:"pending"`
	Consumers       int64  `json:"consumers"`
	OldestPendingMS int64  `json:"oldest_pending_ms"`
	ErrorCode       string `json:"error_code,omitempty"`
}

type DeadLetterDiagnostics struct {
	Name   string `json:"name"`
	Stream string `json:"stream"`
	Count  int64  `json:"count"`
}

type DeadLetterQuery struct {
	Name      string
	Stream    string
	Limit     int
	Before    string
	ErrorCode string
	TaskID    string
	RunID     string
}

// DeadLetterRecord 是 DLQ 的安全白名单投影，永远不包含 payload。
type DeadLetterRecord struct {
	ID                string `json:"id"`
	Source            string `json:"source"`
	OriginalStream    string `json:"original_stream"`
	OriginalMessageID string `json:"original_message_id"`
	ConsumerGroup     string `json:"consumer_group"`
	DeliveryCount     int64  `json:"delivery_count"`
	Reclaimed         bool   `json:"reclaimed"`
	ErrorCode         string `json:"error_code"`
	ErrorMessage      string `json:"error_message"`
	FailedAt          string `json:"failed_at"`
	PayloadSHA256     string `json:"payload_sha256"`
	PayloadSizeBytes  int64  `json:"payload_size_bytes"`
	TaskID            string `json:"task_id,omitempty"`
	RunID             string `json:"run_id,omitempty"`
}

type DeadLetterPage struct {
	Records    []DeadLetterRecord `json:"records"`
	NextCursor string             `json:"next_cursor,omitempty"`
}

// RuntimeDiagnosticsReader 是 Gateway 运行时健康聚合依赖的窄接口。
type RuntimeDiagnosticsReader interface {
	InspectGroup(ctx context.Context, name, stream, group string) StreamDiagnostics
	DeadLetterLength(ctx context.Context, name, stream string) (DeadLetterDiagnostics, error)
	ListDeadLetters(ctx context.Context, query DeadLetterQuery) (DeadLetterPage, error)
	GetDeadLetter(ctx context.Context, name, stream, id string) (*DeadLetterRecord, error)
}

// GoRedisRuntimeDiagnostics 只生成 Redis 治理诊断投影，不向上层暴露消息正文。
type GoRedisRuntimeDiagnostics struct {
	client *redis.Client
}

func NewGoRedisRuntimeDiagnostics(client *redis.Client) (*GoRedisRuntimeDiagnostics, error) {
	if client == nil {
		return nil, fmt.Errorf("redisruntime: diagnostics client is nil")
	}
	return &GoRedisRuntimeDiagnostics{client: client}, nil
}

func (d *GoRedisRuntimeDiagnostics) InspectGroup(
	ctx context.Context, name, stream, group string,
) StreamDiagnostics {
	result := StreamDiagnostics{Name: name, Stream: stream, ConsumerGroup: group, Lag: -1}
	groups, err := d.client.XInfoGroups(ctx, stream).Result()
	if err != nil {
		result.ErrorCode = diagnosticsErrorCode(err)
		return result
	}
	for _, info := range groups {
		if info.Name != group {
			continue
		}
		result.Available = true
		result.Lag = info.Lag
		result.Pending = info.Pending
		result.Consumers = info.Consumers
		if info.Pending > 0 {
			entries, pendingErr := d.client.XPendingExt(ctx, &redis.XPendingExtArgs{
				Stream: stream, Group: group, Start: "-", End: "+", Count: 1,
			}).Result()
			if pendingErr != nil {
				result.ErrorCode = diagnosticsErrorCode(pendingErr)
				return result
			}
			if len(entries) > 0 {
				result.OldestPendingMS = max(entries[0].Idle.Milliseconds(), 0)
			}
		}
		return result
	}
	result.ErrorCode = "CONSUMER_GROUP_NOT_FOUND"
	return result
}

func (d *GoRedisRuntimeDiagnostics) DeadLetterLength(
	ctx context.Context, name, stream string,
) (DeadLetterDiagnostics, error) {
	count, err := d.client.XLen(ctx, stream).Result()
	if err != nil {
		return DeadLetterDiagnostics{Name: name, Stream: stream}, fmt.Errorf("redisruntime: xlen %s: %w", stream, err)
	}
	return DeadLetterDiagnostics{Name: name, Stream: stream, Count: count}, nil
}

// GetDeadLetter 按精确 Redis message id 读取一条安全白名单投影。
func (d *GoRedisRuntimeDiagnostics) GetDeadLetter(
	ctx context.Context, name, stream, id string,
) (*DeadLetterRecord, error) {
	messages, err := d.client.XRangeN(ctx, stream, id, id, 1).Result()
	if err != nil {
		return nil, fmt.Errorf("redisruntime: xrange %s: %w", stream, err)
	}
	if len(messages) == 0 || messages[0].ID != id {
		return nil, nil
	}
	record := decodeDeadLetterRecord(name, messages[0])
	return &record, nil
}

// ListDeadLetters 有界扫描一个 DLQ，并只投影白名单字段。
func (d *GoRedisRuntimeDiagnostics) ListDeadLetters(
	ctx context.Context, query DeadLetterQuery,
) (DeadLetterPage, error) {
	limit := min(max(query.Limit, 1), 50)
	page := DeadLetterPage{Records: []DeadLetterRecord{}}
	start := "+"
	if query.Before != "" {
		start = "(" + query.Before
	}
	const scanLimit = 500
	scanned := 0
	for scanned < scanLimit && len(page.Records) < limit {
		batchSize := int64(min(100, scanLimit-scanned))
		messages, err := d.client.XRevRangeN(ctx, query.Stream, start, "-", batchSize).Result()
		if err != nil {
			return DeadLetterPage{}, fmt.Errorf("redisruntime: xrevrange %s: %w", query.Stream, err)
		}
		if len(messages) == 0 {
			break
		}
		for _, message := range messages {
			scanned++
			page.NextCursor = message.ID
			record := decodeDeadLetterRecord(query.Name, message)
			if query.ErrorCode != "" && record.ErrorCode != query.ErrorCode {
				continue
			}
			if query.TaskID != "" && record.TaskID != query.TaskID {
				continue
			}
			if query.RunID != "" && record.RunID != query.RunID {
				continue
			}
			page.Records = append(page.Records, record)
			if len(page.Records) == limit || scanned == scanLimit {
				break
			}
		}
		if len(messages) < int(batchSize) {
			page.NextCursor = ""
			break
		}
		start = "(" + messages[len(messages)-1].ID
	}
	return page, nil
}

func decodeDeadLetterRecord(source string, message redis.XMessage) DeadLetterRecord {
	value := func(key string, maxRunes int) string {
		raw, exists := message.Values[key]
		if !exists || raw == nil {
			return ""
		}
		text := strings.Join(strings.Fields(fmt.Sprint(raw)), " ")
		runes := []rune(text)
		if len(runes) > maxRunes {
			runes = runes[:maxRunes]
		}
		return string(runes)
	}
	parseInt := func(key string) int64 {
		result, _ := strconv.ParseInt(value(key, 24), 10, 64)
		return max(result, 0)
	}
	reclaimed, _ := strconv.ParseBool(value("reclaimed", 8))
	return DeadLetterRecord{
		ID: message.ID, Source: source,
		OriginalStream: value("original_stream", 128), OriginalMessageID: value("original_message_id", 64),
		ConsumerGroup: value("consumer_group", 128), DeliveryCount: parseInt("delivery_count"), Reclaimed: reclaimed,
		ErrorCode: value("error_code", 80), ErrorMessage: value("error_message", 300), FailedAt: value("failed_at", 64),
		PayloadSHA256: value("payload_sha256", 64), PayloadSizeBytes: parseInt("payload_size_bytes"),
		TaskID: value("task_id", 64), RunID: value("run_id", 64),
	}
}

func diagnosticsErrorCode(err error) string {
	message := strings.ToLower(err.Error())
	if strings.Contains(message, "no such key") {
		return "STREAM_NOT_FOUND"
	}
	if strings.Contains(message, "nogroup") {
		return "CONSUMER_GROUP_NOT_FOUND"
	}
	return "REDIS_DIAGNOSTICS_UNAVAILABLE"
}

var _ RuntimeDiagnosticsReader = (*GoRedisRuntimeDiagnostics)(nil)
