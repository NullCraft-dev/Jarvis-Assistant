package orchestrator

import (
	"context"
	"testing"

	runtimeredis "github.com/jarvis-assistant/gateway/internal/redis"
)

type fakeRuntimeDiagnostics struct {
	streams map[string]runtimeredis.StreamDiagnostics
	dlq     map[string]int64
}

func (f fakeRuntimeDiagnostics) InspectGroup(_ context.Context, name, stream, group string) runtimeredis.StreamDiagnostics {
	if result, ok := f.streams[name]; ok {
		return result
	}
	return runtimeredis.StreamDiagnostics{Name: name, Stream: stream, ConsumerGroup: group, Lag: -1, ErrorCode: "STREAM_NOT_FOUND"}
}
func (f fakeRuntimeDiagnostics) DeadLetterLength(_ context.Context, name, stream string) (runtimeredis.DeadLetterDiagnostics, error) {
	return runtimeredis.DeadLetterDiagnostics{Name: name, Stream: stream, Count: f.dlq[name]}, nil
}
func (f fakeRuntimeDiagnostics) ListDeadLetters(_ context.Context, query runtimeredis.DeadLetterQuery) (runtimeredis.DeadLetterPage, error) {
	return runtimeredis.DeadLetterPage{Records: []runtimeredis.DeadLetterRecord{}}, nil
}
func (f fakeRuntimeDiagnostics) GetDeadLetter(_ context.Context, _, _, _ string) (*runtimeredis.DeadLetterRecord, error) {
	return nil, nil
}

func TestRuntimeHealthAggregatesStreamAndDLQState(t *testing.T) {
	rb := &RedisRuntimeBus{workerStatusView: NewWorkerStatusView(DefaultStaleTimeout)}
	rb.SetRuntimeDiagnosticsReader(fakeRuntimeDiagnostics{
		streams: map[string]runtimeredis.StreamDiagnostics{
			"run_queue":      {Name: "run_queue", Available: true, Lag: 0},
			"worker_command": {Name: "worker_command", Available: true, Lag: 0},
			"runtime_event":  {Name: "runtime_event", Available: true, Lag: 0},
		},
		dlq: map[string]int64{"run_queue": 1, "worker_command": 2, "runtime_event": 3},
	})
	health := rb.GetRuntimeHealth(context.Background())
	if health.Status != "degraded" {
		t.Fatalf("no online worker must be degraded: %#v", health)
	}
	if len(health.Streams) != 3 || len(health.DeadLetters) != 3 {
		t.Fatalf("unexpected diagnostics: %#v", health)
	}
}

func TestRuntimeHealthDegradesOnPendingMessages(t *testing.T) {
	rb := &RedisRuntimeBus{workerStatusView: NewWorkerStatusView(DefaultStaleTimeout)}
	rb.SetRuntimeDiagnosticsReader(fakeRuntimeDiagnostics{streams: map[string]runtimeredis.StreamDiagnostics{
		"run_queue":      {Name: "run_queue", Available: true, Pending: 1, Lag: 0},
		"worker_command": {Name: "worker_command", Available: true, Lag: 0},
		"runtime_event":  {Name: "runtime_event", Available: true, Lag: 0},
	}, dlq: map[string]int64{}})
	if health := rb.GetRuntimeHealth(context.Background()); health.Status != "degraded" {
		t.Fatalf("pending must degrade health: %#v", health)
	}
}
