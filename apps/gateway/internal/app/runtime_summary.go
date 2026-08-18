package app

import (
	"context"
	"log/slog"
	"os"
	"strings"
	"time"

	"github.com/jarvis-assistant/gateway/internal/orchestrator"
)

const defaultRuntimeSummaryInterval = 5 * time.Minute

type runtimeHealthProvider interface {
	GetRuntimeHealth(context.Context) orchestrator.RuntimeHealth
}

func runtimeSummaryInterval() time.Duration {
	raw := strings.TrimSpace(os.Getenv("JARVIS_GATEWAY_SUMMARY_INTERVAL"))
	if raw == "" {
		return defaultRuntimeSummaryInterval
	}
	if strings.EqualFold(raw, "off") || raw == "0" {
		return 0
	}
	interval, err := time.ParseDuration(raw)
	if err != nil || interval <= 0 {
		slog.Warn("Gateway 周期摘要间隔无效，使用默认值",
			"configured", raw,
			"default", defaultRuntimeSummaryInterval,
		)
		return defaultRuntimeSummaryInterval
	}
	return interval
}

func startRuntimeSummary(ctx context.Context, provider runtimeHealthProvider) {
	interval := runtimeSummaryInterval()
	if provider == nil || interval == 0 {
		return
	}

	go func() {
		ticker := time.NewTicker(interval)
		defer ticker.Stop()
		for {
			select {
			case <-ctx.Done():
				return
			case <-ticker.C:
				summaryCtx, cancel := context.WithTimeout(ctx, 5*time.Second)
				health := provider.GetRuntimeHealth(summaryCtx)
				cancel()
				logRuntimeSummary(health, interval)
			}
		}
	}()
}

func logRuntimeSummary(health orchestrator.RuntimeHealth, interval time.Duration) {
	var streamPending int64
	var streamLag int64
	for _, stream := range health.Streams {
		streamPending += stream.Pending
		if stream.Lag > 0 {
			streamLag += stream.Lag
		}
	}
	var deadLetters int64
	for _, deadLetter := range health.DeadLetters {
		deadLetters += deadLetter.Count
	}

	logFn := slog.Info
	if health.Status != "healthy" {
		logFn = slog.Warn
	}
	logFn("Gateway 运行摘要",
		"interval", interval,
		"status", health.Status,
		"runtime_bus", health.RuntimeBus,
		"workers_total", health.Workers.Total,
		"workers_online", health.Workers.Online,
		"workers_busy", health.Workers.Busy,
		"workers_stale", health.Workers.Stale,
		"stream_pending", streamPending,
		"stream_lag", streamLag,
		"dead_letters", deadLetters,
		"event_reclaimed", health.Counters.EventReclaimed,
		"event_retry_deferred", health.Counters.EventRetryDeferred,
	)
}
