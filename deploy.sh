#!/usr/bin/env bash
# Production deploy for 3L-Group Technology — AI Letter Mail.
# Validates config, builds the image, starts the service, waits for health.
set -euo pipefail
cd "$(dirname "$0")"

command -v docker >/dev/null 2>&1 || { echo "Missing: docker" >&2; exit 1; }

if [ ! -f .env ]; then
  cp .env.example .env
  echo "Created .env from the example."
  echo "Fill in POSTGRID_API_KEY and LMS_ADMIN_TOKEN, then re-run ./deploy.sh"
  exit 1
fi

# shellcheck disable=SC1091
set -a; source .env; set +a

PROVIDER="${MAIL_PROVIDER:-postgrid}"
if [ "$PROVIDER" = "lob" ]; then
  [ -n "${LOB_API_KEY:-}" ] || { echo "FATAL: LOB_API_KEY is empty in .env (MAIL_PROVIDER=lob)" >&2; exit 1; }
else
  [ -n "${POSTGRID_API_KEY:-}" ] || { echo "FATAL: POSTGRID_API_KEY is empty in .env" >&2; exit 1; }
  case "${POSTGRID_API_KEY}" in
    *REPLACE_ME*) echo "FATAL: POSTGRID_API_KEY is still a placeholder in .env" >&2; exit 1;;
  esac
fi
[ -n "${LMS_ADMIN_TOKEN:-}" ] || { echo "FATAL: LMS_ADMIN_TOKEN is empty in .env" >&2; exit 1; }
case "$LMS_ADMIN_TOKEN" in
  *REPLACE_ME*) echo "FATAL: LMS_ADMIN_TOKEN is still a placeholder in .env" >&2; exit 1;;
esac

echo "== building =="
docker compose build

echo "== starting =="
docker compose up -d

echo "== waiting for health =="
for _ in $(seq 1 30); do
  if curl -sf http://127.0.0.1:8000/health >/dev/null 2>&1; then
    echo "UP: dashboard at http://127.0.0.1:8000/  (MCP on :8001)"
    docker compose ps
    exit 0
  fi
  sleep 2
done

echo "Service did not become healthy. Last log lines:" >&2
docker compose logs --tail=50 >&2
exit 1
