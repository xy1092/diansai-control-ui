#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
PYTHON_BIN="${PYTHON_BIN:-$ROOT/.venv/bin/python}"

PORT="${1:-${PORT:-/dev/ttyACM0}}"
BAUD="${2:-${BAUD:-115200}}"
RATE="${3:-${RATE:-100}}"
HOST="${HOST:-127.0.0.1}"
WEB_PORT="${WEB_PORT:-8765}"

if [[ ! -x "$PYTHON_BIN" ]]; then
    echo "Python venv not found: $PYTHON_BIN" >&2
    echo "Run: ./scripts/setup.sh" >&2
    exit 1
fi

exec "$PYTHON_BIN" "$ROOT/server.py" \
    --host "$HOST" \
    --web-port "$WEB_PORT" \
    --port "$PORT" \
    --baud "$BAUD" \
    --rate "$RATE" \
    --open \
    --autoconnect
