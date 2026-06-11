#!/usr/bin/env sh
# Container entrypoint: run pending migrations, then start uvicorn.
#
# Migrations are idempotent (tracked via the `schema_migrations` table)
# so running this on every boot is safe. Any failure aborts startup so
# Coolify will mark the container unhealthy and roll back to the
# previous working image — much better than starting with a half-built
# schema.
#
# set -e  : abort on any non-zero exit (prevents starting uvicorn after a
#           failed migration)
# set -u  : treat unset variables as errors — catches missing required env
#           vars like DATABASE_URL before they cause a cryptic psycopg
#           OperationalError deep inside the migration runner.
set -eu

# Guard: DATABASE_URL must be set and non-empty.  Without it the migration
# runner connects to nothing and silently exits 0 (psycopg falls back to
# libpq env vars, which are also unset, producing a confusing error).
# Fail loud here so Coolify's rollback logic kicks in immediately.
if [ -z "${DATABASE_URL:-}" ]; then
    echo "[entrypoint] ERROR: DATABASE_URL is not set. Aborting." >&2
    exit 1
fi

echo "[entrypoint] Running database migrations..."
python /app/scripts/migrate.py
echo "[entrypoint] Migrations complete. Starting uvicorn..."

# `exec` replaces the shell so uvicorn becomes PID 1 and receives
# SIGTERM from Docker/Coolify for graceful shutdown.
exec uvicorn app.main:app --host 0.0.0.0 --port 5001
