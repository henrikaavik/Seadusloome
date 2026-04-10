"""Application-level performance metrics: recording, middleware, and helpers.

Provides:
- ``record_metric(name, value, labels)`` — fire-and-forget INSERT into the
  ``metrics`` Postgres table (created by migration 011).
- ``MetricsMiddleware`` — Starlette ASGI middleware that records per-request
  latency with route path and method as labels.
- ``track_duration(name, **labels)`` — context manager that measures a code
  block's wall-clock duration in milliseconds and records it.
"""

from __future__ import annotations

import logging
import time
from contextlib import contextmanager
from typing import Any

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

from app.db import get_connection as _connect

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Core recording helper
# ---------------------------------------------------------------------------


def record_metric(name: str, value: float, labels: dict[str, Any] | None = None) -> None:
    """INSERT a metric row into the ``metrics`` table.

    This is fire-and-forget: failures are logged but never propagated so
    callers (middleware, helpers) do not crash the request on a DB hiccup.
    """
    try:
        import json

        labels_json = json.dumps(labels) if labels else None
        with _connect() as conn:
            conn.execute(
                "INSERT INTO metrics (name, value, labels) VALUES (%s, %s, %s::jsonb)",
                (name, value, labels_json),
            )
            conn.commit()
    except Exception:
        logger.debug("Failed to record metric %s", name, exc_info=True)


# ---------------------------------------------------------------------------
# Starlette middleware
# ---------------------------------------------------------------------------


class MetricsMiddleware(BaseHTTPMiddleware):
    """Record ``http_request_duration_ms`` for every request.

    Labels include ``method``, ``path``, and ``status``.  Static-file
    requests (``/static/...``) are excluded to avoid flooding the table.
    """

    async def dispatch(self, request: Request, call_next) -> Response:  # type: ignore[type-arg]
        if request.url.path.startswith("/static/"):
            return await call_next(request)

        start = time.perf_counter()
        response: Response = await call_next(request)
        duration_ms = (time.perf_counter() - start) * 1000

        record_metric(
            "http_request_duration_ms",
            round(duration_ms, 2),
            {
                "method": request.method,
                "path": request.url.path,
                "status": response.status_code,
            },
        )
        return response


# ---------------------------------------------------------------------------
# Context manager helper
# ---------------------------------------------------------------------------


@contextmanager
def track_duration(name: str, **labels: Any):  # type: ignore[no-untyped-def]
    """Measure a code block's wall-clock duration and record it as a metric.

    Usage::

        with track_duration("sparql_query_ms", query_type="category"):
            result = run_sparql(query)
    """
    start = time.perf_counter()
    try:
        yield
    finally:
        duration_ms = (time.perf_counter() - start) * 1000
        record_metric(name, round(duration_ms, 2), labels if labels else None)
