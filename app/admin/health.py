"""Admin health checks and health card rendering."""

from __future__ import annotations

import logging

from fasthtml.common import *  # noqa: F403
from starlette.requests import Request
from starlette.responses import JSONResponse

from app.admin._shared import _tooltip
from app.db import get_connection as _connect
from app.sync.jena_loader import check_health as jena_check_health
from app.ui.primitives.badge import StatusBadge
from app.ui.primitives.button import Button  # noqa: F401, F811  -- shadow guard
from app.ui.surfaces.card import Card, CardBody, CardHeader
from app.version import read_version  # noqa: F401  -- rebound onto shim module

logger = logging.getLogger(__name__)


def _check_postgres() -> bool:
    """Check if PostgreSQL is reachable."""
    try:
        with _connect() as conn:
            conn.execute("SELECT 1")
        return True
    except Exception:
        logger.exception("PostgreSQL health check failed")
        return False


def _health_card(jena_ok: bool, pg_ok: bool):
    """Render the system health card."""
    body = Dl(  # noqa: F405
        Dt("Apache Jena Fuseki"),  # noqa: F405
        Dd(StatusBadge("ok") if jena_ok else StatusBadge("failed")),  # noqa: F405
        Dt("PostgreSQL"),  # noqa: F405
        Dd(StatusBadge("ok") if pg_ok else StatusBadge("failed")),  # noqa: F405
        cls="info-list",
    )
    return Card(
        CardHeader(
            H3(  # noqa: F405
                "S\u00fcsteemi tervis",
                _tooltip("Jena ja Postgres \u00fchenduse staatus"),
                cls="card-title",
            )
        ),
        CardBody(body),
    )


def health_check(req: Request):
    """GET /api/health — JSON health check endpoint (unauthenticated).

    Returns a JSON response suitable for Coolify or uptime monitoring:
    {"status": "ok", "jena": true/false, "postgres": true/false}
    """
    jena_ok = jena_check_health()
    pg_ok = _check_postgres()
    overall = "ok" if (jena_ok and pg_ok) else "degraded"

    return JSONResponse(
        {
            "status": overall,
            "jena": jena_ok,
            "postgres": pg_ok,
            "version": read_version(),
        }
    )
