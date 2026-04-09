# Build stage: install dependencies with uv
FROM python:3.13-slim AS builder

COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy

WORKDIR /app

# Install dependencies first (cache layer)
COPY pyproject.toml uv.lock .python-version ./
RUN uv sync --frozen --no-dev --no-install-project

# Copy application code
COPY app/ app/
COPY scripts/ scripts/

# Runtime stage
FROM python:3.13-slim

WORKDIR /app

COPY --from=builder /app/.venv /app/.venv
COPY --from=builder /app/app /app/app
COPY --from=builder /app/scripts /app/scripts

ENV PATH="/app/.venv/bin:$PATH"
ENV PYTHONPATH="/app"

EXPOSE 5001

# Run via -m so the working directory (/app) is on sys.path and
# `from app.* import ...` works. Do NOT use `python app/main.py` — that
# puts /app/app on sys.path instead of /app and breaks every absolute
# import from the app package.
CMD ["python", "-m", "app.main"]
