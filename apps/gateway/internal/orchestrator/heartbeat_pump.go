// HeartbeatPump 从 Redis heartbeat stream 读取 worker 心跳并更新 WorkerStatusView。
//
// 核心流程：
//
//	Redis StreamWorkerHeartbeat
//	  → XReadGroup (group=jarvis:group:gateway-heartbeat, consumer=<gateway-id>)
//	  → HeartbeatReader.ReadHeartbeats（解码 WorkerHeartbeatMessage）
//	  → WorkerStatusView.UpdateFromHeartbeat（更新内存视图）
//	  → XAck
//
// # 职责
//
//   - 从 Redis StreamWorkerHeartbeat 非阻塞轮询读取心跳
//   - 解码校验后更新 WorkerStatusView
//   - 读取失败时指数退避
//   - 支持 context 取消停止
//
// # 不负责
//
//   - 写入 Redis（由 Python worker 的 HeartbeatProducer 负责）
//   - 成为 Worker 状态业务真源
//   - 管理 worker 生命周期
//   - Storage 持久化
//
// # 约束
//
//   - Redis 只是 runtime bus，不是业务真源
//   - 空读取后有小延迟避免 tight loop
//   - 读取失败后指数退避
//   - go-redis 类型不泄漏到本组件外部
package orchestrator

import (
	"context"
	"log/slog"
	"sync"
	"time"

	runtimeredis "github.com/jarvis-assistant/gateway/internal/redis"
)

const (
	// heartbeatPumpConsumerName 是 consumer group 中的 consumer 实例名。
	heartbeatPumpConsumerName = "gateway-01"

	// heartbeatPumpGroupName 是 heartbeat consumer group 名。
	heartbeatPumpGroupName = "jarvis:group:gateway-heartbeat"

	// heartbeatPumpBatchSize 是单次 XReadGroup 的最大消息数。
	heartbeatPumpBatchSize = 32

	// heartbeatPumpPollInterval 是空读取后的轮询间隔。
	heartbeatPumpPollInterval = 100 * time.Millisecond
)

// heartbeatPump 从 Redis heartbeat stream 读取心跳并更新 WorkerStatusView。
type heartbeatPump struct {
	reader       *runtimeredis.HeartbeatReader
	view         *WorkerStatusView
	backoff      EventPumpBackoff
	consumerName string

	cancel context.CancelFunc
	wg     sync.WaitGroup
}

// newHeartbeatPump 创建 heartbeat pump 实例。
func newHeartbeatPump(
	reader *runtimeredis.HeartbeatReader,
	view *WorkerStatusView,
	backoff EventPumpBackoff,
	consumerNames ...string,
) *heartbeatPump {
	consumerName := heartbeatPumpConsumerName
	if len(consumerNames) > 0 && consumerNames[0] != "" {
		consumerName = consumerNames[0]
	}
	return &heartbeatPump{
		reader:       reader,
		view:         view,
		backoff:      backoff,
		consumerName: consumerName,
	}
}

// Start 创建 consumer group（幂等）并启动后台泵循环。
func (p *heartbeatPump) Start() error {
	ctx, cancel := context.WithTimeout(context.Background(), 3*time.Second)
	defer cancel()

	if err := p.ensureConsumerGroup(ctx); err != nil {
		return err
	}

	bgCtx, pumpCancel := context.WithCancel(context.Background())
	p.cancel = pumpCancel

	p.wg.Add(1)
	go p.loop(bgCtx)

	slog.Info("heartbeat-pump 启动",
		"stream", runtimeredis.StreamWorkerHeartbeat,
		"group", heartbeatPumpGroupName,
		"consumer", p.consumerName,
	)
	return nil
}

func (p *heartbeatPump) ensureConsumerGroup(ctx context.Context) error {
	return p.reader.CreateGroupIfNotExists(ctx, heartbeatPumpGroupName, "0")
}

func (p *heartbeatPump) recoverMissingConsumerGroup(ctx context.Context, readErr error) (bool, error) {
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
func (p *heartbeatPump) Close() error {
	if p.cancel != nil {
		p.cancel()
	}
	p.wg.Wait()
	slog.Info("heartbeat-pump 已停止")
	return nil
}

// loop 是主泵循环。
func (p *heartbeatPump) loop(ctx context.Context) {
	defer p.wg.Done()

	for {
		select {
		case <-ctx.Done():
			return
		default:
		}

		processed, err := p.runOnce(ctx)
		if err != nil {
			if ctx.Err() != nil {
				return
			}
			recovered, recoveryErr := p.recoverMissingConsumerGroup(ctx, err)
			if recovered && recoveryErr == nil {
				p.backoff.Reset()
				slog.Info("heartbeat-pump 已重建 Redis consumer group")
				continue
			}
			if recoveryErr != nil {
				slog.Warn("heartbeat-pump 重建 Redis consumer group 失败", "error", recoveryErr)
			} else {
				slog.Warn("heartbeat-pump 读取错误", "error", err)
			}
			if waitErr := p.backoff.Wait(ctx); waitErr != nil {
				return
			}
			continue
		}

		p.backoff.Reset()

		if processed == 0 {
			select {
			case <-time.After(heartbeatPumpPollInterval):
			case <-ctx.Done():
				return
			}
		}
	}
}

// runOnce 执行一次读取→更新→ack 循环。
func (p *heartbeatPump) runOnce(ctx context.Context) (int, error) {
	heartbeats, msgIDs, err := p.reader.ReadHeartbeats(
		ctx,
		heartbeatPumpGroupName,
		p.consumerName,
		heartbeatPumpBatchSize,
	)
	if err != nil {
		return 0, err
	}

	if len(heartbeats) == 0 {
		return 0, nil
	}

	// 更新 WorkerStatusView
	for _, hb := range heartbeats {
		p.view.UpdateFromHeartbeat(hb)
	}

	// ack 所有已处理的消息
	if ackErr := p.reader.AckHeartbeats(ctx, heartbeatPumpGroupName, msgIDs...); ackErr != nil {
		return len(heartbeats), ackErr
	}

	return len(heartbeats), nil
}
