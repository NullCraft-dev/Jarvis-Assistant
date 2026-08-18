package app

import (
	"context"
	"errors"
	"fmt"
	"os"

	"github.com/jarvis-assistant/gateway/internal/contracts"
	"github.com/jarvis-assistant/gateway/internal/controlplane"
	"github.com/jarvis-assistant/gateway/internal/orchestrator"
)

type dependencies struct {
	runtimeConfig orchestrator.RuntimeBusConfig
	runtimeBus    orchestrator.RuntimeBus
	stateStore    orchestrator.RuntimeStateStore
	pump          orchestrator.PumpCloser
	controlPlane  *controlplane.Client
	controlURL    string
}

func buildDependencies() (*dependencies, error) {
	cfg := orchestrator.RuntimeBusConfigFromEnv()
	runtimeBus, stateStore, pump, err := orchestrator.NewRuntimeBus(cfg)
	if err != nil {
		return nil, err
	}

	controlURL := os.Getenv("JARVIS_CONTROL_PLANE_URL")
	if cfg.BusType == "redis" && controlURL == "" {
		return nil, errors.New("JARVIS_RUNTIME_BUS=redis 模式要求 JARVIS_CONTROL_PLANE_URL 必须设置")
	}

	var controlPlane *controlplane.Client
	if controlURL != "" {
		controlPlane = controlplane.NewClient(controlURL)
		if configurable, ok := runtimeBus.(interface {
			SetProjectionLoader(orchestrator.RuntimeProjectionLoader)
		}); ok {
			configurable.SetProjectionLoader(&controlPlaneProjectionLoader{client: controlPlane})
		}
	}

	return &dependencies{
		runtimeConfig: cfg,
		runtimeBus:    runtimeBus,
		stateStore:    stateStore,
		pump:          pump,
		controlPlane:  controlPlane,
		controlURL:    controlURL,
	}, nil
}

type controlPlaneProjectionLoader struct{ client *controlplane.Client }

func (l *controlPlaneProjectionLoader) LoadRuntimeProjection(
	ctx context.Context, taskID, runID contracts.ID,
) (contracts.TaskDTO, contracts.AgentRunDTO, error) {
	response, err := l.client.GetTask(ctx, taskID)
	if err != nil {
		return contracts.TaskDTO{}, contracts.AgentRunDTO{}, err
	}
	if response.Task.ID != taskID {
		return contracts.TaskDTO{}, contracts.AgentRunDTO{}, fmt.Errorf("task/run association mismatch")
	}
	var selected *controlplane.RunDTO
	for index := range response.Runs {
		if response.Runs[index].ID == runID && response.Runs[index].TaskID == taskID {
			selected = &response.Runs[index]
			break
		}
	}
	if selected == nil {
		return contracts.TaskDTO{}, contracts.AgentRunDTO{}, fmt.Errorf("run not found in task projection")
	}
	return contracts.TaskDTO{
			ID: response.Task.ID, ConversationID: response.Task.ConversationID,
			Title: response.Task.Title, UserGoal: response.Task.UserGoal,
			Status: response.Task.Status, WorkspacePath: response.Task.WorkspacePath,
			WorkspaceID: response.Task.WorkspaceID, ActiveRunID: response.Task.ActiveRunID,
			CreatedAt: response.Task.CreatedAt, UpdatedAt: response.Task.UpdatedAt,
		}, contracts.AgentRunDTO{
			ID: selected.ID, TaskID: selected.TaskID, AgentID: selected.AgentID,
			Mode: selected.Mode, Status: selected.Status,
			CreatedAt: selected.CreatedAt, UpdatedAt: selected.UpdatedAt,
		}, nil
}
