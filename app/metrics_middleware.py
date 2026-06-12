"""Starlette ASGI middleware that records per-request latency.

The middleware is the ONLY starlette-coupled part of the metrics subsystem.
Keeping it out of ``app.metrics`` keeps ``record_metric`` (and the buffer +
flush machinery) importable by the framework-free data layer — the SPARQL
client, the LLM/RAG providers — and the standalone background worker without
transitively loading starlette (#895).

Dependency direction: middleware → metrics core (this module imports
``record_metric`` from ``app.metrics``; ``app.metrics`` never imports back).

Provides:
- ``MetricsMiddleware`` — Starlette ASGI middleware that records per-request
  latency labelled with the *matched route template* and method.
"""

from __future__ import annotations

import time

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response
from starlette.routing import Match

from app.metrics import _MAX_LABEL_LEN, record_metric

# ---------------------------------------------------------------------------
# Starlette middleware
# ---------------------------------------------------------------------------


def _matched_route_template(request: Request) -> str | None:
    """Return the matched route's template (e.g. ``/drafts/{id}``), or None.

    Records the *template* rather than ``request.url.path`` so an attacker
    can't blow up label cardinality by hitting ``/x/<random>`` a million times:
    every variable segment collapses to its placeholder. Returns ``None`` when
    no route matched (a 404 / unrouted path) so those are never recorded.
    """
    scope = request.scope
    # A matched route always populates ``endpoint`` in this Starlette version;
    # its absence means the request 404'd before reaching any handler.
    if scope.get("endpoint") is None:
        return None

    for route in request.app.routes:
        try:
            match, _ = route.matches(scope)
        except Exception:
            continue
        if match is Match.FULL:
            template = getattr(route, "path_format", None) or getattr(route, "path", None)
            if isinstance(template, str) and template:
                return template[:_MAX_LABEL_LEN]
            return None
    return None


class MetricsMiddleware(BaseHTTPMiddleware):
    """Record ``http_request_duration_ms`` for every authenticated request.

    Labels include ``method``, ``route`` (the matched template, not the raw
    path), and ``status``.  Requests are skipped to keep the table clean and
    bounded:

    * ``/static/...`` asset requests (high volume, no signal);
    * unrouted paths / 404s (attacker-chosen paths carry no real route);
    * unauthenticated requests (pre-auth probing shouldn't pollute metrics).
    """

    async def dispatch(self, request: Request, call_next) -> Response:  # type: ignore[type-arg]
        if request.url.path.startswith("/static/"):
            return await call_next(request)

        start = time.perf_counter()
        response: Response = await call_next(request)
        duration_ms = (time.perf_counter() - start) * 1000

        # Routing + the auth Beforeware run downstream of this middleware, so
        # ``endpoint`` and ``auth`` are populated by the time ``call_next``
        # returns.
        if not request.scope.get("auth"):
            return response

        route_template = _matched_route_template(request)
        if route_template is None:
            return response

        record_metric(
            "http_request_duration_ms",
            round(duration_ms, 2),
            {
                "method": request.method,
                "route": route_template,
                "status": response.status_code,
            },
        )
        return response
