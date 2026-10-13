#!/usr/bin/env bash
set -Eeuo pipefail

# DSHQuant 本地启动入口：Deck + DeepSeek HARNESS
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PYTHON="$ROOT/.venv/bin/python"
HARNESS_BIN="$ROOT/harness/node_modules/@deepseek-ai/dsh/lib/bin.js"
PORT="${LWQUANT_DECK_PORT:-8787}"
HARNESS_PORT=3080
START_HARNESS=1
OPEN_BROWSER=1
LOG_FILE="$ROOT/logs/start_dhp.log"

usage() {
  cat <<'EOF'
用法: ./start_dhp.sh [--port 8787] [--no-harness] [--no-browser] [--log FILE]

选项:
  --port PORT      Deck 端口，默认 8787
  --no-harness     只启动量化 Deck，不启动 HARNESS GUI
  --no-browser     启动后不自动打开浏览器
  --log FILE       合并日志文件，默认 logs/start_dhp.log
  -h, --help       显示帮助

示例:
  ./start_dhp.sh
  ./start_dhp.sh --port 8877 --no-harness
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --port)
      [[ $# -ge 2 ]] || { echo "[start_dhp] --port 需要参数" >&2; exit 2; }
      PORT="$2"; shift 2 ;;
    --no-harness) START_HARNESS=0; shift ;;
    --no-browser) OPEN_BROWSER=0; shift ;;
    --log)
      [[ $# -ge 2 ]] || { echo "[start_dhp] --log 需要参数" >&2; exit 2; }
      LOG_FILE="$2"; shift 2 ;;
    -h|--help) usage; exit 0 ;;
    *)
      echo "[start_dhp] 未知参数: $1" >&2
      usage >&2
      exit 2 ;;
  esac
done

[[ "$PORT" =~ ^[0-9]+$ ]] || { echo "[start_dhp] 端口必须是数字: $PORT" >&2; exit 2; }
[[ -x "$PYTHON" ]] || {
  echo "[start_dhp] 缺少虚拟环境 Python: $PYTHON" >&2
  echo "[start_dhp] 请先执行: uv venv --python 3.12 .venv && uv pip install --python .venv/bin/python -r requirements.txt" >&2
  exit 1
}
[[ -f "$ROOT/config/params.yaml" ]] || {
  echo "[start_dhp] 缺少 config/params.yaml；请先复制 config/params.yaml.example 并配置" >&2
  exit 1
}

cd "$ROOT"
mkdir -p "$ROOT/logs" "$(dirname "$LOG_FILE")"
touch "$LOG_FILE"

port_busy() {
  local port="$1"
  if command -v lsof >/dev/null 2>&1; then
    lsof -t -iTCP:"$port" -sTCP:LISTEN >/dev/null 2>&1
  else
    python3 -c "import socket; s=socket.create_connection(('127.0.0.1', $port), .5); s.close()" >/dev/null 2>&1
  fi
}

if port_busy "$PORT"; then
  echo "[start_dhp] 端口 $PORT 已被占用。请换 --port，或先停止旧进程。" >&2
  lsof -nP -iTCP:"$PORT" -sTCP:LISTEN >&2 || true
  exit 1
fi

export LWQUANT_CACHE_DIR="${LWQUANT_CACHE_DIR:-$ROOT/data/cache}"
export NO_PROXY='*'
export no_proxy='*'
export PYTHONUTF8=1
export PYTHONUNBUFFERED=1

harness_pid=""
deck_pid=""

cleanup() {
  trap - EXIT INT TERM
  [[ -z "$deck_pid" ]] || kill "$deck_pid" 2>/dev/null || true
  [[ -z "$harness_pid" ]] || kill "$harness_pid" 2>/dev/null || true
  [[ -z "$deck_pid" ]] || wait "$deck_pid" 2>/dev/null || true
  [[ -z "$harness_pid" ]] || wait "$harness_pid" 2>/dev/null || true
}
trap cleanup EXIT INT TERM

echo "[$(date '+%F %T')] [start_dhp] root=$ROOT" | tee -a "$LOG_FILE"
echo "[start_dhp] Python: $PYTHON"
echo "[start_dhp] 数据目录: $LWQUANT_CACHE_DIR"
echo "[start_dhp] 日志: $LOG_FILE"

if [[ ! -f "$LWQUANT_CACHE_DIR/bars.db" ]]; then
  echo "[start_dhp] 提示: 未检测到 bars.db。可先用演示数据或运行 data/free_daily_pipeline.py 初始化。" | tee -a "$LOG_FILE"
fi

if [[ "$START_HARNESS" == "1" ]]; then
  if [[ -f "$HARNESS_BIN" ]] && command -v node >/dev/null 2>&1; then
    if port_busy "$HARNESS_PORT"; then
      echo "[start_dhp] HARNESS 端口 $HARNESS_PORT 已有进程，直接复用: http://127.0.0.1:$HARNESS_PORT/" | tee -a "$LOG_FILE"
    else
      [[ -f "$ROOT/harness/home/.credentials.yaml" ]] || {
        echo "[start_dhp] 提示: HARNESS API Key 未配置，控制台对话可能不可用。" | tee -a "$LOG_FILE"
      }
      (
        cd "$ROOT/harness"
        export DSH_HOME="$ROOT/harness/home"
        exec node "$HARNESS_BIN" web
      ) >>"$LOG_FILE" 2>&1 &
      harness_pid=$!
      echo "[start_dhp] HARNESS PID: ${harness_pid}，地址: http://127.0.0.1:$HARNESS_PORT/" | tee -a "$LOG_FILE"
    fi
  else
    echo "[start_dhp] HARNESS 未启动：缺少 Node.js 或 harness/node_modules。" | tee -a "$LOG_FILE"
  fi
fi

"$PYTHON" -u deck/deck_server.py --port "$PORT" >>"$LOG_FILE" 2>&1 &
deck_pid=$!
echo "[start_dhp] Deck PID: ${deck_pid}，启动中: http://127.0.0.1:$PORT/" | tee -a "$LOG_FILE"

ready=0
for _ in $(seq 1 40); do
  if "$PYTHON" -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:$PORT/', timeout=1).read(1)" >/dev/null 2>&1; then
    ready=1
    break
  fi
  if ! kill -0 "$deck_pid" 2>/dev/null; then
    echo "[start_dhp] Deck 启动失败，最近日志:" >&2
    tail -n 80 "$LOG_FILE" >&2
    exit 1
  fi
  sleep 0.5
done

if [[ "$ready" != "1" ]]; then
  echo "[start_dhp] Deck 在 20 秒内未就绪，请查看日志: $LOG_FILE" >&2
  tail -n 80 "$LOG_FILE" >&2
  exit 1
fi

echo "[start_dhp] Deck 已就绪: http://127.0.0.1:$PORT/"
echo "[start_dhp] 按 Ctrl+C 停止 Deck 和本次脚本拉起的 HARNESS。"

if [[ "$OPEN_BROWSER" == "1" ]]; then
  if command -v open >/dev/null 2>&1; then
    open "http://127.0.0.1:$PORT/" >/dev/null 2>&1 || true
  elif command -v xdg-open >/dev/null 2>&1; then
    xdg-open "http://127.0.0.1:$PORT/" >/dev/null 2>&1 || true
  fi
fi

set +e
wait "$deck_pid"
status=$?
set -e
exit "$status"
