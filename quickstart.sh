#!/usr/bin/env bash
# Quickstart: venv -> install -> pricing sanity test -> run the REST API.
# Uses a PostGrid TEST key by default, so nothing can actually be mailed.
set -euo pipefail
cd "$(dirname "$0")"

if [ ! -d .venv ]; then
  python3 -m venv .venv
fi
.venv/bin/pip install -q -r requirements.txt

echo "== sanity tests =="
.venv/bin/python test_pricing.py
.venv/bin/python test_api_flow.py

echo
echo "== starting REST API on http://127.0.0.1:8000 =="
echo "   Dashboard: http://127.0.0.1:8000/  (enter the admin token printed below)"
echo "   MCP server (separate terminal):"
echo "     POSTGRID_API_KEY=test_yourkey LMS_ADMIN_TOKEN=... .venv/bin/python mcp_server.py"
echo "     -> MCP endpoint: http://127.0.0.1:8001/mcp"
echo
export POSTGRID_API_KEY="${POSTGRID_API_KEY:-test_fake_key_for_local_dev}"
export LMS_ADMIN_TOKEN="${LMS_ADMIN_TOKEN:-dev-admin-token}"
.venv/bin/python -m uvicorn api:app --host 127.0.0.1 --port 8000
