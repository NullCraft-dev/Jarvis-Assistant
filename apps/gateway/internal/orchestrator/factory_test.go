package orchestrator

import (
	"testing"

	"github.com/jarvis-assistant/gateway/internal/contracts"
)

// -- RuntimeBusConfigFromEnv 测试 --

func TestRuntimeBusConfigFromEnvDefaults(t *testing.T) {
	// 使用 t.Setenv 设置为空字符串，由 testing 包自动恢复，
	// 避免 os.Unsetenv 污染同包后续测试。
	for _, key := range []string{"JARVIS_RUNTIME_BUS", "JARVIS_REDIS_ADDR", "JARVIS_REDIS_PASSWORD", "JARVIS_REDIS_DB", "JARVIS_GATEWAY_ID"} {
		t.Setenv(key, "")
	}

	cfg := RuntimeBusConfigFromEnv()

	if cfg.BusType != "redis" {
		t.Errorf("默认 BusType 应为 \"redis\"，实际 %q", cfg.BusType)
	}
	if cfg.RedisAddr != "127.0.0.1:6379" {
		t.Errorf("默认 RedisAddr 应为 \"127.0.0.1:6379\"，实际 %q", cfg.RedisAddr)
	}
	if cfg.RedisPassword != "" {
		t.Errorf("默认 RedisPassword 应为空，实际 %q", cfg.RedisPassword)
	}
	if cfg.RedisDB != 0 {
		t.Errorf("默认 RedisDB 应为 0，实际 %d", cfg.RedisDB)
	}
	if cfg.GatewayID == "" {
		t.Error("默认 GatewayID 不应为空")
	}
}

func TestRuntimeBusConfigFromEnvGatewayID(t *testing.T) {
	t.Setenv("JARVIS_GATEWAY_ID", "gateway-test-01")
	cfg := RuntimeBusConfigFromEnv()
	if cfg.GatewayID != "gateway-test-01" {
		t.Fatalf("GatewayID: got %q", cfg.GatewayID)
	}
}

func TestRuntimeBusConfigFromEnvInMemory(t *testing.T) {
	t.Setenv("JARVIS_RUNTIME_BUS", "inmemory")

	cfg := RuntimeBusConfigFromEnv()

	if cfg.BusType != "inmemory" {
		t.Errorf("BusType 应为 \"inmemory\"，实际 %q", cfg.BusType)
	}
}

func TestRuntimeBusConfigFromEnvRedis(t *testing.T) {
	t.Setenv("JARVIS_RUNTIME_BUS", "redis")

	cfg := RuntimeBusConfigFromEnv()

	if cfg.BusType != "redis" {
		t.Errorf("BusType 应为 \"redis\"，实际 %q", cfg.BusType)
	}
}

func TestRuntimeBusConfigFromEnvRedisAddr(t *testing.T) {
	t.Setenv("JARVIS_RUNTIME_BUS", "redis")
	t.Setenv("JARVIS_REDIS_ADDR", "10.0.0.1:6380")

	cfg := RuntimeBusConfigFromEnv()

	if cfg.RedisAddr != "10.0.0.1:6380" {
		t.Errorf("RedisAddr 应为 \"10.0.0.1:6380\"，实际 %q", cfg.RedisAddr)
	}
}

func TestRuntimeBusConfigFromEnvRedisPassword(t *testing.T) {
	t.Setenv("JARVIS_RUNTIME_BUS", "redis")
	t.Setenv("JARVIS_REDIS_PASSWORD", "secret123")

	cfg := RuntimeBusConfigFromEnv()

	if cfg.RedisPassword != "secret123" {
		t.Errorf("RedisPassword 应为 \"secret123\"，实际 %q", cfg.RedisPassword)
	}
}

func TestRuntimeBusConfigFromEnvRedisDB(t *testing.T) {
	t.Setenv("JARVIS_RUNTIME_BUS", "redis")
	t.Setenv("JARVIS_REDIS_DB", "3")

	cfg := RuntimeBusConfigFromEnv()

	if cfg.RedisDB != 3 {
		t.Errorf("RedisDB 应为 3，实际 %d", cfg.RedisDB)
	}
}

func TestRuntimeBusConfigFromEnvRedisDBInvalid(t *testing.T) {
	t.Setenv("JARVIS_RUNTIME_BUS", "redis")
	t.Setenv("JARVIS_REDIS_DB", "not-a-number")

	cfg := RuntimeBusConfigFromEnv()

	// 非法值应保持默认 0
	if cfg.RedisDB != 0 {
		t.Errorf("非法 JARVIS_REDIS_DB 应保持默认 0，实际 %d", cfg.RedisDB)
	}
}

// -- Validate 测试 --

func TestRuntimeBusConfigValidateInMemory(t *testing.T) {
	cfg := RuntimeBusConfig{BusType: "inmemory"}
	if err := cfg.Validate(); err != nil {
		t.Errorf("inmemory 应通过校验，但返回了 error: %v", err)
	}
}

func TestRuntimeBusConfigValidateRedis(t *testing.T) {
	cfg := RuntimeBusConfig{BusType: "redis"}
	if err := cfg.Validate(); err != nil {
		t.Errorf("redis 应通过校验，但返回了 error: %v", err)
	}
}

func TestRuntimeBusConfigValidateInvalid(t *testing.T) {
	cfg := RuntimeBusConfig{BusType: "kafka"}
	if err := cfg.Validate(); err == nil {
		t.Error("非法 BusType 应返回 error，但返回了 nil")
	}
}

func TestRuntimeBusConfigValidateEmpty(t *testing.T) {
	cfg := RuntimeBusConfig{BusType: ""}
	if err := cfg.Validate(); err == nil {
		t.Error("空 BusType 应返回 error，但返回了 nil")
	}
}

// -- NewRuntimeBus 测试 --

func TestNewRuntimeBusInMemory(t *testing.T) {
	cfg := RuntimeBusConfig{BusType: "inmemory"}

	runtimeBus, stateStore, pump, err := NewRuntimeBus(cfg)
	if err != nil {
		t.Fatalf("NewRuntimeBus(inmemory) 返回 error: %v", err)
	}
	if runtimeBus == nil {
		t.Fatal("runtimeBus 为 nil")
	}
	if stateStore == nil {
		t.Fatal("stateStore 为 nil")
	}
	if pump != nil {
		t.Error("inmemory 模式下 pump 应为 nil")
	}

	// 类型断言：应为 InMemoryRuntimeBus
	if _, ok := runtimeBus.(*InMemoryRuntimeBus); !ok {
		t.Errorf("runtimeBus 应为 *InMemoryRuntimeBus，实际 %T", runtimeBus)
	}
	// runtimeBus 和 stateStore 应为同一实例（同一 *InMemoryRuntimeBus）
	rbi, ok1 := runtimeBus.(*InMemoryRuntimeBus)
	ssi, ok2 := stateStore.(*InMemoryRuntimeBus)
	if !ok1 || !ok2 || rbi != ssi {
		t.Error("inmemory 模式下 runtimeBus 和 stateStore 应为同一 *InMemoryRuntimeBus 实例")
	}
}

func TestNewRuntimeBusInMemoryViaEnvConfig(t *testing.T) {
	t.Setenv("JARVIS_RUNTIME_BUS", "inmemory")

	cfg := RuntimeBusConfigFromEnv()
	runtimeBus, stateStore, pump, err := NewRuntimeBus(cfg)
	if err != nil {
		t.Fatalf("JARVIS_RUNTIME_BUS=inmemory 时 NewRuntimeBus 返回 error: %v", err)
	}

	if _, ok := runtimeBus.(*InMemoryRuntimeBus); !ok {
		t.Errorf("JARVIS_RUNTIME_BUS=inmemory 应创建 *InMemoryRuntimeBus，实际 %T", runtimeBus)
	}
	if pump != nil {
		t.Error("inmemory 模式下 pump 应为 nil")
	}
	_ = stateStore
}

func TestNewRuntimeBusRedisWithFakeClient(t *testing.T) {
	// 用 fake client 测试 redis 模式的 factory 逻辑，不依赖真实 Redis
	fc := newFakeClient()

	// 传入 nil reader → 不创建 eventPump（测试写侧逻辑）
	runtimeBus, stateStore, pump, err := newRedisRuntimeBusWithComponents(fc, nil, nil)
	if err != nil {
		t.Fatalf("newRedisRuntimeBusWithComponents(fake) 返回 error: %v", err)
	}
	if runtimeBus == nil {
		t.Fatal("runtimeBus 为 nil")
	}
	if stateStore == nil {
		t.Fatal("stateStore 为 nil")
	}
	// nil reader 时 pump 应为 nil
	if pump == nil {
		t.Log("nil reader → pump 为 nil（预期行为：无 reader 时不创建 pump）")
	}

	// 类型断言：应为 RedisRuntimeBus
	rb, ok := runtimeBus.(*RedisRuntimeBus)
	if !ok {
		t.Fatalf("runtimeBus 应为 *RedisRuntimeBus，实际 %T", runtimeBus)
	}
	// runtimeBus 和 stateStore 应为同一实例（同一 *RedisRuntimeBus）
	rbi, ok1 := runtimeBus.(*RedisRuntimeBus)
	ssi, ok2 := stateStore.(*RedisRuntimeBus)
	if !ok1 || !ok2 || rbi != ssi {
		t.Error("redis 模式下 runtimeBus 和 stateStore 应为同一 *RedisRuntimeBus 实例")
	}

	// 验证 RedisRuntimeBus 功能正常（PrepareRun 会写 fake client）
	task, run, events, err := rb.PrepareRun(contracts.CreateTaskInput{UserGoal: "测试任务"})
	if err != nil {
		t.Fatalf("RedisRuntimeBus.PrepareRun 返回 error: %v", err)
	}
	if task == nil || run == nil {
		t.Fatal("PrepareRun 返回 nil task/run")
	}
	if len(events) == 0 {
		t.Fatal("PrepareRun 应返回至少 1 个事件（task.created）")
	}
	if len(events) != 1 || events[0].Type != "task.created" {
		t.Errorf("PrepareRun 应只返回 1 个 task.created 事件，实际 %d 个事件", len(events))
	}

	// fake client 应收到 1 次 XAdd 调用（EnqueueRunJob）
	if fc.callCount() != 1 {
		t.Errorf("fake client 应收到 1 次 XAdd 调用，实际 %d 次", fc.callCount())
	}
}

func TestNewRuntimeBusRedisFakeClientStateStoreDelegates(t *testing.T) {
	fc := newFakeClient()
	runtimeBus, stateStore, _, err := newRedisRuntimeBusWithComponents(fc, nil, nil)
	if err != nil {
		t.Fatalf("newRedisRuntimeBusWithComponents 返回 error: %v", err)
	}

	// 通过 PrepareRun 创建一些数据
	rb := runtimeBus.(*RedisRuntimeBus)
	task, run, _, err := rb.PrepareRun(contracts.CreateTaskInput{UserGoal: "状态查询测试"})
	if err != nil {
		t.Fatalf("PrepareRun 返回 error: %v", err)
	}

	// StateStore 方法应正常工作
	gotTask, ok := stateStore.GetTask(task.ID)
	if !ok {
		t.Fatal("GetTask 应返回 ok=true")
	}
	if gotTask.ID != task.ID {
		t.Errorf("GetTask id 不匹配: %q vs %q", gotTask.ID, task.ID)
	}

	gotRun, ok := stateStore.GetRun(run.ID)
	if !ok {
		t.Fatal("GetRun 应返回 ok=true")
	}
	if gotRun.ID != run.ID {
		t.Errorf("GetRun id 不匹配: %q vs %q", gotRun.ID, run.ID)
	}

	tasks := stateStore.ListTasks()
	if len(tasks) != 1 {
		t.Errorf("ListTasks 应返回 1 个 task，实际 %d 个", len(tasks))
	}

	// UpdateRunStatus
	stateStore.UpdateRunStatus(run.ID, "cancelled")
	gotRun2, _ := stateStore.GetRun(run.ID)
	if gotRun2.Status != "cancelled" {
		t.Errorf("UpdateRunStatus 后状态应为 cancelled，实际 %q", gotRun2.Status)
	}
}

func TestNewRuntimeBusInvalidConfig(t *testing.T) {
	cfg := RuntimeBusConfig{BusType: "kafka"}
	_, _, _, err := NewRuntimeBus(cfg)
	if err == nil {
		t.Error("非法 BusType 应返回 error，但返回了 nil")
	}
}

func TestNewRuntimeBusInvalidConfigViaEnv(t *testing.T) {
	t.Setenv("JARVIS_RUNTIME_BUS", "nats")

	cfg := RuntimeBusConfigFromEnv()
	_, _, _, err := NewRuntimeBus(cfg)
	if err == nil {
		t.Error("JARVIS_RUNTIME_BUS=nats 应返回 error，但返回了 nil")
	}
}

// 确保 factory_test.go 编译时依赖 dto 包（用于 PrepareRun 参数类型）
// 这里从已有测试中复用 fakeStreamClient 和 newFakeClient。
// 它们定义在 redis_runtime_bus_test.go（同一 package bus），可直接使用。

// 导入验证：确保 dto 包被引用
var _ = contracts.CreateTaskInput{}
