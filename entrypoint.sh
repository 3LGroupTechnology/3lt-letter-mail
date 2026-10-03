#!/usr/bin/env bash
# Production entrypoint: runs the REST API and the MCP server side by side.
set -euo pipefail

if [ -z "${LMS_ADMIN_TOKEN:-}" ]; then
  echo "[lms] FATAL: LMS_ADMIN_TOKEN is not set. Refusing to start." >&2
  exit 1
fi

: "${LMS_DB_PATH:=/data/lms.db}"
export LMS_DB_PATH

# Start MCP server in the background (agents connect over streamable HTTP).
LMS_MCP_HOST="${LMS_MCP_HOST:-0.0.0.0}" LMS_MCP_PORT="${LMS_MCP_PORT:-8001}" \
  python mcp_server.py &
MCP_PID=$!

# Start the REST API + dashboard in the foreground so Docker tracks it.
_term() {
  kill -TERM "$MCP_PID" 2>/dev/null || true
}
trap _term TERM INT

python -m uvicorn api:app --host 0.0.0.0 --port 8000 &
API_PID=$!

wait "$API_PID"
_term
wait "$MCP_PID" 2>/dev/null || true
