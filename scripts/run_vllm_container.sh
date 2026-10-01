#!/usr/bin/env bash
set -e

docker rm -f vllm-qwen 2>/dev/null || true

docker run -d --name vllm-qwen \
  --gpus all \
  -p 8001:8000 \
  --ipc=host \
  -v ~/.cache/huggingface:/root/.cache/huggingface \
  vllm/vllm-openai:v0.30.0 \
  --model Qwen/Qwen3.5-2B \
  --quantization fp8 \
  --gpu-memory-utilization 0.85 \
  --max-model-len 8192 \
  --enforce-eager \
  --enable-auto-tool-choice \
  --limit-mm-per-prompt '{"image": 0, "video": 0}'\
  --tool-call-parser qwen3_coder \
  --reasoning-parser qwen3