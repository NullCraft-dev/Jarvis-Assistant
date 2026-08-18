// EventPump 退避策略：在读取失败时指数退避，确保不会 tight loop。
//
// EventPumpBackoff 是可注入接口，测试中可用 fake 实现替换真实 time.Sleep。
//
// 职责：
//   - 提供 Wait(ctx) 在失败时等待退避延迟
//   - 提供 Reset() 在成功时重置退避延迟到初始值
//
// 不负责：
//   - 与 Redis / stream / event 通信
//   - 决定何时应退避（由 EventPump loop 决定）
package orchestrator

import (
	"context"
	"sync"
	"time"
)

// EventPumpBackoff 控制 event pump 在读取失败时的退避行为。
//
// 实现必须支持 ctx 取消（Wait 在 ctx.Done 时提前返回）。
type EventPumpBackoff interface {
	// Reset 将退避延迟重置为初始值。
	// 在成功读取后调用。
	Reset()

	// Wait 等待当前退避延迟后返回。
	// 每次调用后延迟翻倍（上限为 max）。
	// 若 ctx 在等待期间被取消，提前返回 ctx.Err()。
	Wait(ctx context.Context) error
}

// ExponentialBackoff 实现指数退避。
//
// 初始延迟 100ms，每次 Wait 后翻倍，上限 5s。
// Reset 回到初始延迟。
//
// 零值可用（默认 start=100ms, max=5s）。
type ExponentialBackoff struct {
	mu      sync.Mutex
	current time.Duration
	start   time.Duration
	max     time.Duration
}

// NewExponentialBackoff 创建可配置的指数退避。
//
// start 为初始延迟，max 为上限。
// 若 start <= 0 则默认为 100ms。
// 若 max <= 0 则默认为 5s。
func NewExponentialBackoff(start, max time.Duration) *ExponentialBackoff {
	if start <= 0 {
		start = 100 * time.Millisecond
	}
	if max <= 0 {
		max = 5 * time.Second
	}
	return &ExponentialBackoff{
		current: start,
		start:   start,
		max:     max,
	}
}

// Reset 将退避延迟重置为初始值。
func (b *ExponentialBackoff) Reset() {
	b.mu.Lock()
	b.current = b.start
	b.mu.Unlock()
}

// Wait 等待当前退避延迟后翻倍（上限 max）。
// ctx 取消时提前返回。
func (b *ExponentialBackoff) Wait(ctx context.Context) error {
	b.mu.Lock()
	d := b.current
	next := d * 2
	if next > b.max {
		next = b.max
	}
	b.current = next
	b.mu.Unlock()

	select {
	case <-time.After(d):
		return nil
	case <-ctx.Done():
		return ctx.Err()
	}
}
