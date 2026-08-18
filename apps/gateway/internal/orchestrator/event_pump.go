// EventPump 从 Redis runtime event stream 读取 worker 事件并写入 InMemoryRuntimeBus。
//
// 核心流程：
//
//	Redis StreamRuntimeEvent
//	  → stale PEL: XPENDING + XCLAIM / new: XReadGroup ">"
//	  → RuntimeEventReader.ReadDeliveries（逐条解码 RuntimeEventEnvelope）
//	  → InMemoryRuntimeBus.AppendRuntimeEvents（按 event id 幂等追加）
//	  → 每条成功后单独 XAck
//	  → GetEvents(runID) 可见 → SSE SubscribeEvents 推送
//
// # 职责
//
//   - 从 Redis StreamRuntimeEvent 非阻塞轮询读取 worker 事件
//   - 解码校验后追加到 InMemoryRuntimeBus 的事件列表
//   - 读取失败时指数退避（无 tight loop）
//   - 支持 context 取消停止
//
// # 不负责
//
//   - 写入 Redis（由 RedisRuntimeTransport 负责）
//   - 执行 LLM / LangGraph / 工具 / Python worker
//   - 成为 Task / Run / Event 的业务真源
//   - Storage 持久化
//   - SSE 推送（由 handler 负责）
//
// # 约束
//
//   - Redis 仍只是 runtime bus，不是业务真源
//   - InMemoryRuntimeBus 作为临时 state owner
//   - 空读取后有小延迟（50ms）避免 tight loop
//   - 读取失败后指数退避（100ms → 5s）
//   - go-redis 类型不泄漏到本组件外部
package orchestrator

import (
	"context"
	"log/slog"
	"strings"
	"sync"
	"time"

	"github.com/jarvis-assistant/gateway/internal/contracts"
	runtimeredis "github.com/jarvis-assistant/gateway/internal/redis"
)

const (
	// eventPumpConsumerName 是 consumer group 中的 consumer 实例名。
	eventPumpConsumerName = "gateway-01"

	// eventPumpBatchSize 是单次 XReadGroup 的最大消息数。
	eventPumpBatchSize = 32

	// eventPumpPollInterval 是空读取后的轮询间隔。
	// 避免 tight loop 持续空轮询 Redis。
	eventPumpPollInterval = 50 * time.Millisecond

	// runtime event 是实时投影；5/10/20 秒重试用于覆盖 SeedAcceptedRun 竞态。
	eventPumpReclaimInterval = time.Second
	eventPumpReclaimIdle     = 5 * time.Second
	eventPumpMaxDeliveries   = int64(3)
)

// eventPump 从 Redis stream 读取 RuntimeEventEnvelope 写入 InMemoryRuntimeBus。
type eventPump struct {
	reader           *runtimeredis.RuntimeEventReader
	streamReader     runtimeredis.RedisStreamReader // 用于 XGroupCreateMkStream
	inMemory         *InMemoryRuntimeBus
	backoff          EventPumpBackoff
	consumerName     string
	lastReclaim      time.Time
	metricsMu        sync.RWMutex
	metrics          eventPumpMetrics
	projectionLoader RuntimeProjectionLoader

	cancel context.CancelFunc
	wg     sync.WaitGroup
}

// RuntimeProjectionLoader verifies an event's Task/Run against PostgreSQL via
// the Control Plane before the Gateway creates a missing realtime projection.
type RuntimeProjectionLoader interface {
	LoadRuntimeProjection(
		ctx context.Context, taskID, runID contracts.ID,
	) (contracts.TaskDTO, contracts.AgentRunDTO, error)
}

func (p *eventPump) setProjectionLoader(loader RuntimeProjectionLoader) {
	p.projectionLoader = loader
}

type eventPumpMetrics struct {
	reclaimed     int64
	retryDeferred int64
	deadLettered  int64
	malformed     int64
}

func (p *eventPump) metricsSnapshot() eventPumpMetrics {
	p.metricsMu.RLock()
	defer p.metricsMu.RUnlock()
	return p.metrics
}

func (p *eventPump) updateMetrics(update func(*eventPumpMetrics)) {
	p.metricsMu.Lock()
	defer p.metricsMu.Unlock()
	update(&p.metrics)
}

// newEventPump 创建 event pump 实例。
//
// 参数均不可为 nil。
// backoff 用于失败退避（生产用 ExponentialBackoff，测试用 fake）。
func newEventPump(
	reader *runtimeredis.RuntimeEventReader,
	streamReader runtimeredis.RedisStreamReader,
	inMemory *InMemoryRuntimeBus,
	backoff EventPumpBackoff,
	consumerNames ...string,
) *eventPump {
	consumerName := eventPumpConsumerName
	if len(consumerNames) > 0 && consumerNames[0] != "" {
		consumerName = consumerNames[0]
	}
	return &eventPump{
		reader:       reader,
		streamReader: streamReader,
		inMemory:     inMemory,
		backoff:      backoff,
		consumerName: consumerName,
	}
}

// Start 创建 consumer group（幂等）并启动后台泵循环。
//
// 流程：
//  1. 调用 XGroupCreateMkStream 确保 consumer group 存在（幂等）
//  2. 创建内部 context（背景 context + cancel）
//  3. 启动后台 goroutine 运行主泵循环
//
// 返回 error 仅当 consumer group 创建失败。
// 调用方应在进程退出前调用 Close() 停止泵。
func (p *eventPump) Start() error {
	// 幂等创建 consumer group（startID="0" 从已有消息开始消费）
	ctx, cancel := context.WithTimeout(context.Background(), 3*time.Second)
	defer cancel()

	if err := p.ensureConsumerGroup(ctx); err != nil {
		return err
	}

	// 使用 background context 确保泵不受 Start 的 ctx 影响
	bgCtx, pumpCancel := context.WithCancel(context.Background())
	p.cancel = pumpCancel

	p.wg.Add(1)
	go p.loop(bgCtx)

	slog.Info("event-pump 启动",
		"stream", runtimeredis.StreamRuntimeEvent,
		"group", runtimeredis.GroupGatewayEvents,
		"consumer", p.consumerName,
	)
	return nil
}

func isNoGroupError(err error) bool {
	return err != nil && strings.Contains(strings.ToUpper(err.Error()), "NOGROUP")
}

func (p *eventPump) ensureConsumerGroup(ctx context.Context) error {
	return p.streamReader.XGroupCreateMkStream(
		ctx,
		runtimeredis.StreamRuntimeEvent,
		runtimeredis.GroupGatewayEvents,
		"0",
	)
}

func (p *eventPump) recoverMissingConsumerGroup(ctx context.Context, readErr error) (bool, error) {
	if !isNoGroupError(readErr) {
		return false, nil
	}
	recoveryCtx, cancel := context.WithTimeout(ctx, 3*time.Second)
	defer cancel()
	if err := p.ensureConsumerGroup(recoveryCtx); err != nil {
		return true, err
	}
	return true, nil
}

// Close 取消泵的 context 并等待 goroutine 退出。
//
// 调用方应在进程退出前调用，确保 goroutine 不泄漏。
// 多次调用安全（cancel 幂等）。
func (p *eventPump) Close() error {
	if p.cancel != nil {
		p.cancel()
	}
	p.wg.Wait()
	slog.Info("event-pump 已停止")
	return nil
}

// loop 是主泵循环：读取事件 → 追加到 inMemory → ack。
//
// 循环逻辑：
//   - ctx 未取消时持续运行
//   - 调用 runOnce 执行单次读取+处理
//   - 成功且非空：reset backoff，继续
//   - 成功但为空：小延迟后继续（避免 tight loop）
//   - 失败：记录日志，指数退避后继续
func (p *eventPump) loop(ctx context.Context) {
	defer p.wg.Done()

	for {
		select {
		case <-ctx.Done():
			return
		default:
		}

		processed, err := p.runOnce(ctx)
		if err != nil {
			// ctx 取消 → 正常退出
			if ctx.Err() != nil {
				return
			}
			recovered, recoveryErr := p.recoverMissingConsumerGroup(ctx, err)
			if recovered && recoveryErr == nil {
				p.backoff.Reset()
				slog.Info("event-pump 已重建 Redis consumer group")
				continue
			}
			if recoveryErr != nil {
				slog.Warn("event-pump 重建 Redis consumer group 失败", "error", recoveryErr)
			} else {
				slog.Warn("event-pump 读取错误", "error", err)
			}
			if waitErr := p.backoff.Wait(ctx); waitErr != nil {
				return
			}
			continue
		}

		// 成功：reset 退避
		p.backoff.Reset()

		if processed == 0 {
			// 空读取：小延迟避免 tight loop
			select {
			case <-time.After(eventPumpPollInterval):
			case <-ctx.Done():
				return
			}
		}
	}
}

// runOnce 执行一次读取→追加→ack 循环。
//
// 返回值：
//   - processed：本次处理的事件数
//   - error：读取或 ack 失败时返回
//
// 确定性解码失败原子进入 DLQ；追加失败（run 不存在）保留 pending，
// 按退避接管重试，并在投递次数耗尽后进入 DLQ。
func (p *eventPump) runOnce(ctx context.Context) (int, error) {
	if time.Since(p.lastReclaim) >= eventPumpReclaimInterval {
		p.lastReclaim = time.Now()
		delivery, claimErr := p.reader.ClaimStaleDelivery(
			ctx, runtimeredis.GroupGatewayEvents, p.consumerName,
			eventPumpReclaimIdle,
		)
		if claimErr != nil {
			return 0, claimErr
		}
		if delivery != nil {
			p.updateMetrics(func(metrics *eventPumpMetrics) { metrics.reclaimed++ })
			return p.processDelivery(ctx, *delivery)
		}
	}

	deliveries, err := p.reader.ReadDeliveries(
		ctx, runtimeredis.GroupGatewayEvents, p.consumerName,
		eventPumpBatchSize,
	)
	if err != nil {
		return 0, err
	}
	if len(deliveries) == 0 {
		return 0, nil
	}
	appended := 0
	for _, delivery := range deliveries {
		processed, processErr := p.processDelivery(ctx, delivery)
		appended += processed
		if processErr != nil {
			return appended, processErr
		}
	}
	return appended, nil
}

func (p *eventPump) processDelivery(
	ctx context.Context, delivery runtimeredis.RuntimeEventDelivery,
) (int, error) {
	if !delivery.Valid() {
		p.updateMetrics(func(metrics *eventPumpMetrics) { metrics.malformed++ })
		return 0, p.deadLetter(ctx, delivery)
	}

	env := delivery.Envelope
	appendErr := p.inMemory.AppendRuntimeEvents(
		env.RunID, []contracts.RuntimeEvent{env.RuntimeEvent},
	)
	if appendErr != nil && p.projectionLoader != nil {
		task, run, loadErr := p.projectionLoader.LoadRuntimeProjection(
			ctx, env.TaskID, env.RunID,
		)
		if loadErr == nil && task.ID == env.TaskID && run.ID == env.RunID && run.TaskID == env.TaskID {
			p.inMemory.SeedAcceptedRun(task, run, nil)
			appendErr = p.inMemory.AppendRuntimeEvents(
				env.RunID, []contracts.RuntimeEvent{env.RuntimeEvent},
			)
		}
	}
	if appendErr != nil {
		slog.Warn("event-pump 追加事件失败，保留 pending",
			"run", env.RunID, "event", env.EventID,
			"delivery", delivery.DeliveryCount, "error", appendErr,
		)
		if delivery.Reclaimed && delivery.DeliveryCount >= eventPumpMaxDeliveries {
			delivery.ErrorCode = "RUNTIME_EVENT_PROJECTION_RETRY_EXHAUSTED"
			delivery.ErrorMessage = "Gateway 实时投影重试次数已耗尽；权威事件保留在 PostgreSQL"
			return 0, p.deadLetter(ctx, delivery)
		}
		p.updateMetrics(func(metrics *eventPumpMetrics) { metrics.retryDeferred++ })
		return 0, nil
	}

	// PostgreSQL 已由 Python Application Service 持久化；Gateway 只确认实时投影。
	if ackErr := p.reader.AckEvents(
		ctx, runtimeredis.GroupGatewayEvents, delivery.MessageID,
	); ackErr != nil {
		return 1, ackErr
	}
	slog.Debug("runtime event 投影完成",
		"trace_id", env.TraceID,
		"task_id", env.TaskID,
		"run_id", env.RunID,
		"event_id", env.EventID,
		"event_type", env.EventType,
		"message_id", delivery.MessageID,
		"reclaimed", delivery.Reclaimed,
	)
	return 1, nil
}

func (p *eventPump) deadLetter(
	ctx context.Context, delivery runtimeredis.RuntimeEventDelivery,
) error {
	dlqID, err := p.reader.DeadLetter(
		ctx, runtimeredis.GroupGatewayEvents, delivery,
	)
	if err != nil {
		return err
	}
	p.updateMetrics(func(metrics *eventPumpMetrics) { metrics.deadLettered++ })
	slog.Error("runtime event 已进入 DLQ",
		"message", delivery.MessageID, "dlq", dlqID,
		"code", delivery.ErrorCode,
	)
	return nil
}
