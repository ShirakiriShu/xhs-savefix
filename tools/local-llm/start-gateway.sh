#!/usr/bin/env bash
# 启动本地 litellm 网关:anthropic /v1/messages 协议 → ollama qwen3.8:27b-mlx
# 用途:harvest-companies/recruit/emailfind 零改动走本地模型(模型别名 glm-5.1)
# 前置:ollama serve 已运行且已 pull 模型
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
if curl -sS -m 3 http://127.0.0.1:4000/health/liveliness >/dev/null 2>&1; then
  echo "网关已在运行(127.0.0.1:4000)"; exit 0
fi
nohup "$HERE/litellm-venv/bin/litellm" --config "$HERE/litellm-config.yaml" \
  --port 4000 --host 127.0.0.1 > /tmp/litellm.log 2>&1 &
echo "litellm 网关启动中 pid=$! (日志 /tmp/litellm.log)"
for i in $(seq 1 30); do
  sleep 2
  curl -sS -m 2 http://127.0.0.1:4000/health/liveliness >/dev/null 2>&1 && { echo "就绪 ✓"; exit 0; }
done
echo "启动超时,看 /tmp/litellm.log" >&2; exit 1
