// InMemoryRuntimeBus 基于内存的 RuntimeBus 和 RuntimeStateStore 实现。
//
// 同时实现 RuntimeBus 和 RuntimeStateStore 接口。
// 复用现有 contracts.InMemoryState + mock event 行为，不接 Redis / Python worker / LLM / Storage。
// 内部使用 sync.RWMutex 保证并发安全：所有对 b.state.* map 的读写都在锁保护内完成，
// 所有返回给 handler 的 DTO 指针和 slice 均为深拷贝，handler 不会持有内部 state 引用。
package orchestrator

import (
	"fmt"
	"strings"
	"sync"

	"github.com/google/uuid"
	"github.com/jarvis-assistant/gateway/internal/contracts"
	"github.com/jarvis-assistant/gateway/internal/testkit"
)

// InMemoryRuntimeBus 同时实现 RuntimeBus 和 RuntimeStateStore 接口，所有状态存于内存。
type InMemoryRuntimeBus struct {
	mu    sync.RWMutex
	state *contracts.InMemoryState
}

// NewInMemoryRuntimeBus 创建基于内存的 RuntimeBus。
func NewInMemoryRuntimeBus() *InMemoryRuntimeBus {
	return &InMemoryRuntimeBus{
		state: contracts.NewInMemoryState(),
	}
}

// -- DTO 值拷贝辅助函数 --

func copyTask(t *contracts.TaskDTO) *contracts.TaskDTO {
	if t == nil {
		return nil
	}
	c := *t
	return &c
}

func copyRun(r *contracts.AgentRunDTO) *contracts.AgentRunDTO {
	if r == nil {
		return nil
	}
	c := *r
	return &c
}

// deepCopyPermReq 深拷贝 PermissionRequestDTO。
// ArgumentsSummary map 和 AllowedDecisions slice 均为独立副本，
// PermissionScopeDTO 是纯值类型（string 字段），结构体赋值即完成拷贝。
func deepCopyPermReq(p *contracts.PermissionRequestDTO) *contracts.PermissionRequestDTO {
	if p == nil {
		return nil
	}
	c := *p
	c.ArgumentsSummary = deepCopyMap(p.ArgumentsSummary)
	if p.AllowedDecisions != nil {
		c.AllowedDecisions = make([]contracts.PermissionDecisionType, len(p.AllowedDecisions))
		copy(c.AllowedDecisions, p.AllowedDecisions)
	}
	return &c
}

// -- RuntimeEvent 深拷贝 --

// deepCopyEvents 返回 RuntimeEvent slice 的深拷贝。
// 每个事件的 Payload map 也会递归深拷贝，保证 handler 不共享内部 state 引用。
func deepCopyEvents(events []contracts.RuntimeEvent) []contracts.RuntimeEvent {
	if events == nil {
		return nil
	}
	result := make([]contracts.RuntimeEvent, len(events))
	for i, e := range events {
		result[i] = contracts.RuntimeEvent{
			ID:        e.ID,
			Type:      e.Type,
			TaskID:    e.TaskID,
			RunID:     e.RunID,
			StepID:    e.StepID,
			Timestamp: e.Timestamp,
			Payload:   deepCopyMap(e.Payload),
		}
	}
	return result
}

// deepCopyMap 深拷贝 map[string]interface{}。
func deepCopyMap(m map[string]interface{}) map[string]interface{} {
	if m == nil {
		return nil
	}
	result := make(map[string]interface{}, len(m))
	for k, v := range m {
		result[k] = deepCopyValue(v)
	}
	return result
}

// deepCopyValue 递归深拷贝 interface{} 值。
// 覆盖当前 mock payload 中出现的所有结构：
//   - map[string]interface{}
//   - []interface{}
//   - []string
//   - string / bool / float64 / nil（不可变，直接返回）
func deepCopyValue(v interface{}) interface{} {
	switch val := v.(type) {
	case nil:
		return nil
	case string:
		return val
	case bool:
		return val
	case float64:
		return val
	case map[string]interface{}:
		return deepCopyMap(val)
	case []interface{}:
		result := make([]interface{}, len(val))
		for i, elem := range val {
			result[i] = deepCopyValue(elem)
		}
		return result
	case []string:
		result := make([]string, len(val))
		copy(result, val)
		return result
	default:
		// 未知类型按值返回（后续如有新类型需在此扩展）
		return val
	}
}

// -- 接口实现 --

// PrepareRun 创建 task + run 并生成 simple_success 初始事件。
// 返回的 task / run / events 均为深拷贝，handler 可安全使用。
func (b *InMemoryRuntimeBus) PrepareRun(input contracts.CreateTaskInput) (*contracts.TaskDTO, *contracts.AgentRunDTO, []contracts.RuntimeEvent, error) {
	now := contracts.NowISO()
	taskID := uuid.NewString()
	runID := uuid.NewString()
	title := input.UserGoal
	if runes := []rune(title); len(runes) > 40 {
		title = string(runes[:40]) + "..."
	}

	task := &contracts.TaskDTO{
		ID:            taskID,
		Title:         title,
		UserGoal:      input.UserGoal,
		Status:        "running",
		WorkspacePath: input.WorkspacePath,
		ActiveRunID:   runID,
		CreatedAt:     now,
		UpdatedAt:     now,
	}

	run := &contracts.AgentRunDTO{
		ID:        runID,
		TaskID:    taskID,
		AgentID:   "agent-default",
		Mode:      "single_agent",
		Status:    "created",
		CreatedAt: now,
		UpdatedAt: now,
	}

	events := testkit.GenerateMockEvents("simple_success", taskID, runID)

	// 先将权限请求提取到临时 map，再在锁内合并到 state
	tempPerms := make(map[contracts.ID]*contracts.PermissionRequestDTO)
	testkit.RegisterPermissionRequests(events, tempPerms)

	b.mu.Lock()
	b.state.Tasks[taskID] = task
	b.state.Runs[runID] = run
	b.state.Events[runID] = events
	for k, v := range tempPerms {
		b.state.PermissionReqs[k] = v
	}
	b.mu.Unlock()

	return copyTask(task), copyRun(run), deepCopyEvents(events), nil
}

// PrepareDevMock 为自动化测试创建 task + run + 场景事件。
// 它不是 RuntimeBus 产品契约，也不通过 HTTP API 暴露。
func (b *InMemoryRuntimeBus) PrepareDevMock(scenario string) (*contracts.TaskDTO, *contracts.AgentRunDTO, []contracts.RuntimeEvent, error) {
	now := contracts.NowISO()
	taskID := uuid.NewString()
	runID := uuid.NewString()

	task := &contracts.TaskDTO{
		ID:          taskID,
		Title:       "Mock: " + scenario,
		UserGoal:    "dev mock scenario: " + scenario,
		Status:      "running",
		ActiveRunID: runID,
		CreatedAt:   now,
		UpdatedAt:   now,
	}

	run := &contracts.AgentRunDTO{
		ID:        runID,
		TaskID:    taskID,
		AgentID:   "agent-default",
		Mode:      "single_agent",
		Status:    "created",
		CreatedAt: now,
		UpdatedAt: now,
	}

	events := testkit.GenerateMockEvents(scenario, taskID, runID)

	// 先将权限请求提取到临时 map，再在锁内合并到 state
	tempPerms := make(map[contracts.ID]*contracts.PermissionRequestDTO)
	testkit.RegisterPermissionRequests(events, tempPerms)

	b.mu.Lock()
	b.state.Tasks[taskID] = task
	b.state.Runs[runID] = run
	b.state.Events[runID] = events
	for k, v := range tempPerms {
		b.state.PermissionReqs[k] = v
	}
	b.mu.Unlock()

	return copyTask(task), copyRun(run), deepCopyEvents(events), nil
}

// GetEvents 返回 run 所有已产生事件的深拷贝。
// 若 run 存在但事件尚未生成，自动生成默认事件并存储。
// 若 run 不存在，返回 error。
func (b *InMemoryRuntimeBus) GetEvents(runID contracts.ID) ([]contracts.RuntimeEvent, error) {
	b.mu.RLock()
	events, ok := b.state.Events[runID]
	if ok {
		result := deepCopyEvents(events)
		b.mu.RUnlock()
		return result, nil
	}

	// events 不存在 → 检查 run 是否在 state 中
	// 在锁内复制出 run.TaskID，释放锁后只用副本
	run, runOk := b.state.Runs[runID]
	var taskID contracts.ID
	if runOk {
		taskID = run.TaskID
	}
	b.mu.RUnlock()

	if !runOk {
		return nil, fmt.Errorf("run not found: %s", runID)
	}

	// 自动生成默认事件（使用锁内复制的 taskID，不持有 state 指针）
	events = testkit.GenerateMockEvents("simple_success", taskID, runID)
	b.mu.Lock()
	b.state.Events[runID] = events
	b.mu.Unlock()

	return deepCopyEvents(events), nil
}

// AppendRuntimeEvents 追加事件到指定 run 的事件列表（深拷贝，线程安全）。
//
// 用于 Redis-backed 路径：EventPump 从 Redis stream 读取 worker 事件后，
// 通过本方法写入 in-memory state，使 SSE 能通过 GetEvents 读到这些事件。
//
// 约束：
//   - 输入 events 会被深拷贝后追加，调用方传入的引用不会被内部持有
//   - 若 run 不存在，返回 error（调用方应记录日志但不影响泵继续）
//   - 空 events 直接返回 nil，无操作
//   - 若事件中包含 terminal RuntimeEvent（completed / failed / cancelled），
//     会同步更新 run.status 和 updated_at（3C：由 worker RuntimeEvent 驱动状态）
//   - 本方法不生成 mock 事件
//
// terminalEventStatus 定义 terminal RuntimeEvent 到 run status 的映射。
var terminalEventStatus = map[string]string{
	"agent.run.completed": "completed",
	"agent.run.failed":    "failed",
	"agent.run.cancelled": "cancelled",
	"agent.run.paused":    "paused",
	"agent.run.resumed":   "running",
}

func (b *InMemoryRuntimeBus) AppendRuntimeEvents(runID contracts.ID, events []contracts.RuntimeEvent) error {
	if len(events) == 0 {
		return nil
	}

	b.mu.Lock()
	defer b.mu.Unlock()

	run, ok := b.state.Runs[runID]
	if !ok {
		return fmt.Errorf("bus: run not found: %s", runID)
	}

	existingIDs := make(map[contracts.ID]struct{}, len(b.state.Events[runID]))
	for _, existing := range b.state.Events[runID] {
		existingIDs[existing.ID] = struct{}{}
	}
	newEvents := make([]contracts.RuntimeEvent, 0, len(events))
	for _, event := range events {
		if _, duplicate := existingIDs[event.ID]; duplicate {
			continue
		}
		existingIDs[event.ID] = struct{}{}
		newEvents = append(newEvents, event)
	}
	if len(newEvents) == 0 {
		return nil
	}

	copied := deepCopyEvents(newEvents)
	b.state.Events[runID] = append(b.state.Events[runID], copied...)

	// 3C: terminal event 驱动 run status 更新
	now := contracts.NowISO()
	for _, e := range newEvents {
		if newStatus, isTerminal := terminalEventStatus[e.Type]; isTerminal {
			run.Status = newStatus
			run.UpdatedAt = now
			break // 同一批中只需最后一个 terminal event 决定状态
		}
	}

	// Permission MVP: 注册 permission.required 事件中的 PermissionRequest
	// 使 /api/permissions/resolve 能找到并 resolve worker 发出的权限请求
	for _, e := range newEvents {
		if e.Type == "permission.required" {
			reqPayload, ok := e.Payload["request"]
			if !ok {
				continue
			}
			reqMap, ok := reqPayload.(map[string]interface{})
			if !ok {
				continue
			}
			// 构造 PermissionRequestDTO 并注册到 pending
			permReqID, _ := reqMap["id"].(string)
			if permReqID == "" {
				continue
			}
			taskID, _ := reqMap["task_id"].(string)
			reqRunID, _ := reqMap["run_id"].(string)
			toolName, _ := reqMap["tool_name"].(string)
			actionSummary, _ := reqMap["action_summary"].(string)
			reason, _ := reqMap["reason"].(string)
			riskLevel, _ := reqMap["risk_level"].(string)
			stepID, _ := reqMap["step_id"].(string)
			createdAt, _ := reqMap["created_at"].(string)
			expiresAt, _ := reqMap["expires_at"].(string)
			scopeMap, _ := reqMap["scope"].(map[string]interface{})

			scope := contracts.PermissionScopeDTO{Type: "once"}
			if scopeMap != nil {
				if st, ok := scopeMap["type"].(string); ok {
					scope.Type = st
				}
				if wp, ok := scopeMap["workspace_path"].(string); ok {
					scope.WorkspacePath = wp
				}
				if tp, ok := scopeMap["path"].(string); ok {
					scope.Path = tp
				}
				if tn, ok := scopeMap["tool_name"].(string); ok {
					scope.ToolName = tn
				}
			}

			argsSummary, _ := reqMap["arguments_summary"].(map[string]interface{})
			if argsSummary == nil {
				argsSummary = map[string]interface{}{}
			}

			var allowedDecisions []contracts.PermissionDecisionType
			if ad, ok := reqMap["allowed_decisions"].([]interface{}); ok {
				for _, d := range ad {
					if ds, ok := d.(string); ok {
						allowedDecisions = append(allowedDecisions, contracts.PermissionDecisionType(ds))
					}
				}
			}
			if len(allowedDecisions) == 0 {
				allowedDecisions = []contracts.PermissionDecisionType{"allow_once", "deny"}
			}

			permReq := &contracts.PermissionRequestDTO{
				ID:               contracts.ID(permReqID),
				TaskID:           contracts.ID(taskID),
				RunID:            contracts.ID(reqRunID),
				StepID:           contracts.ID(stepID),
				ToolName:         toolName,
				ActionSummary:    actionSummary,
				Reason:           reason,
				RiskLevel:        contracts.RiskLevel(riskLevel),
				Scope:            scope,
				ArgumentsSummary: argsSummary,
				AllowedDecisions: allowedDecisions,
				CreatedAt:        createdAt,
				ExpiresAt:        expiresAt,
			}
			b.state.PermissionReqs[permReqID] = permReq
		}
	}

	return nil
}

// GetRun 返回 run 的值拷贝；ok=false 表示不存在。
// 返回的是副本指针，handler 修改不会影响内部 state。
func (b *InMemoryRuntimeBus) GetRun(runID contracts.ID) (*contracts.AgentRunDTO, bool) {
	b.mu.RLock()
	defer b.mu.RUnlock()
	run, ok := b.state.Runs[runID]
	if !ok {
		return nil, false
	}
	return copyRun(run), true
}

// UpdateRunStatus 更新 run 状态和更新时间。在写锁内完成。
func (b *InMemoryRuntimeBus) UpdateRunStatus(runID contracts.ID, status string) {
	b.mu.Lock()
	defer b.mu.Unlock()
	if run, ok := b.state.Runs[runID]; ok {
		run.Status = status
		run.UpdatedAt = contracts.NowISO()
	}
}

// GetTask 返回 task 的值拷贝；ok=false 表示不存在。
// 返回的是副本指针，handler 修改不会影响内部 state。
func (b *InMemoryRuntimeBus) GetTask(taskID contracts.ID) (*contracts.TaskDTO, bool) {
	b.mu.RLock()
	defer b.mu.RUnlock()
	task, ok := b.state.Tasks[taskID]
	if !ok {
		return nil, false
	}
	return copyTask(task), true
}

// ListTasks 返回所有 task 的值拷贝切片。
func (b *InMemoryRuntimeBus) ListTasks() []contracts.TaskDTO {
	b.mu.RLock()
	defer b.mu.RUnlock()
	tasks := make([]contracts.TaskDTO, 0, len(b.state.Tasks))
	for _, t := range b.state.Tasks {
		tasks = append(tasks, *t)
	}
	return tasks
}

// ResolvePermission 处理权限决策，返回权限请求深拷贝和后续事件深拷贝。
// 权限请求具有一次性消费语义：处理成功后从 pending 中删除，
// 同一 RequestID 第二次调用会返回 error。
//
// 内部委托 commitResolveLocked（需持有写锁）。
func (b *InMemoryRuntimeBus) ResolvePermission(decision contracts.PermissionDecisionDTO) (*contracts.PermissionRequestDTO, []contracts.RuntimeEvent, error) {
	b.mu.Lock()
	defer b.mu.Unlock()
	return b.commitResolveLocked(decision)
}

// commitResolveLocked 是权限决策的内部提交逻辑。
// 调用方必须持有 b.mu 写锁。
// 流程：检查 pending 存在 → 深拷贝 → 删除 pending → 构造事件 → 追加到 run → 更新 run。
func (b *InMemoryRuntimeBus) commitResolveLocked(decision contracts.PermissionDecisionDTO) (*contracts.PermissionRequestDTO, []contracts.RuntimeEvent, error) {
	permReq, ok := b.state.PermissionReqs[decision.RequestID]
	if !ok {
		return nil, nil, fmt.Errorf("permission request not found: %s", decision.RequestID)
	}
	// 深拷贝后立即删除，实现一次性消费
	permReqCopy := deepCopyPermReq(permReq)
	delete(b.state.PermissionReqs, decision.RequestID)

	approved := !strings.HasPrefix(decision.Decision, "deny")
	toolCallID := uuid.NewString()

	// 构造 permission.resolved 事件（mock 函数不访问 state，在锁内执行安全）
	resolvedEvent := testkit.BuildPermissionResolvedEvent(
		permReqCopy.TaskID, permReqCopy.RunID, permReqCopy.StepID,
		decision.RequestID, toolCallID,
		decision.Decision, decision.Note,
	)

	// 构造后续 tool/step/run 事件
	postEvents := testkit.BuildPostPermissionEvents(
		permReqCopy.TaskID, permReqCopy.RunID, permReqCopy.StepID,
		toolCallID, approved,
	)

	allPostEvents := append([]contracts.RuntimeEvent{resolvedEvent}, postEvents...)

	// 追加事件 + 更新 run 状态
	existing := b.state.Events[permReqCopy.RunID]
	b.state.Events[permReqCopy.RunID] = append(existing, allPostEvents...)
	if run, ok := b.state.Runs[permReqCopy.RunID]; ok {
		run.Status = "completed"
		run.UpdatedAt = contracts.NowISO()
	}

	return permReqCopy, deepCopyEvents(allPostEvents), nil
}

// ReadPermissionRequest 读取待处理权限请求的深拷贝，不消费。
//
// 注意：本方法是非消费读取，不提供并发保护。两个并发请求可能读到同一 pending。
// Redis-backed 并发安全路径应使用 ReservePermissionRequest / RestorePermissionRequest /
// CommitPermissionDecisionAckFromReserved 三个原子方法。
//
// 返回深拷贝后的 PermissionRequestDTO；ok=false 表示不存在。
func (b *InMemoryRuntimeBus) ReadPermissionRequest(requestID contracts.ID) (*contracts.PermissionRequestDTO, bool) {
	b.mu.RLock()
	defer b.mu.RUnlock()
	permReq, ok := b.state.PermissionReqs[requestID]
	if !ok {
		return nil, false
	}
	return deepCopyPermReq(permReq), true
}

// -- Redis-backed 并发安全路径：Reserve → publish → AckFromReserved / Restore --

// ReservePermissionRequest 原子占用一个 pending permission。
//
// 加写锁后查找并立即从 pending map 中删除，返回深拷贝。
// 并发重复请求只有一个能 reserve 成功，其他会得到 ok=false。
//
// 用于 Redis-backed 路径：reserve 成功后 publish 到 Redis，
// publish 成功调用 CommitPermissionDecisionAckFromReserved 提交 ack，
// publish 失败调用 RestorePermissionRequest 恢复 pending 可重试状态。
//
// 返回深拷贝后的 PermissionRequestDTO；ok=false 表示不存在或已被占用/消费。
func (b *InMemoryRuntimeBus) ReservePermissionRequest(requestID contracts.ID) (*contracts.PermissionRequestDTO, bool) {
	b.mu.Lock()
	defer b.mu.Unlock()
	permReq, ok := b.state.PermissionReqs[requestID]
	if !ok {
		return nil, false
	}
	// 深拷贝后立即删除 —— 原子占用
	permReqCopy := deepCopyPermReq(permReq)
	delete(b.state.PermissionReqs, requestID)
	return permReqCopy, true
}

// RestorePermissionRequest 恢复一个被 reserve 但 publish 失败的 pending permission。
//
// 加写锁后：如果 permReq 非 nil 且 pending map 中尚无同 ID，则深拷贝后存入。
// 用于 Redis publish 失败后的恢复，使同 request_id 可再次被 Reserve。
//
// 恢复的 permReq 会被深拷贝，调用方传入的指针不受影响。
func (b *InMemoryRuntimeBus) RestorePermissionRequest(permReq *contracts.PermissionRequestDTO) {
	if permReq == nil {
		return
	}
	b.mu.Lock()
	defer b.mu.Unlock()
	// 只在尚不存在时恢复（防御性）
	if _, ok := b.state.PermissionReqs[permReq.ID]; !ok {
		b.state.PermissionReqs[permReq.ID] = deepCopyPermReq(permReq)
	}
}

// CommitPermissionDecisionAckFromReserved 使用已 reserve 的 permReq 提交 ack。
//
// 调用方已通过 ReservePermissionRequest 原子占用了 permission（从 pending 中删除），
// 本方法不再查找 pending map，直接使用 reserved permReq 的数据生成并追加
// permission.resolved 确认事件。
//
// 约束：
//   - permReq 必须非 nil
//   - 不生成 tool.call.finished/failed、agent.step.completed、agent.run.completed
//   - 不更新 run 状态为 completed
//   - worker outcome events 后续必须由 Python worker 通过 RuntimeEvent 写入
//
// 返回 permission.resolved 事件深拷贝和 error。
func (b *InMemoryRuntimeBus) CommitPermissionDecisionAckFromReserved(permReq *contracts.PermissionRequestDTO, decision contracts.PermissionDecisionDTO) ([]contracts.RuntimeEvent, error) {
	if permReq == nil {
		return nil, fmt.Errorf("permission request is nil")
	}
	if permReq.ID != decision.RequestID {
		return nil, fmt.Errorf("permReq.ID %q != decision.RequestID %q", permReq.ID, decision.RequestID)
	}

	b.mu.Lock()
	defer b.mu.Unlock()

	// 只生成 permission.resolved 确认事件 —— 不生成 tool/step/run 完成事件
	toolCallID := uuid.NewString()
	resolvedEvent := testkit.BuildPermissionResolvedEvent(
		permReq.TaskID, permReq.RunID, permReq.StepID,
		decision.RequestID, toolCallID,
		decision.Decision, decision.Note,
	)

	b.state.Events[permReq.RunID] = append(b.state.Events[permReq.RunID], resolvedEvent)
	// 不更新 run 状态 —— worker 后续通过 RuntimeEvent 写入

	return deepCopyEvents([]contracts.RuntimeEvent{resolvedEvent}), nil
}

// CommitResolvePermission 提交权限决策的本地消费。
//
// 调用方应先在外部完成 Redis publish 等操作，成功后再调用本方法提交本地状态。
// 内部委托 commitResolveLocked：删除 pending → 构造事件 → 追加 → 更新 run。
//
// 只返回后续事件和 error；调用方通常已通过 ReadPermissionRequest 获取权限请求副本。
//
// 注意：本方法会生成 tool.call.finished/failed、agent.step.completed、agent.run.completed
// 等确定性测试事件。仅用于显式 in-memory 测试路径，不得用于 Redis-backed 路径。
func (b *InMemoryRuntimeBus) CommitResolvePermission(decision contracts.PermissionDecisionDTO) ([]contracts.RuntimeEvent, error) {
	b.mu.Lock()
	defer b.mu.Unlock()
	_, events, err := b.commitResolveLocked(decision)
	return events, err
}

// CommitPermissionDecisionAck 提交权限决策的确认消费（仅 ack，不生成 worker outcome）。
//
// 注意：本方法在锁内查找 pending map 并删除。对于 Redis-backed 并发安全路径，
// 推荐使用 ReservePermissionRequest + CommitPermissionDecisionAckFromReserved 的
// 原子组合，避免两个并发请求都通过 ReadPermissionRequest 读到同一 pending。
//
// 本方法仍可用于单请求路径或测试。
func (b *InMemoryRuntimeBus) CommitPermissionDecisionAck(decision contracts.PermissionDecisionDTO) ([]contracts.RuntimeEvent, error) {
	b.mu.Lock()
	defer b.mu.Unlock()

	permReq, ok := b.state.PermissionReqs[decision.RequestID]
	if !ok {
		return nil, fmt.Errorf("permission request not found: %s", decision.RequestID)
	}
	delete(b.state.PermissionReqs, decision.RequestID)

	toolCallID := uuid.NewString()
	resolvedEvent := testkit.BuildPermissionResolvedEvent(
		permReq.TaskID, permReq.RunID, permReq.StepID,
		decision.RequestID, toolCallID,
		decision.Decision, decision.Note,
	)

	b.state.Events[permReq.RunID] = append(b.state.Events[permReq.RunID], resolvedEvent)

	return deepCopyEvents([]contracts.RuntimeEvent{resolvedEvent}), nil
}

// PrepareMinimalRun 为 Redis-backed 路径创建 task + run，只生成 task.created 初始事件。
//
// 与 PrepareRun 的关键区别：
//   - 不生成 simple_success mock 事件（model.delta / model.call.completed /
//     tool.call.* / artifact.created / agent.run.completed）
//   - run.Status 为 "queued"（表示已入队 Redis，等待 worker 消费）
//   - 只生成一个 task.created 事件
//
// 返回的 task / run / events 均为深拷贝。
func (b *InMemoryRuntimeBus) PrepareMinimalRun(input contracts.CreateTaskInput) (*contracts.TaskDTO, *contracts.AgentRunDTO, []contracts.RuntimeEvent, error) {
	now := contracts.NowISO()
	taskID := uuid.NewString()
	runID := uuid.NewString()
	title := input.UserGoal
	if runes := []rune(title); len(runes) > 40 {
		title = string(runes[:40]) + "..."
	}

	task := &contracts.TaskDTO{
		ID:            taskID,
		Title:         title,
		UserGoal:      input.UserGoal,
		Status:        "running",
		WorkspacePath: input.WorkspacePath,
		ActiveRunID:   runID,
		CreatedAt:     now,
		UpdatedAt:     now,
	}

	// Redis-backed 路径使用 "queued" 表示已入队等待 worker 消费
	run := &contracts.AgentRunDTO{
		ID:        runID,
		TaskID:    taskID,
		AgentID:   "agent-default",
		Mode:      "single_agent",
		Status:    "queued",
		CreatedAt: now,
		UpdatedAt: now,
	}

	// 只生成 task.created 事件，不含任何 worker 事件
	event := contracts.RuntimeEvent{
		ID:        uuid.NewString(),
		Type:      "task.created",
		TaskID:    taskID,
		RunID:     runID,
		Timestamp: now,
		Payload: map[string]interface{}{
			"task": map[string]interface{}{
				"id":             task.ID,
				"title":          task.Title,
				"user_goal":      task.UserGoal,
				"status":         task.Status,
				"workspace_path": task.WorkspacePath,
				"active_run_id":  task.ActiveRunID,
				"created_at":     task.CreatedAt,
				"updated_at":     task.UpdatedAt,
			},
			"run": map[string]interface{}{
				"id":         run.ID,
				"task_id":    run.TaskID,
				"agent_id":   run.AgentID,
				"mode":       run.Mode,
				"status":     run.Status,
				"created_at": run.CreatedAt,
				"updated_at": run.UpdatedAt,
			},
		},
	}

	events := []contracts.RuntimeEvent{event}

	b.mu.Lock()
	b.state.Tasks[taskID] = task
	b.state.Runs[runID] = run
	b.state.Events[runID] = events
	b.mu.Unlock()

	return copyTask(task), copyRun(run), deepCopyEvents(events), nil
}

// CancelRun 实现 RuntimeBus.CancelRun。
//
// InMemory 模式：直接更新 run 状态为 cancelled，生成 agent.run.cancelled RuntimeEvent。
// 这是 mock/dev 路径的行为，不应在 Redis 模式下使用。
func (b *InMemoryRuntimeBus) CancelRun(runID contracts.ID) (*contracts.AgentRunDTO, error) {
	b.mu.Lock()
	defer b.mu.Unlock()

	run, ok := b.state.Runs[runID]
	if !ok {
		return nil, fmt.Errorf("bus: run not found: %s", runID)
	}

	now := contracts.NowISO()
	run.Status = "cancelled"
	run.UpdatedAt = now

	// 生成 agent.run.cancelled terminal event（仅 in-memory/mock 模式）
	cancelledEvent := contracts.RuntimeEvent{
		ID:        uuid.NewString(),
		Type:      "agent.run.cancelled",
		TaskID:    run.TaskID,
		RunID:     runID,
		Timestamp: now,
		Payload: map[string]interface{}{
			"run_id": runID,
			"reason": "cancelled_by_user",
		},
	}
	b.state.Events[runID] = append(b.state.Events[runID], cancelledEvent)

	return copyRun(run), nil
}

// SeedAcceptedRun 使用 Control Plane 返回的权威 ID 创建只读投影。
// 不生成新 ID，不入队 Redis，不修改 PostgreSQL。
func (b *InMemoryRuntimeBus) SeedAcceptedRun(
	task contracts.TaskDTO, run contracts.AgentRunDTO, initialEvents []contracts.RuntimeEvent,
) {
	b.mu.Lock()
	defer b.mu.Unlock()

	taskCopy := copyTask(&task)
	runCopy := copyRun(&run)
	b.state.Tasks[task.ID] = taskCopy
	b.state.Runs[run.ID] = runCopy
	eventsCopy := make([]contracts.RuntimeEvent, len(initialEvents))
	copy(eventsCopy, initialEvents)
	b.state.Events[run.ID] = eventsCopy
}
