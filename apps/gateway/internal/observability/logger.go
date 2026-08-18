// Package logging 提供统一应用日志系统。
//
// 格式：
//
//	时间 | 级别  | 服务/实例 | - | 调用位置 | 关联上下文 | 消息
//
// 同时输出到彩色终端（stderr）和无颜色滚动文件。
// 基于 Go 标准库 log/slog，无第三方依赖。
//
// 职责边界：
//
//	应用日志 → 开发与运行排障（本包）
//	RuntimeEvent → 任务进度、工具调用、权限和产物的用户可见状态
//	AuditLog → 权限、安全与本地影响操作的持久化审计
package observability

import (
	"context"
	"fmt"
	"io"
	"log/slog"
	"os"
	"path/filepath"
	"regexp"
	"runtime"
	"strings"
	"sync"
)

// ── 常量 ──────────────────────────────────────────────────────────

const (
	maxFileBytes  = 20 * 1024 * 1024 // 20 MiB
	maxBackupCnt  = 10
	maxMessageLen = 4096
	maxContextLen = 128
)

// ANSI 颜色代码
const (
	ansiReset   = "\033[0m"
	ansiGreen   = "\033[32m"
	ansiGrey    = "\033[90m"
	ansiCyan    = "\033[36m"
	ansiYellow  = "\033[33m"
	ansiRed     = "\033[31m"
	ansiBlue    = "\033[34m"
	ansiMagenta = "\033[35m"
)

// 敏感键名
var sensitiveKeys = []string{
	"key", "api_key", "apikey", "token", "secret", "password",
	"cookie", "credential", "passwd", "pwd",
	"access_key", "secret_key", "private_key", "api_secret",
}

// 敏感值正则
var sensitivePatterns = []*regexp.Regexp{
	regexp.MustCompile(`(?i)(?:bearer\s+)([a-zA-Z0-9\-._~+/]+=*)`),
	regexp.MustCompile(`(?:sk-)[a-zA-Z0-9\-_]{20,}`),
	regexp.MustCompile(`(?:eyJ)[a-zA-Z0-9\-_]+\.(?:eyJ)[a-zA-Z0-9\-_]+\.(?:[a-zA-Z0-9\-_]+)`),
}

var traceIDPattern = regexp.MustCompile(`^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$`)

// ── 关联上下文 ────────────────────────────────────────────────────

// CtxFields 携带日志关联上下文字段。
type CtxFields struct {
	TraceID   string
	RequestID string
	TaskID    string
	RunID     string
	StepID    string
}

func formatContext(f CtxFields) string {
	return fmt.Sprintf("trace=%s request=%s task=%s run=%s step=%s",
		orDash(f.TraceID, maxContextLen), orDash(f.RequestID, maxContextLen),
		orDash(f.TaskID, maxContextLen),
		orDash(f.RunID, maxContextLen), orDash(f.StepID, maxContextLen))
}

func orDash(s string, maxLen int) string {
	if s == "" {
		return "-"
	}
	return normalizeText(s, maxLen)
}

type traceIDContextKey struct{}
type requestIDContextKey struct{}

// NormalizeTraceID 仅接受可安全写入固定列日志格式的关联 ID。
func NormalizeTraceID(value string) string {
	candidate := strings.TrimSpace(value)
	if traceIDPattern.MatchString(candidate) {
		return candidate
	}
	return ""
}

// WithTraceID 将已校验的 trace id 写入 context，供 Control Plane client 透传。
func WithTraceID(ctx context.Context, traceID string) context.Context {
	return context.WithValue(ctx, traceIDContextKey{}, traceID)
}

// TraceIDFromContext 获取当前请求的 trace id。
func TraceIDFromContext(ctx context.Context) string {
	traceID, _ := ctx.Value(traceIDContextKey{}).(string)
	return NormalizeTraceID(traceID)
}

// WithRequestID 将单次 HTTP 请求 ID 写入 context。
func WithRequestID(ctx context.Context, requestID string) context.Context {
	return context.WithValue(ctx, requestIDContextKey{}, requestID)
}

// RequestIDFromContext 获取当前单次 HTTP 请求 ID。
func RequestIDFromContext(ctx context.Context) string {
	requestID, _ := ctx.Value(requestIDContextKey{}).(string)
	return NormalizeTraceID(requestID)
}

// ── 脱敏 ──────────────────────────────────────────────────────────

func sanitize(msg string) string {
	// 先处理敏感值模式，避免被 key=value 规则先捕获
	msg = sensitivePatterns[0].ReplaceAllString(msg, "Bearer ***")
	msg = sensitivePatterns[1].ReplaceAllString(msg, "sk-***")
	msg = sensitivePatterns[2].ReplaceAllString(msg, "***")

	for _, key := range sensitiveKeys {
		re2 := regexp.MustCompile(fmt.Sprintf(`(?i)(%s[_\w]*)\s*[=:]\s*"([^"]+)"`, key))
		msg = re2.ReplaceAllString(msg, `${1}="***"`)
		re := regexp.MustCompile(fmt.Sprintf(`(?i)(%s[_\w]*)\s*[=:]\s*([^\s,}"'][^\s,}]*)`, key))
		msg = re.ReplaceAllString(msg, "${1}=***")
	}
	return normalizeText(msg, maxMessageLen)
}

func normalizeText(value string, maxLen int) string {
	text := strings.ReplaceAll(value, "\n", " ")
	text = strings.ReplaceAll(text, "\r", " ")
	text = strings.Join(strings.Fields(text), " ")
	// 固定列以 " | " 分隔；替换竖线可阻止外部输入伪造新列。
	text = strings.ReplaceAll(text, "|", "¦")
	if len(text) > maxLen {
		return text[:maxLen-1] + "…"
	}
	return text
}

func isSensitiveKey(key string) bool {
	key = strings.ToLower(key)
	key = key[strings.LastIndex(key, ".")+1:]
	for _, sensitiveKey := range sensitiveKeys {
		if key == sensitiveKey || strings.HasSuffix(key, "_"+sensitiveKey) {
			return true
		}
	}
	return false
}

// ── 日志级别 ──────────────────────────────────────────────────────

var levelNames = map[slog.Level]string{
	slog.LevelDebug: "DEBUG",
	slog.LevelInfo:  "INFO ",
	slog.LevelWarn:  "WARN ",
	slog.LevelError: "ERROR",
}

// ── 旋转文件 ──────────────────────────────────────────────────────

type rotatingFile struct {
	mu          sync.Mutex
	path        string
	file        *os.File
	currentSize int64
	maxSize     int64
	maxBackups  int
}

func openRotatingFile(dir, baseName string, maxSize int64, maxBackups int) (*rotatingFile, error) {
	rf := &rotatingFile{
		path:       filepath.Join(dir, baseName),
		maxSize:    maxSize,
		maxBackups: maxBackups,
	}
	if err := rf.reopen(); err != nil {
		return nil, err
	}
	return rf, nil
}

func (rf *rotatingFile) reopen() error {
	f, err := os.OpenFile(rf.path, os.O_CREATE|os.O_APPEND|os.O_WRONLY, 0o644)
	if err != nil {
		return err
	}
	info, err := f.Stat()
	if err != nil {
		f.Close()
		return err
	}
	if rf.file != nil {
		rf.file.Close()
	}
	rf.file = f
	rf.currentSize = info.Size()
	return nil
}

func (rf *rotatingFile) Write(p []byte) (int, error) {
	rf.mu.Lock()
	defer rf.mu.Unlock()

	if rf.currentSize+int64(len(p)) > rf.maxSize {
		if err := rf.rotate(); err != nil {
			fmt.Fprintf(os.Stderr, "[logging] 日志滚动失败: %v\n", err)
		}
	}

	n, err := rf.file.Write(p)
	if err == nil {
		rf.currentSize += int64(n)
	}
	return n, err
}

func (rf *rotatingFile) rotate() error {
	if err := rf.file.Close(); err != nil {
		return err
	}

	for i := rf.maxBackups - 1; i >= 0; i-- {
		var oldPath, newPath string
		if i == 0 {
			oldPath = rf.path
		} else {
			oldPath = fmt.Sprintf("%s.%d", rf.path, i)
		}
		newPath = fmt.Sprintf("%s.%d", rf.path, i+1)
		os.Rename(oldPath, newPath) // 忽略错误（文件可能不存在）
	}

	return rf.reopen()
}

func (rf *rotatingFile) Close() error {
	rf.mu.Lock()
	defer rf.mu.Unlock()
	if rf.file != nil {
		return rf.file.Close()
	}
	return nil
}

// ── JarvisHandler ─────────────────────────────────────────────────

// JarvisHandler 实现 slog.Handler，输出统一格式日志。
type JarvisHandler struct {
	mu              sync.Mutex
	w               io.Writer
	useColor        bool
	serviceInstance string
	level           slog.Level
	attrs           []slog.Attr
}

// NewJarvisHandler 创建 JarvisHandler。
func NewJarvisHandler(w io.Writer, serviceInstance string, useColor bool, level slog.Level) *JarvisHandler {
	return &JarvisHandler{
		w:               w,
		useColor:        useColor,
		serviceInstance: serviceInstance,
		level:           level,
	}
}

// Enabled 判断是否记录该级别日志。
func (h *JarvisHandler) Enabled(_ context.Context, level slog.Level) bool {
	return level >= h.level
}

// Handle 格式化并输出一条日志记录。
func (h *JarvisHandler) Handle(_ context.Context, r slog.Record) error {
	h.mu.Lock()
	defer h.mu.Unlock()

	// 时间（使用 Record.Time 保留原始时间）
	t := r.Time
	ts := t.Format("2006-01-02 15:04:05") + fmt.Sprintf(".%03d", t.Nanosecond()/1_000_000)

	// 级别（固定宽度 5）
	lvl := "INFO "
	if name, ok := levelNames[r.Level]; ok {
		lvl = name
	}

	// 执行上下文（Go 不获取 goroutine ID）
	execCtx := "-"

	// 调用位置
	caller := "?:0"
	if r.PC != 0 {
		frames := runtime.CallersFrames([]uintptr{r.PC})
		frame, _ := frames.Next()
		if frame.Function != "" {
			caller = frame.File[strings.LastIndex(frame.File, "/")+1:] +
				"/" + frame.Function[strings.LastIndex(frame.Function, "/")+1:] +
				fmt.Sprintf(":%d", frame.Line)
		}
	}

	// 关联上下文：合并 handler attrs 和 record attrs；其他属性保留在消息列，
	// 否则 error、HTTP status、duration 等排障信息会被静默丢弃。
	ctxF := CtxFields{}
	extras := make([]string, 0, len(h.attrs)+r.NumAttrs())
	collectAttrs(h.attrs, &ctxF, &extras, "")
	// Record 级别 attrs 覆盖 handler 级别上下文，其他属性追加到消息列。
	r.Attrs(func(a slog.Attr) bool {
		collectAttrs([]slog.Attr{a}, &ctxF, &extras, "")
		return true
	})
	ctxStr := formatContext(ctxF)

	// 消息（单行化 + 脱敏）
	msg := r.Message
	msg = sanitize(msg)
	if len(extras) > 0 {
		msg += " " + strings.Join(extras, " ")
	}

	// 组装
	line := fmt.Sprintf(
		"%s | %s | %s | %s | %s | %s | %s",
		ts, lvl, h.serviceInstance, execCtx, caller, ctxStr, msg,
	)

	if h.useColor {
		line = h.applyColor(line, r.Level)
	}

	_, err := io.WriteString(h.w, line+"\n")
	return err
}

func collectAttrs(attrs []slog.Attr, ctxF *CtxFields, extras *[]string, prefix string) {
	for _, attr := range attrs {
		value := attr.Value.Resolve()
		key := prefix + attr.Key
		if value.Kind() == slog.KindGroup {
			groupPrefix := key
			if groupPrefix != "" {
				groupPrefix += "."
			}
			collectAttrs(value.Group(), ctxF, extras, groupPrefix)
			continue
		}

		formattedValue := formatAttrValue(key, value)
		switch key {
		case "trace_id":
			ctxF.TraceID = formattedValue
		case "request_id":
			ctxF.RequestID = formattedValue
		case "task_id":
			ctxF.TaskID = formattedValue
		case "run_id":
			ctxF.RunID = formattedValue
		case "step_id":
			ctxF.StepID = formattedValue
		default:
			*extras = append(*extras, normalizeText(key, maxContextLen)+"="+formattedValue)
		}
	}
}

func formatAttrValue(key string, value slog.Value) string {
	if isSensitiveKey(key) {
		return "***"
	}
	return sanitize(fmt.Sprint(value.Any()))
}

// WithAttrs 返回携带额外属性的 handler。
func (h *JarvisHandler) WithAttrs(attrs []slog.Attr) slog.Handler {
	newAttrs := make([]slog.Attr, len(h.attrs)+len(attrs))
	copy(newAttrs, h.attrs)
	copy(newAttrs[len(h.attrs):], attrs)
	return &JarvisHandler{
		w:               h.w,
		useColor:        h.useColor,
		serviceInstance: h.serviceInstance,
		level:           h.level,
		attrs:           newAttrs,
	}
}

// WithGroup 不支持分组，返回自身。
func (h *JarvisHandler) WithGroup(name string) slog.Handler {
	return h
}

// ── 颜色 ──────────────────────────────────────────────────────────

func (h *JarvisHandler) applyColor(line string, level slog.Level) string {
	parts := strings.SplitN(line, " | ", 7)
	if len(parts) < 7 {
		return line
	}

	parts[0] = ansiGreen + parts[0] + ansiReset

	switch {
	case level >= slog.LevelError:
		parts[1] = ansiRed + parts[1] + ansiReset
	case level >= slog.LevelWarn:
		parts[1] = ansiYellow + parts[1] + ansiReset
	case level >= slog.LevelInfo:
		parts[1] = ansiCyan + parts[1] + ansiReset
	default:
		parts[1] = ansiGrey + parts[1] + ansiReset
	}

	parts[2] = serviceColor(h.serviceInstance) + parts[2] + ansiReset
	parts[3] = ansiGrey + parts[3] + ansiReset
	parts[5] = ansiGrey + parts[5] + ansiReset
	parts[4] = ansiBlue + parts[4] + ansiReset

	return strings.Join(parts, " | ")
}

func serviceColor(serviceInstance string) string {
	serviceName := strings.SplitN(serviceInstance, "/", 2)[0]
	switch serviceName {
	case "gateway":
		return ansiBlue
	case "control-plane":
		return ansiCyan
	case "agent-worker":
		return ansiMagenta
	default:
		return ansiGrey
	}
}

// ── 初始化 ─────────────────────────────────────────────────────────

func resolveLogLevel() slog.Level {
	switch strings.ToUpper(os.Getenv("LOG_LEVEL")) {
	case "DEBUG":
		return slog.LevelDebug
	case "WARN", "WARNING":
		return slog.LevelWarn
	case "ERROR":
		return slog.LevelError
	default:
		return slog.LevelInfo
	}
}

// Setup 初始化统一日志系统。
//
// 返回 *slog.Logger。同时输出到彩色终端（stderr）和滚动文件。
// 文件输出失败时仅 stderr 告警，不阻塞启动。
//
// 参数：
//   - serviceName: "gateway" / "control-plane" / "agent-worker"
//   - defaultInstanceID: 未设置 JARVIS_INSTANCE_ID 时的默认值
//   - logBaseName: 日志文件名，如 "gateway.log"
func Setup(serviceName, defaultInstanceID, logBaseName string) *slog.Logger {
	level := resolveLogLevel()

	instanceID := os.Getenv("JARVIS_INSTANCE_ID")
	if instanceID == "" {
		instanceID = defaultInstanceID
	}
	serviceInstance := serviceName + "/" + instanceID

	colorEnabled := useColor()

	// ── 文件 handler（无颜色，滚动）──
	var fileHandler slog.Handler
	logDir := resolveLogDir()
	if err := os.MkdirAll(logDir, 0o755); err == nil {
		rf, err := openRotatingFile(logDir, logBaseName, maxFileBytes, maxBackupCnt)
		if err == nil {
			fileHandler = NewJarvisHandler(rf, serviceInstance, false, level)
		} else {
			fmt.Fprintf(os.Stderr, "[logging] 无法创建日志文件 %s/%s: %v\n", logDir, logBaseName, err)
		}
	} else {
		fmt.Fprintf(os.Stderr, "[logging] 无法创建日志目录 %s: %v\n", logDir, err)
	}

	// ── 控制台 handler（彩色，stderr）──
	consoleHandler := NewJarvisHandler(os.Stderr, serviceInstance, colorEnabled, level)

	// ── fan-out：合并两个 handler ──
	var handler slog.Handler
	if fileHandler != nil {
		handler = newTeeHandler(consoleHandler, fileHandler)
	} else {
		handler = consoleHandler
	}

	logger := slog.New(handler)

	fileStatus := "disabled"
	if fileHandler != nil {
		fileStatus = filepath.Join(logDir, logBaseName)
	}

	logger.Info("日志系统已初始化",
		"level", level.String(),
		"instance", instanceID,
		"console_color", colorEnabled,
		"file", fileStatus,
	)

	return logger
}

// ── TeeHandler（fan-out 到多个 handler）───────────────────────────
func newTeeHandler(handlers ...slog.Handler) slog.Handler {
	return &teeHandler{handlers: handlers}
}

type teeHandler struct {
	handlers []slog.Handler
}

func (h *teeHandler) Enabled(ctx context.Context, level slog.Level) bool {
	for _, handler := range h.handlers {
		if handler.Enabled(ctx, level) {
			return true
		}
	}
	return false
}

func (h *teeHandler) Handle(ctx context.Context, r slog.Record) error {
	var lastErr error
	for _, handler := range h.handlers {
		if handler.Enabled(ctx, r.Level) {
			// 每个 handler 需要独立的 Record 副本
			r2 := r
			r2.Time = r.Time // 时间字段不会被 Clone 影响
			if err := handler.Handle(ctx, r2); err != nil {
				lastErr = err
			}
		}
	}
	return lastErr
}

func (h *teeHandler) WithAttrs(attrs []slog.Attr) slog.Handler {
	handlers := make([]slog.Handler, len(h.handlers))
	for i, handler := range h.handlers {
		handlers[i] = handler.WithAttrs(attrs)
	}
	return &teeHandler{handlers: handlers}
}

func (h *teeHandler) WithGroup(name string) slog.Handler {
	handlers := make([]slog.Handler, len(h.handlers))
	for i, handler := range h.handlers {
		handlers[i] = handler.WithGroup(name)
	}
	return &teeHandler{handlers: handlers}
}

// ── 辅助 ──────────────────────────────────────────────────────────

func useColor() bool {
	if os.Getenv("NO_COLOR") != "" {
		return false
	}

	switch strings.ToLower(strings.TrimSpace(os.Getenv("JARVIS_LOG_COLOR"))) {
	case "always", "force", "1", "true":
		return true
	case "never", "0", "false":
		return false
	default:
		return isTerminal()
	}
}

func isTerminal() bool {
	stat, err := os.Stderr.Stat()
	if err != nil {
		return false
	}
	return (stat.Mode() & os.ModeCharDevice) != 0
}

func resolveLogDir() string {
	if d := os.Getenv("JARVIS_LOG_DIR"); d != "" {
		return d
	}
	cwd, err := os.Getwd()
	if err != nil {
		return filepath.Join(".local", "logs")
	}
	return filepath.Join(findProjectRoot(cwd), ".local", "logs")
}

func findProjectRoot(start string) string {
	dir, err := filepath.Abs(start)
	if err != nil {
		return start
	}
	for {
		if info, err := os.Stat(filepath.Join(dir, "compose.yaml")); err == nil && !info.IsDir() {
			return dir
		}
		parent := filepath.Dir(dir)
		if parent == dir {
			return start
		}
		dir = parent
	}
}

// ContextLogger 创建带上"下文字段的子 logger。
func ContextLogger(base *slog.Logger, fields CtxFields) *slog.Logger {
	var args []any
	if fields.TraceID != "" {
		args = append(args, "trace_id", fields.TraceID)
	}
	if fields.RequestID != "" {
		args = append(args, "request_id", fields.RequestID)
	}
	if fields.TaskID != "" {
		args = append(args, "task_id", fields.TaskID)
	}
	if fields.RunID != "" {
		args = append(args, "run_id", fields.RunID)
	}
	if fields.StepID != "" {
		args = append(args, "step_id", fields.StepID)
	}
	if len(args) == 0 {
		return base
	}
	return base.With(args...)
}

// Default 返回零配置 logger（仅 stderr，INFO 级别），用于测试或 fallback。
func Default() *slog.Logger {
	handler := NewJarvisHandler(os.Stderr, "gateway/gateway-01", false, slog.LevelInfo)
	return slog.New(handler)
}
