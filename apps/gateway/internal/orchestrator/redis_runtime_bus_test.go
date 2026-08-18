package orchestrator

import (
	"context"
	"encoding/json"
	"fmt"
	"sync"
	"testing"

	"github.com/google/uuid"
	"github.com/jarvis-assistant/gateway/internal/contracts"
	runtimeredis "github.com/jarvis-assistant/gateway/internal/redis"
)

// -- fake RedisStreamClient for bus tests --

type fakeStreamClient struct {
	mu      sync.Mutex
	Calls   []streamCall
	XAddErr error
}

type streamCall struct {
	Stream string
	Values map[string]interface{}
}

var _ runtimeredis.RedisStreamClient = (*fakeStreamClient)(nil)

func (f *fakeStreamClient) XAdd(_ context.Context, stream string, values map[string]interface{}) error {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.Calls = append(f.Calls, streamCall{Stream: stream, Values: values})
	return f.XAddErr
}

// callCount 线程安全地返回调用次数。
func (f *fakeStreamClient) callCount() int {
	f.mu.Lock()
	defer f.mu.Unlock()
	return len(f.Calls)
}

func newFakeClient() *fakeStreamClient {
	return &fakeStreamClient{}
}

func newTestRedisRuntimeBus(t *testing.T, fc *fakeStreamClient) *RedisRuntimeBus {
	t.Helper()
	tr, err := runtimeredis.NewRedisRuntimeTransport(fc)
	if err != nil {
		t.Fatalf("创建 RedisRuntimeTransport 失败: %v", err)
	}
	// 测试用：不传 reader → 不创建 eventPump（测试不依赖 Redis 读取）
	bus, err := NewRedisRuntimeBus(tr, nil, nil, nil, nil, 0)
	if err != nil {
		t.Fatalf("创建 RedisRuntimeBus 失败: %v", err)
	}
	return bus
}

// -- helpers --

func getFieldStr(values map[string]interface{}, key string) string {
	if v, ok := values[key].(string); ok {
		return v
	}
	return ""
}

func assertNoEvent(t *testing.T, events []contracts.RuntimeEvent, eventType string) {
	t.Helper()
	for _, e := range events {
		if e.Type == eventType {
			t.Errorf("不应包含 %q 事件，但找到了", eventType)
		}
	}
}

// assertEventCount 断言事件列表中某类型事件的准确数量。
func assertEventCount(t *testing.T, events []contracts.RuntimeEvent, eventType string, expected int) {
	t.Helper()
	count := 0
	for _, e := range events {
		if e.Type == eventType {
			count++
		}
	}
	if count != expected {
		t.Errorf("%q 事件数量 = %d，期望 %d", eventType, count, expected)
	}
}

// registerPendingPermForTest 在 InMemoryRuntimeBus 中直接注册一个权限请求（测试 helper）。
// 返回 permission request ID。
func registerPendingPermForTest(t *testing.T, inMemory *InMemoryRuntimeBus, taskID, runID contracts.ID) contracts.ID {
	t.Helper()
	permReqID := uuid.NewString()
	now := contracts.NowISO()

	inMemory.mu.Lock()
	defer inMemory.mu.Unlock()

	inMemory.state.PermissionReqs[permReqID] = &contracts.PermissionRequestDTO{
		ID:            permReqID,
		TaskID:        taskID,
		RunID:         runID,
		StepID:        uuid.NewString(),
		ToolName:      "shell",
		ActionSummary: "测试权限请求 - 执行 Shell 命令",
		Reason:        "集成测试需要",
		RiskLevel:     "L3",
		Scope:         contracts.PermissionScopeDTO{Type: "once"},
		ArgumentsSummary: map[string]interface{}{
			"command": "ls -la",
		},
		AllowedDecisions: []contracts.PermissionDecisionType{"allow_once", "deny"},
		CreatedAt:        now,
	}
	return permReqID
}

// -- 编译期接口断言 --

func TestRedisRuntimeBusImplementsInterfaces(t *testing.T) {
	var _ RuntimeBus = (*RedisRuntimeBus)(nil)
	var _ RuntimeStateStore = (*RedisRuntimeBus)(nil)
	var _ PumpCloser = (*RedisRuntimeBus)(nil)
	t.Log("RedisRuntimeBus 同时实现 RuntimeBus、RuntimeStateStore 和 PumpCloser")
}

func TestNewRedisRuntimeBusNilTransport(t *testing.T) {
	_, err := NewRedisRuntimeBus(nil, nil, nil, nil, nil, 0)
	if err == nil {
		t.Fatal("期望 nil transport 返回 error，但 err 为 nil")
	}
}

func TestRedisRuntimeBusSeedAcceptedRunFeedsRealtimeProjection(t *testing.T) {
	fc := newFakeClient()
	runtimeBus := newTestRedisRuntimeBus(t, fc)
	task := contracts.TaskDTO{ID: "task-authority", ActiveRunID: "run-authority", Status: "running"}
	run := contracts.AgentRunDTO{ID: "run-authority", TaskID: task.ID, Status: "queued"}
	initial := []contracts.RuntimeEvent{{
		ID: "event-created", Type: "task.created", TaskID: task.ID, RunID: run.ID,
	}}

	runtimeBus.SeedAcceptedRun(task, run, initial)

	if _, ok := runtimeBus.GetRun(run.ID); !ok {
		t.Fatal("Control Plane 权威 run 未写入 RedisRuntimeBus 的实时投影")
	}
	if err := runtimeBus.inMemory.AppendRuntimeEvents(run.ID, []contracts.RuntimeEvent{{
		ID: "event-started", Type: "agent.run.started", TaskID: task.ID, RunID: run.ID,
	}}); err != nil {
		t.Fatalf("EventPump 后续事件应能追加到权威 run: %v", err)
	}
	events, err := runtimeBus.GetEvents(run.ID)
	if err != nil || len(events) != 2 {
		t.Fatalf("实时投影事件数量异常: events=%v err=%v", events, err)
	}
}

// -- PrepareRun：最小初始状态 --

func TestRedisRuntimeBusPrepareRunMinimalEvents(t *testing.T) {
	fc := newFakeClient()
	bus := newTestRedisRuntimeBus(t, fc)

	_, _, events, err := bus.PrepareRun(contracts.CreateTaskInput{
		UserGoal:      "最小事件测试",
		WorkspacePath: "/tmp/minimal",
	})
	if err != nil {
		t.Fatalf("PrepareRun 失败: %v", err)
	}

	if len(events) != 1 {
		t.Fatalf("期望 1 个事件（task.created），实际 %d", len(events))
	}
	if events[0].Type != "task.created" {
		t.Fatalf("唯一事件类型 = %q，期望 task.created", events[0].Type)
	}

	assertNoEvent(t, events, "model.delta")
	assertNoEvent(t, events, "model.call.completed")
	assertNoEvent(t, events, "tool.call.started")
	assertNoEvent(t, events, "tool.call.finished")
	assertNoEvent(t, events, "tool.call.failed")
	assertNoEvent(t, events, "artifact.created")
	assertNoEvent(t, events, "agent.run.completed")
	assertNoEvent(t, events, "agent.run.started")
	assertNoEvent(t, events, "agent.step.started")
	assertNoEvent(t, events, "agent.step.completed")

	t.Logf("最小事件验证通过: event type=%s", events[0].Type)
}

func TestRedisRuntimeBusPrepareRunSuccess(t *testing.T) {
	fc := newFakeClient()
	bus := newTestRedisRuntimeBus(t, fc)

	task, run, events, err := bus.PrepareRun(contracts.CreateTaskInput{
		UserGoal:      "Redis 接线测试",
		WorkspacePath: "/tmp/redis-test",
	})
	if err != nil {
		t.Fatalf("PrepareRun 失败: %v", err)
	}

	if task == nil || task.ID == "" {
		t.Fatal("task 不应为 nil 且 ID 不应为空")
	}
	if run == nil || run.ID == "" {
		t.Fatal("run 不应为 nil 且 ID 不应为空")
	}
	if run.TaskID != task.ID {
		t.Errorf("run.TaskID = %q，应与 task.ID = %q 一致", run.TaskID, task.ID)
	}
	if run.Status != "queued" {
		t.Errorf("run.Status = %q，期望 queued", run.Status)
	}

	if len(fc.Calls) != 1 {
		t.Fatalf("期望 1 次 XAdd 调用，实际 %d", len(fc.Calls))
	}
	call := fc.Calls[0]
	if call.Stream != runtimeredis.StreamRunQueue {
		t.Errorf("stream = %q，期望 %q", call.Stream, runtimeredis.StreamRunQueue)
	}

	payloadStr := getFieldStr(call.Values, "payload")
	if payloadStr == "" {
		t.Fatal("payload 字段为空")
	}
	var msg runtimeredis.RunJobMessage
	if err := json.Unmarshal([]byte(payloadStr), &msg); err != nil {
		t.Fatalf("payload JSON decode 失败: %v", err)
	}

	if msg.JobID == "" {
		t.Error("job_id 为空")
	}
	if msg.TraceID == "" {
		t.Error("trace_id 为空")
	}
	if msg.TaskID != task.ID {
		t.Errorf("msg.TaskID = %q，应与 task.ID = %q 一致", msg.TaskID, task.ID)
	}
	if msg.RunID != run.ID {
		t.Errorf("msg.RunID = %q，应与 run.ID = %q 一致", msg.RunID, run.ID)
	}
	if msg.UserGoal != "Redis 接线测试" {
		t.Errorf("msg.UserGoal = %q", msg.UserGoal)
	}
	if msg.WorkspacePath != "/tmp/redis-test" {
		t.Errorf("msg.WorkspacePath = %q", msg.WorkspacePath)
	}
	if msg.CreatedAt == "" {
		t.Error("created_at 为空")
	}
	if msg.SchemaVersion != runtimeredis.SchemaVersion {
		t.Errorf("msg.SchemaVersion = %q", msg.SchemaVersion)
	}

	if getFieldStr(call.Values, "job_id") != msg.JobID {
		t.Error("标量 job_id 与 payload 不一致")
	}
	if getFieldStr(call.Values, "trace_id") != msg.TraceID {
		t.Error("标量 trace_id 与 payload 不一致")
	}

	if len(events) != 1 || events[0].Type != "task.created" {
		t.Errorf("events 应仅为 task.created，实际 %d events", len(events))
	}

	t.Logf("job_id=%s trace_id=%s task_id=%s run_id=%s events=%d",
		msg.JobID, msg.TraceID, msg.TaskID, msg.RunID, len(events))
}

func TestRedisRuntimeBusPrepareRunWithEmptyWorkspace(t *testing.T) {
	fc := newFakeClient()
	bus := newTestRedisRuntimeBus(t, fc)

	_, _, _, err := bus.PrepareRun(contracts.CreateTaskInput{UserGoal: "无工作区"})
	if err != nil {
		t.Fatalf("PrepareRun 失败: %v", err)
	}
	if len(fc.Calls) != 1 {
		t.Fatalf("期望 1 次 XAdd 调用，实际 %d", len(fc.Calls))
	}
	payloadStr := getFieldStr(fc.Calls[0].Values, "payload")
	var msg runtimeredis.RunJobMessage
	if err := json.Unmarshal([]byte(payloadStr), &msg); err != nil {
		t.Fatalf("payload JSON decode 失败: %v", err)
	}
	if msg.UserGoal != "无工作区" {
		t.Errorf("UserGoal = %q，期望 无工作区", msg.UserGoal)
	}
}

func TestRedisRuntimeBusPrepareRunEnqueueFails(t *testing.T) {
	fc := newFakeClient()
	fc.XAddErr = fmt.Errorf("redis connection refused")
	bus := newTestRedisRuntimeBus(t, fc)

	_, _, _, err := bus.PrepareRun(contracts.CreateTaskInput{UserGoal: "入队失败测试"})
	if err == nil {
		t.Fatal("期望 enqueue 失败时 PrepareRun 返回 error，但 err 为 nil")
	}
	t.Logf("enqueue 失败正确返回 error: %v", err)
	if len(fc.Calls) != 1 {
		t.Errorf("期望 1 次 XAdd 调用，实际 %d", len(fc.Calls))
	}
}

// -- GetEvents：最小初始事件 --

func TestRedisRuntimeBusGetEventsReturnsMinimalEvents(t *testing.T) {
	fc := newFakeClient()
	bus := newTestRedisRuntimeBus(t, fc)

	_, run, _, err := bus.PrepareRun(contracts.CreateTaskInput{UserGoal: "GetEvents 最小事件测试"})
	if err != nil {
		t.Fatalf("PrepareRun 失败: %v", err)
	}

	events, err := bus.GetEvents(run.ID)
	if err != nil {
		t.Fatalf("GetEvents 失败: %v", err)
	}
	if len(events) == 0 {
		t.Fatal("GetEvents 返回空事件列表")
	}

	if e := findEvent(events, "task.created"); e == nil {
		t.Error("缺少 task.created 事件")
	}
	assertNoEvent(t, events, "model.delta")
	assertNoEvent(t, events, "model.call.completed")
	assertNoEvent(t, events, "tool.call.started")
	assertNoEvent(t, events, "tool.call.finished")
	assertNoEvent(t, events, "tool.call.failed")
	assertNoEvent(t, events, "artifact.created")
	assertNoEvent(t, events, "agent.run.completed")

	t.Logf("GetEvents 只返回最小事件: %d events", len(events))
}

func TestRedisRuntimeBusGetEventsNonexistentRun(t *testing.T) {
	fc := newFakeClient()
	bus := newTestRedisRuntimeBus(t, fc)

	_, err := bus.GetEvents("non-existent-run")
	if err == nil {
		t.Fatal("期望不存在的 run 返回 error，但 err 为 nil")
	}
}

// -- ResolvePermission：ack-only + trace_id 连续性 --

// TestRedisRuntimeBusResolvePermissionTraceIDFullFlow 验证真实 trace 贯通：
// PrepareRun → 记录 trace_id → 在同 run 上注册权限 → ResolvePermission →
// 断言 PermissionDecisionCommand.trace_id == RunJobMessage.trace_id，
// 且返回 events 只包含 permission.resolved。
func TestRedisRuntimeBusResolvePermissionTraceIDFullFlow(t *testing.T) {
	fc := newFakeClient()
	bus := newTestRedisRuntimeBus(t, fc)

	// 1. PrepareRun
	task, run, _, err := bus.PrepareRun(contracts.CreateTaskInput{UserGoal: "trace 贯通测试"})
	if err != nil {
		t.Fatalf("PrepareRun 失败: %v", err)
	}

	// 提取 RunJobMessage.trace_id
	if len(fc.Calls) != 1 {
		t.Fatalf("期望 1 次 PrepareRun XAdd，实际 %d", len(fc.Calls))
	}
	payloadStr := getFieldStr(fc.Calls[0].Values, "payload")
	var jobMsg runtimeredis.RunJobMessage
	if err := json.Unmarshal([]byte(payloadStr), &jobMsg); err != nil {
		t.Fatalf("RunJobMessage decode 失败: %v", err)
	}
	runJobTraceID := jobMsg.TraceID
	t.Logf("RunJobMessage.trace_id = %s", runJobTraceID)

	// 2. 在同 run 上注册 pending permission
	permReqID := registerPendingPermForTest(t, bus.inMemory, task.ID, run.ID)

	// 清空 PrepareRun 的 XAdd 记录
	fc.Calls = nil

	// 3. ResolvePermission
	permReq, postEvents, err := bus.ResolvePermission(contracts.PermissionDecisionDTO{
		RequestID: permReqID,
		Decision:  "allow_once",
		Note:      "trace 贯通测试",
	})
	if err != nil {
		t.Fatalf("ResolvePermission 失败: %v", err)
	}
	if permReq == nil {
		t.Fatal("permReq 不应为 nil")
	}

	// 4. Redis 模式不生成 Go 侧 events（worker 负责生成 permission.resolved）
	if len(postEvents) != 0 {
		t.Errorf("Redis 模式 ResolvePermission 不应生成 events，实际 %d", len(postEvents))
	}

	// 5. 解码 PermissionDecisionCommand → 断言 trace_id 一致
	if len(fc.Calls) != 1 {
		t.Fatalf("期望 1 次 PublishPermissionDecision，实际 %d", len(fc.Calls))
	}
	if fc.Calls[0].Stream != runtimeredis.StreamWorkerCommand {
		t.Errorf("stream = %q，期望 %q", fc.Calls[0].Stream, runtimeredis.StreamWorkerCommand)
	}

	cmdPayloadStr := getFieldStr(fc.Calls[0].Values, "payload")
	var cmd runtimeredis.PermissionDecisionCommand
	if err := json.Unmarshal([]byte(cmdPayloadStr), &cmd); err != nil {
		t.Fatalf("PermissionDecisionCommand decode 失败: %v", err)
	}

	if cmd.TraceID != runJobTraceID {
		t.Errorf("PermissionDecisionCommand.trace_id = %q，与 RunJobMessage.trace_id = %q 不一致",
			cmd.TraceID, runJobTraceID)
	}
	if cmd.RequestID != permReqID {
		t.Errorf("cmd.RequestID = %q，期望 %q", cmd.RequestID, permReqID)
	}
	if cmd.RunID != run.ID {
		t.Errorf("cmd.RunID = %q，应与 run.ID = %q 一致", cmd.RunID, run.ID)
	}

	t.Logf("trace 贯通验证通过: job.trace_id=%s == cmd.trace_id=%s", runJobTraceID, cmd.TraceID)
}

// TestRedisRuntimeBusResolvePermissionSuccess 验证 ResolvePermission 成功时：
// 只返回 permission.resolved ack，不包含任何 worker outcome 事件。
func TestRedisRuntimeBusResolvePermissionSuccess(t *testing.T) {
	t.Run("allow_once", func(t *testing.T) {
		fc := newFakeClient()
		bus := newTestRedisRuntimeBus(t, fc)

		_, _, events, err := bus.inMemory.PrepareDevMock("permission_required")
		if err != nil {
			t.Fatalf("PrepareDevMock 失败: %v", err)
		}
		permEvent := requireEvent(t, events, "permission.required")
		reqMap := getMap(permEvent.Payload, "request")
		permReqID := getString(reqMap, "id")
		if permReqID == "" {
			t.Fatal("无法提取 permission request id")
		}

		fc.Calls = nil

		permReq, postEvents, err := bus.ResolvePermission(contracts.PermissionDecisionDTO{
			RequestID: permReqID,
			Decision:  "allow_once",
			Note:      "批准执行",
		})
		if err != nil {
			t.Fatalf("ResolvePermission 失败: %v", err)
		}
		if permReq == nil {
			t.Fatal("permReq 不应为 nil")
		}

		// Redis 模式不生成 Go 侧 permission.resolved（worker 负责生成）
		if len(postEvents) != 0 {
			t.Errorf("Redis mode should not generate Go-side events, got %d", len(postEvents))
		}
		assertNoEvent(t, postEvents, "tool.call.finished")
		assertNoEvent(t, postEvents, "tool.call.failed")
		assertNoEvent(t, postEvents, "agent.step.completed")
		assertNoEvent(t, postEvents, "agent.run.completed")

		// -- 验证 Redis publish --
		if len(fc.Calls) != 1 {
			t.Fatalf("期望 1 次 XAdd 调用，实际 %d", len(fc.Calls))
		}
		call := fc.Calls[0]
		if call.Stream != runtimeredis.StreamWorkerCommand {
			t.Errorf("stream = %q，期望 %q", call.Stream, runtimeredis.StreamWorkerCommand)
		}

		payloadStr := getFieldStr(call.Values, "payload")
		if payloadStr == "" {
			t.Fatal("payload 字段为空")
		}
		var cmd runtimeredis.PermissionDecisionCommand
		if err := json.Unmarshal([]byte(payloadStr), &cmd); err != nil {
			t.Fatalf("payload JSON decode 失败: %v", err)
		}

		if cmd.CommandID == "" {
			t.Error("command_id 为空")
		}
		if cmd.TraceID == "" {
			t.Error("trace_id 为空")
		}
		if cmd.RequestID != permReqID {
			t.Errorf("cmd.RequestID = %q，期望 %q", cmd.RequestID, permReqID)
		}
		if cmd.TaskID != permReq.TaskID {
			t.Errorf("cmd.TaskID = %q", cmd.TaskID)
		}
		if cmd.RunID != permReq.RunID {
			t.Errorf("cmd.RunID = %q", cmd.RunID)
		}
		if cmd.Decision != "allow_once" {
			t.Errorf("cmd.Decision = %q，期望 allow_once", cmd.Decision)
		}
		if cmd.Note != "批准执行" {
			t.Errorf("cmd.Note = %q，期望 批准执行", cmd.Note)
		}
		if cmd.DecidedAt == "" {
			t.Error("decided_at 为空")
		}
		if cmd.SchemaVersion != runtimeredis.SchemaVersion {
			t.Errorf("cmd.SchemaVersion = %q", cmd.SchemaVersion)
		}

		t.Logf("allow_once ack: command_id=%s trace_id=%s request_id=%s events=%d",
			cmd.CommandID, cmd.TraceID, cmd.RequestID, len(postEvents))
	})

	t.Run("deny", func(t *testing.T) {
		fc := newFakeClient()
		bus := newTestRedisRuntimeBus(t, fc)

		_, _, events, _ := bus.inMemory.PrepareDevMock("permission_required")
		permEvent := requireEvent(t, events, "permission.required")
		reqMap := getMap(permEvent.Payload, "request")
		permReqID := getString(reqMap, "id")

		fc.Calls = nil

		_, postEvents, err := bus.ResolvePermission(contracts.PermissionDecisionDTO{
			RequestID: permReqID,
			Decision:  "deny",
		})
		if err != nil {
			t.Fatalf("deny ResolvePermission 失败: %v", err)
		}

		// Redis 模式不生成 Go 侧 permission.resolved（worker 负责生成）
		if len(postEvents) != 0 {
			t.Errorf("Redis mode should not generate Go-side events, got %d", len(postEvents))
		}
		assertNoEvent(t, postEvents, "tool.call.finished")
		assertNoEvent(t, postEvents, "tool.call.failed")
		assertNoEvent(t, postEvents, "agent.step.completed")
		assertNoEvent(t, postEvents, "agent.run.completed")

		// -- 验证 Redis publish（decision=deny） --
		if len(fc.Calls) != 1 {
			t.Fatalf("deny 时也应有 1 次 publish 调用，实际 %d", len(fc.Calls))
		}
		payloadStr := getFieldStr(fc.Calls[0].Values, "payload")
		var cmd runtimeredis.PermissionDecisionCommand
		if err := json.Unmarshal([]byte(payloadStr), &cmd); err != nil {
			t.Fatalf("payload JSON decode 失败: %v", err)
		}
		if cmd.Decision != "deny" {
			t.Errorf("deny 时 cmd.Decision = %q，期望 deny", cmd.Decision)
		}

		t.Logf("deny ack: decision=%s events=%d", cmd.Decision, len(postEvents))
	})
}

// TestRedisRuntimeBusResolvePermissionRetryAfterPublishFail 验证：
// publish 失败 → RestorePermissionRequest 恢复 pending →
// 清除错误后重试成功 → ack events 不含 worker outcome。
func TestRedisRuntimeBusResolvePermissionRetryAfterPublishFail(t *testing.T) {
	fc := newFakeClient()
	bus := newTestRedisRuntimeBus(t, fc)

	_, _, events, _ := bus.inMemory.PrepareDevMock("permission_required")
	permEvent := requireEvent(t, events, "permission.required")
	reqMap := getMap(permEvent.Payload, "request")
	permReqID := getString(reqMap, "id")

	fc.Calls = nil

	// 第一次：publish 失败
	fc.XAddErr = fmt.Errorf("redis publish failed")
	_, _, err := bus.ResolvePermission(contracts.PermissionDecisionDTO{
		RequestID: permReqID,
		Decision:  "allow_once",
	})
	if err == nil {
		t.Fatal("期望 publish 失败时返回 error，但 err 为 nil")
	}
	t.Logf("第一次 resolve（publish 失败）: %v", err)

	// publish 失败后 RestorePermissionRequest 已将 pending 恢复
	// → ReservePermissionRequest 应能再次拿到同 request_id
	permReq, ok := bus.inMemory.ReservePermissionRequest(permReqID)
	if !ok {
		t.Fatal("publish 失败后 Restore 应恢复 pending，但 Reserve 返回 ok=false")
	}
	if permReq.ID != permReqID {
		t.Errorf("permReq.ID = %q，期望 %q", permReq.ID, permReqID)
	}
	// 验证成功后手动 restore（因为 reserve 又消费了），让后续 ResolvePermission 可正常走
	bus.inMemory.RestorePermissionRequest(permReq)
	t.Logf("publish 失败→Restore→Reserve 成功，pending 正确恢复")

	// 清除错误，重试
	fc.XAddErr = nil
	fc.Calls = nil

	permReq2, postEvents, err := bus.ResolvePermission(contracts.PermissionDecisionDTO{
		RequestID: permReqID,
		Decision:  "allow_once",
	})
	if err != nil {
		t.Fatalf("重试 ResolvePermission 失败: %v", err)
	}
	if permReq2 == nil {
		t.Fatal("permReq2 不应为 nil")
	}

	// -- 重试成功后只包含 permission.resolved --
	if len(postEvents) != 0 {
		t.Errorf("Redis mode should not generate Go-side events, got %d", len(postEvents))
	}
	assertNoEvent(t, postEvents, "tool.call.finished")
	assertNoEvent(t, postEvents, "tool.call.failed")
	assertNoEvent(t, postEvents, "agent.step.completed")
	assertNoEvent(t, postEvents, "agent.run.completed")

	if len(fc.Calls) != 1 {
		t.Fatalf("期望重试成功有 1 次 XAdd，实际 %d", len(fc.Calls))
	}
	if fc.Calls[0].Stream != runtimeredis.StreamWorkerCommand {
		t.Errorf("stream = %q，期望 %q", fc.Calls[0].Stream, runtimeredis.StreamWorkerCommand)
	}

	// 权限已被消费，第三次 resolve 返回 error（reserve 失败，不 publish）
	fc.Calls = nil
	_, _, err = bus.ResolvePermission(contracts.PermissionDecisionDTO{
		RequestID: permReqID,
		Decision:  "allow_once",
	})
	if err == nil {
		t.Error("成功消费后第三次 resolve 应返回 error（reserve 失败）")
	}
	if len(fc.Calls) != 0 {
		t.Errorf("第三次 resolve 不应 publish，但实际 XAdd %d 次", len(fc.Calls))
	}

	t.Logf("重试成功: publish 失败→Restore pending→重试成功→权限已消费→events=%d (ack only)", len(postEvents))
}

// TestRedisRuntimeBusResolvePermissionConcurrentReservePublishesOnce 验证：
// 两个并发 ResolvePermission 请求中只有一个能 reserve 成功并 publish，
// 另一个 reserve 失败不 publish。
func TestRedisRuntimeBusResolvePermissionConcurrentReservePublishesOnce(t *testing.T) {
	fc := newFakeClient()
	bus := newTestRedisRuntimeBus(t, fc)

	_, _, events, _ := bus.inMemory.PrepareDevMock("permission_required")
	permEvent := requireEvent(t, events, "permission.required")
	reqMap := getMap(permEvent.Payload, "request")
	permReqID := getString(reqMap, "id")

	fc.Calls = nil

	// 并发调用 ResolvePermission
	var wg sync.WaitGroup
	var results [2]struct {
		err        error
		postEvents []contracts.RuntimeEvent
	}

	for i := 0; i < 2; i++ {
		wg.Add(1)
		go func(idx int) {
			defer wg.Done()
			_, pe, e := bus.ResolvePermission(contracts.PermissionDecisionDTO{
				RequestID: permReqID,
				Decision:  "allow_once",
			})
			results[idx] = struct {
				err        error
				postEvents []contracts.RuntimeEvent
			}{err: e, postEvents: pe}
		}(i)
	}
	wg.Wait()

	// 统计成功和失败
	successCount := 0
	failCount := 0
	for _, r := range results {
		if r.err == nil {
			successCount++
			// 成功返回 events 只含 permission.resolved
			if len(r.postEvents) != 0 {
				t.Errorf("Redis mode should not generate Go-side events, got %d", len(r.postEvents))
			}
			assertNoEvent(t, r.postEvents, "tool.call.finished")
			assertNoEvent(t, r.postEvents, "tool.call.failed")
			assertNoEvent(t, r.postEvents, "agent.step.completed")
			assertNoEvent(t, r.postEvents, "agent.run.completed")
		} else {
			failCount++
			t.Logf("并发失败（预期）: %v", r.err)
		}
	}

	if successCount != 1 {
		t.Errorf("期望 1 个成功，实际 %d", successCount)
	}
	if failCount != 1 {
		t.Errorf("期望 1 个失败（reserve 冲突），实际 %d", failCount)
	}

	// 只有一次 XAdd（只有一个 publish 成功）
	callCnt := fc.callCount()
	if callCnt != 1 {
		t.Errorf("期望 1 次 XAdd 调用，实际 %d（并发重复 publish 风险）", callCnt)
	}
	if callCnt > 0 {
		// 线程安全地检查 stream
		fc.mu.Lock()
		stream := fc.Calls[0].Stream
		fc.mu.Unlock()
		if stream != runtimeredis.StreamWorkerCommand {
			t.Errorf("stream = %q，期望 %q", stream, runtimeredis.StreamWorkerCommand)
		}
	}

	t.Logf("并发安全验证通过: 成功=%d, 失败=%d, XAdd=%d", successCount, failCount, callCnt)
}

// TestRedisRuntimeBusResolvePermissionUnknownRequest 验证 unknown request 不触发 publish。
func TestRedisRuntimeBusResolvePermissionUnknownRequest(t *testing.T) {
	fc := newFakeClient()
	bus := newTestRedisRuntimeBus(t, fc)

	_, _, err := bus.ResolvePermission(contracts.PermissionDecisionDTO{
		RequestID: "non-existent",
		Decision:  "allow_once",
	})
	if err == nil {
		t.Fatal("期望不存在的权限请求返回 error，但 err 为 nil")
	}
	if len(fc.Calls) != 0 {
		t.Errorf("未找到权限请求时不应调用 XAdd，实际调用 %d 次", len(fc.Calls))
	}
}

// -- RuntimeStateStore 委托 --

func TestRedisRuntimeBusStateStoreDelegates(t *testing.T) {
	fc := newFakeClient()
	bus := newTestRedisRuntimeBus(t, fc)

	task, run, _, err := bus.PrepareRun(contracts.CreateTaskInput{UserGoal: "StateStore 委托测试"})
	if err != nil {
		t.Fatalf("PrepareRun 失败: %v", err)
	}

	gotRun, ok := bus.GetRun(run.ID)
	if !ok {
		t.Fatal("GetRun 未找到 run")
	}
	if gotRun.ID != run.ID {
		t.Errorf("GetRun.ID = %q，期望 %q", gotRun.ID, run.ID)
	}
	if gotRun.Status != "queued" {
		t.Errorf("GetRun.Status = %q，期望 queued", gotRun.Status)
	}

	bus.UpdateRunStatus(run.ID, "paused")
	gotRun, ok = bus.GetRun(run.ID)
	if !ok || gotRun.Status != "paused" {
		t.Errorf("UpdateRunStatus 后 status = %q，期望 paused", gotRun.Status)
	}

	bus.UpdateRunStatus("non-existent", "cancelled") // 不 panic

	gotTask, ok := bus.GetTask(task.ID)
	if !ok || gotTask.ID != task.ID {
		t.Error("GetTask 失败")
	}

	_, ok = bus.GetTask("non-existent")
	if ok {
		t.Error("期望不存在的 task 返回 ok=false")
	}
	_, ok = bus.GetRun("non-existent")
	if ok {
		t.Error("期望不存在的 run 返回 ok=false")
	}

	tasks := bus.ListTasks()
	if len(tasks) < 1 {
		t.Fatal("ListTasks 应返回至少 1 个 task")
	}
	found := false
	for _, ts := range tasks {
		if ts.ID == task.ID {
			found = true
			break
		}
	}
	if !found {
		t.Error("ListTasks 中未找到 task")
	}

	t.Logf("StateStore 委托验证通过: task=%s, run=%s", task.ID, run.ID)
}

// -- Pump 生命周期测试 --

func TestRedisRuntimeBusPumpNilWhenNoReader(t *testing.T) {
	// 不传 reader 时，Start/Close 应为 nil（无操作）
	fc := newFakeClient()
	bus := newTestRedisRuntimeBus(t, fc)

	err := bus.Start()
	if err != nil {
		t.Errorf("无 pump 时 Start 应返回 nil，got: %v", err)
	}

	err = bus.Close()
	if err != nil {
		t.Errorf("无 pump 时 Close 应返回 nil，got: %v", err)
	}
}

func TestRedisRuntimeBusPumpStartClose(t *testing.T) {
	fc := newFakeClient()
	tr, err := runtimeredis.NewRedisRuntimeTransport(fc)
	if err != nil {
		t.Fatalf("创建 transport 失败: %v", err)
	}

	// 使用 fake stream reader 创建带 pump 的 bus
	fr := &fakeStreamReader{} // 来自 event_pump_test.go
	eventReader, err := runtimeredis.NewRuntimeEventReader(fr)
	if err != nil {
		t.Fatalf("创建 RuntimeEventReader 失败: %v", err)
	}

	bus, err := NewRedisRuntimeBus(tr, eventReader, fr, &fakeBackoff{}, nil, 0)
	if err != nil {
		t.Fatalf("创建 RedisRuntimeBus 失败: %v", err)
	}

	// Start
	err = bus.Start()
	if err != nil {
		t.Fatalf("Start 失败: %v", err)
	}
	if fr.CreateCalls != 1 {
		t.Errorf("Start 应调用 XGroupCreateMkStream 1 次，got %d", fr.CreateCalls)
	}

	// Close
	err = bus.Close()
	if err != nil {
		t.Fatalf("Close 失败: %v", err)
	}
}

// -- CancelRun 测试（3C review） --

func TestRedisRuntimeBusCancelRunPublishesCommand(t *testing.T) {
	fc := newFakeClient()
	bus := newTestRedisRuntimeBus(t, fc)
	_, run, _, err := bus.PrepareRun(contracts.CreateTaskInput{UserGoal: "cancel command 测试"})
	if err != nil {
		t.Fatalf("PrepareRun 失败: %v", err)
	}

	result, err := bus.CancelRun(run.ID)
	if err != nil {
		t.Fatalf("CancelRun 失败: %v", err)
	}
	if result == nil {
		t.Fatal("CancelRun 应返回 run DTO")
	}

	// 验证 XADD 到 StreamWorkerCommand
	if len(fc.Calls) != 2 {
		t.Fatalf("期望 2 次 XAdd（run job + cancel），实际 %d", len(fc.Calls))
	}
	cancelCall := fc.Calls[1]
	if cancelCall.Stream != runtimeredis.StreamWorkerCommand {
		t.Errorf("stream = %q, want %q", cancelCall.Stream, runtimeredis.StreamWorkerCommand)
	}

	// 验证 command type 为 run.cancel
	cmdType := getFieldStr(cancelCall.Values, "type")
	if cmdType != "run.cancel" {
		t.Errorf("type = %q, want run.cancel", cmdType)
	}

	// 验证 payload 字段完整
	payloadStr := getFieldStr(cancelCall.Values, "payload")
	if payloadStr == "" {
		t.Fatal("payload 为空")
	}
	var cmd runtimeredis.RunCancelCommand
	if err := json.Unmarshal([]byte(payloadStr), &cmd); err != nil {
		t.Fatalf("payload decode 失败: %v", err)
	}
	if cmd.CommandID == "" {
		t.Error("command_id 为空")
	}
	if cmd.TraceID == "" {
		t.Error("trace_id 为空")
	}
	if cmd.RunID != run.ID {
		t.Errorf("run_id = %q, want %q", cmd.RunID, run.ID)
	}
	if cmd.TaskID != run.TaskID {
		t.Errorf("task_id mismatch")
	}
	if cmd.Type != "run.cancel" {
		t.Errorf("type = %q", cmd.Type)
	}
	if cmd.RequestedAt == "" {
		t.Error("requested_at 为空")
	}
	if cmd.SchemaVersion != runtimeredis.SchemaVersion {
		t.Errorf("schema_version = %q", cmd.SchemaVersion)
	}
}

func TestRedisRuntimeBusCancelRunReusesTraceID(t *testing.T) {
	fc := newFakeClient()
	bus := newTestRedisRuntimeBus(t, fc)
	_, run, _, err := bus.PrepareRun(contracts.CreateTaskInput{UserGoal: "trace_id 复用测试"})
	if err != nil {
		t.Fatalf("PrepareRun 失败: %v", err)
	}

	_, err = bus.CancelRun(run.ID)
	if err != nil {
		t.Fatalf("CancelRun 失败: %v", err)
	}

	// 验证 run job 和 cancel command 的 trace_id 一致
	runCall := fc.Calls[0]
	cancelCall := fc.Calls[1]
	runTraceID := getFieldStr(runCall.Values, "trace_id")
	cancelTraceID := getFieldStr(cancelCall.Values, "trace_id")
	if runTraceID != cancelTraceID {
		t.Errorf("trace_id 不一致: run=%q cancel=%q", runTraceID, cancelTraceID)
	}
	if runTraceID == "" {
		t.Error("trace_id 不应为空")
	}
}

func TestRedisRuntimeBusCancelRunDoesNotGenerateCancelledEvent(t *testing.T) {
	fc := newFakeClient()
	bus := newTestRedisRuntimeBus(t, fc)
	_, run, events, err := bus.PrepareRun(contracts.CreateTaskInput{UserGoal: "不生成 cancelled event 测试"})
	if err != nil {
		t.Fatalf("PrepareRun 失败: %v", err)
	}

	// PrepareRun 只应有 task.created（1 个事件）
	if len(events) != 1 {
		t.Fatalf("PrepareRun 应有 1 个事件，实际 %d", len(events))
	}

	_, err = bus.CancelRun(run.ID)
	if err != nil {
		t.Fatalf("CancelRun 失败: %v", err)
	}

	// CancelRun 后 GetEvents 不应增加 agent.run.cancelled
	allEvents, err := bus.GetEvents(run.ID)
	if err != nil {
		t.Fatalf("GetEvents 失败: %v", err)
	}
	for _, e := range allEvents {
		if e.Type == "agent.run.cancelled" {
			t.Error("Redis 模式 CancelRun 不应直接生成 agent.run.cancelled 事件")
		}
	}
}

func TestRedisRuntimeBusCancelRunDoesNotChangeRunStatus(t *testing.T) {
	fc := newFakeClient()
	bus := newTestRedisRuntimeBus(t, fc)
	_, run, _, err := bus.PrepareRun(contracts.CreateTaskInput{UserGoal: "不改变状态测试"})
	if err != nil {
		t.Fatalf("PrepareRun 失败: %v", err)
	}

	_, err = bus.CancelRun(run.ID)
	if err != nil {
		t.Fatalf("CancelRun 失败: %v", err)
	}

	// 验证 run 状态未被改为 cancelled
	updatedRun, ok := bus.GetRun(run.ID)
	if !ok {
		t.Fatal("run 应存在")
	}
	if updatedRun.Status == "cancelled" {
		t.Error("Redis 模式 CancelRun 不应直接改 run status 为 cancelled（应由 worker terminal event 驱动）")
	}
}
