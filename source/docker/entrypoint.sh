#!/bin/sh
# Migrate, then serve.
#
# Migrations run here rather than in the application's startup hook because
# uvicorn forks its workers before that hook runs, so two processes would race
# to apply the same revision. One process, before anything is listening.
set -eu

echo "[entrypoint] migrating database to head"
alembic upgrade head

echo "[entrypoint] starting uvicorn"
exec uvicorn app.main:app \
  --host 0.0.0.0 \
  --port 8000 \
  --proxy-headers \
  --forwarded-allow-ips '*' \
  --workers "${UVICORN_WORKERS:-2}"
