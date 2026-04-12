# Build stage: install dependencies with uv
FROM python:3.13-slim AS builder

# Build metadata — populated by CI via --build-arg (see .github/workflows/ci.yml).
# Defaults let local `docker build` work without explicit args; in that case we
# try to derive GIT_SHA from the copied .git directory below, and BUILD_TIME
# stays "unknown" unless the caller passes one in.
ARG GIT_SHA=dev
ARG BUILD_TIME=unknown

COPY --from=ghcr.io/astral-sh/uv:0.11.5 /uv /usr/local/bin/uv

ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy

WORKDIR /app

# git lets us derive the sha from .git when GIT_SHA build-arg is the
# default "dev". Installed in the builder stage only — the runtime
# stage already installs git for the sync pipeline.
RUN apt-get update && \
    apt-get install -y --no-install-recommends git && \
    rm -rf /var/lib/apt/lists/*

# Install dependencies first (cache layer)
COPY pyproject.toml uv.lock .python-version ./
RUN uv sync --frozen --no-dev --no-install-project

# Copy application code, helper scripts, and DB migrations.
# Migrations are applied on container boot by docker/entrypoint.sh.
COPY app/ app/
# Only copy runtime scripts (build_icons.py is excluded via .dockerignore)
COPY scripts/ scripts/
COPY migrations/ migrations/

# Copy .git so we can resolve HEAD at build time when GIT_SHA is unset.
# Coolify builds from a fresh git clone so .git is present in the build
# context. The directory is NOT copied into the runtime stage — only the
# generated VERSION.json is — so image size is unaffected.
COPY .git/ .git/

# Write /app/VERSION.json. If the build-arg GIT_SHA is still the default
# "dev" we fall back to `git rev-parse HEAD`; if that also fails we keep
# "dev". We do this after the app copy so the runtime stage can just
# COPY --from=builder /app/VERSION.json without caring how it was built.
RUN resolved_sha="$GIT_SHA"; \
    if [ "$resolved_sha" = "dev" ] && [ -d .git ]; then \
      resolved_sha="$(git rev-parse HEAD 2>/dev/null || echo dev)"; \
    fi; \
    printf '{"app": "0.1.0", "sha": "%s", "built_at": "%s"}\n' \
      "$resolved_sha" "$BUILD_TIME" > /app/VERSION.json && \
    cat /app/VERSION.json

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
COPY --from=builder /app/VERSION.json /app/VERSION.json
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
