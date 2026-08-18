package orchestrator

import (
	"context"
	"errors"
	"fmt"
	"sync"
	"testing"
	"time"

	"github.com/jarvis-assistant/gateway/internal/contracts"
	runtimeredis "github.com/jarvis-assistant/gateway/internal/redis"
)

// -- fake backoff for testing --

// fakeBackoff 是 EventPumpBackoff 的测试替身。
// 不实际 sleep，只记录调用次数和是否触发 reset。
type fakeBackoff struct {
	mu         sync.Mutex
	WaitCalls  int
	ResetCalls int
	WaitErr    error // 注入 Wait 错误（模拟 ctx 取消等）
}

func (f *fakeBackoff) Reset() {
	f.mu.Lock()
	f.ResetCalls++
	f.mu.Unlock()
}

func (f *fakeBackoff) Wait(_ context.Context) error {
	f.mu.Lock()
	f.WaitCalls++
	err := f.WaitErr
	f.mu.Unlock()
	return err
}

func (f *fakeBackoff) waitCallCount() int {
	f.mu.Lock()
	defer f.mu.Unlock()
	return f.WaitCalls
}

func (f *fakeBackoff) resetCallCount() int {
	f.mu.Lock()
	defer f.mu.Unlock()
	return f.ResetCalls
}

// -- fake stream reader for testing --

// fakeStreamReader 实现 runtimeredis.RedisStreamReader，用于 EventPump 测试。
type fakeStreamReader struct {
	Messages []runtimeredis.StreamMessage
	ReadErr  error
	AckErr   error

	AckedIDs        []string
	ReadCalls       int
	AckCalls        int
	CreateCalls     int
	CreateErr       error
	Pending         []runtimeredis.PendingStreamEntry
	Claimed         []runtimeredis.StreamMessage
	PendingErr      error
	ClaimErr        error
	DeadLetterErr   error
	DeadLetteredIDs []string
}

var _ runtimeredis.RedisStreamReader = (*fakeStreamReader)(nil)

func (f *fakeStreamReader) XReadGroup(_ context.Context, group, consumer, stream, id string, count int64) ([]runtimeredis.StreamMessage, error) {
	f.ReadCalls++
	if f.ReadErr != nil {
		return nil, f.ReadErr
	}
	return f.Messages, nil
}

func (f *fakeStreamReader) XAck(_ context.Context, stream, group string, ids ...string) error {
	f.AckCalls++
	f.AckedIDs = append(f.AckedIDs, ids...)
	return f.AckErr
}

func (f *fakeStreamReader) XGroupCreateMkStream(_ context.Context, stream, group, startID string) error {
	f.CreateCalls++
	return f.CreateErr
}

func (f *fakeStreamReader) XPending(
	_ context.Context, stream, group, start string, count int64,
) ([]runtimeredis.PendingStreamEntry, error) {
	return f.Pending, f.PendingErr
}

func (f *fakeStreamReader) XClaim(
	_ context.Context, stream, group, consumer string,
	minIdle time.Duration, ids ...string,
) ([]runtimeredis.StreamMessage, error) {
	return f.Claimed, f.ClaimErr
}

func (f *fakeStreamReader) MoveToDeadLetter(
	_ context.Context,
	sourceStream, deadLetterStream, group, messageID, dedupeKey string,
	dedupeTTL time.Duration,
	maxLen int64,
	fields map[string]interface{},
) (string, error) {
	if f.DeadLetterErr != nil {
		return "", f.DeadLetterErr
	}
	f.DeadLetteredIDs = append(f.DeadLetteredIDs, messageID)
	return "dlq-1", nil
}

// -- helpers --

// makeTestEnvelope 构造一个合法的 RuntimeEventEnvelope 并用 RuntimeEventToStreamFields
// 转换为 StreamMessage，可被 RuntimeEventReader 正确解码。
func makeTestEnvelope(seq int, runID string) runtimeredis.StreamMessage {
	env := runtimeredis.RuntimeEventEnvelope{
		EventID:   fmt.Sprintf("evt-%03d", seq),
		TraceID:   fmt.Sprintf("trace-%03d", seq),
		TaskID:    fmt.Sprintf("task-%03d", seq),
		RunID:     runID,
		EventType: "agent.run.completed",
		RuntimeEvent: contracts.RuntimeEvent{
			ID:        fmt.Sprintf("evt-%03d", seq),
			Type:      "agent.run.completed",
			TaskID:    fmt.Sprintf("task-%03d", seq),
			RunID:     runID,
			StepID:    "step-001",
			Timestamp: "2026-07-06T10:00:00Z",
			Payload: map[string]interface{}{
				"seq": seq,
			},
		},
		ProducedBy:    "worker-01",
		SchemaVersion: runtimeredis.SchemaVersion,
	}

	fields, err := runtimeredis.RuntimeEventToStreamFields(env)
	if err != nil {
		panic(fmt.Sprintf("makeTestEnvelope: %v", err))
	}
	return runtimeredis.StreamMessage{
		ID:     fmt.Sprintf("1700000000000-%d", seq),
		Values: fields,
	}
}

func makeTestEnvelopes(n int, runID string) []runtimeredis.StreamMessage {
	msgs := make([]runtimeredis.StreamMessage, n)
	for i := 0; i < n; i++ {
		msgs[i] = makeTestEnvelope(i+1, runID)
	}
	return msgs
}

// newTestPump 创建测试用的 eventPump（注入 fake）。
func newTestPump(fr *fakeStreamReader, inMemory *InMemoryRuntimeBus) (*eventPump, *fakeBackoff) {
	eventReader, err := runtimeredis.NewRuntimeEventReader(fr)
	if err != nil {
		panic(fmt.Sprintf("newTestPump: NewRuntimeEventReader: %v", err))
	}
	backoff := &fakeBackoff{}
	pump := newEventPump(eventReader, fr, inMemory, backoff)
	return pump, backoff
}

// newBusWithRun 创建 InMemoryRuntimeBus 并准备一个 run，返回 bus、run、task。
func newBusWithRun(t *testing.T) (*InMemoryRuntimeBus, *contracts.AgentRunDTO, *contracts.TaskDTO) {
	t.Helper()
	b := NewInMemoryRuntimeBus()
	task, run, _, err := b.PrepareMinimalRun(contracts.CreateTaskInput{UserGoal: "pump 测试"})
	if err != nil {
		t.Fatalf("PrepareMinimalRun 失败: %v", err)
	}
	return b, run, task
}

// -- EventPump 测试 --

func TestEventPumpSingleEvent(t *testing.T) {
	fr := &fakeStreamReader{}
	b, run, _ := newBusWithRun(t)

	fr.Messages = makeTestEnvelopes(1, run.ID)
	pump, _ := newTestPump(fr, b)

	processed, err := pump.runOnce(context.Background())
	if err != nil {
		t.Fatalf("runOnce 失败: %v", err)
	}
	if processed != 1 {
		t.Errorf("processed: got %d, want 1", processed)
	}

	// 验证事件通过 GetEvents 可见
	events, err := b.GetEvents(run.ID)
	if err != nil {
		t.Fatalf("GetEvents 失败: %v", err)
	}

	// 检查追加的事件
	found := false
	for _, e := range events {
		if e.ID == "evt-001" && e.Type == "agent.run.completed" {
			found = true
			if seq, ok := e.Payload["seq"]; !ok || seq.(float64) != 1 {
				t.Errorf("payload seq 不正确: %v", seq)
			}
			break
		}
	}
	if !found {
		t.Error("未找到 pump 追加的事件 evt-001")
	}

	// 验证 ack 被调用
	if fr.AckCalls != 1 {
		t.Errorf("期望 1 次 ack，got %d", fr.AckCalls)
	}
}

func TestEventPumpRecoversConsumerGroupAfterRedisStateLoss(t *testing.T) {
	fr := &fakeStreamReader{}
	pump, _ := newTestPump(fr, NewInMemoryRuntimeBus())

	recovered, err := pump.recoverMissingConsumerGroup(
		context.Background(), errors.New("redis: NOGROUP no such key or consumer group"),
	)
	if err != nil {
		t.Fatalf("重建 consumer group 失败: %v", err)
	}
	if !recovered {
		t.Fatal("NOGROUP 应触发恢复")
	}
	if fr.CreateCalls != 1 {
		t.Fatalf("CreateCalls = %d, want 1", fr.CreateCalls)
	}

	recovered, err = pump.recoverMissingConsumerGroup(
		context.Background(), errors.New("redis connection refused"),
	)
	if err != nil || recovered {
		t.Fatalf("非 NOGROUP 不应触发恢复: recovered=%v err=%v", recovered, err)
	}
}

func TestEventPumpMultipleEvents(t *testing.T) {
	fr := &fakeStreamReader{}
	b, run, _ := newBusWithRun(t)

	const n = 3
	fr.Messages = makeTestEnvelopes(n, run.ID)
	pump, _ := newTestPump(fr, b)

	processed, err := pump.runOnce(context.Background())
	if err != nil {
		t.Fatalf("runOnce 失败: %v", err)
	}
	if processed != n {
		t.Errorf("processed: got %d, want %d", processed, n)
	}

	// 验证顺序
	events, err := b.GetEvents(run.ID)
	if err != nil {
		t.Fatalf("GetEvents 失败: %v", err)
	}

	seqs := 0
	for _, e := range events {
		if e.Type == "agent.run.completed" {
			seqs++
		}
	}
	if seqs != n {
		t.Errorf("agent.run.completed 事件数: got %d, want %d", seqs, n)
	}

	// 验证所有 msgIDs 都被 ack
	if len(fr.AckedIDs) != n {
		t.Errorf("期望 ack %d 个 id，got %d", n, len(fr.AckedIDs))
	}
}

func TestEventPumpEmptyMessages(t *testing.T) {
	fr := &fakeStreamReader{}
	b, _, _ := newBusWithRun(t)

	fr.Messages = nil
	pump, _ := newTestPump(fr, b)

	processed, err := pump.runOnce(context.Background())
	if err != nil {
		t.Fatalf("空消息应不报错: %v", err)
	}
	if processed != 0 {
		t.Errorf("processed: got %d, want 0", processed)
	}
	if fr.AckCalls > 0 {
		t.Errorf("空消息不应 ack，但调用了 %d 次", fr.AckCalls)
	}
}

func TestEventPumpReadError(t *testing.T) {
	fr := &fakeStreamReader{}
	b, _, _ := newBusWithRun(t)

	fr.ReadErr = errors.New("redis: connection refused")
	pump, _ := newTestPump(fr, b)

	_, err := pump.runOnce(context.Background())
	if err == nil {
		t.Error("读取错误应返回 error")
	}
}

func TestEventPumpDecodeErrorMovesToDLQ(t *testing.T) {
	fr := &fakeStreamReader{}
	b, run, _ := newBusWithRun(t)

	// 构造一个缺少 payload 字段的消息
	fr.Messages = []runtimeredis.StreamMessage{{
		ID: "1700000000000-1",
		Values: map[string]interface{}{
			"schema_version": runtimeredis.SchemaVersion,
			"event_id":       "env-001",
			// 无 payload 字段
		},
	}}
	pump, _ := newTestPump(fr, b)

	_, err := pump.runOnce(context.Background())
	if err != nil {
		t.Fatalf("decode 失败应由 DLQ 隔离: %v", err)
	}
	// 原子 DLQ adapter 负责 ACK，不走普通 AckEvents。
	if fr.AckCalls > 0 {
		t.Errorf("decode 失败不应单独 ack，但调用了 %d 次", fr.AckCalls)
	}
	if len(fr.DeadLetteredIDs) != 1 {
		t.Fatalf("decode 失败应进入 DLQ: %#v", fr.DeadLetteredIDs)
	}
	// 验证事件未追加（GetEvents 不应包含该事件）
	events, err := b.GetEvents(run.ID)
	if err != nil {
		t.Fatalf("GetEvents 失败: %v", err)
	}
	for _, e := range events {
		if e.ID == "evt-001" {
			t.Error("decode 失败的事件不应出现在 state 中")
		}
	}
}

func TestEventPumpAckError(t *testing.T) {
	fr := &fakeStreamReader{}
	b, run, _ := newBusWithRun(t)

	fr.Messages = makeTestEnvelopes(1, run.ID)
	fr.AckErr = errors.New("redis: ack failed")
	pump, _ := newTestPump(fr, b)

	processed, err := pump.runOnce(context.Background())
	if err == nil {
		t.Error("ack 错误应返回 error")
	}
	// 事件应已追加（ack 错误只在处理之后）
	if processed != 1 {
		t.Errorf("processed: got %d, want 1", processed)
	}
}

func TestEventPumpRunNotFound(t *testing.T) {
	fr := &fakeStreamReader{}
	b, run, _ := newBusWithRun(t)

	// 使用不存在的 runID
	fr.Messages = makeTestEnvelopes(1, "nonexistent-run")
	pump, _ := newTestPump(fr, b)

	processed, err := pump.runOnce(context.Background())
	if err != nil {
		t.Fatalf("runOnce 失败: %v (run 不存在不应阻止 pump)", err)
	}
	// 事件未成功追加 → processed 为 0
	if processed != 0 {
		t.Errorf("processed: got %d, want 0 (run 不存在)", processed)
	}
	// 先保留 pending，给 SeedAcceptedRun 竞态一个有界恢复窗口。
	if fr.AckCalls != 0 {
		t.Errorf("run 不存在时不应立即 ack，got %d", fr.AckCalls)
	}

	// 确保原有 run 不受影响
	events, _ := b.GetEvents(run.ID)
	if len(events) == 0 {
		t.Error("原有 run 的事件不应被清空")
	}
}

type fakeProjectionLoader struct {
	task  contracts.TaskDTO
	run   contracts.AgentRunDTO
	calls int
}

func (l *fakeProjectionLoader) LoadRuntimeProjection(
	_ context.Context, _, _ contracts.ID,
) (contracts.TaskDTO, contracts.AgentRunDTO, error) {
	l.calls++
	return l.task, l.run, nil
}

func TestEventPumpHydratesBackgroundCreatedRun(t *testing.T) {
	fr := &fakeStreamReader{Messages: makeTestEnvelopes(1, "scheduled-run")}
	b := NewInMemoryRuntimeBus()
	pump, _ := newTestPump(fr, b)
	loader := &fakeProjectionLoader{
		task: contracts.TaskDTO{ID: "task-001", ActiveRunID: "scheduled-run"},
		run:  contracts.AgentRunDTO{ID: "scheduled-run", TaskID: "task-001", Status: "running"},
	}
	pump.setProjectionLoader(loader)

	processed, err := pump.runOnce(context.Background())
	if err != nil || processed != 1 {
		t.Fatalf("background run projection failed: processed=%d err=%v", processed, err)
	}
	if loader.calls != 1 || fr.AckCalls != 1 {
		t.Fatalf("loader/ack mismatch: calls=%d ack=%d", loader.calls, fr.AckCalls)
	}
	events, err := b.GetEvents("scheduled-run")
	if err != nil || len(events) != 1 || events[0].ID != "evt-001" {
		t.Fatalf("hydrated events mismatch: events=%v err=%v", events, err)
	}
}

func TestEventPumpPoisonMessageDoesNotBlockValidSibling(t *testing.T) {
	fr := &fakeStreamReader{}
	b, run, _ := newBusWithRun(t)
	fr.Messages = []runtimeredis.StreamMessage{
		{ID: "poison-1", Values: map[string]interface{}{
			"schema_version": runtimeredis.SchemaVersion,
			"payload":        "{not-json",
			"type":           "agent.run.completed",
		}},
		makeTestEnvelope(2, run.ID),
	}
	pump, _ := newTestPump(fr, b)

	processed, err := pump.runOnce(context.Background())

	if err != nil {
		t.Fatalf("poison event 应隔离且继续处理同批正常事件: %v", err)
	}
	if processed != 1 || len(fr.DeadLetteredIDs) != 1 || fr.AckCalls != 1 {
		t.Fatalf(
			"处理结果不符: processed=%d dlq=%v ack=%d",
			processed, fr.DeadLetteredIDs, fr.AckCalls,
		)
	}
}

func TestEventPumpReclaimsPendingAndDeduplicatesProjection(t *testing.T) {
	fr := &fakeStreamReader{}
	b, run, _ := newBusWithRun(t)
	message := makeTestEnvelope(1, run.ID)
	fr.Pending = []runtimeredis.PendingStreamEntry{{
		ID: message.ID, Idle: 10 * time.Second, Deliveries: 1,
	}}
	fr.Claimed = []runtimeredis.StreamMessage{message}
	pump, _ := newTestPump(fr, b)

	processed, err := pump.runOnce(context.Background())
	if err != nil || processed != 1 {
		t.Fatalf("pending event 接管失败: processed=%d err=%v", processed, err)
	}
	// 模拟 ACK 结果不确定后的重复投影；event ID 去重，状态不重复追加。
	processed, err = pump.processDelivery(context.Background(), runtimeredis.RuntimeEventDelivery{
		MessageID:     message.ID,
		Fields:        message.Values,
		Envelope:      mustDecodeEnvelope(t, message),
		DeliveryCount: 3,
		Reclaimed:     true,
	})
	if err != nil || processed != 1 {
		t.Fatalf("重复投影处理失败: processed=%d err=%v", processed, err)
	}
	events, _ := b.GetEvents(run.ID)
	count := 0
	for _, event := range events {
		if event.ID == "evt-001" {
			count++
		}
	}
	if count != 1 {
		t.Fatalf("event ID 应幂等投影一次，实际 %d", count)
	}
}

func TestEventPumpProjectionRetryExhaustionMovesToDLQ(t *testing.T) {
	fr := &fakeStreamReader{}
	b, _, _ := newBusWithRun(t)
	message := makeTestEnvelope(1, "missing-run")
	fr.Pending = []runtimeredis.PendingStreamEntry{{
		ID: message.ID, Idle: 30 * time.Second, Deliveries: 2,
	}}
	fr.Claimed = []runtimeredis.StreamMessage{message}
	pump, _ := newTestPump(fr, b)

	processed, err := pump.runOnce(context.Background())

	if err != nil || processed != 0 {
		t.Fatalf("投影耗尽应安全 DLQ: processed=%d err=%v", processed, err)
	}
	if len(fr.DeadLetteredIDs) != 1 {
		t.Fatalf("投影耗尽未进入 DLQ: %#v", fr.DeadLetteredIDs)
	}
}

func mustDecodeEnvelope(
	t *testing.T, message runtimeredis.StreamMessage,
) runtimeredis.RuntimeEventEnvelope {
	t.Helper()
	reader := &fakeStreamReader{Messages: []runtimeredis.StreamMessage{message}}
	runtimeReader, err := runtimeredis.NewRuntimeEventReader(reader)
	if err != nil {
		t.Fatal(err)
	}
	deliveries, err := runtimeReader.ReadDeliveries(
		context.Background(), runtimeredis.GroupGatewayEvents, "test", 1,
	)
	if err != nil || len(deliveries) != 1 || !deliveries[0].Valid() {
		t.Fatalf("测试 envelope decode 失败: %#v err=%v", deliveries, err)
	}
	return deliveries[0].Envelope
}

func TestEventPumpContextCancellation(t *testing.T) {
	fr := &fakeStreamReader{}
	b, _, _ := newBusWithRun(t)

	// 在 reader 返回错误前先正常返回空消息，避免 tight loop
	fr.Messages = nil
	pump, _ := newTestPump(fr, b)

	// 通过 Start 启动 loop（管理 wg 生命周期）
	err := pump.Start()
	if err != nil {
		t.Fatalf("Start 失败: %v", err)
	}

	// 给 loop 一点时间运行
	time.Sleep(100 * time.Millisecond)

	// Close 应取消内部 context 并等待 loop 退出
	err = pump.Close()
	if err != nil {
		t.Fatalf("Close 失败: %v", err)
	}
	// 到达此处说明 wg.Wait() 已返回，loop 已退出
}

func TestEventPumpStartStop(t *testing.T) {
	fr := &fakeStreamReader{}
	b, _, _ := newBusWithRun(t)

	pump, _ := newTestPump(fr, b)

	// Start 应创建 consumer group 并启动 loop
	err := pump.Start()
	if err != nil {
		t.Fatalf("Start 失败: %v", err)
	}
	if fr.CreateCalls != 1 {
		t.Errorf("Start 应调用 XGroupCreateMkStream，got %d", fr.CreateCalls)
	}

	// Close 应停止 loop
	err = pump.Close()
	if err != nil {
		t.Fatalf("Close 失败: %v", err)
	}
}

func TestEventPumpConsumerGroupCreationError(t *testing.T) {
	fr := &fakeStreamReader{}
	fr.CreateErr = errors.New("redis: no auth")
	b, _, _ := newBusWithRun(t)

	pump, _ := newTestPump(fr, b)

	err := pump.Start()
	if err == nil {
		t.Error("XGroupCreateMkStream 失败时 Start 应返回 error")
	}
}

func TestEventPumpBackoffOnReadError(t *testing.T) {
	fr := &fakeStreamReader{}
	b, _, _ := newBusWithRun(t)

	fr.ReadErr = errors.New("redis: temp error")
	pump, backoff := newTestPump(fr, b)

	// 通过 Start 启动 loop
	err := pump.Start()
	if err != nil {
		t.Fatalf("Start 失败: %v", err)
	}

	// 等待足够时间让 loop 经历失败 → backoff
	time.Sleep(200 * time.Millisecond)

	// Close 停止 loop
	pump.Close()

	if backoff.waitCallCount() < 1 {
		t.Error("读取失败时应调用 backoff.Wait")
	}
}

func TestEventPumpImplementsPattern(t *testing.T) {
	// 编译期检查：eventPump 的组合模式
	fr := &fakeStreamReader{}
	b, _, _ := newBusWithRun(t)
	pump, _ := newTestPump(fr, b)

	if pump.reader == nil {
		t.Error("reader 不应为 nil")
	}
	if pump.inMemory == nil {
		t.Error("inMemory 不应为 nil")
	}
	if pump.backoff == nil {
		t.Error("backoff 不应为 nil")
	}
	_ = b
}

func TestEventPumpResetOnSuccess(t *testing.T) {
	fr := &fakeStreamReader{}
	b, run, _ := newBusWithRun(t)

	// 失败模式：先让 loop 经历一次错误
	fr.ReadErr = errors.New("temp error")
	pump, _ := newTestPump(fr, b)

	err := pump.Start()
	if err != nil {
		t.Fatalf("Start 失败: %v", err)
	}
	time.Sleep(200 * time.Millisecond)
	pump.Close()

	// 现在切换到成功模式：新建 pump，reader 返回合法消息
	fr2 := &fakeStreamReader{}
	fr2.Messages = makeTestEnvelopes(1, run.ID)
	pump2, _ := newTestPump(fr2, b)

	err = pump2.Start()
	if err != nil {
		t.Fatalf("Start 2 失败: %v", err)
	}
	time.Sleep(200 * time.Millisecond)
	pump2.Close()

	// 成功读取后应看到 ack 被调用
	if fr2.AckCalls < 1 {
		t.Error("成功读取后应 ack")
	}
}
