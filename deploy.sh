#!/usr/bin/env bash
# =============================================================================
# summery 一鍵啟動／部署
#
#   ./deploy.sh              建置前端（需要的話）並在本機啟動完整服務
#   ./deploy.sh --dev        開發模式：同時跑 Vite 前端熱更新 + Python API
#   ./deploy.sh --build      強制重新建置前端
#   ./deploy.sh --no-build   跳過前端建置（直接用現成的 frontend/dist）
#   ./deploy.sh --port 9000  換一個連接埠
#   ./deploy.sh --host 0.0.0.0  綁到整個網路（請自行處理防火牆／身分驗證）
#   ./deploy.sh --jobs-dir /data/jobs  換工作目錄
#   ./deploy.sh --python .venv/bin/python  指定 Python 直譯器
#   ./deploy.sh --open       啟動後打開瀏覽器
#
# 重要：這個腳本「從頭到尾在前台執行」。
#   * 按 Ctrl+C，或直接關閉終端機視窗 → 服務與所有子工程式立即一起停止。
#   * 沒有 nohup、沒有背景精靈、沒有開機自啟；終端機關掉，系統就停了。
# =============================================================================
set -euo pipefail

ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
cd "${ROOT}"

HOST="127.0.0.1"
PORT="8765"
JOBS_DIR=""
PYTHON="${PYTHON:-}"
BUILD="auto"          # auto | yes | no
DEV="no"
OPEN="no"

die() { printf '\033[31m✗ %s\033[0m\n' "$*" >&2; exit 1; }
info() { printf '\033[36m•\033[0m %s\n' "$*"; }
warn() { printf '\033[33m!\033[0m %s\n' "$*"; }
ok() { printf '\033[32m✓\033[0m %s\n' "$*"; }

while [[ $# -gt 0 ]]; do
  case "$1" in
    --dev) DEV="yes"; shift ;;
    --build) BUILD="yes"; shift ;;
    --no-build) BUILD="no"; shift ;;
    --port) PORT="${2:?--port 需要一個數字}"; shift 2 ;;
    --host) HOST="${2:?--host 需要一個位址}"; shift 2 ;;
    --jobs-dir) JOBS_DIR="${2:?--jobs-dir 需要一個路徑}"; shift 2 ;;
    --python) PYTHON="${2:?--python 需要一個 Python 直譯器路徑}"; shift 2 ;;
    --open) OPEN="yes"; shift ;;
    -h | --help)
      awk 'NR > 1 && /^#/ { sub(/^# ?/, ""); print; next } NR > 1 { exit }' "${BASH_SOURCE[0]}"
      exit 0
      ;;
    *) die "不認識的參數：${1}（用 --help 看說明）" ;;
  esac
done

# ---------------------------------------------------------------- 1. Python
if [[ -z "${PYTHON}" ]]; then
  if [[ -x "${ROOT}/.venv/bin/python" ]]; then
    PYTHON="${ROOT}/.venv/bin/python"
  elif command -v python3 >/dev/null 2>&1; then
    PYTHON="$(command -v python3)"
  else
    die "找不到 python3。請先安裝 Python 3.10 以上版本。"
  fi
fi

if [[ ! -x "${PYTHON}" ]]; then
  die "指定的 Python 不可執行：${PYTHON}"
fi

PY_VERSION="$("${PYTHON}" -c 'import sys; print("%d.%d" % sys.version_info[:2])')"
info "Python ${PY_VERSION}（${PYTHON}）"

# 缺套件時自動建 venv 並安裝（只裝一次；之後直接重用）
if ! "${PYTHON}" -c "import summery" >/dev/null 2>&1; then
  if [[ "${PYTHON}" == "${ROOT}/.venv/bin/python" ]] || [[ -f "${ROOT}/.venv/pyvenv.cfg" ]]; then
    info "安裝 summery（第一次會慢一點）…"
    "${PYTHON}" -m pip install --quiet --disable-pip-version-check -e "${ROOT}" \
      || die "安裝失敗，請查看上面的 pip 訊息"
    ok "已安裝到 .venv"
  else
    # 沒裝成套件就用原始碼路徑跑，免安裝、免動到系統環境
    export PYTHONPATH="${ROOT}/src${PYTHONPATH:+:${PYTHONPATH}}"
    warn "未安裝為套件，改用 PYTHONPATH=src 執行（要安裝可先執行：python3 -m venv .venv && .venv/bin/pip install -e .）"
  fi
fi

# ---------------------------------------------------------------- 2. ffmpeg
if command -v ffmpeg >/dev/null 2>&1; then
  ok "ffmpeg 就緒"
else
  warn "找不到 ffmpeg：轉錄需要它來切塊與抽音軌。macOS 可先 brew install ffmpeg"
fi

# ---------------------------------------------------------------- 3. .env 檢查
if [[ -f "${ROOT}/.env" ]]; then
  grep -qE '^[[:space:]]*(ASR|LLM)_API_KEY=.+' "${ROOT}/.env" \
    && ok "已找到 .env（summery 啟動時會自動載入）" \
    || warn ".env 存在但沒有看到 ASR_API_KEY／LLM_API_KEY，工作可能會失敗"
else
  warn "沒有 .env：請複製 .env.example 為 .env 並填入金鑰"
fi

# ---------------------------------------------------------------- 4. 前端
FRONTEND="${ROOT}/frontend"
DIST="${FRONTEND}/dist/index.html"
PM_CMD=()
DEV_CMD=()

node_ready() {
  command -v node >/dev/null 2>&1 && node -e "const [a]=process.versions.node.split('.');process.exit(Number(a)>=18?0:1)" 2>/dev/null
}

pick_package_manager() {
  if command -v pnpm >/dev/null 2>&1; then
    PM_CMD=(pnpm)
  elif command -v corepack >/dev/null 2>&1 && corepack pnpm --version >/dev/null 2>&1; then
    PM_CMD=(corepack pnpm)
  elif command -v npm >/dev/null 2>&1; then
    PM_CMD=(npm)
  else
    PM_CMD=()
  fi

  if [[ ${#PM_CMD[@]} -gt 0 ]]; then
    DEV_CMD=("${PM_CMD[@]}" run dev)
  elif node_ready && [[ -f "${FRONTEND}/node_modules/vite/bin/vite.js" ]]; then
    # 有 node、依賴也裝好了，只是 PATH 上沒有套件管理員：直接叫用 Vite
    DEV_CMD=(node "${FRONTEND}/node_modules/vite/bin/vite.js")
  else
    DEV_CMD=()
  fi
}

build_frontend() {
  if ! node_ready; then
    if [[ "${BUILD}" == "yes" ]]; then
      die "建置前端需要 Node.js 18 以上（目前找不到可用的 node）"
    fi
    warn "找不到 Node.js 18+，跳過前端建置，將使用內建的陽春介面"
    return 0
  fi

  pick_package_manager
  [[ ${#PM_CMD[@]} -gt 0 ]] || die "需要 pnpm 或 npm 才能安裝前端依賴（請先安裝 Node.js 18+）"
  info "安裝前端依賴（${PM_CMD[*]}）…"
  (cd "${FRONTEND}" && "${PM_CMD[@]}" install --frozen-lockfile=false 2>/dev/null) \
    || (cd "${FRONTEND}" && "${PM_CMD[@]}" install)

  info "建置前端（Vite）…"
  (cd "${FRONTEND}" && "${PM_CMD[@]}" run build) >/dev/null
  [[ -f "${DIST}" ]] || die "前端建置未產生 dist/index.html"
  ok "前端已建置 → frontend/dist"
}

if [[ "${DEV}" == "yes" ]]; then
  node_ready || die "開發模式需要 Node.js 18 以上"
  if [[ -d "${FRONTEND}/node_modules" ]]; then pick_package_manager; else build_frontend; fi
  [[ ${#DEV_CMD[@]} -gt 0 ]] || die "找不到可啟動 Vite 的方式（pnpm／npm／node_modules/vite 都沒有）"
elif [[ "${BUILD}" == "yes" || ! -f "${DIST}" ]]; then
  build_frontend
else
  ok "前端已就緒（frontend/dist）；要重建請加 --build"
fi

# ---------------------------------------------------------------- 5. 啟動
SERVE_ARGS=(serve --host "${HOST}" --port "${PORT}")
[[ -n "${JOBS_DIR}" ]] && SERVE_ARGS+=(--jobs-dir "${JOBS_DIR}")
[[ "${OPEN}" == "yes" ]] && SERVE_ARGS+=(--open)

PIDS=()
VITE_PORT="${VITE_PORT:-5173}"

cleanup() {
  local code=$?
  trap - INT TERM HUP EXIT
  printf '\n'
  info "正在停止…（關閉終端機也會有同樣效果）"
  for pid in "${PIDS[@]:-}"; do
    kill_tree TERM "${pid}"
  done
  for _ in 1 2 3 4 5 6 7 8 9 10; do
    local alive="no"
    for pid in "${PIDS[@]:-}"; do
      [[ -n "${pid}" ]] && kill -0 "${pid}" 2>/dev/null && alive="yes"
    done
    [[ "${alive}" == "no" ]] && break
    sleep 0.3
  done
  for pid in "${PIDS[@]:-}"; do
    kill_tree KILL "${pid}"
  done
  ok "已全部停止"
  exit ${code}
}

# 遞迴處理子行程（例如 pnpm → vite 的兩層結構），確保不會留下孤兒
kill_tree() {
  local signal_name="$1" pid="$2" child
  [[ -z "${pid}" ]] && return 0
  for child in $(pgrep -P "${pid}" 2>/dev/null); do
    kill_tree "${signal_name}" "${child}"
  done
  kill -"${signal_name}" "${pid}" 2>/dev/null || true
}
trap cleanup INT TERM HUP EXIT

printf '\n'
info "summery 啟動中 → http://${HOST}:${PORT}/"
info "停止：按 Ctrl+C，或直接關閉這個終端機視窗"
printf '\n'

if [[ "${DEV}" == "yes" ]]; then
  # 開發模式：Python API + Vite 開發伺服器（Vite 會把 /api 反向代理到上面的埠）
  info "API   http://127.0.0.1:${PORT}"
  info "前端  http://127.0.0.1:${VITE_PORT}（/api 自動轉到 ${PORT}）"
  "${PYTHON}" -m summery serve --host 127.0.0.1 --port "${PORT}" &
  PIDS+=("$!")
  if [[ "${PM_CMD[0]:-}" == "npm" ]]; then
    VITE_ARGS=(-- --host 127.0.0.1 --port "${VITE_PORT}")  # npm 需要 -- 才會轉交參數
  else
    VITE_ARGS=(--host 127.0.0.1 --port "${VITE_PORT}")
  fi
  # 讓 Vite 把 /api 轉到這次的埠，而不是寫死的 8765
  (cd "${FRONTEND}" && SUMMERY_API_ORIGIN="http://127.0.0.1:${PORT}" exec "${DEV_CMD[@]}" "${VITE_ARGS[@]}") &
  PIDS+=("$!")
  # 任一邊結束就整套收掉（不用 wait -n，以兼容 macOS 內建 bash 3.2）
  while true; do
    for pid in "${PIDS[@]}"; do
      kill -0 "${pid}" 2>/dev/null || cleanup
    done
    sleep 1
  done
else
  if [[ "${BASH_VERSINFO[0]:-0}" -ge 4 ]]; then
    exec "${PYTHON}" -m summery "${SERVE_ARGS[@]}"
  fi
  # bash 3.2（macOS 內建）：用子工程式 + trap，確保關掉視窗時不會留下孤兒
  "${PYTHON}" -m summery "${SERVE_ARGS[@]}" &
  PIDS+=("$!")
  wait "${PIDS[0]}"
fi
