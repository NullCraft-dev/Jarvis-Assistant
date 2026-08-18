package app

import (
	"bytes"
	"log/slog"
	"net/http"
	"strings"
	"testing"
	"time"

	"github.com/jarvis-assistant/gateway/internal/orchestrator"
	runtimeredis "github.com/jarvis-assistant/gateway/internal/redis"
)

func TestRuntimeSummaryIntervalDefaultsAndCanBeDisabled(t *testing.T) {
	t.Setenv("JARVIS_GATEWAY_SUMMARY_INTERVAL", "")
	if got := runtimeSummaryInterval(); got != 5*time.Minute {
		t.Fatalf("default interval = %s, want 5m", got)
	}
	t.Setenv("JARVIS_GATEWAY_SUMMARY_INTERVAL", "off")
	if got := runtimeSummaryInterval(); got != 0 {
		t.Fatalf("off interval = %s, want 0", got)
	}
}

func TestGatewayListenAddressDefaultsToLoopbackAndRejectsNonLoopback(t *testing.T) {
	t.Setenv("JARVIS_GATEWAY_HOST", "")
	if got, err := gatewayListenAddress(); err != nil || got != "127.0.0.1:8080" {
		t.Fatalf("default gateway address = %q, %v", got, err)
	}
	t.Setenv("JARVIS_GATEWAY_HOST", "::1")
	if got, err := gatewayListenAddress(); err != nil || got != "[::1]:8080" {
		t.Fatalf("IPv6 loopback address = %q, %v", got, err)
	}
	t.Setenv("JARVIS_GATEWAY_HOST", "0.0.0.0")
	if _, err := gatewayListenAddress(); err == nil {
		t.Fatal("non-loopback address must be rejected")
	}
}

func TestNewHTTPServerSetsDefensiveLimits(t *testing.T) {
	srv := newHTTPServer("127.0.0.1:8080", http.NotFoundHandler())
	if srv.ReadHeaderTimeout != 5*time.Second || srv.IdleTimeout != 60*time.Second || srv.MaxHeaderBytes != 1<<20 {
		t.Fatalf("unexpected HTTP limits: %#v", srv)
	}
}

func TestLogRuntimeSummaryAggregatesSafeHealthMetadata(t *testing.T) {
	var output bytes.Buffer
	previous := slog.Default()
	slog.SetDefault(slog.New(slog.NewTextHandler(&output, &slog.HandlerOptions{Level: slog.LevelDebug})))
	defer slog.SetDefault(previous)

	logRuntimeSummary(orchestrator.RuntimeHealth{
		Status: "healthy", RuntimeBus: "redis",
		Workers: orchestrator.RuntimeWorkerSummary{Total: 2, Online: 2, Busy: 1},
		Streams: []runtimeredis.StreamDiagnostics{
			{Pending: 2, Lag: 3}, {Pending: 1, Lag: 0},
		},
		DeadLetters: []runtimeredis.DeadLetterDiagnostics{{Count: 4}},
		Counters:    orchestrator.RuntimeHealthCounters{EventReclaimed: 5},
	}, 5*time.Minute)

	line := output.String()
	for _, expected := range []string{
		"Gateway 运行摘要", "workers_online=2", "stream_pending=3",
		"stream_lag=3", "dead_letters=4", "event_reclaimed=5",
	} {
		if !strings.Contains(line, expected) {
			t.Fatalf("summary missing %q: %s", expected, line)
		}
	}
}
