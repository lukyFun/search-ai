#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT_DIR"

RUN_DIR="${ROOT_DIR}/.run"
PID_FILE="${RUN_DIR}/app.pid"
LOG_FILE="${RUN_DIR}/app.log"
# Python 端 RotatingFileHandler 自管理 app.log；shell 只把 nohup 的 stdout/stderr
# 转到独立文件，避免双写覆盖 + 给"Python 启动前抛错"留兜底。
STDERR_LOG_FILE="${RUN_DIR}/app.stderr.log"

APP_HOST="${APP_HOST:-0.0.0.0}"
APP_PORT="${APP_PORT:-8100}"
RUN_MODE="${RUN_MODE:-dev}"
FOREGROUND="${FOREGROUND:-0}"

if [[ "${1:-}" == "--prod" ]]; then
  RUN_MODE="prod"
fi
if [[ "${1:-}" == "--fg" ]] || [[ "${2:-}" == "--fg" ]]; then
  FOREGROUND="1"
fi

mkdir -p "$RUN_DIR" "${ROOT_DIR}/data"

if [[ -f "${ROOT_DIR}/.env" ]]; then
  set -a
  source "${ROOT_DIR}/.env"
  set +a
fi

export MODEL_PATH="${MODEL_PATH:-${ROOT_DIR}/models/bge-m3}"
export CHROMA_PERSIST_DIRECTORY="${CHROMA_PERSIST_DIRECTORY:-${ROOT_DIR}/data/chromadb}"

wait_for_app_port() {
  local port="$1"
  local i
  for i in {1..120}; do
    if python3 - "$port" <<'PY'
import socket, sys
port = int(sys.argv[1])
try:
    s = socket.create_connection(("127.0.0.1", port), timeout=0.2)
    s.close()
    raise SystemExit(0)
except OSError:
    raise SystemExit(1)
PY
    then
      return 0
    fi
    sleep 0.5
  done
  return 1
}

if [[ -f "$PID_FILE" ]]; then
  existing_pid="$(cat "$PID_FILE" || true)"
  if [[ -n "$existing_pid" ]] && kill -0 "$existing_pid" >/dev/null 2>&1; then
    echo "服务已在运行 (pid=${existing_pid})，日志：${LOG_FILE}"
    exit 0
  fi
  rm -f "$PID_FILE"
fi

if ! command -v python3 >/dev/null 2>&1; then
  echo "python3 不可用，请先安装 Python 并执行: pip install -r requirements.txt"
  exit 1
fi

python3 -c 'import fastapi,uvicorn,aiosqlite,chromadb' >/dev/null 2>&1 || {
  echo "Python 依赖未安装，请先执行: pip install -r requirements.txt"
  exit 1
}

UVICORN_ARGS=(app.main:app --host "$APP_HOST" --port "$APP_PORT")
if [[ "$RUN_MODE" != "prod" ]]; then
  UVICORN_ARGS+=(--reload)
fi

if [[ "$FOREGROUND" == "1" ]]; then
  python3 -m uvicorn "${UVICORN_ARGS[@]}"
else
  nohup python3 -m uvicorn "${UVICORN_ARGS[@]}" >"$STDERR_LOG_FILE" 2>&1 &
  echo $! >"$PID_FILE"
  if ! wait_for_app_port "$APP_PORT"; then
    echo "服务启动超时，请检查日志: ${LOG_FILE} 或 ${STDERR_LOG_FILE}"
    tail -n 120 "$STDERR_LOG_FILE" || true
    tail -n 120 "$LOG_FILE" 2>/dev/null || true
    exit 1
  fi
  echo "服务已启动: http://localhost:${APP_PORT}  (pid=$(cat "$PID_FILE"))"
  echo "结构化日志: ${LOG_FILE}（RotatingFileHandler 自动滚动）"
  echo "启动 stderr: ${STDERR_LOG_FILE}"
fi
