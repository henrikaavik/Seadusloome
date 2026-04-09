#!/usr/bin/env sh
# Container entrypoint: run pending migrations, then start uvicorn.
#
# Migrations are idempotent (tracked via the `schema_migrations` table)
# so running this on every boot is safe. Any failure aborts startup so
# Coolify will mark the container unhealthy and roll back to the
# previous working image — much better than starting with a half-built
# schema.
set -e

echo "[entrypoint] Running database migrations..."
python /app/scripts/migrate.py
echo "[entrypoint] Migrations complete. Starting uvicorn..."

# `exec` replaces the shell so uvicorn becomes PID 1 and receives
# SIGTERM from Docker/Coolify for graceful shutdown.
exec uvicorn app.main:app --host 0.0.0.0 --port 5001
