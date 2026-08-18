package handlers

import (
	"context"
	"fmt"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/jarvis-assistant/gateway/internal/contracts"
)

// -- fake RuntimeBus + RuntimeStateStore for testing --

// fakeBus 实现 orchestrator.RuntimeBus + orchestrator.RuntimeStateStore，用于 handler 测试。
type fakeBus struct {
	mu     sync.RWMutex
	tasks  map[contracts.ID]*contracts.TaskDTO
	runs   map[contracts.ID]*contracts.AgentRunDTO
	events map[contracts.ID][]contracts.RuntimeEvent

	getEventsErr error
}

func newFakeBus() *fakeBus {
	return &fakeBus{
		tasks:  make(map[contracts.ID]*contracts.TaskDTO),
		runs:   make(map[contracts.ID]*contracts.AgentRunDTO),
		events: make(map[contracts.ID][]contracts.RuntimeEvent),
	}
}

func (f *fakeBus) addRun(runID, taskID string, initialEvents ...contracts.RuntimeEvent) {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.runs[runID] = &contracts.AgentRunDTO{
		ID: runID, TaskID: taskID, Status: "running",
		CreatedAt: "2026-07-06T10:00:00Z", UpdatedAt: "2026-07-06T10:00:00Z",
	}
	f.tasks[taskID] = &contracts.TaskDTO{
		ID: taskID, Title: "测试任务", Status: "running",
		CreatedAt: "2026-07-06T10:00:00Z", UpdatedAt: "2026-07-06T10:00:00Z",
	}
	f.events[runID] = initialEvents
}

// appendEvent 模拟 EventPump 追加事件。
func (f *fakeBus) appendEvent(runID string, event contracts.RuntimeEvent) {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.events[runID] = append(f.events[runID], event)
}

// -- RuntimeBus 接口实现 --

func (f *fakeBus) PrepareRun(input contracts.CreateTaskInput) (*contracts.TaskDTO, *contracts.AgentRunDTO, []contracts.RuntimeEvent, error) {
	now := contracts.NowISO()
	taskID := fmt.Sprintf("task-%d", time.Now().UnixNano())
	runID := fmt.Sprintf("run-%d", time.Now().UnixNano())
	task := &contracts.TaskDTO{
		ID: taskID, Title: input.UserGoal, UserGoal: input.UserGoal,
		Status: "pending", WorkspacePath: input.WorkspacePath,
		CreatedAt: now, UpdatedAt: now, ActiveRunID: runID,
	}
	run := &contracts.AgentRunDTO{
		ID: runID, TaskID: taskID, AgentID: "agent-default",
		Mode: "single_agent", Status: "queued", CreatedAt: now, UpdatedAt: now,
	}
	initialEvent := contracts.RuntimeEvent{
		ID:   fmt.Sprintf("ev-init-%d", time.Now().UnixNano()),
		Type: "task.created", TaskID: taskID, RunID: runID, Timestamp: now,
		Payload: map[string]interface{}{},
	}
	f.mu.Lock()
	f.tasks[taskID] = task
	f.runs[runID] = run
	f.events[runID] = []contracts.RuntimeEvent{initialEvent}
	f.mu.Unlock()
	return task, run, f.events[runID], nil
}
func (f *fakeBus) PrepareDevMock(scenario string) (*contracts.TaskDTO, *contracts.AgentRunDTO, []contracts.RuntimeEvent, error) {
	return nil, nil, nil, fmt.Errorf("not implemented")
}
func (f *fakeBus) ResolvePermission(decision contracts.PermissionDecisionDTO) (*contracts.PermissionRequestDTO, []contracts.RuntimeEvent, error) {
	return nil, nil, fmt.Errorf("not implemented")
}
func (f *fakeBus) CancelRun(runID contracts.ID) (*contracts.AgentRunDTO, error) {
	f.mu.Lock()
	defer f.mu.Unlock()
	run, ok := f.runs[runID]
	if !ok {
		return nil, fmt.Errorf("run not found: %s", runID)
	}
	cp := *run
	cp.Status = "cancelled"
	f.runs[runID] = &cp
	return &cp, nil
}

func (f *fakeBus) GetEvents(runID contracts.ID) ([]contracts.RuntimeEvent, error) {
	f.mu.RLock()
	defer f.mu.RUnlock()
	if f.getEventsErr != nil {
		return nil, f.getEventsErr
	}
	events, ok := f.events[runID]
	if !ok {
		return nil, fmt.Errorf("run not found: %s", runID)
	}
	// 返回深拷贝
	result := make([]contracts.RuntimeEvent, len(events))
	for i, e := range events {
		result[i] = e
		if e.Payload != nil {
			cp := make(map[string]interface{}, len(e.Payload))
			for k, v := range e.Payload {
				cp[k] = v
			}
			result[i].Payload = cp
		}
	}
	return result, nil
}

// -- RuntimeStateStore 接口实现 --

func (f *fakeBus) GetRun(runID contracts.ID) (*contracts.AgentRunDTO, bool) {
	f.mu.RLock()
	defer f.mu.RUnlock()
	run, ok := f.runs[runID]
	if !ok {
		return nil, false
	}
	cp := *run
	return &cp, true
}

func (f *fakeBus) UpdateRunStatus(runID contracts.ID, status string) {
	f.mu.Lock()
	defer f.mu.Unlock()
	if run, ok := f.runs[runID]; ok {
		run.Status = status
		run.UpdatedAt = fmt.Sprintf("updated-%d", time.Now().UnixNano())
	}
}

func (f *fakeBus) GetTask(taskID contracts.ID) (*contracts.TaskDTO, bool) {
	f.mu.RLock()
	defer f.mu.RUnlock()
	task, ok := f.tasks[taskID]
	if !ok {
		return nil, false
	}
	cp := *task
	return &cp, true
}

func (f *fakeBus) ListTasks() []contracts.TaskDTO {
	f.mu.RLock()
	defer f.mu.RUnlock()
	var out []contracts.TaskDTO
	for _, t := range f.tasks {
		out = append(out, *t)
	}
	return out
}

// -- SSE handler 测试 --

func TestSubscribeEventsInitialSnapshot(t *testing.T) {
	fb := newFakeBus()
	initialEvents := []contracts.RuntimeEvent{
		{ID: "evt-1", Type: "task.created", RunID: "run-001", Timestamp: "2026-07-06T10:00:00Z"},
		{ID: "evt-2", Type: "agent.run.started", RunID: "run-001", Timestamp: "2026-07-06T10:00:01Z"},
	}
	fb.addRun("run-001", "task-001", initialEvents...)

	handler := NewRunHandler(fb, fb, nil)

	ctx, cancel := context.WithTimeout(context.Background(), 500*time.Millisecond)
	defer cancel()

	req := httptest.NewRequest(http.MethodGet, "/api/runs/run-001/events", nil)
	req = req.WithContext(ctx)
	rec := httptest.NewRecorder()

	done := make(chan struct{})
	go func() {
		handler.SubscribeEvents(rec, req, "run-001")
		close(done)
	}()

	time.Sleep(400 * time.Millisecond)
	cancel()
	<-done

	body := rec.Body.String()
	if !strings.Contains(body, "evt-1") {
		t.Error("SSE 输出应包含 evt-1")
	}
	if !strings.Contains(body, "evt-2") {
		t.Error("SSE 输出应包含 evt-2")
	}
	if !strings.Contains(body, "data: ") {
		t.Error("SSE 输出应包含 data: 前缀")
	}
}

func TestSubscribeEventsReturnsAfterTerminalSnapshot(t *testing.T) {
	fb := newFakeBus()
	fb.addRun("run-001", "task-001", contracts.RuntimeEvent{
		ID: "evt-terminal", Type: "agent.run.completed", RunID: "run-001",
		Timestamp: "2026-07-06T10:00:00Z",
	})
	handler := NewRunHandler(fb, fb, nil)
	req := httptest.NewRequest(http.MethodGet, "/api/runs/run-001/events", nil)
	rec := httptest.NewRecorder()

	done := make(chan struct{})
	go func() {
		handler.SubscribeEvents(rec, req, "run-001")
		close(done)
	}()

	select {
	case <-done:
	case <-time.After(100 * time.Millisecond):
		t.Fatal("SSE 在发送 terminal event 后未退出")
	}
	if body := rec.Body.String(); !strings.Contains(body, "evt-terminal") {
		t.Fatalf("SSE 输出应包含 terminal event: %s", body)
	}
}

func TestSubscribeEventsSendsNewEvents(t *testing.T) {
	fb := newFakeBus()
	initialEvents := []contracts.RuntimeEvent{
		{ID: "evt-1", Type: "task.created", RunID: "run-001", Timestamp: "2026-07-06T10:00:00Z"},
	}
	fb.addRun("run-001", "task-001", initialEvents...)

	handler := NewRunHandler(fb, fb, nil)

	ctx, cancel := context.WithTimeout(context.Background(), 800*time.Millisecond)
	defer cancel()

	req := httptest.NewRequest(http.MethodGet, "/api/runs/run-001/events", nil)
	req = req.WithContext(ctx)
	rec := httptest.NewRecorder()

	done := make(chan struct{})
	go func() {
		handler.SubscribeEvents(rec, req, "run-001")
		close(done)
	}()

	// 等初始快照发送后，动态追加新事件（模拟 EventPump 行为）
	time.Sleep(200 * time.Millisecond)
	fb.appendEvent("run-001", contracts.RuntimeEvent{
		ID: "evt-new", Type: "tool.call.finished", RunID: "run-001", Timestamp: "2026-07-06T10:00:02Z",
	})

	time.Sleep(500 * time.Millisecond)
	cancel()
	<-done

	body := rec.Body.String()
	if !strings.Contains(body, "evt-1") {
		t.Error("SSE 输出应包含初始事件 evt-1")
	}
	if !strings.Contains(body, "evt-new") {
		t.Error("SSE 输出应包含后续追加的事件 evt-new")
	}
}

func TestSubscribeEventsNoDuplicates(t *testing.T) {
	fb := newFakeBus()
	initialEvents := []contracts.RuntimeEvent{
		{ID: "evt-1", Type: "task.created", RunID: "run-001", Timestamp: "2026-07-06T10:00:00Z"},
	}
	fb.addRun("run-001", "task-001", initialEvents...)

	handler := NewRunHandler(fb, fb, nil)

	ctx, cancel := context.WithTimeout(context.Background(), 1200*time.Millisecond)
	defer cancel()

	req := httptest.NewRequest(http.MethodGet, "/api/runs/run-001/events", nil)
	req = req.WithContext(ctx)
	rec := httptest.NewRecorder()

	done := make(chan struct{})
	go func() {
		handler.SubscribeEvents(rec, req, "run-001")
		close(done)
	}()

	// 多次轮询后取消，确保没有重复事件
	time.Sleep(1000 * time.Millisecond)
	cancel()
	<-done

	body := rec.Body.String()
	count := strings.Count(body, `"evt-1"`)
	if count != 1 {
		t.Errorf("evt-1 应只出现 1 次，实际 %d 次", count)
	}
}

func TestSubscribeEventsRunNotFound(t *testing.T) {
	fb := newFakeBus()
	handler := NewRunHandler(fb, fb, nil)

	req := httptest.NewRequest(http.MethodGet, "/api/runs/nonexistent/events", nil)
	rec := httptest.NewRecorder()

	handler.SubscribeEvents(rec, req, "nonexistent")

	if rec.Code != http.StatusNotFound {
		t.Errorf("不存在的 run 应返回 404，got %d", rec.Code)
	}
}

func TestSubscribeEventsClientDisconnect(t *testing.T) {
	fb := newFakeBus()
	initialEvents := []contracts.RuntimeEvent{
		{ID: "evt-1", Type: "task.created", RunID: "run-001", Timestamp: "2026-07-06T10:00:00Z"},
	}
	fb.addRun("run-001", "task-001", initialEvents...)

	handler := NewRunHandler(fb, fb, nil)

	ctx, cancel := context.WithTimeout(context.Background(), 50*time.Millisecond)
	defer cancel()

	req := httptest.NewRequest(http.MethodGet, "/api/runs/run-001/events", nil)
	req = req.WithContext(ctx)
	rec := httptest.NewRecorder()

	done := make(chan struct{})
	go func() {
		handler.SubscribeEvents(rec, req, "run-001")
		close(done)
	}()

	select {
	case <-done:
		// handler 正常退出
	case <-time.After(2 * time.Second):
		t.Fatal("handler 在 ctx 取消后未在 2s 内退出（可能 goroutine 泄漏）")
	}
}

func TestSubscribeEventsDoesNotMarkCompleted(t *testing.T) {
	fb := newFakeBus()
	initialEvents := []contracts.RuntimeEvent{
		{ID: "evt-1", Type: "task.created", RunID: "run-001", Timestamp: "2026-07-06T10:00:00Z"},
	}
	fb.addRun("run-001", "task-001", initialEvents...)

	// 设置 run 初始状态为 "queued"（模拟 redis 模式）
	fb.mu.Lock()
	fb.runs["run-001"].Status = "queued"
	fb.mu.Unlock()

	handler := NewRunHandler(fb, fb, nil)

	ctx, cancel := context.WithTimeout(context.Background(), 500*time.Millisecond)
	defer cancel()

	req := httptest.NewRequest(http.MethodGet, "/api/runs/run-001/events", nil)
	req = req.WithContext(ctx)
	rec := httptest.NewRecorder()

	done := make(chan struct{})
	go func() {
		handler.SubscribeEvents(rec, req, "run-001")
		close(done)
	}()

	time.Sleep(400 * time.Millisecond)
	cancel()
	<-done

	// 验证 run 状态没有被改为 completed
	run, ok := fb.GetRun("run-001")
	if !ok {
		t.Fatal("run 应该仍然存在")
	}
	if run.Status == "completed" {
		t.Error("SubscribeEvents 不应把 run 标为 completed，实际 status=completed")
	}
}

func TestSubscribeEventsMultipleNewEvents(t *testing.T) {
	fb := newFakeBus()
	initialEvents := []contracts.RuntimeEvent{
		{ID: "evt-1", Type: "task.created", RunID: "run-001", Timestamp: "2026-07-06T10:00:00Z"},
	}
	fb.addRun("run-001", "task-001", initialEvents...)

	handler := NewRunHandler(fb, fb, nil)

	ctx, cancel := context.WithTimeout(context.Background(), 1200*time.Millisecond)
	defer cancel()

	req := httptest.NewRequest(http.MethodGet, "/api/runs/run-001/events", nil)
	req = req.WithContext(ctx)
	rec := httptest.NewRecorder()

	done := make(chan struct{})
	go func() {
		handler.SubscribeEvents(rec, req, "run-001")
		close(done)
	}()

	// 分两批追加新事件
	time.Sleep(200 * time.Millisecond)
	fb.appendEvent("run-001", contracts.RuntimeEvent{
		ID: "evt-batch1-a", Type: "tool.call.started", RunID: "run-001", Timestamp: "2026-07-06T10:00:01Z",
	})
	fb.appendEvent("run-001", contracts.RuntimeEvent{
		ID: "evt-batch1-b", Type: "tool.call.finished", RunID: "run-001", Timestamp: "2026-07-06T10:00:02Z",
	})

	time.Sleep(500 * time.Millisecond)
	fb.appendEvent("run-001", contracts.RuntimeEvent{
		ID: "evt-batch2-a", Type: "agent.run.completed", RunID: "run-001", Timestamp: "2026-07-06T10:00:03Z",
	})

	time.Sleep(400 * time.Millisecond)
	cancel()
	<-done

	body := rec.Body.String()
	for _, expectedID := range []string{"evt-1", "evt-batch1-a", "evt-batch1-b", "evt-batch2-a"} {
		if !strings.Contains(body, expectedID) {
			t.Errorf("SSE 输出应包含 %s", expectedID)
		}
	}
}

func TestMergeRuntimeEventsOrdersEphemeralDeltasBeforeDurableTerminal(t *testing.T) {
	history := []contracts.RuntimeEvent{
		{ID: "resumed", Type: "agent.run.resumed", Sequence: 7, Timestamp: "2026-08-03T02:52:43.113579+00:00"},
		{ID: "model-end", Type: "model.call.completed", Sequence: 8, Timestamp: "2026-08-03T02:52:43.143480+00:00"},
		{ID: "terminal", Type: "agent.run.completed", Sequence: 10, Timestamp: "2026-08-03T02:52:43.143519+00:00"},
	}
	memory := []contracts.RuntimeEvent{
		{ID: "delta-2", Type: "model.delta", Timestamp: "2026-08-03T02:52:03.331149+00:00"},
		{ID: "delta-1", Type: "model.delta", Timestamp: "2026-08-03T02:52:02.154822+00:00"},
		{ID: "terminal", Type: "agent.run.completed", Timestamp: "2026-08-03T02:52:43.143519+00:00"},
	}

	merged := mergeRuntimeEvents(history, memory)
	ids := make([]contracts.ID, 0, len(merged))
	for _, event := range merged {
		ids = append(ids, event.ID)
	}
	want := []contracts.ID{"delta-1", "delta-2", "resumed", "model-end", "terminal"}
	if fmt.Sprint(ids) != fmt.Sprint(want) {
		t.Fatalf("恢复快照顺序错误: got=%v want=%v", ids, want)
	}
	if merged[len(merged)-1].Sequence != 10 {
		t.Fatal("去重后应保留 PostgreSQL durable 事件及其 sequence")
	}
}

func TestCollectUnseenRuntimeEventsStopsAtTerminal(t *testing.T) {
	sent := map[string]bool{"already-sent": true}
	events := []contracts.RuntimeEvent{
		{ID: "terminal", Type: "agent.run.completed", Timestamp: "2026-08-03T02:52:43Z"},
		{ID: "late-delta", Type: "model.delta", Timestamp: "2026-08-03T02:52:44Z"},
	}

	unseen, terminalSent := collectUnseenRuntimeEvents(events, sent, false)
	if !terminalSent || len(unseen) != 1 || unseen[0].ID != "terminal" {
		t.Fatalf("终态边界未收口: events=%v terminal=%v", unseen, terminalSent)
	}
	if !sent["late-delta"] {
		t.Fatal("迟到事件应被消费标记，避免每轮重复处理")
	}

	more, stillTerminal := collectUnseenRuntimeEvents([]contracts.RuntimeEvent{
		{ID: "late-log", Type: "log.appended", Timestamp: "2026-08-03T02:52:45Z"},
	}, sent, terminalSent)
	if len(more) != 0 || !stillTerminal {
		t.Fatalf("终态后不得重新打开 SSE 业务流: events=%v terminal=%v", more, stillTerminal)
	}
}

// -- CreateTask persistence test --

func TestCreateTaskDelegatesToPrepareRun(t *testing.T) {
	// InMemory runtime: handler persists if stores are set.
	fb := newFakeBus()
	taskHandler := NewTaskHandler(fb, fb, nil)

	body := `{"user_goal":"delegation test"}`
	req := httptest.NewRequest("POST", "/api/tasks", strings.NewReader(body))
	req.Header.Set("Content-Type", "application/json")
	rec := httptest.NewRecorder()
	taskHandler.CreateTask(rec, req)

	if rec.Code != http.StatusOK {
		t.Fatalf("CreateTask 应返回 200，实际 %d: %s", rec.Code, rec.Body.String())
	}
}

func TestCreateTaskEmptyGoalReturnsError(t *testing.T) {
	fb := newFakeBus()
	taskHandler := NewTaskHandler(fb, fb, nil)

	body := `{"user_goal":""}`
	req := httptest.NewRequest("POST", "/api/tasks", strings.NewReader(body))
	req.Header.Set("Content-Type", "application/json")
	rec := httptest.NewRecorder()
	taskHandler.CreateTask(rec, req)

	if rec.Code != http.StatusBadRequest {
		t.Errorf("空 user_goal 应返回 400，实际 %d", rec.Code)
	}
}

// -- StateStoreWithError fake for storage error tests --

type fakeStateStoreErr struct {
	listTasksErr error
	getTaskErr   error
	getTaskFound bool
	getRunErr    error // 非 nil → storage error
	getRunFound  bool  // true → run 存在（无 error 时使用）
}

func (f *fakeStateStoreErr) GetTask(id contracts.ID) (*contracts.TaskDTO, bool) { return nil, false }
func (f *fakeStateStoreErr) ListTasks() []contracts.TaskDTO                     { return nil }
func (f *fakeStateStoreErr) GetRun(runID contracts.ID) (*contracts.AgentRunDTO, bool) {
	return nil, false
}
func (f *fakeStateStoreErr) UpdateRunStatus(runID contracts.ID, status string) {}

func (f *fakeStateStoreErr) GetTaskWithError(id contracts.ID) (*contracts.TaskDTO, bool, error) {
	if f.getTaskErr != nil {
		return nil, false, f.getTaskErr
	}
	if f.getTaskFound {
		return &contracts.TaskDTO{ID: id, Title: "test", ActiveRunID: "run-1"}, true, nil
	}
	return nil, false, nil
}
func (f *fakeStateStoreErr) ListTasksWithError() ([]contracts.TaskDTO, error) {
	if f.listTasksErr != nil {
		return nil, f.listTasksErr
	}
	return []contracts.TaskDTO{}, nil
}
func (f *fakeStateStoreErr) GetRunWithError(runID contracts.ID) (*contracts.AgentRunDTO, bool, error) {
	if f.getRunErr != nil {
		return nil, false, f.getRunErr
	}
	if f.getRunFound {
		return &contracts.AgentRunDTO{ID: runID}, true, nil
	}
	return nil, false, nil
}
func (f *fakeStateStoreErr) UpdateRunStatusWithError(runID contracts.ID, status string) error {
	return f.getRunErr
}

// -- Storage read error 测试 --

func TestGetTaskNotFoundStill404(t *testing.T) {
	// 真实 not found（无 error）仍然是 404
	fb := newFakeBus()
	state := &fakeStateStoreErr{} // 无 error，但 task 不存在
	taskHandler := NewTaskHandler(fb, state, nil)

	req := httptest.NewRequest("GET", "/api/tasks/task-001", nil)
	rec := httptest.NewRecorder()
	taskHandler.GetTask(rec, req, "task-001")

	if rec.Code != http.StatusNotFound {
		t.Errorf("真实 not found 应返回 404，实际 %d", rec.Code)
	}
	if !strings.Contains(rec.Body.String(), "NOT_FOUND") {
		t.Error("真实 not found 应包含 NOT_FOUND")
	}
}

func TestSubscribeEventsRunNotFoundStill404(t *testing.T) {
	// StateStoreWithError.GetRunWithError 返回 not found（无 error）→ 404
	fb := newFakeBus()
	state := &fakeStateStoreErr{} // getRunFound=false, getRunErr=nil

	runHandler := NewRunHandler(fb, state, nil)

	req := httptest.NewRequest("GET", "/api/runs/run-404/events", nil)
	rec := httptest.NewRecorder()
	ctx, cancel := context.WithCancel(req.Context())
	defer cancel()
	req = req.WithContext(ctx)

	done := make(chan struct{})
	go func() {
		runHandler.SubscribeEvents(rec, req, "run-404")
		close(done)
	}()

	time.Sleep(200 * time.Millisecond)
	cancel()
	<-done

	if rec.Code != http.StatusNotFound {
		t.Errorf("not found 应返回 404，实际 %d: %s", rec.Code, rec.Body.String())
	}
	body := rec.Body.String()
	if !strings.Contains(body, "NOT_FOUND") {
		t.Errorf("真实 not found 应返回 NOT_FOUND，实际: %s", body)
	}
}

// -- GetTask activeRun storage error 测试 --
