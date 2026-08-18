#!/usr/bin/env bash

set -Eeuo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
AGENT_DIR="$REPO_DIR/apps/agent-worker"
GATEWAY_DIR="$REPO_DIR/apps/gateway"
WEB_DIR="$REPO_DIR/apps/web"
SHARED_DIR="$REPO_DIR/packages/shared"
GO_CACHE_DIR="$REPO_DIR/.cache/go-build"
CONDA_ENV_NAME="${JARVIS_CONDA_ENV:-jarvis-assistant}"
DEFAULT_RAG_BASELINE="$AGENT_DIR/eval/baselines/rag-promoted-p4-v1.json"
RUN_STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
RESULT_DIR="${JARVIS_RELEASE_GATE_OUTPUT_DIR:-$REPO_DIR/.local/release-gate/$RUN_STAMP}"
MODE="${1:-automated}"
EVIDENCE_FILE="${2:-${JARVIS_RC1_EVIDENCE:-}}"
SUMMARY_FILE="$RESULT_DIR/summary.txt"
STEPS_FILE="$RESULT_DIR/steps.tsv"
REPORT_FILE="$RESULT_DIR/report.json"
PASSED_STEPS=0
GATE_FINISHED=0
FAILED_STEP=""

usage() {
  printf '%s\n' \
    'Usage: scripts/release-gate.sh [ci|automated|runtime|rag|p4|evidence|rc1|rc2] [evidence.json]' \
    '' \
    '  ci         CI 使用的确定性代码门；与 automated 同一实现' \
    '  automated  运行共享契约、Go、Web、Python 全量质量门（默认）' \
    '  runtime    对已启动的本地 Runtime 运行 health/worker/task/SSE smoke' \
    '  rag        从本地 PostgreSQL 自动运行 promoted-only RAG 数据飞轮门禁' \
    '  p4         依次运行 automated 与 rag，二者全部通过才放行' \
    '  evidence   校验八条 RC1 真实用户旅程证据' \
    '  rc1        依次运行 automated、runtime 和 evidence，三层全部通过才放行' \
    '  rc2        在干净候选上依次运行 automated、runtime 和 rag 工程发布门' \
    '' \
    '结果写入 .local/release-gate/<UTC timestamp>/summary.txt、steps.tsv 和 report.json。'
}

fail() {
  printf '[release-gate] FAILED: %s\n' "$*" >&2
  exit 1
}

need_cmd() {
  if ! command -v "$1" >/dev/null 2>&1; then
    FAILED_STEP="dependency:$1"
    fail "缺少命令: $1"
  fi
}

run_step() {
  local name="$1"
  shift
  local log_file="$RESULT_DIR/${name}.log"
  local started_epoch
  local finished_epoch
  local duration_seconds
  local exit_code
  started_epoch="$(date +%s)"
  printf '[release-gate] START %s\n' "$name"
  if "$@" >"$log_file" 2>&1; then
    finished_epoch="$(date +%s)"
    duration_seconds=$((finished_epoch - started_epoch))
    PASSED_STEPS=$((PASSED_STEPS + 1))
    printf '%s\tpassed\t0\t%s\t%s\n' \
      "$name" "$duration_seconds" "${name}.log" >>"$STEPS_FILE"
    printf '[release-gate] PASS  %s\n' "$name"
    return 0
  else
    exit_code="$?"
  fi
  finished_epoch="$(date +%s)"
  duration_seconds=$((finished_epoch - started_epoch))
  FAILED_STEP="$name"
  printf '%s\tfailed\t%s\t%s\t%s\n' \
    "$name" "$exit_code" "$duration_seconds" "${name}.log" >>"$STEPS_FILE"
  printf '[release-gate] FAIL  %s (log: %s)\n' "$name" "$log_file" >&2
  tail -n 80 "$log_file" >&2 || true
  return "$exit_code"
}

shared_typecheck() {
  cd "$SHARED_DIR"
  npm run typecheck
}

gateway_test() {
  cd "$GATEWAY_DIR"
  GOCACHE="$GO_CACHE_DIR" go test ./...
}

gateway_vet() {
  cd "$GATEWAY_DIR"
  GOCACHE="$GO_CACHE_DIR" go vet ./...
}

web_test() {
  cd "$WEB_DIR"
  npm test
}

web_build() {
  cd "$WEB_DIR"
  npm run build
}

worker_test() {
  cd "$AGENT_DIR"
  conda run --no-capture-output -n "$CONDA_ENV_NAME" python -m pytest
}

worker_quality() {
  "$SCRIPT_DIR/check-python-quality.sh"
}

worker_compile() {
  cd "$AGENT_DIR"
  conda run -n "$CONDA_ENV_NAME" python -m compileall -q src tests
}

rag_flywheel() {
  local baseline="${JARVIS_RAG_FLYWHEEL_BASELINE:-$DEFAULT_RAG_BASELINE}"
  local args=(
    eval/runners/run_rag_flywheel.py
    --output-dir "$RESULT_DIR/rag-flywheel"
    --replay-promoted
    --fail-on-blocked
    --revision "$REVISION"
  )
  [ -n "${JARVIS_DATABASE_URL:-}" ] \
    || fail "rag/p4 模式必须显式设置 JARVIS_DATABASE_URL"
  [ -f "$baseline" ] \
    || fail "RAG 飞轮版本基线不存在: $baseline"
  args+=(--baseline "$baseline")
  cd "$AGENT_DIR"
  conda run --no-capture-output -n "$CONDA_ENV_NAME" python \
    "${args[@]}"
}

run_automated() {
  need_cmd git
  need_cmd go
  need_cmd npm
  need_cmd conda
  need_cmd python3
  run_step release_report_self_test python3 "$SCRIPT_DIR/write-release-gate-report.py" --self-test
  run_step dev_preflight_self_test python3 "$SCRIPT_DIR/dev-preflight.py" --self-test
  run_step data_lifecycle_self_test python3 "$SCRIPT_DIR/data-lifecycle.py" --self-test
  run_step runtime_support_self_test python3 "$SCRIPT_DIR/runtime-support.py" --self-test
  run_step rc2_candidate_self_test python3 "$SCRIPT_DIR/rc2-candidate.py" --self-test
  run_step evidence_validator_self_test python3 "$SCRIPT_DIR/validate-rc1-evidence.py" --self-test
  run_step git_diff_check git -C "$REPO_DIR" diff --check
  run_step shared_typecheck shared_typecheck
  run_step gateway_test gateway_test
  run_step gateway_vet gateway_vet
  run_step web_test web_test
  run_step web_build web_build
  run_step worker_test worker_test
  run_step worker_quality worker_quality
  run_step worker_compile worker_compile
}

run_runtime() {
  need_cmd curl
  need_cmd python3
  run_step runtime_smoke "$SCRIPT_DIR/dev-runtime-check.sh"
}

run_rag() {
  need_cmd conda
  run_step rag_flywheel rag_flywheel
}

run_evidence() {
  need_cmd python3
  [ -n "$EVIDENCE_FILE" ] || fail "evidence/rc1 模式必须提供 evidence.json"
  [ -f "$EVIDENCE_FILE" ] || fail "证据文件不存在: $EVIDENCE_FILE"
  run_step rc1_evidence python3 "$SCRIPT_DIR/validate-rc1-evidence.py" \
    --expected-revision "$REVISION" "$EVIDENCE_FILE"
}

require_clean_worktree() {
  if [ -n "$(git -C "$REPO_DIR" status --porcelain)" ]; then
    FAILED_STEP="clean_worktree"
    fail "$MODE 模式要求干净工作区；请先提交或移出候选版本之外的改动"
  fi
}

write_report() {
  python3 "$SCRIPT_DIR/write-release-gate-report.py" \
    --summary "$SUMMARY_FILE" \
    --steps "$STEPS_FILE" \
    --output "$REPORT_FILE"
}

record_failure() {
  local exit_code="$?"
  if [ "$GATE_FINISHED" -eq 0 ]; then
    printf 'finished_at=%s\nstatus=failed\npassed_steps=%s\nfailed_step=%s\nexit_code=%s\n' \
      "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$PASSED_STEPS" "$FAILED_STEP" "$exit_code" \
      >>"$SUMMARY_FILE"
    write_report || printf '[release-gate] WARN: 失败报告生成失败\n' >&2
  fi
}

case "$MODE" in
  ci|automated|runtime|rag|p4|evidence|rc1|rc2) ;;
  -h|--help)
    usage
    exit 0
    ;;
  *)
    usage >&2
    exit 2
    ;;
esac

mkdir -p "$RESULT_DIR"
mkdir -p "$GO_CACHE_DIR"
REVISION="$(git -C "$REPO_DIR" rev-parse HEAD)"
WORKTREE_CLEAN=true
if [ -n "$(git -C "$REPO_DIR" status --porcelain)" ]; then
  WORKTREE_CLEAN=false
fi
STARTED_AT="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
printf 'mode=%s\nrevision=%s\nworktree_clean=%s\nstarted_at=%s\n' \
  "$MODE" "$REVISION" "$WORKTREE_CLEAN" "$STARTED_AT" \
  >"$SUMMARY_FILE"
: >"$STEPS_FILE"
trap record_failure EXIT

case "$MODE" in
  ci|automated) run_automated ;;
  runtime) run_runtime ;;
  rag) run_rag ;;
  p4)
    require_clean_worktree
    run_automated
    run_rag
    ;;
  evidence) run_evidence ;;
  rc1)
    require_clean_worktree
    run_automated
    run_runtime
    run_evidence
    ;;
  rc2)
    require_clean_worktree
    run_automated
    run_runtime
    run_rag
    ;;
esac

printf 'finished_at=%s\nstatus=passed\npassed_steps=%s\n' \
  "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$PASSED_STEPS" >>"$SUMMARY_FILE"
write_report
GATE_FINISHED=1
printf '[release-gate] PASSED mode=%s steps=%s result=%s\n' "$MODE" "$PASSED_STEPS" "$RESULT_DIR"
