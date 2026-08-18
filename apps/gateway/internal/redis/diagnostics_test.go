package redis

import (
	"strings"
	"testing"

	"github.com/redis/go-redis/v9"
)

func TestDecodeDeadLetterRecordUsesWhitelistAndSanitizes(t *testing.T) {
	record := decodeDeadLetterRecord("run_queue", redis.XMessage{ID: "10-1", Values: map[string]interface{}{
		"original_stream": StreamRunQueue, "original_message_id": "9-0", "consumer_group": GroupWorkerPool,
		"delivery_count": "3", "reclaimed": "true", "error_code": "RUN_QUEUE_MALFORMED",
		"error_message": "invalid\n payload", "failed_at": "2026-07-22T10:00:00Z",
		"payload_sha256": strings.Repeat("a", 64), "payload_size_bytes": "42",
		"task_id": "task-1", "run_id": "run-1", "payload": "sensitive user goal",
	}})
	if record.ID != "10-1" || record.DeliveryCount != 3 || !record.Reclaimed || record.ErrorMessage != "invalid payload" {
		t.Fatalf("unexpected safe projection: %#v", record)
	}
	if strings.Contains(record.ErrorMessage, "sensitive") || strings.Contains(record.PayloadSHA256, "sensitive") {
		t.Fatal("payload leaked into safe projection")
	}
}

func TestDecodeDeadLetterRecordBoundsDiagnosticText(t *testing.T) {
	record := decodeDeadLetterRecord("runtime_event", redis.XMessage{ID: "1-0", Values: map[string]interface{}{
		"error_message": strings.Repeat("界", 400), "delivery_count": "-2", "payload_size_bytes": "invalid",
	}})
	if len([]rune(record.ErrorMessage)) != 300 || record.DeliveryCount != 0 || record.PayloadSizeBytes != 0 {
		t.Fatalf("bounds not applied: %#v", record)
	}
}
