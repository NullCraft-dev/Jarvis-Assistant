// Jarvis Assistant — Go Gateway / Runtime Orchestrator
//
// Storage 架构重构（2026-07-14）：
// Go Gateway 不再直接访问数据库。持久化通过 Python Control Plane Internal API 完成。
// Redis 仍是运行时通信层。SSE 通过 EventPump 从 Redis 读取 worker 事件。
//
// 分层：
//
//	Vue UI → typed API client → Go Gateway (本服务)
//	  → Python Control Plane (持久化，短事务)
//	  → Redis Runtime Bus (运行时通信)
//	  → Python Agent Worker (Agent 执行)
//	  → Redis RuntimeEvent → Go EventPump → SSE → Vue
package app

import (
	"context"
	"fmt"
	"log/slog"
	"net"
	"net/http"
	"os"
	"os/signal"
	"strings"
	"syscall"
	"time"

	"github.com/jarvis-assistant/gateway/internal/observability"
)

// Run 组装 Gateway 的依赖、HTTP 路由和运行时生命周期。
func Run() {
	// 日志必须最早初始化
	logger := observability.Setup("gateway", "gateway-01", "gateway.log")
	slog.SetDefault(logger)

	deps, err := buildDependencies()
	if err != nil {
		slog.Error("创建 Gateway 依赖失败", "error", err)
		os.Exit(1)
	}

	slog.Info("RuntimeBus 模式: " + deps.runtimeConfig.BusType)
	if deps.runtimeConfig.BusType == "redis" {
		slog.Info("Redis 地址: " + deps.runtimeConfig.RedisAddr)
	}
	if deps.controlPlane != nil {
		suffix := ""
		if deps.runtimeConfig.BusType != "redis" {
			suffix = " (inmemory mode)"
		}
		slog.Info("Control Plane: " + deps.controlURL + suffix)
	}

	// redis 模式：启动 event pump
	if deps.pump != nil {
		if err := deps.pump.Start(); err != nil {
			slog.Error("启动 event pump 失败", "error", err)
			os.Exit(1)
		}
		defer func() {
			if cerr := deps.pump.Close(); cerr != nil {
				slog.Warn("event pump 关闭错误", "error", cerr)
			}
		}()
		slog.Info("Event pump 已启动（Redis → InMemory → SSE）")
	}

	h := buildRouter(
		deps.runtimeConfig,
		deps.runtimeBus,
		deps.stateStore,
		deps.controlPlane,
	)

	addr, err := gatewayListenAddress()
	if err != nil {
		slog.Error("Gateway 监听地址无效", "error", err)
		os.Exit(1)
	}
	srv := newHTTPServer(addr, h)

	ctx, stop := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
	defer stop()
	if healthProvider, ok := deps.runtimeBus.(runtimeHealthProvider); ok {
		startRuntimeSummary(ctx, healthProvider)
	}

	go func() {
		slog.Info("Jarvis Gateway 启动在 http://" + addr)
		slog.Info("API: http://" + addr + "/api")
		slog.Info("SSE: http://" + addr + "/api/runs/:id/events")
		if deps.runtimeConfig.BusType == "redis" {
			slog.Info("Redis: " + deps.runtimeConfig.RedisAddr + " (event pump active)")
		}
		if deps.controlPlane != nil {
			slog.Info("Control Plane: " + deps.controlURL)
		}

		if err := srv.ListenAndServe(); err != nil && err != http.ErrServerClosed {
			slog.Error("Gateway 启动失败", "error", err)
			os.Exit(1)
		}
	}()

	<-ctx.Done()
	slog.Info("收到关闭信号...")

	shutdownCtx, shutdownCancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer shutdownCancel()

	if err := srv.Shutdown(shutdownCtx); err != nil {
		slog.Warn("HTTP server 关闭错误", "error", err)
	}

	slog.Info("Gateway 已安全关闭")
}

const (
	defaultGatewayHost = "127.0.0.1"
	defaultGatewayPort = "8080"
)

// gatewayListenAddress 只接受 loopback IP，防止未认证的本地控制面意外暴露到局域网。
// Gateway 尚未提供远程身份认证；需要远程访问时必须先引入独立的认证与 TLS 边界。
func gatewayListenAddress() (string, error) {
	host := strings.TrimSpace(os.Getenv("JARVIS_GATEWAY_HOST"))
	if host == "" {
		host = defaultGatewayHost
	}
	ip := net.ParseIP(host)
	if ip == nil || !ip.IsLoopback() {
		return "", fmt.Errorf("JARVIS_GATEWAY_HOST 必须是 loopback IP，当前值为 %q", host)
	}
	return net.JoinHostPort(host, defaultGatewayPort), nil
}

func newHTTPServer(addr string, handler http.Handler) *http.Server {
	return &http.Server{
		Addr:              addr,
		Handler:           handler,
		ReadHeaderTimeout: 5 * time.Second,
		IdleTimeout:       60 * time.Second,
		MaxHeaderBytes:    1 << 20,
	}
}
