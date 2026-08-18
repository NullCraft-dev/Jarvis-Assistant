#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
AGENT_DIR="$REPO_ROOT/apps/agent-worker"
CONDA_ENV="${JARVIS_CONDA_ENV:-jarvis-assistant}"

cd "$AGENT_DIR"

# 存量代码的阻断基线：语法错误、未定义名称及高风险 Pyflakes 问题。
conda run -n "$CONDA_ENV" python -m ruff check src tests

# 发布门禁报告生成器由 CI 直接执行，始终使用严格 lint/format，不能只依赖工作区 diff。
conda run -n "$CONDA_ENV" python -m ruff check ../../scripts/write-release-gate-report.py
conda run -n "$CONDA_ENV" python -m ruff format --check ../../scripts/write-release-gate-report.py
conda run -n "$CONDA_ENV" python -m ruff check ../../scripts/dev-preflight.py
conda run -n "$CONDA_ENV" python -m ruff format --check ../../scripts/dev-preflight.py
conda run -n "$CONDA_ENV" python -m ruff check ../../scripts/data-lifecycle.py
conda run -n "$CONDA_ENV" python -m ruff format --check ../../scripts/data-lifecycle.py
conda run -n "$CONDA_ENV" python -m ruff check ../../scripts/runtime-support.py
conda run -n "$CONDA_ENV" python -m ruff format --check ../../scripts/runtime-support.py
conda run -n "$CONDA_ENV" python -m ruff check ../../scripts/rc2-candidate.py
conda run -n "$CONDA_ENV" python -m ruff format --check ../../scripts/rc2-candidate.py

declare -a changed_files=()
while IFS= read -r path; do
  [[ -z "$path" ]] && continue
  if [[ "$path" == apps/agent-worker/* ]]; then
    relative="${path#apps/agent-worker/}"
  else
    relative="../../$path"
  fi
  changed_files+=("$relative")
done < <(
  {
    git -C "$REPO_ROOT" diff --name-only --diff-filter=ACMR HEAD -- \
      apps/agent-worker/src apps/agent-worker/tests scripts
    git -C "$REPO_ROOT" ls-files --others --exclude-standard -- \
      apps/agent-worker/src apps/agent-worker/tests scripts
  } | awk '/\.py$/ && !seen[$0]++'
)

if ((${#changed_files[@]} == 0)); then
  echo "Ruff: no changed Python files require the strict gate"
  exit 0
fi

# 从本次改动开始执行严格门：import、关键 pycodestyle 与完整 Pyflakes。
conda run -n "$CONDA_ENV" python -m ruff check \
  --select E4,E7,E9,F,I "${changed_files[@]}"

# 新文件必须从第一天起遵守统一格式；旧文件不做无关的大面积重排。
declare -a new_files=()
while IFS= read -r path; do
  [[ -z "$path" ]] && continue
  if [[ "$path" == apps/agent-worker/* ]]; then
    new_files+=("${path#apps/agent-worker/}")
  else
    new_files+=("../../$path")
  fi
done < <(
  git -C "$REPO_ROOT" ls-files --others --exclude-standard -- \
    apps/agent-worker/src apps/agent-worker/tests scripts | awk '/\.py$/'
)

if ((${#new_files[@]} > 0)); then
  conda run -n "$CONDA_ENV" python -m ruff format --check "${new_files[@]}"
fi
