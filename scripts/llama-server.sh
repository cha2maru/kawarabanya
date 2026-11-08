#!/bin/bash
# 要約用の llama-server を起動する（OpenAI 互換 API: http://127.0.0.1:8080/v1）
# 環境変数で上書きできる: LLAMA_DIR, LLAMA_MODEL, LLAMA_PORT, LLAMA_THREADS, LLAMA_CACHE_RAM
# --cache-ram 0: 過去のプロンプトをメモリにためない（既定 8 GB まで。1 件ずつ処理するので効かず、メモリを 8 GB 使っていた）
set -euo pipefail
LLAMA_DIR=${LLAMA_DIR:-$HOME/.local/opt/llama.cpp-b12182/llama-b12182}
LLAMA_MODEL=${LLAMA_MODEL:-$HOME/.local/share/models/Qwen3.5-4B-Q4_K_M.gguf}
# libgomp を llama.cpp のディレクトリに置いた場合のため
export LD_LIBRARY_PATH="$LLAMA_DIR${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"
exec "$LLAMA_DIR/llama-server" \
  --model "$LLAMA_MODEL" --alias "$(basename "$LLAMA_MODEL" .gguf)" \
  --host 127.0.0.1 --port "${LLAMA_PORT:-8080}" \
  -c 8192 -np 1 -t "${LLAMA_THREADS:-8}" \
  --cache-ram "${LLAMA_CACHE_RAM:-0}"
