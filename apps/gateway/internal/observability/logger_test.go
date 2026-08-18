package observability

import (
	"bytes"
	"context"
	"log/slog"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

// ── Formatter: 固定列格式 ──────────────────────────────────────────

func TestInfoLineHasFixedColumns(t *testing.T) {
	var buf bytes.Buffer
	handler := NewJarvisHandler(&buf, "gateway/gateway-01", false, slog.LevelDebug)
	logger := slog.New(handler)

	logger.Info("测试消息", "trace_id", "tr_1")

	out := buf.String()
	parts := strings.SplitN(out, " | ", 7)
	if len(parts) != 7 {
		t.Fatalf("期望 7 列，实际 %d: %s", len(parts), out)
	}

	// 级别列
	if strings.TrimSpace(parts[1]) != "INFO" {
		t.Errorf("级别列: 期望 INFO, 实际 %q", parts[1])
	}
	// 服务/实例列
	if parts[2] != "gateway/gateway-01" {
		t.Errorf("服务列: 期望 gateway/gateway-01, 实际 %q", parts[2])
	}
	// 消息列
	if !strings.Contains(parts[6], "测试消息") {
		t.Errorf("消息列不含'测试消息': %q", parts[6])
	}
}

func TestErrorLevelCorrect(t *testing.T) {
	var buf bytes.Buffer
	handler := NewJarvisHandler(&buf, "gateway/gateway-01", false, slog.LevelDebug)
	logger := slog.New(handler)

	logger.Error("故障")

	out := buf.String()
	parts := strings.SplitN(out, " | ", 7)
	if strings.TrimSpace(parts[1]) != "ERROR" {
		t.Errorf("期望 ERROR, 实际 %q", parts[1])
	}
}

func TestWarnLevelCorrect(t *testing.T) {
	var buf bytes.Buffer
	handler := NewJarvisHandler(&buf, "gateway/gateway-01", false, slog.LevelDebug)
	logger := slog.New(handler)

	logger.Warn("警告")

	out := buf.String()
	parts := strings.SplitN(out, " | ", 7)
	if strings.TrimSpace(parts[1]) != "WARN" {
		t.Errorf("期望 WARN, 实际 %q", parts[1])
	}
}

func TestDebugLevelFilteredWhenLevelInfo(t *testing.T) {
	// INFO 级别时 DEBUG 消息不被记录
	var buf bytes.Buffer
	handler := NewJarvisHandler(&buf, "gateway/gateway-01", false, slog.LevelInfo)
	logger := slog.New(handler)

	logger.Debug("调试")
	if buf.Len() != 0 {
		t.Error("INFO 级别不应记录 DEBUG 消息")
	}
}

// ── 上下文 ─────────────────────────────────────────────────────────

func TestMissingContextShowsDash(t *testing.T) {
	var buf bytes.Buffer
	handler := NewJarvisHandler(&buf, "gateway/gateway-01", false, slog.LevelDebug)
	logger := slog.New(handler)

	logger.Info("消息")

	out := buf.String()
	parts := strings.SplitN(out, " | ", 7)
	ctx := parts[5]
	if !strings.Contains(ctx, "trace=-") {
		t.Error("缺少 trace_id 时应显示 trace=-")
	}
	if !strings.Contains(ctx, "request=-") {
		t.Error("缺少 request_id 时应显示 request=-")
	}
}

func TestContextFromAttrs(t *testing.T) {
	var buf bytes.Buffer
	handler := NewJarvisHandler(&buf, "gateway/gateway-01", false, slog.LevelDebug)
	logger := slog.New(handler)

	logger.Info("消息", "trace_id", "tr_abc", "request_id", "req_abc", "run_id", "run_xyz")

	out := buf.String()
	parts := strings.SplitN(out, " | ", 7)
	ctx := parts[5]
	if !strings.Contains(ctx, "trace=tr_abc") {
		t.Error("上下文应包含 trace=tr_abc")
	}
	if !strings.Contains(ctx, "request=req_abc") {
		t.Error("上下文应包含 request=req_abc")
	}
	if !strings.Contains(ctx, "run=run_xyz") {
		t.Error("上下文应包含 run=run_xyz")
	}
	if !strings.Contains(ctx, "task=-") {
		t.Error("未设置的 task_id 应显示 -")
	}
}

func TestNonContextAttrsAreRetainedInMessage(t *testing.T) {
	var buf bytes.Buffer
	handler := NewJarvisHandler(&buf, "gateway/gateway-01", false, slog.LevelDebug)
	logger := slog.New(handler)

	logger.Error("Control Plane 请求失败", "error", "connection refused", "status", 503, "duration_ms", 42)

	out := buf.String()
	for _, want := range []string{"error=connection refused", "status=503", "duration_ms=42"} {
		if !strings.Contains(out, want) {
			t.Errorf("日志丢失属性 %q: %s", want, out)
		}
	}
}

func TestSensitiveAttrIsRedacted(t *testing.T) {
	var buf bytes.Buffer
	handler := NewJarvisHandler(&buf, "gateway/gateway-01", false, slog.LevelDebug)
	logger := slog.New(handler)

	logger.Info("请求", "api_key", "sk-super-secret")
	out := buf.String()
	if strings.Contains(out, "sk-super-secret") || !strings.Contains(out, "api_key=***") {
		t.Fatalf("敏感属性未正确脱敏: %s", out)
	}
}

// ── 脱敏 ───────────────────────────────────────────────────────────

func TestSanitizeApiKey(t *testing.T) {
	result := sanitize("请求失败 api_key=sk-abc123def456")
	if strings.Contains(result, "sk-abc123def456") {
		t.Error("敏感值未被脱敏")
	}
	if !strings.Contains(result, "api_key=***") {
		t.Errorf("期望 api_key=***, 实际: %s", result)
	}
}

func TestSanitizeToken(t *testing.T) {
	result := sanitize("使用 token=secret123 认证")
	if strings.Contains(result, "secret123") {
		t.Error("token 值未被脱敏")
	}
}

func TestSanitizeBearerToken(t *testing.T) {
	result := sanitize("Authorization: Bearer eyJhbGciOiJIUzI1NiJ9.token")
	if !strings.Contains(result, "Bearer ***") {
		t.Errorf("期望 Bearer ***, 实际: %s", result)
	}
}

func TestSanitizeSkPrefix(t *testing.T) {
	result := sanitize("使用密钥 sk-proj-abc123def456ghijkl")
	if !strings.Contains(result, "sk-***") {
		t.Errorf("期望 sk-***, 实际: %s", result)
	}
}

func TestNormalMessageUnchanged(t *testing.T) {
	msg := "任务已创建 task_id=abc run_id=xyz"
	result := sanitize(msg)
	if result != msg {
		t.Errorf("普通消息不应被修改: %s", result)
	}
}

// ── 颜色 ───────────────────────────────────────────────────────────

func TestNoAnsiInNonColorMode(t *testing.T) {
	var buf bytes.Buffer
	handler := NewJarvisHandler(&buf, "gateway/gateway-01", false, slog.LevelDebug)
	logger := slog.New(handler)

	logger.Info("消息")

	out := buf.String()
	if strings.Contains(out, "\033[") {
		t.Error("非颜色模式下不应包含 ANSI escape sequence")
	}
}

func TestAnsiInColorMode(t *testing.T) {
	var buf bytes.Buffer
	handler := NewJarvisHandler(&buf, "gateway/gateway-01", true, slog.LevelDebug)
	logger := slog.New(handler)

	logger.Info("消息")

	out := buf.String()
	if !strings.Contains(out, "\033[") {
		t.Error("颜色模式下应包含 ANSI escape sequence")
	}
	if !strings.Contains(out, ansiBlue+"gateway/gateway-01"+ansiReset) {
		t.Error("Gateway 服务字段应使用固定蓝色")
	}
}

// ── 多行消息 ───────────────────────────────────────────────────────

func TestMultilineMessageFlattened(t *testing.T) {
	var buf bytes.Buffer
	handler := NewJarvisHandler(&buf, "gateway/gateway-01", false, slog.LevelDebug)
	logger := slog.New(handler)

	logger.Info("第一行\n第二行")

	out := buf.String()
	if strings.Contains(out, "\n") {
		// 除了最后一个换行（handler 追加的），不应有其他换行
		trimmed := strings.TrimSuffix(out, "\n")
		if strings.Contains(trimmed, "\n") {
			t.Error("消息不应包含换行符")
		}
	}
}

// ── 关联上下文 helper ──────────────────────────────────────────────

func TestContextLogger(t *testing.T) {
	var buf bytes.Buffer
	handler := NewJarvisHandler(&buf, "gateway/gateway-01", false, slog.LevelDebug)
	base := slog.New(handler)

	ctxLogger := ContextLogger(base, CtxFields{
		TraceID: "tr_ctx",
		RunID:   "run_ctx",
	})
	ctxLogger.Info("上下文消息")

	out := buf.String()
	if !strings.Contains(out, "trace=tr_ctx") {
		t.Error("ContextLogger 应包含 trace_id")
	}
}

func TestContextLoggerEmptyFields(t *testing.T) {
	var buf bytes.Buffer
	handler := NewJarvisHandler(&buf, "gateway/gateway-01", false, slog.LevelDebug)
	base := slog.New(handler)

	ctxLogger := ContextLogger(base, CtxFields{})
	ctxLogger.Info("无上下文")

	out := buf.String()
	if !strings.Contains(out, "trace=-") {
		t.Error("空上下文应显示 trace=-")
	}
}

// ── TTY 检测 ───────────────────────────────────────────────────────

func TestNoColorEnv(t *testing.T) {
	t.Setenv("NO_COLOR", "1")
	t.Setenv("JARVIS_LOG_COLOR", "always")

	if useColor() {
		t.Error("NO_COLOR=1 时应优先于强制颜色")
	}
}

func TestForceColorEnv(t *testing.T) {
	t.Setenv("NO_COLOR", "")
	t.Setenv("JARVIS_LOG_COLOR", "always")
	if !useColor() {
		t.Error("JARVIS_LOG_COLOR=always 应在子进程管道输出中强制启用颜色")
	}
}

func TestNeverColorEnv(t *testing.T) {
	t.Setenv("NO_COLOR", "")
	t.Setenv("JARVIS_LOG_COLOR", "never")
	if useColor() {
		t.Error("JARVIS_LOG_COLOR=never 应禁用颜色")
	}
}

func TestServiceColor(t *testing.T) {
	if got := serviceColor("agent-worker/worker-01"); got != ansiMagenta {
		t.Fatalf("Worker 服务色应为洋红，实际 %q", got)
	}
	if got := serviceColor("control-plane/control-plane-01"); got != ansiCyan {
		t.Fatalf("Control Plane 服务色应为青色，实际 %q", got)
	}
}

func TestNormalizeTraceID(t *testing.T) {
	if got := NormalizeTraceID("trace-01:abc"); got != "trace-01:abc" {
		t.Fatalf("合法 trace id 被错误拒绝: %q", got)
	}
	if got := NormalizeTraceID("trace | ERROR | forged"); got != "" {
		t.Fatalf("包含分隔符的 trace id 不应通过: %q", got)
	}
}

func TestContextTraceID(t *testing.T) {
	ctx := WithTraceID(context.Background(), "trace-01")
	if got := TraceIDFromContext(ctx); got != "trace-01" {
		t.Fatalf("context trace id 不正确: %q", got)
	}
}

// ── 日志级别解析 ───────────────────────────────────────────────────

func TestResolveLogLevelDefault(t *testing.T) {
	os.Unsetenv("LOG_LEVEL")
	if lvl := resolveLogLevel(); lvl != slog.LevelInfo {
		t.Errorf("默认级别应为 INFO, 实际 %v", lvl)
	}
}

func TestResolveLogLevelDebug(t *testing.T) {
	os.Setenv("LOG_LEVEL", "DEBUG")
	defer os.Unsetenv("LOG_LEVEL")

	if lvl := resolveLogLevel(); lvl != slog.LevelDebug {
		t.Errorf("期望 DEBUG, 实际 %v", lvl)
	}
}

func TestResolveLogLevelError(t *testing.T) {
	os.Setenv("LOG_LEVEL", "ERROR")
	defer os.Unsetenv("LOG_LEVEL")

	if lvl := resolveLogLevel(); lvl != slog.LevelError {
		t.Errorf("期望 ERROR, 实际 %v", lvl)
	}
}

func TestResolveLogDirFindsProjectRoot(t *testing.T) {
	oldDir, err := os.Getwd()
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = os.Chdir(oldDir) })
	t.Setenv("JARVIS_LOG_DIR", "")
	if err := os.Chdir("../.."); err != nil {
		t.Fatal(err)
	}
	logDir := resolveLogDir()
	projectRoot := filepath.Clean(filepath.Join(oldDir, "..", "..", "..", ".."))
	expected := filepath.Join(projectRoot, ".local", "logs")
	if logDir != expected {
		t.Fatalf("日志目录未解析到项目根目录: got %s, want %s", logDir, expected)
	}
}

// ── Setup ───────────────────────────────────────────────────────────

func TestSetupReturnsLogger(t *testing.T) {
	logger := Setup("gateway", "gateway-01", "gateway.log")
	if logger == nil {
		t.Fatal("Setup 不应返回 nil")
	}
	// 验证 logger 可以正常输出
	logger.Info("setup 测试")
}

// ── TeeHandler ─────────────────────────────────────────────────────

func TestTeeHandler(t *testing.T) {
	var buf1, buf2 bytes.Buffer
	h1 := NewJarvisHandler(&buf1, "svc/1", false, slog.LevelDebug)
	h2 := NewJarvisHandler(&buf2, "svc/1", false, slog.LevelDebug)

	tee := newTeeHandler(h1, h2)
	logger := slog.New(tee)

	logger.Info("fan-out 消息")

	if buf1.Len() == 0 {
		t.Error("handler 1 应接收到消息")
	}
	if buf2.Len() == 0 {
		t.Error("handler 2 应接收到消息")
	}
}

func TestTeeHandlerLevelFiltering(t *testing.T) {
	var buf1, buf2 bytes.Buffer
	h1 := NewJarvisHandler(&buf1, "svc/1", false, slog.LevelInfo)  // INFO+
	h2 := NewJarvisHandler(&buf2, "svc/1", false, slog.LevelDebug) // DEBUG+

	tee := newTeeHandler(h1, h2)
	logger := slog.New(tee)

	logger.Debug("调试", "key", "val")

	// h1 是 INFO 级别，不应记录 DEBUG
	if buf1.Len() != 0 {
		t.Error("INFO 级别的 handler 不应记录 DEBUG 消息")
	}
	// h2 是 DEBUG 级别，应记录
	if buf2.Len() == 0 {
		t.Error("DEBUG 级别的 handler 应记录 DEBUG 消息")
	}
}

// ── 基准 ────────────────────────────────────────────────────────────

func BenchmarkJarvisHandler(b *testing.B) {
	var buf bytes.Buffer
	handler := NewJarvisHandler(&buf, "gateway/gateway-01", false, slog.LevelInfo)
	logger := slog.New(handler)

	b.ResetTimer()
	for i := 0; i < b.N; i++ {
		logger.Info("基准测试消息", "trace_id", "tr_1", "run_id", "run_1")
	}
}

func BenchmarkSanitize(b *testing.B) {
	msg := "请求失败 api_key=sk-abc123def456 token=secret123 password=mypass"
	for i := 0; i < b.N; i++ {
		sanitize(msg)
	}
}

// Ensure context.Background is importable
var _ = context.Background
