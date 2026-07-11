#!/bin/bash
# One-tap dashboard launcher: starts the FastAPI backend + Vite frontend dev
# servers if they aren't already running (same commands as .claude/launch.json),
# waits for the frontend to come up, then opens it in the default browser.
# Meant to be wired into a macOS Shortcut / Automator app for a single click.
set -e
cd "$(dirname "$0")/.."

BACKEND_PORT=8000
FRONTEND_PORT=5173
LOG_DIR="/tmp/stockmoney-dashboard"
mkdir -p "$LOG_DIR"

if ! lsof -i ":$BACKEND_PORT" -sTCP:LISTEN -t >/dev/null 2>&1; then
  echo "starting backend on :$BACKEND_PORT..."
  nohup uv run uvicorn stockmoney.api.main:app --reload --port "$BACKEND_PORT" \
    > "$LOG_DIR/backend.log" 2>&1 &
  disown
fi

if ! lsof -i ":$FRONTEND_PORT" -sTCP:LISTEN -t >/dev/null 2>&1; then
  echo "starting frontend on :$FRONTEND_PORT..."
  nohup npm --prefix frontend run dev -- --port "$FRONTEND_PORT" --strictPort \
    > "$LOG_DIR/frontend.log" 2>&1 &
  disown
fi

# Wait up to ~15s for the frontend to actually be listening before opening it.
for _ in $(seq 1 30); do
  if lsof -i ":$FRONTEND_PORT" -sTCP:LISTEN -t >/dev/null 2>&1; then
    break
  fi
  sleep 0.5
done

open "http://localhost:$FRONTEND_PORT"
