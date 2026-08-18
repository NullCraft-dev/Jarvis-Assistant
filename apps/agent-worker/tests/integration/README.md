# Local multimodal integration checks

本目录保存依赖真实本地模型或较重运行环境的 Agent Worker 验收程序，不进入默认 `pytest`
单元测试集合。

先使用项目统一入口启动系统。已安装 `.local/rag-runtimes/mlx-vlm` 时，
`scripts/dev.sh start` 的默认 `auto` 模式会一并启动并监管 MLX-VLM；可通过
`JARVIS_LOCAL_VLM_ENABLED=false` 显式关闭。

生成受控多模态 PDF：

```bash
.local/rag-runtimes/paddleocr-client/.venv/bin/python \
  apps/agent-worker/tests/fixtures/create_multimodal_fixture.py \
  .local/rag-runtimes/smoke/multimodal-fixture.pdf
```

执行 Jarvis 完整预处理与分片链路：

```bash
.local/rag-runtimes/paddleocr-client/.venv/bin/python \
  apps/agent-worker/tests/integration/jarvis_preprocessing_smoke.py \
  .local/rag-runtimes/smoke/multimodal-fixture.pdf \
  --output .local/rag-runtimes/smoke/multimodal-result.json
```

`scripts/rag/paddleocr_vl_smoke.py` 是绕开 Jarvis 编排、直接诊断 PaddleOCR-VL/MLX
依赖边界的运维工具，不代表产品调用路径。
