#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
PYTHON_BIN="${PYTHON_BIN:-$ROOT/.venv/bin/python}"

mode="${1:-0}"
port=""
host="${HOST:-}"
baud="${BAUD:-115200}"
rate="${RATE:-100}"
WEB_PORT="${WEB_PORT:-8765}"

case "$mode" in
  0|usb|wired|wire)
    port="${2:-/dev/ttyACM0}"
    host="${host:-127.0.0.1}"
    ;;
  1|esp32|wifi)
    port="socket://192.168.4.1:3333"
    host="${host:-127.0.0.1}"
    ;;
  2|phone|mobile)
    port="socket://192.168.4.1:3333"
    host="${host:-0.0.0.0}"
    ;;
  socket://*|/dev/*|COM*)
    port="$mode"
    host="${host:-127.0.0.1}"
    ;;
  *)
    echo "用法:"
    echo "  ./scripts/start.sh 0 [/dev/ttyACM0] # 有线 USB"
    echo "  ./scripts/start.sh 1                # ESP32 无线，电脑本机打开"
    echo "  ./scripts/start.sh 2                # 手机模式，手机可打开电脑面板"
    echo "  ./scripts/start.sh socket://192.168.4.1:3333"
    exit 2
    ;;
esac

if [[ ! -x "$PYTHON_BIN" ]]; then
    echo "Python venv not found: $PYTHON_BIN" >&2
    echo "Run: ./scripts/setup.sh" >&2
    exit 1
fi

exec "$PYTHON_BIN" "$ROOT/server.py" \
    --host "$host" \
    --web-port "$WEB_PORT" \
    --port "$port" \
    --baud "$baud" \
    --rate "$rate" \
    --open \
    --autoconnect
