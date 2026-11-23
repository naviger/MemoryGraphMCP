#!/bin/sh
set -eu
export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:${PATH:-}"
cd "$(dirname "$0")/.."
exec .venv/bin/python -m uvicorn scripts.test_draft_worker:app --env-file .env \
  --host 127.0.0.1 --port "${MGMCP_DRAFT_WORKER_PORT:-3211}"
