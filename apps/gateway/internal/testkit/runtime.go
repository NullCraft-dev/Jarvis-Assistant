// Package mock 提供 deterministic mock RuntimeEvent 生成器。
// 用于 UI-first 开发，遵循 docs/13-interface-contract.md § Mock Runtime 验收场景。
package testkit

import (
	"fmt"
	"time"

	"github.com/google/uuid"
	"github.com/jarvis-assistant/gateway/internal/contracts"
)

// GenerateMockEvents 按场景生成一组有序 RuntimeEvent。
// 返回的事件序列可以被 Go Gateway 通过 SSE 按时间间隔推送给前端。
func GenerateMockEvents(scenario string, taskID, runID contracts.ID) []contracts.RuntimeEvent {
	switch scenario {
	case "permission_required", "permission_denied":
		return permissionScenario(taskID, runID)
	default:
		return simpleSuccess(taskID, runID)
	}
}

// simpleSuccess 生成最简成功路径事件序列。
// 对应 docs/13-interface-contract.md § simple_success
func simpleSuccess(taskID, runID contracts.ID) []contracts.RuntimeEvent {
	now := time.Now().UTC()
	stepID1 := uuid.NewString()
	stepID2 := uuid.NewString()
	stepID3 := uuid.NewString()
	artifactID := uuid.NewString()

	return []contracts.RuntimeEvent{
		{
			ID:        uuid.NewString(),
			Type:      "task.created",
			TaskID:    taskID,
			RunID:     runID,
			Timestamp: now.Format(time.RFC3339),
			Payload: map[string]interface{}{
				"task": map[string]interface{}{
					"id":         taskID,
					"title":      "示例任务",
					"user_goal":  "帮我分析当前项目结构",
					"status":     "running",
					"created_at": now.Format(time.RFC3339),
					"updated_at": now.Format(time.RFC3339),
				},
				"run": map[string]interface{}{
					"id":         runID,
					"task_id":    taskID,
					"agent_id":   "agent-default",
					"mode":       "single_agent",
					"status":     "created",
					"created_at": now.Format(time.RFC3339),
					"updated_at": now.Format(time.RFC3339),
				},
			},
		},
		{
			ID:        uuid.NewString(),
			Type:      "agent.run.started",
			TaskID:    taskID,
			RunID:     runID,
			Timestamp: now.Add(300 * time.Millisecond).Format(time.RFC3339),
			Payload: map[string]interface{}{
				"run_id": runID,
				"status": "running",
			},
		},
		{
			ID:        uuid.NewString(),
			Type:      "agent.step.started",
			TaskID:    taskID,
			RunID:     runID,
			StepID:    stepID1,
			Timestamp: now.Add(800 * time.Millisecond).Format(time.RFC3339),
			Payload: map[string]interface{}{
				"step": map[string]interface{}{
					"id":      stepID1,
					"run_id":  runID,
					"type":    "user_message",
					"status":  "running",
					"title":   "接收用户指令",
					"summary": "帮我分析当前项目结构",
				},
			},
		},
		{
			ID:        uuid.NewString(),
			Type:      "agent.step.completed",
			TaskID:    taskID,
			RunID:     runID,
			StepID:    stepID1,
			Timestamp: now.Add(1200 * time.Millisecond).Format(time.RFC3339),
			Payload: map[string]interface{}{
				"step": map[string]interface{}{
					"id":          stepID1,
					"run_id":      runID,
					"type":        "user_message",
					"status":      "completed",
					"title":       "接收用户指令",
					"duration_ms": 400,
				},
			},
		},
		{
			ID:        uuid.NewString(),
			Type:      "agent.step.started",
			TaskID:    taskID,
			RunID:     runID,
			StepID:    stepID2,
			Timestamp: now.Add(1600 * time.Millisecond).Format(time.RFC3339),
			Payload: map[string]interface{}{
				"step": map[string]interface{}{
					"id":      stepID2,
					"run_id":  runID,
					"type":    "model_call",
					"status":  "running",
					"title":   "模型推理",
					"summary": "分析用户意图，制定执行计划",
				},
			},
		},
		{
			ID:        uuid.NewString(),
			Type:      "model.delta",
			TaskID:    taskID,
			RunID:     runID,
			StepID:    stepID2,
			Timestamp: now.Add(2000 * time.Millisecond).Format(time.RFC3339),
			Payload: map[string]interface{}{
				"step_id":     stepID2,
				"delta":       "我来分析一下当前项目的结构。",
				"accumulated": "我来分析一下当前项目的结构。",
			},
		},
		{
			ID:        uuid.NewString(),
			Type:      "model.delta",
			TaskID:    taskID,
			RunID:     runID,
			StepID:    stepID2,
			Timestamp: now.Add(2400 * time.Millisecond).Format(time.RFC3339),
			Payload: map[string]interface{}{
				"step_id":     stepID2,
				"delta":       " 项目采用 Vue 3 + Go Gateway + Python Worker 架构。",
				"accumulated": "我来分析一下当前项目的结构。 项目采用 Vue 3 + Go Gateway + Python Worker 架构。",
			},
		},
		{
			ID:        uuid.NewString(),
			Type:      "model.call.completed",
			TaskID:    taskID,
			RunID:     runID,
			StepID:    stepID2,
			Timestamp: now.Add(2800 * time.Millisecond).Format(time.RFC3339),
			Payload: map[string]interface{}{
				"step_id":     stepID2,
				"accumulated": "我来分析一下当前项目的结构。 项目采用 Vue 3 + Go Gateway + Python Worker 架构。",
			},
		},
		{
			ID:        uuid.NewString(),
			Type:      "agent.step.completed",
			TaskID:    taskID,
			RunID:     runID,
			StepID:    stepID2,
			Timestamp: now.Add(3000 * time.Millisecond).Format(time.RFC3339),
			Payload: map[string]interface{}{
				"step": map[string]interface{}{
					"id":          stepID2,
					"run_id":      runID,
					"type":        "model_call",
					"status":      "completed",
					"title":       "模型推理",
					"duration_ms": 1400,
				},
			},
		},
		{
			ID:        uuid.NewString(),
			Type:      "agent.step.started",
			TaskID:    taskID,
			RunID:     runID,
			StepID:    stepID3,
			Timestamp: now.Add(3400 * time.Millisecond).Format(time.RFC3339),
			Payload: map[string]interface{}{
				"step": map[string]interface{}{
					"id":      stepID3,
					"run_id":  runID,
					"type":    "final_output",
					"status":  "running",
					"title":   "生成最终结果",
					"summary": "整理分析结果",
				},
			},
		},
		{
			ID:        uuid.NewString(),
			Type:      "artifact.created",
			TaskID:    taskID,
			RunID:     runID,
			StepID:    stepID3,
			Timestamp: now.Add(3800 * time.Millisecond).Format(time.RFC3339),
			Payload: map[string]interface{}{
				"artifact": map[string]interface{}{
					"id":      artifactID,
					"task_id": taskID,
					"run_id":  runID,
					"kind":    "markdown",
					"title":   "项目结构分析结果",
					"purpose": "final_response",
					"producer": map[string]interface{}{
						"type": "runtime",
					},
					"content":  "## 项目结构分析\n\n当前项目采用分层架构：\n- **前端**: Vue 3 + TypeScript + Vite\n- **网关**: Go Gateway / Runtime Orchestrator\n- **运行时**: Python Agent Worker\n\n项目目录结构清晰，遵循 docs/ 中定义的分层边界。",
					"metadata": map[string]interface{}{},
				},
			},
		},
		{
			ID:        uuid.NewString(),
			Type:      "agent.step.completed",
			TaskID:    taskID,
			RunID:     runID,
			StepID:    stepID3,
			Timestamp: now.Add(4200 * time.Millisecond).Format(time.RFC3339),
			Payload: map[string]interface{}{
				"step": map[string]interface{}{
					"id":          stepID3,
					"run_id":      runID,
					"type":        "final_output",
					"status":      "completed",
					"title":       "生成最终结果",
					"duration_ms": 800,
				},
			},
		},
		{
			ID:        uuid.NewString(),
			Type:      "agent.run.completed",
			TaskID:    taskID,
			RunID:     runID,
			Timestamp: now.Add(4500 * time.Millisecond).Format(time.RFC3339),
			Payload: map[string]interface{}{
				"run_id":                   runID,
				"status":                   "completed",
				"final_output_artifact_id": artifactID,
			},
		},
	}
}

// permissionScenario 生成权限请求场景事件序列。
// 对应 docs/13-interface-contract.md § permission_required
func permissionScenario(taskID, runID contracts.ID) []contracts.RuntimeEvent {
	now := time.Now().UTC()
	stepID1 := uuid.NewString()
	toolCallID := uuid.NewString()
	permReqID := uuid.NewString()

	return []contracts.RuntimeEvent{
		{
			ID:        uuid.NewString(),
			Type:      "task.created",
			TaskID:    taskID,
			RunID:     runID,
			Timestamp: now.Format(time.RFC3339),
			Payload: map[string]interface{}{
				"task": map[string]interface{}{
					"id":         taskID,
					"title":      "Shell 命令示例",
					"user_goal":  "列出当前目录下的所有文件",
					"status":     "running",
					"created_at": now.Format(time.RFC3339),
					"updated_at": now.Format(time.RFC3339),
				},
				"run": map[string]interface{}{
					"id":         runID,
					"task_id":    taskID,
					"agent_id":   "agent-default",
					"mode":       "single_agent",
					"status":     "created",
					"created_at": now.Format(time.RFC3339),
					"updated_at": now.Format(time.RFC3339),
				},
			},
		},
		{
			ID:        uuid.NewString(),
			Type:      "agent.run.started",
			TaskID:    taskID,
			RunID:     runID,
			Timestamp: now.Add(300 * time.Millisecond).Format(time.RFC3339),
			Payload: map[string]interface{}{
				"run_id": runID,
				"status": "running",
			},
		},
		{
			ID:        uuid.NewString(),
			Type:      "agent.step.started",
			TaskID:    taskID,
			RunID:     runID,
			StepID:    stepID1,
			Timestamp: now.Add(600 * time.Millisecond).Format(time.RFC3339),
			Payload: map[string]interface{}{
				"step": map[string]interface{}{
					"id":      stepID1,
					"run_id":  runID,
					"type":    "tool_call",
					"status":  "running",
					"title":   "执行 Shell 命令",
					"summary": "执行 ls -la 查看目录内容",
				},
			},
		},
		{
			ID:        uuid.NewString(),
			Type:      "tool.call.started",
			TaskID:    taskID,
			RunID:     runID,
			StepID:    stepID1,
			Timestamp: now.Add(800 * time.Millisecond).Format(time.RFC3339),
			Payload: map[string]interface{}{
				"tool_call": map[string]interface{}{
					"id":                toolCallID,
					"run_id":            runID,
					"step_id":           stepID1,
					"tool_name":         "shell",
					"provider":          "native",
					"risk_level":        "L3",
					"arguments":         map[string]interface{}{"command": "ls -la"},
					"permission_status": "pending",
					"status":            "pending",
				},
			},
		},
		{
			ID:        uuid.NewString(),
			Type:      "permission.required",
			TaskID:    taskID,
			RunID:     runID,
			StepID:    stepID1,
			Timestamp: now.Add(1000 * time.Millisecond).Format(time.RFC3339),
			Payload: map[string]interface{}{
				"request": map[string]interface{}{
					"id":                permReqID,
					"task_id":           taskID,
					"run_id":            runID,
					"step_id":           stepID1,
					"tool_name":         "shell",
					"action_summary":    "执行 Shell 命令: ls -la",
					"reason":            "需要查看当前目录文件列表以分析项目结构",
					"risk_level":        "L3",
					"scope":             map[string]interface{}{"type": "once", "workspace_path": "/Users/test/Jarvis-Assistant"},
					"arguments_summary": map[string]interface{}{"command": "ls -la"},
					"allowed_decisions": []string{"allow_once", "allow_for_task", "deny"},
					"created_at":        now.Add(1000 * time.Millisecond).Format(time.RFC3339),
					"expires_at":        now.Add(16 * time.Minute).Format(time.RFC3339),
				},
			},
		},
		// permission.resolved 不在事件序列中——由用户通过 API 决策后触发
	}
}

// BuildPermissionResolvedEvent 根据用户决策构造 permission.resolved 事件
func BuildPermissionResolvedEvent(taskID, runID, stepID, permReqID, toolCallID contracts.ID, decision, note string) contracts.RuntimeEvent {
	return contracts.RuntimeEvent{
		ID:        uuid.NewString(),
		Type:      "permission.resolved",
		TaskID:    taskID,
		RunID:     runID,
		StepID:    stepID,
		Timestamp: time.Now().UTC().Format(time.RFC3339),
		Payload: map[string]interface{}{
			"request_id":   permReqID,
			"decision":     decision,
			"tool_call_id": toolCallID,
			"note":         note,
		},
	}
}

// BuildPostPermissionEvents 构造权限通过后的后续事件（tool.call.finished + agent.run.completed）
func BuildPostPermissionEvents(taskID, runID, stepID, toolCallID contracts.ID, approved bool) []contracts.RuntimeEvent {
	now := time.Now().UTC()
	events := []contracts.RuntimeEvent{}

	completedAt := now.Format(time.RFC3339)
	if approved {
		events = append(events, contracts.RuntimeEvent{
			ID:        uuid.NewString(),
			Type:      "tool.call.finished",
			TaskID:    taskID,
			RunID:     runID,
			StepID:    stepID,
			Timestamp: completedAt,
			Payload: map[string]interface{}{
				"tool_call": map[string]interface{}{
					"id":                toolCallID,
					"run_id":            runID,
					"step_id":           stepID,
					"tool_name":         "shell",
					"provider":          "native",
					"risk_level":        "L3",
					"arguments":         map[string]interface{}{"command": "ls -la"},
					"status":            "completed",
					"permission_status": "approved",
					"completed_at":      completedAt,
					"result": map[string]interface{}{
						"kind":    "text",
						"summary": "命令执行成功：total 64 entries listed",
					},
				},
			},
		})
	} else {
		events = append(events, contracts.RuntimeEvent{
			ID:        uuid.NewString(),
			Type:      "tool.call.failed",
			TaskID:    taskID,
			RunID:     runID,
			StepID:    stepID,
			Timestamp: completedAt,
			Payload: map[string]interface{}{
				"tool_call": map[string]interface{}{
					"id":                toolCallID,
					"run_id":            runID,
					"step_id":           stepID,
					"tool_name":         "shell",
					"provider":          "native",
					"risk_level":        "L3",
					"arguments":         map[string]interface{}{"command": "ls -la"},
					"status":            "failed",
					"permission_status": "denied",
					"completed_at":      completedAt,
					"error": map[string]interface{}{
						"code":        "PERMISSION_DENIED",
						"message":     "用户拒绝了 Shell 命令执行请求",
						"category":    "permission",
						"recoverable": true,
					},
				},
			},
		})
	}

	// 完成 step（拒绝权限时 step 标记为 failed）
	stepStatus := "completed"
	stepTitle := "执行 Shell 命令"
	if !approved {
		stepStatus = "failed"
		stepTitle = "执行 Shell 命令（权限被拒绝）"
	}
	events = append(events, contracts.RuntimeEvent{
		ID:        uuid.NewString(),
		Type:      "agent.step.completed",
		TaskID:    taskID,
		RunID:     runID,
		StepID:    stepID,
		Timestamp: now.Add(200 * time.Millisecond).Format(time.RFC3339),
		Payload: map[string]interface{}{
			"step": map[string]interface{}{
				"id":     stepID,
				"run_id": runID,
				"type":   "tool_call",
				"status": stepStatus,
				"title":  stepTitle,
			},
		},
	})

	// 完成 run
	events = append(events, contracts.RuntimeEvent{
		ID:        uuid.NewString(),
		Type:      "agent.run.completed",
		TaskID:    taskID,
		RunID:     runID,
		Timestamp: now.Add(500 * time.Millisecond).Format(time.RFC3339),
		Payload: map[string]interface{}{
			"run_id": runID,
			"status": "completed",
		},
	})

	return events
}

// RegisterPermissionRequests 从事件列表中提取 permission.required 并注册到 state
func RegisterPermissionRequests(events []contracts.RuntimeEvent, permReqs map[contracts.ID]*contracts.PermissionRequestDTO) {
	for _, evt := range events {
		if evt.Type != "permission.required" {
			continue
		}
		reqMap, ok := evt.Payload["request"].(map[string]interface{})
		if !ok {
			continue
		}
		id, _ := reqMap["id"].(string)
		if id == "" {
			continue
		}
		permReqs[id] = &contracts.PermissionRequestDTO{
			ID:            id,
			TaskID:        evt.TaskID,
			RunID:         evt.RunID,
			StepID:        evt.StepID,
			ToolName:      getString(reqMap, "tool_name"),
			ActionSummary: getString(reqMap, "action_summary"),
			Reason:        getString(reqMap, "reason"),
			RiskLevel:     getString(reqMap, "risk_level"),
			Scope: contracts.PermissionScopeDTO{
				Type: getString(getMap(reqMap, "scope"), "type"),
			},
			ArgumentsSummary: getMap(reqMap, "arguments_summary"),
			AllowedDecisions: getStringSlice(reqMap, "allowed_decisions"),
			CreatedAt:        getString(reqMap, "created_at"),
			ExpiresAt:        getString(reqMap, "expires_at"),
		}
	}
}

func getString(m map[string]interface{}, key string) string {
	if v, ok := m[key].(string); ok {
		return v
	}
	return ""
}

func getMap(m map[string]interface{}, key string) map[string]interface{} {
	if v, ok := m[key].(map[string]interface{}); ok {
		return v
	}
	return map[string]interface{}{}
}

func getStringSlice(m map[string]interface{}, key string) []string {
	var result []string
	if arr, ok := m[key].([]interface{}); ok {
		for _, item := range arr {
			if s, ok := item.(string); ok {
				result = append(result, s)
			}
		}
	}
	return result
}

// 工具函数
func ptr[T any](v T) *T { return &v }

// 确保 uuid 被引用
var _ = fmt.Sprintf("%s", uuid.NewString())
