#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
runtime_bin="$repo_root/.local/rag-runtimes/mlx-vlm/.venv/bin/mlx_vlm.server"
runtime_python="$repo_root/.local/rag-runtimes/mlx-vlm/.venv/bin/python"
log_dir="${JARVIS_LOG_DIR:-$repo_root/.local/logs}"

if [[ ! -x "$runtime_bin" ]]; then
  echo "MLX-VLM runtime is not installed under .local/rag-runtimes" >&2
  exit 1
fi

cd "$repo_root/.local/rag-runtimes"
"$runtime_bin" --host 127.0.0.1 --port 8111 2>&1 \
  | "$runtime_python" -u "$repo_root/scripts/external_log_adapter.py" \
      --service mlx-vlm \
      --instance "${JARVIS_INSTANCE_ID:-mlx-vlm-01}" \
      --log-file "$log_dir/mlx-vlm.log"
