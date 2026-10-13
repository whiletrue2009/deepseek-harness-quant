#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
PYTHON="$ROOT/.venv/bin/python"
HARNESS_BIN="$ROOT/harness/node_modules/@deepseek-ai/dsh/lib/bin.js"
PORT=8787
START_HARNESS=1

usage() {
  cat <<'EOF'
用法: scripts/run_macos.sh [--port 8787] [--no-harness]

选项:
  --port PORT     Deck 端口，默认 8787
  --no-harness    只启动量化 Deck，不启动 HARNESS GUI
  -h, --help      显示帮助
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --port)
      [[ $# -ge 2 ]] || { echo "--port 需要参数" >&2; exit 2; }
      PORT="$2"; shift 2 ;;
    --no-harness) START_HARNESS=0; shift ;;
    -h|--help) usage; exit 0 ;;
    *) echo "未知参数: $1" >&2; usage >&2; exit 2 ;;
  esac
done

[[ "$PORT" =~ ^[0-9]+$ ]] || { echo "端口必须是数字: $PORT" >&2; exit 2; }
[[ -x "$PYTHON" ]] || { echo "缺少虚拟环境 Python: $PYTHON；先运行 uv venv .venv && uv pip install -r requirements.txt" >&2; exit 1; }
[[ -f "$ROOT/config/params.yaml" ]] || { echo "缺少 config/params.yaml；先复制并配置模板" >&2; exit 1; }

if command -v lsof >/dev/null 2>&1 && lsof -t -iTCP:"$PORT" -sTCP:LISTEN >/dev/null 2>&1; then
  echo "端口 $PORT 已被占用。先停止旧服务，或换 --port。" >&2
  exit 1
fi

export LWQUANT_CACHE_DIR="${LWQUANT_CACHE_DIR:-$ROOT/data/cache}"
export NO_PROXY='*'
export no_proxy='*'

harness_pid=""
cleanup() {
  if [[ -n "$harness_pid" ]] && kill -0 "$harness_pid" 2>/dev/null; then
    kill "$harness_pid" 2>/dev/null || true
    wait "$harness_pid" 2>/dev/null || true
  fi
}
trap cleanup EXIT INT TERM

echo "[DSHQuant] Python: $PYTHON"
echo "[DSHQuant] 数据目录: $LWQUANT_CACHE_DIR"
if [[ "$START_HARNESS" == "1" ]]; then
  if [[ -f "$HARNESS_BIN" ]] && command -v node >/dev/null 2>&1; then
    echo "[DSHQuant] HARNESS: http://127.0.0.1:3080/"
    (
      cd "$ROOT/harness"
      DSH_HOME="$ROOT/harness/home" exec node "$HARNESS_BIN" web
    ) &
    harness_pid=$!
  else
    echo "[DSHQuant] 未启动 HARNESS：缺少 node 或 harness/node_modules" >&2
  fi
fi

echo "[DSHQuant] Deck: http://127.0.0.1:$PORT/"
cd "$ROOT"
exec "$PYTHON" -u deck/deck_server.py --port "$PORT"
