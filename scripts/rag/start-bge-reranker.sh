#!/usr/bin/env bash

set -Eeuo pipefail

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
repo_root="$(cd "$script_dir/../.." && pwd)"
runtime_python="$repo_root/.local/rag-runtimes/bge-reranker/.venv/bin/python"

if [ ! -x "$runtime_python" ] || [ ! -f "$repo_root/.local/rag-runtimes/bge-reranker/.ready" ]; then
  echo "BGE reranker runtime 未完整安装；请先运行 scripts/rag/setup-bge-reranker.sh" >&2
  exit 1
fi

export PYTHONPATH="$repo_root/apps/agent-worker/src${PYTHONPATH:+:$PYTHONPATH}"
exec "$runtime_python" -m jarvis_worker.agent.rag.reranking.local_server \
  --host "${JARVIS_DEV_HOST:-127.0.0.1}" \
  --port "${JARVIS_RAG_RERANKER_PORT:-8121}"
