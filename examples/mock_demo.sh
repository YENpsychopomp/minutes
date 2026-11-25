#!/usr/bin/env bash
# 免 API key 的完整示範：產生 12 分鐘測試影音 → mock ASR/LLM → 逐字稿 + 摘要
# 用法：bash examples/mock_demo.sh
set -euo pipefail
cd "$(dirname "$0")/.."
export PYTHONPATH=src ASR_API_KEY=mock LLM_API_KEY=mock
PORT=${PORT:-8899}
OUT=${OUT:-/tmp/summery_demo}
mkdir -p "$OUT"

python3 -m summery make-media --out "$OUT/meeting.mkv" --minutes 12 --segments 40 --video

python3 -m summery mock-server --port "$PORT" --slow-ms 80 &
MOCK_PID=$!
trap 'kill $MOCK_PID 2>/dev/null || true' EXIT
sleep 1
export SUMMERY__ASR__BASE_URL="http://127.0.0.1:$PORT/v1"
export SUMMERY__LLM__BASE_URL="http://127.0.0.1:$PORT/v1"

python3 -m summery run "$OUT/meeting.mkv" \
  --out-dir "$OUT/out" --work-dir "$OUT/work" \
  --chunk-seconds 120 --concurrency 4 --style standard

echo "結果：$OUT/out"
ls -1 "$OUT/out"
