package middleware

import (
	"encoding/json"
	"net/http"
	"os"
	"strings"
)

// allowedOrigins 返回允许的 Origin 白名单。
// 本地开发默认允许 localhost:5173；可通过 JARVIS_CORS_ORIGINS 以逗号分隔配置。
func allowedOrigins() []string {
	raw := os.Getenv("JARVIS_CORS_ORIGINS")
	if raw == "" {
		raw = "http://localhost:5173,http://127.0.0.1:5173"
	}
	return splitAndTrim(raw, ",")
}

func splitAndTrim(s, sep string) []string {
	parts := strings.Split(s, sep)
	result := make([]string, 0, len(parts))
	for _, p := range parts {
		trimmed := strings.TrimSpace(p)
		if trimmed != "" {
			result = append(result, trimmed)
		}
	}
	return result
}

// isOriginAllowed 检查 origin 是否在白名单中。
// 无 Origin 头的本地非浏览器请求（如 curl）按允许处理。
func isOriginAllowed(origin string, allowed []string) bool {
	if origin == "" {
		return true // 非浏览器请求
	}
	for _, a := range allowed {
		if strings.EqualFold(a, origin) {
			return true
		}
	}
	return false
}

// CORS 返回受限 Origin 白名单的 CORS middleware。
//
// 不允许 Access-Control-Allow-Origin: *。
// 只反射白名单中的 Origin。
// OPTIONS 预检返回正确结果。
func CORS(next http.Handler) http.Handler {
	allowed := allowedOrigins()

	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		origin := r.Header.Get("Origin")
		w.Header().Add("Vary", "Origin")

		if !isOriginAllowed(origin, allowed) {
			writeOriginForbidden(w)
			return
		}

		if origin != "" {
			w.Header().Set("Access-Control-Allow-Origin", origin)
		}
		w.Header().Set("Access-Control-Allow-Methods", "GET, POST, PUT, PATCH, DELETE, OPTIONS")
		w.Header().Set("Access-Control-Allow-Headers", "Content-Type, Authorization")
		w.Header().Set("Access-Control-Expose-Headers", "Content-Type")

		if r.Method == http.MethodOptions {
			w.WriteHeader(http.StatusNoContent)
			return
		}

		next.ServeHTTP(w, r)
	})
}

func writeOriginForbidden(w http.ResponseWriter) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(http.StatusForbidden)
	_ = json.NewEncoder(w).Encode(map[string]interface{}{
		"ok": false,
		"error": map[string]interface{}{
			"code":        "ORIGIN_NOT_ALLOWED",
			"message":     "请求来源不在允许列表中",
			"category":    "permission",
			"recoverable": false,
		},
	})
}
