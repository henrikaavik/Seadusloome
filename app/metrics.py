"""Application-level performance metrics: recording, middleware, and helpers.

Provides:
- ``record_metric(name, value, labels)`` — buffer a metric for periodic
  bulk INSERT into the ``metrics`` Postgres table (created by migration 011).
  Never blocks the caller; flushes every ``_FLUSH_INTERVAL`` seconds or when
  the buffer reaches ``_FLUSH_SIZE`` entries.
- ``MetricsMiddleware`` — Starlette ASGI middleware that records per-request
  latency with route path and method as labels.
- ``track_duration(name, **labels)`` — context manager that measures a code
  block's wall-clock duration in milliseconds and records it.
"""

from __future__ import annotations

import atexit
import json
import logging
import threading
import time
from collections import deque
from contextlib import contextmanager
from typing import Any

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

from app.db import get_connection as _connect

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Buffered metric recording
# ---------------------------------------------------------------------------

_BUFFER: deque[tuple[str, float, str | None]] = deque()
_FLUSH_INTERVAL = 10.0  # seconds
_FLUSH_SIZE = 100  # flush if buffer hits this many entries
_lock = threading.Lock()
_flush_timer: threading.Timer | None = None


def _flush_buffer() -> None:
    """Bulk-INSERT all buffered metrics into Postgres."""
    global _flush_timer
    items: list[tuple[str, float, str | None]] = []
    with _lock:
        while _BUFFER:
            items.append(_BUFFER.popleft())
        _flush_timer = None

    if not items:
        return

    try:
        with _connect() as conn:
            cur = conn.cursor()
            cur.executemany(
                "INSERT INTO metrics (name, value, labels) VALUES (%s, %s, %s::jsonb)",
                items,
            )
            conn.commit()
    except Exception:
        logger.debug("Failed to flush %d metrics", len(items), exc_info=True)


def _schedule_flush() -> None:
    """Schedule a deferred flush if one isn't already pending."""
    global _flush_timer
    if _flush_timer is None:
        _flush_timer = threading.Timer(_FLUSH_INTERVAL, _flush_buffer)
        _flush_timer.daemon = True
        _flush_timer.start()


def record_metric(name: str, value: float, labels: dict[str, Any] | None = None) -> None:
    """Buffer a metric for bulk INSERT. Never blocks the caller."""
    try:
        labels_json = json.dumps(labels) if labels else None
        with _lock:
            _BUFFER.append((name, value, labels_json))
            buf_len = len(_BUFFER)

        if buf_len >= _FLUSH_SIZE:
            # Flush immediately in a background thread
            threading.Thread(target=_flush_buffer, daemon=True).start()
        else:
            _schedule_flush()
    except Exception:
        logger.debug("Failed to buffer metric %s", name, exc_info=True)


# Flush remaining metrics on interpreter shutdown
atexit.register(_flush_buffer)


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
