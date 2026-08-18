#!/usr/bin/env bash

set -Eeuo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
AGENT_DIR="$ROOT_DIR/apps/agent-worker"
GATEWAY_DIR="$ROOT_DIR/apps/gateway"
WEB_DIR="$ROOT_DIR/apps/web"
RUNTIME_DIR="$ROOT_DIR/.cache/dev-runtime"
GATEWAY_BIN="$RUNTIME_DIR/bin/jarvis-gateway"
LOG_DIR="$ROOT_DIR/.local/logs"
ARTIFACT_ROOT="${JARVIS_ARTIFACT_ROOT:-$ROOT_DIR/.local/artifacts}"
RAG_ASSET_ROOT="${JARVIS_RAG_ASSET_ROOT:-$ROOT_DIR/.local/rag-assets}"

HOST="${JARVIS_DEV_HOST:-127.0.0.1}"
GATEWAY_HOST="${JARVIS_GATEWAY_HOST:-127.0.0.1}"
CONTROL_PLANE_PORT="${JARVIS_CONTROL_PLANE_PORT:-8100}"
GATEWAY_PORT=8080
WEB_PORT="${JARVIS_WEB_PORT:-5173}"
LOCAL_VLM_ENABLED="${JARVIS_LOCAL_VLM_ENABLED:-auto}"
LOCAL_VLM_PORT=8111
LOCAL_VLM_URL="http://${HOST}:${LOCAL_VLM_PORT}"
LOCAL_VLM_RUNTIME_DIR="$ROOT_DIR/.local/rag-runtimes"
LOCAL_VLM_BIN="$LOCAL_VLM_RUNTIME_DIR/mlx-vlm/.venv/bin/mlx_vlm.server"
LOCAL_RERANKER_ENABLED="${JARVIS_LOCAL_RERANKER_ENABLED:-auto}"
LOCAL_RERANKER_PORT="${JARVIS_RAG_RERANKER_PORT:-8121}"
LOCAL_RERANKER_URL="http://${HOST}:${LOCAL_RERANKER_PORT}"
LOCAL_RERANKER_BIN="$LOCAL_VLM_RUNTIME_DIR/bge-reranker/.ready"
LOCAL_RERANKER_MODEL="${JARVIS_RAG_RERANKER_MODEL:-BAAI/bge-reranker-v2-m3}"
DATABASE_URL="${JARVIS_DATABASE_URL:-postgresql+asyncpg://jarvis:jarvis@127.0.0.1:5432/jarvis}"
REDIS_ADDR="${JARVIS_REDIS_ADDR:-127.0.0.1:6379}"
CONTROL_PLANE_URL="http://${HOST}:${CONTROL_PLANE_PORT}"
WORKSPACE_ROOT="${JARVIS_WORKSPACE_ROOT:-$ROOT_DIR}"
ALLOWED_WORKSPACE_PATHS="${JARVIS_ALLOWED_WORKSPACE_PATHS:-$WORKSPACE_ROOT}"
CONDA_ENV="${JARVIS_CONDA_ENV:-jarvis-assistant}"
PYTHON_VERSION="${JARVIS_PYTHON_VERSION:-3.12}"
PADDLEOCR_SITE_PACKAGES="${JARVIS_RAG_PADDLEOCR_SITE_PACKAGES:-$LOCAL_VLM_RUNTIME_DIR/paddleocr-client/.venv/lib/python${PYTHON_VERSION}/site-packages}"

# 子进程的 stderr 会被 prefix_output 接入管道，导致其自身无法判断外层终端。
# 这里在真实终端中显式传递 always；重定向输出或 NO_COLOR 时保持无颜色。
resolve_console_color_mode() {
  if [ -n "${NO_COLOR:-}" ]; then
    printf 'never'
    return
  fi

  case "${JARVIS_LOG_COLOR:-auto}" in
    always|force|1|true)
      printf 'always'
      ;;
    never|0|false)
      printf 'never'
      ;;
    *)
      if [ -t 1 ] || [ -t 2 ]; then
        printf 'always'
      else
        printf 'never'
      fi
      ;;
  esac
}

CONSOLE_COLOR_MODE="$(resolve_console_color_mode)"
ANSI_RESET=$'\033[0m'
ANSI_GREY=$'\033[90m'
ANSI_GREEN=$'\033[32m'
ANSI_CYAN=$'\033[36m'
ANSI_YELLOW=$'\033[33m'
ANSI_RED=$'\033[31m'
ANSI_BLUE=$'\033[34m'
ANSI_MAGENTA=$'\033[35m'

PIDS=()
NAMES=()

usage() {
  cat <<'EOF'
Usage: scripts/dev.sh [start|setup|doctor|infra-down]

  start       启动基础设施、可选 MLX-VLM、Control Plane、Gateway、Agent/RAG Worker 和 Web（默认）
  setup       创建/更新 Conda 环境并安装 Python、Node、Go 依赖和 Docker images
  doctor      检查依赖、生产配置、目录和端口，并生成结构化 preflight 报告
  infra-down  停止 PostgreSQL、Redis 容器；不会删除 PostgreSQL 数据卷

环境变量：
  JARVIS_DATABASE_URL       PostgreSQL asyncpg DSN
  JARVIS_REDIS_ADDR         Redis 地址，默认 127.0.0.1:6379
  JARVIS_DEV_HOST           Control Plane / Web 监听地址，默认 127.0.0.1
  JARVIS_GATEWAY_HOST       Gateway 监听地址，仅支持 loopback IP，默认 127.0.0.1
  JARVIS_CONTROL_PLANE_PORT Control Plane 端口，默认 8100
  JARVIS_WEB_PORT           Web 端口，默认 5173
  JARVIS_LOCAL_VLM_ENABLED  本地 MLX-VLM：auto（已安装则启动）/ true / false
  JARVIS_LOCAL_RERANKER_ENABLED 本地 BGE Reranker：auto（已安装则启动）/ true / false
  JARVIS_WORKSPACE_ROOT     默认工作区，默认项目根目录
  JARVIS_ARTIFACT_ROOT      大产物文件根目录，默认 .local/artifacts
  JARVIS_RAG_ASSET_ROOT     RAG 图片/图表等二进制元素目录，默认 .local/rag-assets
  JARVIS_RAG_STRUCTURE_PROVIDER  paddleocr-vl 或 native-only；默认随本地 VLM 可用性选择
  JARVIS_RAG_PADDLEOCR_SITE_PACKAGES  隔离 PaddleOCR 客户端依赖目录
  JARVIS_ALLOWED_WORKSPACE_PATHS 允许选择的工作区根目录，使用系统 PATH 分隔符
  JARVIS_MODEL_ADAPTER      模型实现：langchain（默认）或 direct（显式回退）
  JARVIS_AGENT_MAX_ITERATIONS 单个 Run 的工具调用预算，默认 14，范围 1-20
  JARVIS_CONDA_ENV          Conda 环境名，默认 jarvis-assistant
  JARVIS_PYTHON_VERSION     setup 创建环境时的 Python 版本，默认 3.12
  JARVIS_PREFLIGHT_OUTPUT_DIR preflight 报告目录，默认 .local/preflight
  JARVIS_LOG_COLOR          终端颜色模式：auto（默认）/ always / never
  NO_COLOR                  设置后强制关闭所有终端颜色

启动后按 Ctrl+C 统一关闭应用进程。PostgreSQL 和 Redis 容器会保留，便于下次快速启动。
Conda、Docker、Go、Node/npm 属于系统级工具，需要预先安装；setup 负责项目依赖。
EOF
}

log() {
  if [ "$CONSOLE_COLOR_MODE" = "always" ]; then
    printf '%s[dev]%s %s\n' "$ANSI_GREY" "$ANSI_RESET" "$*"
  else
    printf '[dev] %s\n' "$*"
  fi
}

fail() {
  if [ "$CONSOLE_COLOR_MODE" = "always" ]; then
    printf '%s[dev] ERROR:%s %s\n' "$ANSI_RED" "$ANSI_RESET" "$*" >&2
  else
    printf '[dev] ERROR: %s\n' "$*" >&2
  fi
  exit 1
}

need_cmd() {
  command -v "$1" >/dev/null 2>&1 || fail "缺少命令: $1"
}

check_port_free() {
  local port="$1"
  local name="$2"
  if command -v lsof >/dev/null 2>&1 && lsof -nP -iTCP:"$port" -sTCP:LISTEN >/dev/null 2>&1; then
    fail "$name 端口 $port 已被占用；请先停止旧进程，再重新运行脚本"
  fi
}

local_vlm_enabled() {
  case "$LOCAL_VLM_ENABLED" in
    auto)
      [ -x "$LOCAL_VLM_BIN" ]
      ;;
    true|1|yes|on)
      [ -x "$LOCAL_VLM_BIN" ] \
        || fail "已要求启动 MLX-VLM，但本地运行环境不存在: $LOCAL_VLM_BIN"
      return 0
      ;;
    false|0|no|off)
      return 1
      ;;
    *)
      fail "JARVIS_LOCAL_VLM_ENABLED 仅支持 auto、true 或 false"
      ;;
  esac
}

local_reranker_enabled() {
  case "$LOCAL_RERANKER_ENABLED" in
    auto)
      [ -f "$LOCAL_RERANKER_BIN" ]
      ;;
    true|1|yes|on)
      [ -f "$LOCAL_RERANKER_BIN" ] \
        || fail "已要求启动 BGE Reranker，但本地运行环境不存在；请运行 scripts/rag/setup-bge-reranker.sh"
      return 0
      ;;
    false|0|no|off)
      return 1
      ;;
    *)
      fail "JARVIS_LOCAL_RERANKER_ENABLED 仅支持 auto、true 或 false"
      ;;
  esac
}

check_project() {
  need_cmd docker
  need_cmd conda
  need_cmd go
  need_cmd npm
  need_cmd curl

  docker compose version >/dev/null 2>&1 || fail "Docker Compose 不可用"
  [ -f "$ROOT_DIR/compose.yaml" ] || fail "缺少 compose.yaml"
  [ -f "$AGENT_DIR/pyproject.toml" ] || fail "缺少 Python Worker pyproject.toml"
  [ -f "$WEB_DIR/package.json" ] || fail "缺少 Web package.json"
  [ -f "$GATEWAY_DIR/go.mod" ] || fail "缺少 Gateway go.mod"
}

conda_env_exists() {
  conda env list | sed 's/\*//g' | awk '{print $1}' | grep -Fxq "$CONDA_ENV"
}

check_conda_runtime() {
  conda_env_exists || fail "Conda 环境 $CONDA_ENV 不存在；请先运行 scripts/dev.sh setup"
  conda run -n "$CONDA_ENV" python -c \
    'import alembic, asyncpg, fastapi, langchain_core, langchain_deepseek, langchain_openai, redis, sqlalchemy, uvicorn, jarvis_worker' \
    >/dev/null 2>&1 \
    || fail "Conda 环境 $CONDA_ENV 的 Python 依赖不完整；请运行 scripts/dev.sh setup"
}

check_app_ports() {
  check_port_free "$CONTROL_PLANE_PORT" "Control Plane"
  check_port_free "$GATEWAY_PORT" "Gateway"
  check_port_free "$WEB_PORT" "Web"
  if local_vlm_enabled; then
    check_port_free "$LOCAL_VLM_PORT" "MLX-VLM"
  fi
  if local_reranker_enabled; then
    check_port_free "$LOCAL_RERANKER_PORT" "BGE Reranker"
  fi
}

doctor() {
  local python_executable
  need_cmd python3
  env JARVIS_WORKSPACE_ROOT="$WORKSPACE_ROOT" \
    JARVIS_ALLOWED_WORKSPACE_PATHS="$ALLOWED_WORKSPACE_PATHS" \
    JARVIS_ARTIFACT_ROOT="$ARTIFACT_ROOT" \
    JARVIS_RAG_ASSET_ROOT="$RAG_ASSET_ROOT" \
    JARVIS_DEV_HOST="$HOST" \
    JARVIS_CONTROL_PLANE_PORT="$CONTROL_PLANE_PORT" \
    JARVIS_WEB_PORT="$WEB_PORT" \
    JARVIS_LOCAL_VLM_ENABLED="$LOCAL_VLM_ENABLED" \
    JARVIS_LOCAL_RERANKER_ENABLED="$LOCAL_RERANKER_ENABLED" \
    python3 "$SCRIPT_DIR/dev-preflight.py" \
    --repo "$ROOT_DIR" \
    --conda-env "$CONDA_ENV"
  check_project
  check_conda_runtime
  python_executable="$(conda run -n "$CONDA_ENV" python -c 'import sys; print(sys.executable)' | tr -d '\r')"

  if [ ! -f "$AGENT_DIR/.env" ]; then
    log "WARN: apps/agent-worker/.env 不存在；Worker 必须从外部环境获得完整模型配置"
  fi

  check_app_ports
  log "环境检查通过（Conda: ${CONDA_ENV}）"
  log "Python runtime: $python_executable"
  if local_vlm_enabled; then
    log "本地 MLX-VLM 已启用（mode=${LOCAL_VLM_ENABLED}, port=${LOCAL_VLM_PORT}）"
  else
    log "本地 MLX-VLM 未启用（mode=${LOCAL_VLM_ENABLED}）"
  fi
  if local_reranker_enabled; then
    log "本地 BGE Reranker 已启用（mode=${LOCAL_RERANKER_ENABLED}, port=${LOCAL_RERANKER_PORT}）"
  else
    log "本地 BGE Reranker 未启用（mode=${LOCAL_RERANKER_ENABLED}）"
  fi
}

setup_dependencies() {
  check_project
  check_app_ports

  if conda_env_exists; then
    log "使用现有 Conda 环境: $CONDA_ENV"
  else
    log "创建 Conda 环境: $CONDA_ENV (Python $PYTHON_VERSION)"
    conda create -y -n "$CONDA_ENV" "python=$PYTHON_VERSION" pip
  fi

  log "安装 Python Worker / Control Plane 依赖..."
  conda run --no-capture-output -n "$CONDA_ENV" \
    python -m pip install -e "${AGENT_DIR}[dev]"

  log "安装 Web 依赖..."
  (cd "$WEB_DIR" && npm ci)

  log "安装 shared contract 依赖..."
  (cd "$ROOT_DIR/packages/shared" && npm ci)

  log "下载 Go Gateway modules..."
  (cd "$GATEWAY_DIR" && go mod download)

  log "拉取 PostgreSQL 和 Redis images..."
  (cd "$ROOT_DIR" && docker compose pull postgres redis)

  if [ ! -f "$AGENT_DIR/.env" ]; then
    cp "$AGENT_DIR/.env.example" "$AGENT_DIR/.env"
    chmod 600 "$AGENT_DIR/.env"
    log "已创建 apps/agent-worker/.env；启动前请填写真实模型配置和密钥"
  fi

  check_conda_runtime
  log "项目依赖安装完成；填写模型与 RAG 配置后，先运行 scripts/dev.sh doctor"
}

wait_for_infra() {
  local attempt
  for attempt in $(seq 1 60); do
    if (cd "$ROOT_DIR" && docker compose exec -T postgres pg_isready -U jarvis -d jarvis >/dev/null 2>&1) \
      && [ "$(cd "$ROOT_DIR" && docker compose exec -T redis redis-cli ping 2>/dev/null | tr -d '\r')" = "PONG" ]; then
      log "PostgreSQL 和 Redis 已就绪"
      return 0
    fi
    sleep 1
  done
  fail "PostgreSQL 或 Redis 在 60 秒内未就绪"
}

prefix_output() {
  local name="$1"
  local line
  while IFS= read -r line || [ -n "$line" ]; do
    if [ "$CONSOLE_COLOR_MODE" = "always" ]; then
      case "$name" in
        control-plane) printf '%s[%s]%s %s\n' "$ANSI_CYAN" "$name" "$ANSI_RESET" "$line" ;;
        gateway) printf '%s[%s]%s %s\n' "$ANSI_BLUE" "$name" "$ANSI_RESET" "$line" ;;
        worker|rag-worker|mlx-vlm|bge-reranker) printf '%s[%s]%s %s\n' "$ANSI_MAGENTA" "$name" "$ANSI_RESET" "$line" ;;
        web) printf '%s[%s]%s %s\n' "$ANSI_GREEN" "$name" "$ANSI_RESET" "$line" ;;
        *) printf '%s[%s]%s %s\n' "$ANSI_GREY" "$name" "$ANSI_RESET" "$line" ;;
      esac
    else
      printf '[%s] %s\n' "$name" "$line"
    fi
  done
}

start_service() {
  local name="$1"
  local cwd="$2"
  shift 2

  (
    cd "$cwd"
    export JARVIS_LOG_COLOR="$CONSOLE_COLOR_MODE"
    exec "$@"
  ) > >(prefix_output "$name") 2>&1 &

  PIDS+=("$!")
  NAMES+=("$name")
  log "$name 已启动 (pid=$!)"
}

wait_http() {
  local name="$1"
  local url="$2"
  local expected="${3:-}"
  local attempt body

  for attempt in $(seq 1 60); do
    body="$(curl -fsS --max-time 2 "$url" 2>/dev/null || true)"
    if [ -n "$body" ] && { [ -z "$expected" ] || printf '%s' "$body" | grep -q "$expected"; }; then
      log "$name 已就绪"
      return 0
    fi
    sleep 1
  done
  fail "$name 在 60 秒内未通过健康检查: $url"
}

stop_processes() {
  local index pid
  if [ "${#PIDS[@]}" -eq 0 ]; then
    return
  fi

  log "正在关闭应用进程..."
  for ((index=${#PIDS[@]}-1; index>=0; index--)); do
    pid="${PIDS[$index]}"
    if kill -0 "$pid" >/dev/null 2>&1; then
      if command -v pkill >/dev/null 2>&1; then
        pkill -TERM -P "$pid" >/dev/null 2>&1 || true
      fi
      kill -TERM "$pid" >/dev/null 2>&1 || true
    fi
  done

  for _ in $(seq 1 50); do
    local alive=0
    for pid in "${PIDS[@]}"; do
      if kill -0 "$pid" >/dev/null 2>&1; then
        alive=1
        break
      fi
    done
    [ "$alive" -eq 0 ] && break
    sleep 0.1
  done

  for pid in "${PIDS[@]}"; do
    if kill -0 "$pid" >/dev/null 2>&1; then
      kill -KILL "$pid" >/dev/null 2>&1 || true
    fi
    wait "$pid" >/dev/null 2>&1 || true
  done
  log "应用进程已关闭；PostgreSQL 和 Redis 容器仍在运行"
}

supervise() {
  local index pid code
  while true; do
    for ((index=0; index<${#PIDS[@]}; index++)); do
      pid="${PIDS[$index]}"
      if ! kill -0 "$pid" >/dev/null 2>&1; then
        set +e
        wait "$pid"
        code=$?
        set -e
        fail "${NAMES[$index]} 意外退出 (code=$code)"
      fi
    done
    sleep 1
  done
}

start_all() {
  local rag_structure_provider reranker_enabled_value
  doctor
  mkdir -p "$RUNTIME_DIR/bin" "$ROOT_DIR/.cache/go-build" "$LOG_DIR"

  log "启动 PostgreSQL 和 Redis..."
  (cd "$ROOT_DIR" && docker compose up -d postgres redis)
  wait_for_infra

  log "检查 PostgreSQL migration 状态..."
  if ! JARVIS_CONDA_ENV="$CONDA_ENV" \
    python3 "$SCRIPT_DIR/data-lifecycle.py" status --repo "$ROOT_DIR"; then
    fail "数据库尚未完成安全升级；请先停止应用并执行 scripts/data-lifecycle.py upgrade --confirm"
  fi

  log "构建 Go Gateway..."
  (
    cd "$GATEWAY_DIR"
    GOCACHE="$ROOT_DIR/.cache/go-build" go build -o "$GATEWAY_BIN" ./cmd/gateway
  )

  if local_vlm_enabled; then
    start_service "mlx-vlm" "$LOCAL_VLM_RUNTIME_DIR" \
      "$ROOT_DIR/scripts/rag/start-mlx-vlm.sh"
    wait_http "MLX-VLM" "$LOCAL_VLM_URL/openapi.json" '"openapi"'
    rag_structure_provider="${JARVIS_RAG_STRUCTURE_PROVIDER:-paddleocr-vl}"
  else
    rag_structure_provider="${JARVIS_RAG_STRUCTURE_PROVIDER:-native-only}"
  fi
  if local_reranker_enabled; then
    start_service "bge-reranker" "$LOCAL_VLM_RUNTIME_DIR" \
      env JARVIS_RAG_RERANKER_MODEL="$LOCAL_RERANKER_MODEL" \
      "$ROOT_DIR/scripts/rag/start-bge-reranker.sh"
    wait_http "BGE Reranker" "$LOCAL_RERANKER_URL/health" '"status":"ok"'
    reranker_enabled_value=true
  else
    reranker_enabled_value=false
  fi
  case "$rag_structure_provider" in
    paddleocr-vl|native-only) ;;
    *) fail "JARVIS_RAG_STRUCTURE_PROVIDER 仅支持 paddleocr-vl 或 native-only" ;;
  esac
  if [ "$rag_structure_provider" = "paddleocr-vl" ] && ! local_vlm_enabled; then
    fail "RAG 已要求 paddleocr-vl，但本地 MLX-VLM 未启用"
  fi
  if [ "$rag_structure_provider" = "paddleocr-vl" ] && [ ! -d "$PADDLEOCR_SITE_PACKAGES" ]; then
    fail "RAG 已要求 paddleocr-vl，但客户端依赖目录不存在: $PADDLEOCR_SITE_PACKAGES"
  fi

  start_service "control-plane" "$AGENT_DIR" \
    env JARVIS_DATABASE_URL="$DATABASE_URL" JARVIS_REDIS_ADDR="$REDIS_ADDR" \
    JARVIS_WORKSPACE_ROOT="$WORKSPACE_ROOT" \
    JARVIS_ARTIFACT_ROOT="$ARTIFACT_ROOT" \
    JARVIS_ALLOWED_WORKSPACE_PATHS="$ALLOWED_WORKSPACE_PATHS" \
    JARVIS_LOG_DIR="$LOG_DIR" \
    conda run --no-capture-output -n "$CONDA_ENV" \
    python -m jarvis_worker.control_plane.main --host "$HOST" --port "$CONTROL_PLANE_PORT"
  wait_http "Control Plane" "$CONTROL_PLANE_URL/internal/health" '"status":"ok"'

  start_service "gateway" "$GATEWAY_DIR" \
    env JARVIS_RUNTIME_BUS=redis JARVIS_REDIS_ADDR="$REDIS_ADDR" \
    JARVIS_GATEWAY_HOST="$GATEWAY_HOST" \
    JARVIS_CONTROL_PLANE_URL="$CONTROL_PLANE_URL" \
    JARVIS_WORKSPACE_ROOT="$WORKSPACE_ROOT" \
    JARVIS_ARTIFACT_ROOT="$ARTIFACT_ROOT" \
    JARVIS_ALLOWED_WORKSPACE_PATHS="$ALLOWED_WORKSPACE_PATHS" \
    JARVIS_LOG_DIR="$LOG_DIR" "$GATEWAY_BIN"
  wait_http "Gateway" "http://${HOST}:${GATEWAY_PORT}/api/health" '"status":"healthy"'

  start_service "worker" "$AGENT_DIR" \
    env JARVIS_DATABASE_URL="$DATABASE_URL" JARVIS_REDIS_ADDR="$REDIS_ADDR" \
    JARVIS_WORKSPACE_ROOT="$WORKSPACE_ROOT" \
    JARVIS_SKILLS_ROOT="$ROOT_DIR/skills" \
    JARVIS_ARTIFACT_ROOT="$ARTIFACT_ROOT" \
    JARVIS_ALLOWED_WORKSPACE_PATHS="$ALLOWED_WORKSPACE_PATHS" \
    JARVIS_LOG_DIR="$LOG_DIR" \
    JARVIS_RAG_RERANKER_ENABLED="$reranker_enabled_value" \
    JARVIS_RAG_RERANKER_URL="$LOCAL_RERANKER_URL" \
    JARVIS_RAG_RERANKER_MODEL="$LOCAL_RERANKER_MODEL" \
    conda run --no-capture-output -n "$CONDA_ENV" python -m jarvis_worker.main

  start_service "rag-worker" "$AGENT_DIR" \
    env JARVIS_DATABASE_URL="$DATABASE_URL" JARVIS_REDIS_ADDR="$REDIS_ADDR" \
    JARVIS_WORKSPACE_ROOT="$WORKSPACE_ROOT" \
    JARVIS_ARTIFACT_ROOT="$ARTIFACT_ROOT" \
    JARVIS_RAG_ASSET_ROOT="$RAG_ASSET_ROOT" \
    JARVIS_RAG_STRUCTURE_PROVIDER="$rag_structure_provider" \
    JARVIS_RAG_PADDLEOCR_SITE_PACKAGES="$PADDLEOCR_SITE_PACKAGES" \
    JARVIS_RAG_MLX_VLM_URL="$LOCAL_VLM_URL/" \
    JARVIS_LOG_DIR="$LOG_DIR" \
    conda run --no-capture-output -n "$CONDA_ENV" \
    python -m jarvis_worker.agent.rag.worker

  start_service "web" "$WEB_DIR" \
    npm run dev -- --host "$HOST" --port "$WEB_PORT" --strictPort
  wait_http "Web" "http://${HOST}:${WEB_PORT}/"
  wait_http "Worker heartbeat" "http://${HOST}:${GATEWAY_PORT}/api/runtime/workers" '"is_stale":false'

  printf '\n'
  log "Jarvis Assistant 已启动"
  log "Web:           http://${HOST}:${WEB_PORT}"
  log "Gateway API:   http://${HOST}:${GATEWAY_PORT}/api"
  log "Control Plane: $CONTROL_PLANE_URL/internal"
  log "RAG Worker:    $rag_structure_provider (single concurrency)"
  if [ "$reranker_enabled_value" = "true" ]; then
    log "Reranker:     BGE local"
  else
    log "Reranker:     deterministic fallback"
  fi
  log "按 Ctrl+C 关闭应用进程"
  printf '\n'

  supervise
}

command="${1:-start}"
case "$command" in
  start)
    trap stop_processes EXIT
    trap 'exit 130' INT TERM
    start_all
    ;;
  setup)
    setup_dependencies
    ;;
  doctor)
    doctor
    ;;
  infra-down)
    need_cmd docker
    (cd "$ROOT_DIR" && docker compose down)
    ;;
  -h|--help|help)
    usage
    ;;
  *)
    usage >&2
    exit 2
    ;;
esac
