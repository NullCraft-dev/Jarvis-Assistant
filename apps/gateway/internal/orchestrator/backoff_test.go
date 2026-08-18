package orchestrator

import (
	"context"
	"sync"
	"testing"
	"time"
)

// -- ExponentialBackoff 测试 --

func TestExponentialBackoffReset(t *testing.T) {
	b := NewExponentialBackoff(10*time.Millisecond, 1*time.Second)

	ctx := context.Background()

	// 第一次 wait
	start := time.Now()
	err := b.Wait(ctx)
	elapsed := time.Since(start)
	if err != nil {
		t.Fatalf("Wait 失败: %v", err)
	}
	if elapsed < 5*time.Millisecond {
		t.Errorf("第一次 wait 太短: %v", elapsed)
	}

	// 第二次 wait（翻倍）
	start = time.Now()
	err = b.Wait(ctx)
	elapsed2 := time.Since(start)
	if err != nil {
		t.Fatalf("Wait 2 失败: %v", err)
	}
	if elapsed2 < elapsed {
		t.Errorf("第二次 wait 应 >= 第一次: %v < %v", elapsed2, elapsed)
	}

	// Reset
	b.Reset()

	// 第三次 wait（应回到初始延迟）
	start = time.Now()
	err = b.Wait(ctx)
	elapsed3 := time.Since(start)
	if err != nil {
		t.Fatalf("Wait 3 失败: %v", err)
	}
	// 允许一点浮动
	if elapsed3 > elapsed2 {
		t.Errorf("Reset 后延迟应小于第二次: %v >= %v", elapsed3, elapsed2)
	}
}

func TestExponentialBackoffMax(t *testing.T) {
	b := NewExponentialBackoff(10*time.Millisecond, 30*time.Millisecond)

	ctx := context.Background()

	// 多次 wait 应被 max 截断
	var lastDuration time.Duration
	hitMax := false
	for i := 0; i < 10; i++ {
		start := time.Now()
		err := b.Wait(ctx)
		d := time.Since(start)
		if err != nil {
			t.Fatalf("Wait 失败: %v", err)
		}
		// 不应超过 max 的 2 倍（允许调度延迟）
		if d > 60*time.Millisecond {
			t.Errorf("Wait 超过 max 两倍: %v", d)
		}
		if d >= 25*time.Millisecond && d <= 35*time.Millisecond {
			hitMax = true
		}
		lastDuration = d
		_ = lastDuration
	}
	if !hitMax {
		t.Log("警告: 10 次 wait 后未观察到 max 截断（可能因调度延迟）")
	}
}

func TestExponentialBackoffContextCancel(t *testing.T) {
	b := NewExponentialBackoff(500*time.Millisecond, 5*time.Second)

	ctx, cancel := context.WithCancel(context.Background())

	var wg sync.WaitGroup
	wg.Add(1)

	var waitErr error
	go func() {
		defer wg.Done()
		waitErr = b.Wait(ctx)
	}()

	// 立即取消
	time.Sleep(10 * time.Millisecond)
	cancel()

	wg.Wait()

	if waitErr != context.Canceled {
		t.Errorf("ctx 取消后 Wait 应返回 context.Canceled，got: %v", waitErr)
	}
}

func TestExponentialBackoffDefaultValues(t *testing.T) {
	// 零值参数应使用默认值
	b := NewExponentialBackoff(0, 0)

	ctx := context.Background()
	start := time.Now()
	err := b.Wait(ctx)
	elapsed := time.Since(start)
	if err != nil {
		t.Fatalf("Wait 失败: %v", err)
	}
	// 默认 start 为 100ms，允许调度浮动
	if elapsed < 50*time.Millisecond || elapsed > 200*time.Millisecond {
		t.Errorf("默认 start 应为 ~100ms，got %v", elapsed)
	}
}

// -- 编译期接口断言 --

func TestExponentialBackoffImplementsInterface(t *testing.T) {
	var _ EventPumpBackoff = (*ExponentialBackoff)(nil)
}
