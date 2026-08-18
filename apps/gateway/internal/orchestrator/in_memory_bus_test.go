package orchestrator

import (
	"testing"
	"unicode/utf8"

	"github.com/jarvis-assistant/gateway/internal/contracts"
)

// -- 编译期接口合规断言 --

func TestInMemoryRuntimeBusImplementsInterfaces(t *testing.T) {
	// 编译期断言：InMemoryRuntimeBus 必须同时实现两个接口
	var _ RuntimeBus = (*InMemoryRuntimeBus)(nil)
	var _ RuntimeStateStore = (*InMemoryRuntimeBus)(nil)
	// 若能编译到这里，断言通过
	t.Log("InMemoryRuntimeBus 同时实现 RuntimeBus 和 RuntimeStateStore")
}

// -- 辅助函数 --

// findEvent 在事件列表中查找第一个匹配 type 的事件，未找到返回 nil
func findEvent(events []contracts.RuntimeEvent, eventType string) *contracts.RuntimeEvent {
	for i := range events {
		if events[i].Type == eventType {
			return &events[i]
		}
	}
	return nil
}

// requireEvent 查找事件，不存在则 Fatal
func requireEvent(t *testing.T, events []contracts.RuntimeEvent, eventType string) *contracts.RuntimeEvent {
	t.Helper()
	e := findEvent(events, eventType)
	if e == nil {
		t.Fatalf("期望事件 %q 存在于 events 中，但未找到", eventType)
	}
	return e
}

// getMap 从 map[string]interface{} 中取嵌套 map
func getMap(m map[string]interface{}, key string) map[string]interface{} {
	if v, ok := m[key].(map[string]interface{}); ok {
		return v
	}
	return nil
}

// getString 从 map[string]interface{} 中取 string
func getString(m map[string]interface{}, key string) string {
	if v, ok := m[key].(string); ok {
		return v
	}
	return ""
}

// -- PrepareRun 测试 --

func TestPrepareRunCreatesTaskRunAndEvents(t *testing.T) {
	bus := NewInMemoryRuntimeBus()

	task, run, events, err := bus.PrepareRun(contracts.CreateTaskInput{
		UserGoal:      "测试任务",
		WorkspacePath: "/tmp/test",
	})

	if err != nil {
		t.Fatalf("PrepareRun 失败: %v", err)
	}
	if task == nil {
		t.Fatal("task 不应为 nil")
	}
	if run == nil {
		t.Fatal("run 不应为 nil")
	}
	if len(events) == 0 {
		t.Fatal("events 不应为空")
	}

	// task 字段完整性
	if task.ID == "" {
		t.Error("task.ID 不应为空")
	}
	if task.Title == "" {
		t.Error("task.Title 不应为空")
	}
	if task.UserGoal != "测试任务" {
		t.Errorf("task.UserGoal = %q，期望 %q", task.UserGoal, "测试任务")
	}
	if task.WorkspacePath != "/tmp/test" {
		t.Errorf("task.WorkspacePath = %q，期望 %q", task.WorkspacePath, "/tmp/test")
	}
	if task.Status != "running" {
		t.Errorf("task.Status = %q，期望 running", task.Status)
	}
	if task.ActiveRunID != run.ID {
		t.Errorf("task.ActiveRunID = %q，应与 run.ID = %q 一致", task.ActiveRunID, run.ID)
	}

	// run 字段完整性
	if run.ID == "" {
		t.Error("run.ID 不应为空")
	}
	if run.TaskID != task.ID {
		t.Errorf("run.TaskID = %q，应与 task.ID = %q 一致", run.TaskID, task.ID)
	}
	if run.Status != "created" {
		t.Errorf("run.Status = %q，期望 created", run.Status)
	}

	// events 包含关键类型
	requireEvent(t, events, "task.created")
	requireEvent(t, events, "agent.run.started")

	// StateStore 能查到
	gotTask, ok := bus.GetTask(task.ID)
	if !ok {
		t.Fatal("GetTask 未找到刚创建的 task")
	}
	if gotTask.ID != task.ID {
		t.Errorf("GetTask.ID = %q，期望 %q", gotTask.ID, task.ID)
	}

	gotRun, ok := bus.GetRun(run.ID)
	if !ok {
		t.Fatal("GetRun 未找到刚创建的 run")
	}
	if gotRun.ID != run.ID {
		t.Errorf("GetRun.ID = %q，期望 %q", gotRun.ID, run.ID)
	}

	tasks := bus.ListTasks()
	if len(tasks) != 1 {
		t.Errorf("ListTasks count = %d，期望 1", len(tasks))
	}
}

// -- PrepareDevMock + ResolvePermission 测试 --

func TestPrepareDevMockRegistersPermissionRequest(t *testing.T) {
	bus := NewInMemoryRuntimeBus()

	task, run, events, err := bus.PrepareDevMock("permission_required")
	if err != nil {
		t.Fatalf("PrepareDevMock 失败: %v", err)
	}

	// 验证 task / run 创建
	if task.Status != "running" {
		t.Errorf("task.Status = %q，期望 running", task.Status)
	}
	if run.TaskID != task.ID {
		t.Errorf("run.TaskID = %q，应与 task.ID = %q 一致", run.TaskID, task.ID)
	}

	// 找到 permission.required 事件
	permEvent := requireEvent(t, events, "permission.required")
	reqMap := getMap(permEvent.Payload, "request")
	if reqMap == nil {
		t.Fatal("permission.required 事件的 payload.request 不应为 nil")
	}
	permReqID := getString(reqMap, "id")
	if permReqID == "" {
		t.Fatal("无法从 permission.required 事件中提取 request.id")
	}
	t.Logf("permission request id: %s", permReqID)

	// 用 request_id 调用 ResolvePermission
	permReq, postEvents, err := bus.ResolvePermission(contracts.PermissionDecisionDTO{
		RequestID: permReqID,
		Decision:  "allow_once",
	})
	if err != nil {
		t.Fatalf("ResolvePermission 失败: %v", err)
	}
	if permReq == nil {
		t.Fatal("permReq 不应为 nil")
	}
	if len(postEvents) == 0 {
		t.Fatal("postEvents 不应为空")
	}

	// 验证后续事件包含预期类型
	requireEvent(t, postEvents, "permission.resolved")
	requireEvent(t, postEvents, "tool.call.finished")
	requireEvent(t, postEvents, "agent.step.completed")
	requireEvent(t, postEvents, "agent.run.completed")
}

// TestResolvePermissionAllowAndDeny 分别验证 allow 和 deny 两种决策
func TestResolvePermissionAllowAndDeny(t *testing.T) {
	t.Run("allow_once", func(t *testing.T) {
		bus := NewInMemoryRuntimeBus()
		_, _, events, _ := bus.PrepareDevMock("permission_required")

		permEvent := requireEvent(t, events, "permission.required")
		reqMap := getMap(permEvent.Payload, "request")
		permReqID := getString(reqMap, "id")

		permReq, postEvents, err := bus.ResolvePermission(contracts.PermissionDecisionDTO{
			RequestID: permReqID,
			Decision:  "allow_once",
		})
		if err != nil {
			t.Fatalf("allow_once ResolvePermission 失败: %v", err)
		}
		if permReq == nil {
			t.Fatal("permReq 不应为 nil")
		}

		// 验证事件序列
		if e := findEvent(postEvents, "permission.resolved"); e == nil {
			t.Fatal("缺少 permission.resolved")
		}
		if e := findEvent(postEvents, "tool.call.finished"); e == nil {
			t.Fatal("缺少 tool.call.finished")
		} else {
			tc := getMap(e.Payload, "tool_call")
			if getString(tc, "status") != "completed" {
				t.Errorf("tool_call status = %q，期望 completed", getString(tc, "status"))
			}
			if getString(tc, "tool_name") != "shell" {
				t.Errorf("tool_name = %q，期望 shell", getString(tc, "tool_name"))
			}
		}
		if e := findEvent(postEvents, "agent.step.completed"); e == nil {
			t.Fatal("缺少 agent.step.completed")
		} else {
			step := getMap(e.Payload, "step")
			if getString(step, "status") != "completed" {
				t.Errorf("step status = %q，期望 completed", getString(step, "status"))
			}
		}
		if e := findEvent(postEvents, "agent.run.completed"); e == nil {
			t.Fatal("缺少 agent.run.completed")
		}
	})

	t.Run("deny", func(t *testing.T) {
		bus := NewInMemoryRuntimeBus()
		_, _, events, _ := bus.PrepareDevMock("permission_required")

		permEvent := requireEvent(t, events, "permission.required")
		reqMap := getMap(permEvent.Payload, "request")
		permReqID := getString(reqMap, "id")

		permReq, postEvents, err := bus.ResolvePermission(contracts.PermissionDecisionDTO{
			RequestID: permReqID,
			Decision:  "deny",
		})
		if err != nil {
			t.Fatalf("deny ResolvePermission 失败: %v", err)
		}
		if permReq == nil {
			t.Fatal("permReq 不应为 nil")
		}

		// 验证 permission.resolved
		requireEvent(t, postEvents, "permission.resolved")

		// 验证 tool.call.failed
		if e := findEvent(postEvents, "tool.call.failed"); e == nil {
			t.Fatal("缺少 tool.call.failed")
		} else {
			tc := getMap(e.Payload, "tool_call")
			if getString(tc, "status") != "failed" {
				t.Errorf("tool_call status = %q，期望 failed", getString(tc, "status"))
			}
			errMap := getMap(tc, "error")
			if getString(errMap, "code") != "PERMISSION_DENIED" {
				t.Errorf("error code = %q，期望 PERMISSION_DENIED", getString(errMap, "code"))
			}
		}

		// 验证 agent.step.completed 状态为 failed
		if e := findEvent(postEvents, "agent.step.completed"); e == nil {
			t.Fatal("缺少 agent.step.completed")
		} else {
			step := getMap(e.Payload, "step")
			if getString(step, "status") != "failed" {
				t.Errorf("step status = %q，期望 failed（deny 时 step 应为 failed）", getString(step, "status"))
			}
		}

		// 验证 agent.run.completed
		requireEvent(t, postEvents, "agent.run.completed")
	})
}

// TestResolvePermissionOneTimeConsumption 验证权限请求一次性消费语义：
// 同一个 RequestID 第二次 ResolvePermission 必须返回 error，且不会追加重复事件。
func TestResolvePermissionOneTimeConsumption(t *testing.T) {
	bus := NewInMemoryRuntimeBus()

	_, run, events, _ := bus.PrepareDevMock("permission_required")
	permEvent := requireEvent(t, events, "permission.required")
	reqMap := getMap(permEvent.Payload, "request")
	permReqID := getString(reqMap, "id")

	// 记录第一次 resolve 前的事件数量
	eventsBefore, err := bus.GetEvents(run.ID)
	if err != nil {
		t.Fatalf("GetEvents 失败: %v", err)
	}
	countBefore := len(eventsBefore)

	// 第一次 resolve：成功
	_, postEvents1, err := bus.ResolvePermission(contracts.PermissionDecisionDTO{
		RequestID: permReqID,
		Decision:  "allow_once",
	})
	if err != nil {
		t.Fatalf("第一次 ResolvePermission 失败: %v", err)
	}
	if len(postEvents1) == 0 {
		t.Fatal("第一次 resolve 应返回非空 postEvents")
	}

	// 验证第一次 resolve 后 events 增加了
	eventsAfterFirst, err := bus.GetEvents(run.ID)
	if err != nil {
		t.Fatalf("第一次 resolve 后 GetEvents 失败: %v", err)
	}
	expectedAfterFirst := countBefore + len(postEvents1)
	if len(eventsAfterFirst) != expectedAfterFirst {
		t.Errorf("第一次 resolve 后事件数 = %d，期望 %d (before=%d + post=%d)",
			len(eventsAfterFirst), expectedAfterFirst, countBefore, len(postEvents1))
	}

	// 第二次 resolve：必须返回 error
	_, _, err = bus.ResolvePermission(contracts.PermissionDecisionDTO{
		RequestID: permReqID,
		Decision:  "allow_once",
	})
	if err == nil {
		t.Fatal("同一 RequestID 第二次 ResolvePermission 应返回 error（一次性消费），但 err 为 nil")
	}
	t.Logf("第二次 resolve 正确返回 error: %v", err)

	// 断言第二次失败后事件数量未增加（无重复事件）
	eventsAfterSecond, err := bus.GetEvents(run.ID)
	if err != nil {
		t.Fatalf("第二次 resolve 后 GetEvents 失败: %v", err)
	}
	if len(eventsAfterSecond) != expectedAfterFirst {
		t.Errorf("第二次（失败）resolve 后事件数 = %d，期望仍为 %d（不应追加重复事件）",
			len(eventsAfterSecond), expectedAfterFirst)
	}
}

// -- 深拷贝测试 --

// TestGetEventsReturnsDeepCopy 验证 GetEvents 返回的是深拷贝，修改返回值不影响内部 state
func TestGetEventsReturnsDeepCopy(t *testing.T) {
	bus := NewInMemoryRuntimeBus()

	// 准备一个 permission_required 场景（有丰富的嵌套 payload）
	_, run, _, err := bus.PrepareDevMock("permission_required")
	if err != nil {
		t.Fatalf("PrepareDevMock 失败: %v", err)
	}

	// 第一次获取事件
	events1, err := bus.GetEvents(run.ID)
	if err != nil {
		t.Fatalf("GetEvents 失败: %v", err)
	}

	// 找到 permission.required 事件，验证原始值
	permEvent1 := requireEvent(t, events1, "permission.required")
	reqMap1 := getMap(permEvent1.Payload, "request")
	origToolName := getString(reqMap1, "tool_name")
	if origToolName == "" {
		t.Fatal("原始 tool_name 不应为空")
	}
	origArgs := getMap(reqMap1, "arguments_summary")
	origCommand := getString(origArgs, "command")
	if origCommand == "" {
		t.Fatal("原始 command 不应为空")
	}

	// 修改返回事件中的嵌套 Payload 字段
	reqMap1["tool_name"] = "INJECTED_VALUE"
	if origArgs != nil {
		origArgs["command"] = "INJECTED_COMMAND"
	}

	// 第二次获取事件，断言原始值未被污染
	events2, err := bus.GetEvents(run.ID)
	if err != nil {
		t.Fatalf("第二次 GetEvents 失败: %v", err)
	}
	permEvent2 := requireEvent(t, events2, "permission.required")
	reqMap2 := getMap(permEvent2.Payload, "request")

	if getString(reqMap2, "tool_name") != origToolName {
		t.Errorf("深拷贝被破坏：tool_name = %q，期望保持 %q", getString(reqMap2, "tool_name"), origToolName)
	}
	args2 := getMap(reqMap2, "arguments_summary")
	if getString(args2, "command") != origCommand {
		t.Errorf("深拷贝被破坏：command = %q，期望保持 %q", getString(args2, "command"), origCommand)
	}
}

// TestStateStoreReturnsCopies 验证 GetRun / GetTask 返回值修改不污染内部 state
func TestStateStoreReturnsCopies(t *testing.T) {
	bus := NewInMemoryRuntimeBus()

	task, run, _, err := bus.PrepareRun(contracts.CreateTaskInput{
		UserGoal: "拷贝测试",
	})
	if err != nil {
		t.Fatalf("PrepareRun 失败: %v", err)
	}

	// GetRun 返回拷贝 —— 修改不影响内部
	runCopy, ok := bus.GetRun(run.ID)
	if !ok {
		t.Fatal("GetRun 未找到")
	}
	originalRunStatus := runCopy.Status
	runCopy.Status = "INJECTED_STATUS"
	runCopy.TaskID = "INJECTED_TASK_ID"

	runCopy2, ok := bus.GetRun(run.ID)
	if !ok {
		t.Fatal("第二次 GetRun 未找到")
	}
	if runCopy2.Status != originalRunStatus {
		t.Errorf("GetRun 拷贝被破坏：status = %q，期望 %q", runCopy2.Status, originalRunStatus)
	}
	if runCopy2.TaskID != task.ID {
		t.Errorf("GetRun 拷贝被破坏：TaskID = %q，期望 %q", runCopy2.TaskID, task.ID)
	}

	// GetTask 返回拷贝 —— 修改不影响内部
	taskCopy, ok := bus.GetTask(task.ID)
	if !ok {
		t.Fatal("GetTask 未找到")
	}
	taskCopy.Status = "INJECTED_STATUS"
	taskCopy.Title = "INJECTED_TITLE"

	taskCopy2, ok := bus.GetTask(task.ID)
	if !ok {
		t.Fatal("第二次 GetTask 未找到")
	}
	if taskCopy2.Status != "running" {
		t.Errorf("GetTask 拷贝被破坏：status = %q，期望 running", taskCopy2.Status)
	}
	if taskCopy2.Title == "INJECTED_TITLE" {
		t.Errorf("GetTask 拷贝被破坏：title 被注入值污染")
	}

	// UpdateRunStatus 后 GetRun 应反映最新状态
	bus.UpdateRunStatus(run.ID, "paused")
	runAfterPause, ok := bus.GetRun(run.ID)
	if !ok {
		t.Fatal("pause 后 GetRun 未找到")
	}
	if runAfterPause.Status != "paused" {
		t.Errorf("UpdateRunStatus 后 status = %q，期望 paused", runAfterPause.Status)
	}
}

// TestResolvePermissionReturnsDeepCopy 验证 ResolvePermission 返回值是深拷贝，
// 且权限请求具有一次性消费语义（第二次 resolve 返回 error）。
func TestResolvePermissionReturnsDeepCopy(t *testing.T) {
	bus := NewInMemoryRuntimeBus()

	_, _, events, _ := bus.PrepareDevMock("permission_required")
	permEvent := requireEvent(t, events, "permission.required")
	reqMap := getMap(permEvent.Payload, "request")
	permReqID := getString(reqMap, "id")

	// 第一次 resolve
	permReq1, events1, err := bus.ResolvePermission(contracts.PermissionDecisionDTO{
		RequestID: permReqID,
		Decision:  "allow_once",
	})
	if err != nil {
		t.Fatalf("ResolvePermission 失败: %v", err)
	}

	// 修改返回值
	permReq1.ActionSummary = "INJECTED_ACTION"
	if permReq1.ArgumentsSummary != nil {
		permReq1.ArgumentsSummary["command"] = "INJECTED_CMD"
	}
	// 修改返回事件
	if len(events1) > 1 {
		tc := getMap(events1[1].Payload, "tool_call")
		if tc != nil {
			tc["tool_name"] = "INJECTED_TOOL"
		}
	}

	// GetEvents 验证内部事件未被污染
	events2, err := bus.GetEvents(permReq1.RunID)
	if err != nil {
		t.Fatalf("GetEvents 失败: %v", err)
	}
	// permission.resolved 应出现在追加后的事件中
	if findEvent(events2, "permission.resolved") == nil {
		t.Fatal("缺少 permission.resolved 事件")
	}

	// tool.call.finished 不应有 INJECTED_TOOL
	finishedEvent := findEvent(events2, "tool.call.finished")
	if finishedEvent != nil {
		tc := getMap(finishedEvent.Payload, "tool_call")
		if getString(tc, "tool_name") == "INJECTED_TOOL" {
			t.Error("事件深拷贝被破坏：tool_name 被注入值污染")
		}
	}

	// 同一 RequestID 第二次 ResolvePermission 必须返回 error（一次性消费）
	_, _, err = bus.ResolvePermission(contracts.PermissionDecisionDTO{
		RequestID: permReqID,
		Decision:  "allow_once",
	})
	if err == nil {
		t.Error("同一 RequestID 第二次 ResolvePermission 应返回 error（一次性消费），但 err 为 nil")
	}
}

// TestResolvePermissionUnknownRequest 验证不存在的权限请求返回错误
func TestResolvePermissionUnknownRequest(t *testing.T) {
	bus := NewInMemoryRuntimeBus()

	_, _, err := bus.ResolvePermission(contracts.PermissionDecisionDTO{
		RequestID: "non-existent-request-id",
		Decision:  "allow_once",
	})
	if err == nil {
		t.Fatal("期望不存在的权限请求返回 error，但 err 为 nil")
	}
}

// -- Events 查询测试 --

// TestGetEventsNonexistentRun 验证不存在的 run 返回错误
func TestGetEventsNonexistentRun(t *testing.T) {
	bus := NewInMemoryRuntimeBus()

	_, err := bus.GetEvents("non-existent-run-id")
	if err == nil {
		t.Fatal("期望不存在的 run 返回 error，但 err 为 nil")
	}
}

// TestGetEventsAfterPrepareRun 验证 PrepareRun 后 GetEvents 可查到 events，
// 同时验证 ListTasks 包含新 task。
func TestGetEventsAfterPrepareRun(t *testing.T) {
	bus := NewInMemoryRuntimeBus()

	task, run, _, err := bus.PrepareRun(contracts.CreateTaskInput{
		UserGoal: "GetEvents 后查 events",
	})
	if err != nil {
		t.Fatalf("PrepareRun 失败: %v", err)
	}

	// events 已经生成，GetEvents 应直接返回
	events, err := bus.GetEvents(run.ID)
	if err != nil {
		t.Fatalf("GetEvents 失败: %v", err)
	}
	if len(events) == 0 {
		t.Fatal("events 不应为空")
	}

	// 验证 task 在列表中
	tasks := bus.ListTasks()
	found := false
	for _, ts := range tasks {
		if ts.ID == task.ID {
			found = true
			break
		}
	}
	if !found {
		t.Error("ListTasks 中未找到刚创建的 task")
	}
}

// -- UpdateRunStatus 测试 --

func TestUpdateRunStatus(t *testing.T) {
	bus := NewInMemoryRuntimeBus()

	_, run, _, err := bus.PrepareRun(contracts.CreateTaskInput{UserGoal: "状态更新测试"})
	if err != nil {
		t.Fatalf("PrepareRun 失败: %v", err)
	}

	// paused
	bus.UpdateRunStatus(run.ID, "paused")
	got, ok := bus.GetRun(run.ID)
	if !ok {
		t.Fatal("UpdateRunStatus 后 GetRun 未找到")
	}
	if got.Status != "paused" {
		t.Errorf("status = %q，期望 paused", got.Status)
	}

	// cancelled
	bus.UpdateRunStatus(run.ID, "cancelled")
	got, ok = bus.GetRun(run.ID)
	if !ok {
		t.Fatal("cancelled 后 GetRun 未找到")
	}
	if got.Status != "cancelled" {
		t.Errorf("status = %q，期望 cancelled", got.Status)
	}

	// UpdateRunStatus 对不存在的 run 不应 panic
	bus.UpdateRunStatus("non-existent-run", "paused")
	// 无 panic 即通过
}

// -- GetTask / GetRun 不存在测试 --

func TestGetTaskNotFound(t *testing.T) {
	bus := NewInMemoryRuntimeBus()

	_, ok := bus.GetTask("non-existent-task")
	if ok {
		t.Error("期望不存在的 task 返回 ok=false")
	}
}

func TestGetRunNotFound(t *testing.T) {
	bus := NewInMemoryRuntimeBus()

	_, ok := bus.GetRun("non-existent-run")
	if ok {
		t.Error("期望不存在的 run 返回 ok=false")
	}
}

// -- empty ListTasks --

func TestListTasksEmpty(t *testing.T) {
	bus := NewInMemoryRuntimeBus()

	tasks := bus.ListTasks()
	if len(tasks) != 0 {
		t.Errorf("空 state 时 ListTasks 应返回 0 条，实际 %d", len(tasks))
	}
}

// -- Title truncation (rune-safe) --

func TestPrepareRunTitleTruncation(t *testing.T) {
	bus := NewInMemoryRuntimeBus()

	longGoal := "这是一个超过四十个字符的用户目标用来测试标题截断功能是否正常工作"
	task, _, _, err := bus.PrepareRun(contracts.CreateTaskInput{
		UserGoal: longGoal,
	})
	if err != nil {
		t.Fatalf("PrepareRun 失败: %v", err)
	}

	// title 必须是合法 UTF-8
	if !utf8.ValidString(task.Title) {
		t.Fatalf("title 不是合法 UTF-8 字符串: %q", task.Title)
	}
	// UserGoal 原文不变
	if task.UserGoal != longGoal {
		t.Errorf("UserGoal 被修改: %q != %q", task.UserGoal, longGoal)
	}

	// 按 rune 断言：标题 rune 数 ≤ 40 + 3（中文场景下 "..." 也是 3 runes，但后缀固定为 "...")
	titleRunes := []rune(task.Title)
	const maxRunes = 40
	if len(titleRunes) > maxRunes+3 {
		t.Errorf("title rune 数 = %d，期望 <= %d (40 runes + '...')", len(titleRunes), maxRunes+3)
	}
	// title 应以 "..." 结尾
	if len(titleRunes) > maxRunes {
		suffix := string(titleRunes[len(titleRunes)-3:])
		if suffix != "..." {
			t.Errorf("超长 title 应以 ... 结尾，实际 suffix = %q", suffix)
		}
	}

	t.Logf("truncated title: %s (runes=%d)", task.Title, len(titleRunes))
}

// TestPrepareRunTitleShort 验证短标题不截断
func TestPrepareRunTitleShort(t *testing.T) {
	bus := NewInMemoryRuntimeBus()

	shortGoal := "短标题"
	task, _, _, err := bus.PrepareRun(contracts.CreateTaskInput{
		UserGoal: shortGoal,
	})
	if err != nil {
		t.Fatalf("PrepareRun 失败: %v", err)
	}

	if task.Title != shortGoal {
		t.Errorf("短标题不应被截断: %q != %q", task.Title, shortGoal)
	}
	if !utf8.ValidString(task.Title) {
		t.Fatalf("短标题不是合法 UTF-8: %q", task.Title)
	}
}

// -- AppendRuntimeEvents 测试 --

func TestAppendRuntimeEventsSuccess(t *testing.T) {
	b := NewInMemoryRuntimeBus()

	task, run, initialEvents, err := b.PrepareRun(contracts.CreateTaskInput{UserGoal: "测试"})
	if err != nil {
		t.Fatalf("PrepareRun 失败: %v", err)
	}
	_ = task

	initialCount := len(initialEvents)

	newEvents := []contracts.RuntimeEvent{
		{
			ID:        "evt-new-1",
			Type:      "tool.call.finished",
			TaskID:    run.TaskID,
			RunID:     run.ID,
			Timestamp: "2026-07-06T11:00:00Z",
			Payload:   map[string]interface{}{"tool": "read_file"},
		},
		{
			ID:        "evt-new-2",
			Type:      "agent.run.completed",
			TaskID:    run.TaskID,
			RunID:     run.ID,
			Timestamp: "2026-07-06T11:01:00Z",
			Payload:   map[string]interface{}{"output": "完成"},
		},
	}

	err = b.AppendRuntimeEvents(run.ID, newEvents)
	if err != nil {
		t.Fatalf("AppendRuntimeEvents 失败: %v", err)
	}

	allEvents, err := b.GetEvents(run.ID)
	if err != nil {
		t.Fatalf("GetEvents 失败: %v", err)
	}
	if len(allEvents) != initialCount+2 {
		t.Errorf("期望 %d 个事件，got %d", initialCount+2, len(allEvents))
	}

	lastEvent := allEvents[len(allEvents)-1]
	if lastEvent.ID != "evt-new-2" {
		t.Errorf("最后一个事件 ID: got %q, want %q", lastEvent.ID, "evt-new-2")
	}
}

func TestAppendRuntimeEventsRunNotFound(t *testing.T) {
	b := NewInMemoryRuntimeBus()

	err := b.AppendRuntimeEvents("nonexistent-run", []contracts.RuntimeEvent{
		{ID: "evt-1", Type: "task.created", Timestamp: "2026-07-06T10:00:00Z"},
	})
	if err == nil {
		t.Error("不存在的 run 应返回 error")
	}
}

func TestAppendRuntimeEventsDeepCopy(t *testing.T) {
	b := NewInMemoryRuntimeBus()

	_, run, _, err := b.PrepareRun(contracts.CreateTaskInput{UserGoal: "深拷贝测试"})
	if err != nil {
		t.Fatalf("PrepareRun 失败: %v", err)
	}

	originalPayload := map[string]interface{}{"key": "original"}
	events := []contracts.RuntimeEvent{
		{
			ID:        "evt-copy",
			Type:      "log.appended",
			TaskID:    run.TaskID,
			RunID:     run.ID,
			Timestamp: "2026-07-06T10:00:00Z",
			Payload:   originalPayload,
		},
	}

	err = b.AppendRuntimeEvents(run.ID, events)
	if err != nil {
		t.Fatalf("AppendRuntimeEvents 失败: %v", err)
	}

	// 修改原始事件
	originalPayload["key"] = "modified"
	events[0].ID = "modified-id"

	// 验证存储的事件未受污染
	stored, err := b.GetEvents(run.ID)
	if err != nil {
		t.Fatalf("GetEvents 失败: %v", err)
	}

	var appended *contracts.RuntimeEvent
	for i := range stored {
		if stored[i].ID == "evt-copy" {
			appended = &stored[i]
			break
		}
	}
	if appended == nil {
		t.Fatal("未找到追加的事件 evt-copy")
	}
	if v, ok := appended.Payload["key"]; !ok || v != "original" {
		t.Errorf("Payload 被浅拷贝污染: got %v, want %q", v, "original")
	}
}

func TestAppendRuntimeEventsEmptyEvents(t *testing.T) {
	b := NewInMemoryRuntimeBus()

	_, run, _, err := b.PrepareRun(contracts.CreateTaskInput{UserGoal: "空事件测试"})
	if err != nil {
		t.Fatalf("PrepareRun 失败: %v", err)
	}

	err = b.AppendRuntimeEvents(run.ID, nil)
	if err != nil {
		t.Errorf("nil events 应返回 nil，got: %v", err)
	}

	err = b.AppendRuntimeEvents(run.ID, []contracts.RuntimeEvent{})
	if err != nil {
		t.Errorf("空 slice 应返回 nil，got: %v", err)
	}
}

func TestAppendRuntimeEventsDeduplicatesByEventID(t *testing.T) {
	b, run, _ := newBusWithRun(t)
	event := contracts.RuntimeEvent{
		ID: "event-dedupe", Type: "model.call.completed",
		TaskID: run.TaskID, RunID: run.ID, Timestamp: contracts.NowISO(),
		Payload: map[string]interface{}{"ok": true},
	}
	if err := b.AppendRuntimeEvents(run.ID, []contracts.RuntimeEvent{event}); err != nil {
		t.Fatal(err)
	}
	if err := b.AppendRuntimeEvents(run.ID, []contracts.RuntimeEvent{event}); err != nil {
		t.Fatal(err)
	}
	events, err := b.GetEvents(run.ID)
	if err != nil {
		t.Fatal(err)
	}
	count := 0
	for _, candidate := range events {
		if candidate.ID == event.ID {
			count++
		}
	}
	if count != 1 {
		t.Fatalf("重复 RuntimeEvent 应只投影一次，实际 %d", count)
	}
}

// -- AppendRuntimeEvents terminal status update 测试（3C） --

func TestAppendRuntimeEventsUpdatesStatusOnCompleted(t *testing.T) {
	b := NewInMemoryRuntimeBus()
	task, run, _, err := b.PrepareRun(contracts.CreateTaskInput{UserGoal: "completed status test"})
	if err != nil {
		t.Fatalf("PrepareRun failed: %v", err)
	}
	_ = task

	completedEvent := contracts.RuntimeEvent{
		ID: "ev-001", Type: "agent.run.completed",
		TaskID: run.TaskID, RunID: run.ID, Timestamp: contracts.NowISO(),
		Payload: map[string]interface{}{},
	}
	err = b.AppendRuntimeEvents(run.ID, []contracts.RuntimeEvent{completedEvent})
	if err != nil {
		t.Fatalf("AppendRuntimeEvents failed: %v", err)
	}

	updated, ok := b.GetRun(run.ID)
	if !ok {
		t.Fatal("run should exist")
	}
	if updated.Status != "completed" {
		t.Errorf("run status = %q, want completed", updated.Status)
	}
}

func TestAppendRuntimeEventsUpdatesStatusOnFailed(t *testing.T) {
	b := NewInMemoryRuntimeBus()
	task, run, _, err := b.PrepareRun(contracts.CreateTaskInput{UserGoal: "failed status test"})
	if err != nil {
		t.Fatalf("PrepareRun failed: %v", err)
	}
	_ = task

	failedEvent := contracts.RuntimeEvent{
		ID: "ev-fail", Type: "agent.run.failed",
		TaskID: run.TaskID, RunID: run.ID, Timestamp: contracts.NowISO(),
		Payload: map[string]interface{}{},
	}
	err = b.AppendRuntimeEvents(run.ID, []contracts.RuntimeEvent{failedEvent})
	if err != nil {
		t.Fatalf("AppendRuntimeEvents failed: %v", err)
	}

	updated, _ := b.GetRun(run.ID)
	if updated.Status != "failed" {
		t.Errorf("run status = %q, want failed", updated.Status)
	}
}

func TestAppendRuntimeEventsUpdatesStatusOnCancelled(t *testing.T) {
	b := NewInMemoryRuntimeBus()
	task, run, _, err := b.PrepareRun(contracts.CreateTaskInput{UserGoal: "cancelled status test"})
	if err != nil {
		t.Fatalf("PrepareRun failed: %v", err)
	}
	_ = task

	cancelledEvent := contracts.RuntimeEvent{
		ID: "ev-cancel", Type: "agent.run.cancelled",
		TaskID: run.TaskID, RunID: run.ID, Timestamp: contracts.NowISO(),
		Payload: map[string]interface{}{"reason": "cancelled_by_user"},
	}
	err = b.AppendRuntimeEvents(run.ID, []contracts.RuntimeEvent{cancelledEvent})
	if err != nil {
		t.Fatalf("AppendRuntimeEvents failed: %v", err)
	}

	updated, _ := b.GetRun(run.ID)
	if updated.Status != "cancelled" {
		t.Errorf("run status = %q, want cancelled", updated.Status)
	}
}
