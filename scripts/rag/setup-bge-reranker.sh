#!/usr/bin/env bash

set -Eeuo pipefail

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
repo_root="$(cd "$script_dir/../.." && pwd)"
runtime_dir="$repo_root/.local/rag-runtimes/bge-reranker"
model_name="${JARVIS_RAG_RERANKER_MODEL:-BAAI/bge-reranker-v2-m3}"

if [ -n "${JARVIS_RAG_RERANKER_SETUP_PYTHON:-}" ]; then
  python_bin="$JARVIS_RAG_RERANKER_SETUP_PYTHON"
elif command -v conda >/dev/null 2>&1; then
  python_bin="$(conda run -n "${JARVIS_CONDA_ENV:-jarvis-assistant}" python -c 'import sys; print(sys.executable)' | tr -d '\r')"
else
  python_bin="python3"
fi

"$python_bin" -c 'import sys; assert sys.version_info >= (3, 11), "BGE reranker 需要 Python 3.11+"'

mkdir -p "$runtime_dir"
if [ ! -x "$runtime_dir/.venv/bin/python" ]; then
  "$python_bin" -m venv "$runtime_dir/.venv"
fi

"$runtime_dir/.venv/bin/python" -m pip install --upgrade pip
"$runtime_dir/.venv/bin/python" -m pip install \
  'torch>=2.4,<3.0' 'transformers>=4.45,<5.0' \
  'fastapi>=0.115,<1.0' 'uvicorn[standard]>=0.30,<1.0' 'httpx>=0.27,<1.0'

# 预下载模型并显示 Hugging Face 的真实下载进度；dev.sh 启动时不再临时下载。
"$runtime_dir/.venv/bin/python" -c \
  'import sys; from transformers import AutoModelForSequenceClassification, AutoTokenizer; name=sys.argv[1]; AutoTokenizer.from_pretrained(name); AutoModelForSequenceClassification.from_pretrained(name); print(f"BGE reranker ready: {name}")' \
  "$model_name"
touch "$runtime_dir/.ready"
