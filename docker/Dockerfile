# Build stage: install dependencies with uv
FROM python:3.13-slim AS builder

COPY --from=ghcr.io/astral-sh/uv:0.11.5 /uv /usr/local/bin/uv

ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy

WORKDIR /app

# Install dependencies first (cache layer)
COPY pyproject.toml uv.lock .python-version ./
RUN uv sync --frozen --no-dev --no-install-project

# Copy application code
COPY app/ app/
# Only copy runtime scripts (build_icons.py is excluded via .dockerignore)
COPY scripts/ scripts/

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

ENV PATH="/app/.venv/bin:$PATH"
ENV PYTHONPATH="/app"

# Single-worker intentional: FastHTML WebSocket state lives in-process.
# To scale horizontally, add more container replicas (not workers).
ENV WEB_CONCURRENCY=1

# Non-root user for security
RUN adduser --system --no-create-home --group appuser && \
    chown -R appuser:appuser /app
USER appuser

EXPOSE 5001

# Liveness probe hits /api/ping (see app/main.py). start_period raised to
# 60s (#432) because cold-start imports (rdflib, SPARQLWrapper, FastHTML,
# psycopg) can exceed 30s on a small VPS under load.
HEALTHCHECK --interval=15s --timeout=5s --start-period=60s --retries=3 \
    CMD curl -fsS http://localhost:5001/api/ping || exit 1

# Invoke uvicorn directly (bypass FastHTML's serve() auto-detection,
# which assumes the script is run as a top-level file and passes
# "main:app" to uvicorn — that fails when the app lives at app.main:app).
# --host 0.0.0.0 lets Traefik reach the container over the internal network.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "5001"]
