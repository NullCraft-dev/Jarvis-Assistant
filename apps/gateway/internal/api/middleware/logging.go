package middleware

import (
	"log/slog"
	"net/http"
	"time"

	"github.com/google/uuid"
	observability "github.com/jarvis-assistant/gateway/internal/observability"
)

// Logging 记录 HTTP 请求日志。
//
// 记录字段：method、path、status、duration、trace_id。
// 不记录 query 中的敏感信息或请求体。
// 为每个请求生成 trace_id 并注入 context。
func Logging(next http.Handler) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		start := time.Now()

		// trace_id 关联端到端操作；request_id 只标识当前 HTTP 请求。
		requestID := observability.NormalizeTraceID(r.Header.Get("X-Request-ID"))
		if requestID == "" {
			requestID = uuid.NewString()
		}
		traceID := observability.NormalizeTraceID(r.Header.Get("X-Trace-ID"))
		if _, err := uuid.Parse(traceID); err != nil {
			if _, requestErr := uuid.Parse(requestID); requestErr == nil {
				traceID = requestID
			} else {
				traceID = uuid.NewString()
			}
		}
		ctx := observability.WithTraceID(r.Context(), traceID)
		ctx = observability.WithRequestID(ctx, requestID)
		r = r.WithContext(ctx)
		r.Header.Set("X-Trace-ID", traceID)
		r.Header.Set("X-Request-ID", requestID)
		w.Header().Set("X-Trace-ID", traceID)
		w.Header().Set("X-Request-ID", requestID)

		// 包装 ResponseWriter 以捕获状态码
		wrapped := &responseWriter{ResponseWriter: w, statusCode: http.StatusOK}

		next.ServeHTTP(wrapped, r)

		duration := time.Since(start)

		// 成功请求保留在 DEBUG，避免浏览器刷新、轮询和 SSE 重连淹没
		// Gateway 生命周期与周期健康摘要。客户端错误保留 INFO，服务端
		// 错误提升为 ERROR，默认 INFO 视图仍可直接看到异常。
		logFn := slog.DebugContext
		if wrapped.statusCode >= http.StatusInternalServerError {
			logFn = slog.ErrorContext
		} else if wrapped.statusCode >= http.StatusBadRequest {
			logFn = slog.InfoContext
		}
		logFn(r.Context(), "HTTP",
			"trace_id", traceID,
			"request_id", requestID,
			"method", r.Method,
			"path", r.URL.Path,
			"status", wrapped.statusCode,
			"duration_ms", duration.Milliseconds(),
		)
	})
}

// responseWriter 包装 http.ResponseWriter 以捕获状态码。
type responseWriter struct {
	http.ResponseWriter
	statusCode int
}

func (rw *responseWriter) WriteHeader(code int) {
	rw.statusCode = code
	rw.ResponseWriter.WriteHeader(code)
}

// Flush 保留底层 ResponseWriter 的 SSE 刷新能力。
// Logging middleware 不能因为包装响应而让 RunHandler 无法断言 http.Flusher。
func (rw *responseWriter) Flush() {
	if flusher, ok := rw.ResponseWriter.(http.Flusher); ok {
		flusher.Flush()
	}
}
