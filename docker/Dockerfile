# Build stage: install dependencies with uv
FROM python:3.13-slim AS builder

COPY --from=ghcr.io/astral-sh/uv:0.11.5 /uv /usr/local/bin/uv

ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy

WORKDIR /app

# Install dependencies first (cache layer)
COPY pyproject.toml uv.lock .python-version ./
RUN uv sync --frozen --no-dev --no-install-project

# Copy application code, helper scripts, and DB migrations.
# Migrations are applied on container boot by docker/entrypoint.sh.
COPY app/ app/
# Only copy runtime scripts (build_icons.py is excluded via .dockerignore)
COPY scripts/ scripts/
COPY migrations/ migrations/

# Runtime stage
FROM python:3.13-slim

# Install curl for HEALTHCHECK and git for the sync pipeline.
# curl: used by the /api/ping HEALTHCHECK below.
# git: app/sync/orchestrator.py clones/pulls the Estonian Legal Ontology repo
#      via subprocess. Without git the sync silently fails and Jena stays
#      empty, which is how we debugged the "Jena has no data" issue at the
#      end of Phase 1.5.
RUN apt-get update && \
    apt-get install -y --no-install-recommends curl git && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY --from=builder /app/.venv /app/.venv
COPY --from=builder /app/app /app/app
COPY --from=builder /app/scripts /app/scripts
COPY --from=builder /app/migrations /app/migrations
COPY docker/entrypoint.sh /app/entrypoint.sh

ENV PATH="/app/.venv/bin:$PATH"
ENV PYTHONPATH="/app"

# Single-worker intentional: FastHTML WebSocket state lives in-process.
# To scale horizontally, add more container replicas (not workers).
ENV WEB_CONCURRENCY=1

# Non-root user for security. Ensure the entrypoint is executable
# and owned by appuser along with the rest of /app.
RUN adduser --system --no-create-home --group appuser && \
    chmod +x /app/entrypoint.sh && \
    chown -R appuser:appuser /app

# Phase 2: pre-create persistent storage directories so appuser can
# write to them at runtime. /var is root-owned; mkdir + chown must
# happen here (as root) before USER appuser. Without this, the first
# upload or export raises PermissionError because appuser cannot
# create /var/seadusloome/ on a fresh container.
RUN mkdir -p /var/seadusloome/drafts /var/seadusloome/exports && \
    chown -R appuser:appuser /var/seadusloome

USER appuser

EXPOSE 5001

# Liveness probe hits /api/ping (see app/main.py). start_period raised to
# 120s (#516) because Phase 3A adds migrations 006 (encrypt_parsed_text +
# drafting_sessions tables) and 007 (llm_usage table), Phase 3C adds
# migrations 008 (chat tables) and 009 (rag_chunks with HNSW vector index),
# plus lazy-load of the anthropic SDK, Voyage embedding provider, and
# cost_tracker on first real LLM call, on top of the existing cold-start
# overhead from Phase 2 (migration 005, lxml, background worker thread,
# rdflib, SPARQLWrapper, FastHTML, psycopg).
HEALTHCHECK --interval=15s --timeout=5s --start-period=120s --retries=3 \
    CMD curl -fsS http://localhost:5001/api/ping || exit 1

# entrypoint.sh runs pending migrations, then execs uvicorn directly
# so uvicorn becomes PID 1 (bypassing FastHTML's serve() auto-detection,
# which assumes the script is run as a top-level file and passes
# "main:app" to uvicorn — that fails when the app lives at app.main:app).
# --host 0.0.0.0 is set inside entrypoint.sh so Traefik can reach the
# container over the internal network.
ENTRYPOINT ["/app/entrypoint.sh"]
