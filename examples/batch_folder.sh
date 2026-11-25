#!/usr/bin/env bash
# 批次處理一個資料夾（含中斷續跑）：失敗的檔案不會影響其他檔案
# 用法：ASR_API_KEY=... LLM_API_KEY=... bash examples/batch_folder.sh /path/to/media
set -uo pipefail
cd "$(dirname "$0")/.."
export PYTHONPATH=${PYTHONPATH:-src}
SRC=${1:-.}
OUT=${OUT:-./output}
mkdir -p "$OUT"
fail=0
find "$SRC" -type f \( -iname '*.mp4' -o -iname '*.mov' -o -iname '*.mkv' -o -iname '*.mp3' -o -iname '*.m4a' -o -iname '*.wav' \) | sort | while read -r f; do
  echo "=== $f"
  python3 -m summery run "$f" --out-dir "$OUT" --chunk-seconds 600 --concurrency 4 || {
    code=$?
    echo "  回傳碼 $code（2 = 部分完成，可重跑續傳）"
    [ "$code" -eq 1 ] && fail=1
  }
done
exit $fail
