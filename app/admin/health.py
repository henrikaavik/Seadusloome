"""Admin health checks, aggregator card, and aggregator detail page.

The original module owned a tiny ``_health_card`` showing two pills
(Jena + Postgres) plus the unauthenticated ``/api/health`` JSON probe.
Issue #183 layered on a richer rollup: ``_get_system_health`` collects a
broader set of signals (pgvector extension, last sync_log status,
worker activity, HTTP traffic) and ``_health_aggregator_card`` renders
those as a single Card on the admin dashboard, with a link to the
``/admin/health/aggregator`` detail page for timestamps + error
messages + a manual refresh button.

Why this lives next to the existing health code instead of a new
``app.admin.health_aggregator`` module:

* The two pieces share the postgres / jena reachability probes — the
  aggregator is a strict superset, not a parallel implementation.
* The shim re-exports ``_check_postgres`` and ``health_check`` from
  this module; keeping the new helpers here means the shim's import
  list stays minimal and the patch-where-used contract is unchanged.
* Tests can patch ``app.admin.health.<name>`` for every signal in one
  place rather than juggling two modules.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

from fasthtml.common import *  # noqa: F403
from starlette.requests import Request
from starlette.responses import JSONResponse

from app.admin._shared import _render_admin_error_page, _tooltip
from app.db import get_connection as _connect
from app.sync.jena_loader import check_health as jena_check_health
from app.ui.layout import PageShell
from app.ui.primitives.badge import StatusBadge
from app.ui.primitives.button import Button  # noqa: F401, F811  -- shadow guard
from app.ui.surfaces.card import Card, CardBody, CardHeader
from app.ui.theme import get_theme_from_request
from app.ui.time import format_tallinn
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


def _check_pgvector() -> bool:
    """Check if the pgvector extension is installed in the active database.

    Returns ``False`` if the extension is missing **or** the lookup
    query itself fails (treated as "not available"). The aggregator
    downgrades to ``degraded`` when this is false because chat and the
    RAG retriever can't function without it, but core legal workflows
    (impact analysis, drafter wizard) continue to work.
    """
    try:
        with _connect() as conn:
            row = conn.execute(
                "SELECT extname FROM pg_extension WHERE extname = 'vector'"
            ).fetchone()
        return row is not None
    except Exception:
        logger.exception("pgvector extension check failed")
        return False


def _get_last_sync() -> dict[str, Any] | None:
    """Return the most recent ``sync_log`` row's summary, or ``None``.

    Only the fields the aggregator card + detail page need are returned:
    status, finished_at, duration in seconds, and the error_message
    (which the detail page surfaces inline). When the table is empty
    or the query fails, ``None`` is returned so callers can render the
    "Sünki pole veel tehtud" empty state.
    """
    try:
        with _connect() as conn:
            row = conn.execute(
                "SELECT status, started_at, finished_at, error_message "
                "FROM sync_log "
                "ORDER BY started_at DESC LIMIT 1"
            ).fetchone()
        if not row:
            return None
        status, started_at, finished_at, error_message = row
        duration_s: float | None = None
        if started_at is not None and finished_at is not None:
            try:
                duration_s = max(0.0, (finished_at - started_at).total_seconds())
            except Exception:  # pragma: no cover — defensive
                duration_s = None
        return {
            "status": status,
            "started_at": started_at,
            "finished_at": finished_at,
            "duration_s": duration_s,
            "error_message": error_message,
        }
    except Exception:
        logger.exception("Failed to fetch last sync_log row")
        return None


def _get_worker_recent_activity() -> int:
    """Return count of ``job_execution_ms`` metric rows in the last 5 minutes.

    This is the signal PR #835's worker-side instrumentation publishes
    on every completed job. Until that PR lands the value will be 0,
    which is intentional — the aggregator surfaces "Töötaja aktiivsus:
    0" rather than crashing or hiding the row.
    """
    try:
        with _connect() as conn:
            row = conn.execute(
                "SELECT COUNT(*) FROM metrics "
                "WHERE name = 'job_execution_ms' "
                "  AND recorded_at >= now() - interval '5 minutes'"
            ).fetchone()
        return int(row[0]) if row else 0
    except Exception:
        logger.exception("Failed to count recent worker activity")
        return 0


def _get_metrics_recent_count() -> int:
    """Return total metric rows recorded in the last 5 minutes.

    Sourced from ``MetricsMiddleware`` which records one row per HTTP
    request. A non-zero value is the simplest proof that the app is
    serving traffic and the metrics flush pipeline is healthy.
    """
    try:
        with _connect() as conn:
            row = conn.execute(
                "SELECT COUNT(*) FROM metrics WHERE recorded_at >= now() - interval '5 minutes'"
            ).fetchone()
        return int(row[0]) if row else 0
    except Exception:
        logger.exception("Failed to count recent metrics rows")
        return 0


def _get_system_health() -> dict[str, Any]:
    """Aggregate broad system signals for the dashboard rollup card.

    ``overall_status`` is derived as:

    * ``"down"`` if Postgres is unreachable (everything else depends on it),
    * ``"degraded"`` if pgvector or Jena is failing (core workflows still
      function but a major subsystem is offline),
    * ``"ok"`` when every hard check passes.

    The function never raises — each underlying probe swallows its own
    exception and returns a falsy / safe default so a partially
    broken environment still renders a card the admin can act on.
    """
    pg_ok = _check_postgres()
    jena_ok = jena_check_health()
    pgvector_ok = _check_pgvector() if pg_ok else False
    last_sync = _get_last_sync() if pg_ok else None
    worker_recent = _get_worker_recent_activity() if pg_ok else 0
    metrics_recent = _get_metrics_recent_count() if pg_ok else 0

    if not pg_ok:
        overall = "down"
    elif not (pgvector_ok and jena_ok):
        overall = "degraded"
    else:
        overall = "ok"

    return {
        "postgres_ok": pg_ok,
        "jena_ok": jena_ok,
        "pgvector_ok": pgvector_ok,
        "last_sync": last_sync,
        "worker_recent_activity": worker_recent,
        "metrics_recent_count": metrics_recent,
        "overall_status": overall,
    }


def _health_card(jena_ok: bool, pg_ok: bool):
    """Render the legacy small two-pill health card.

    Kept for callers that haven't migrated to the richer aggregator
    card yet (the JSON ``/api/health`` endpoint still uses the same
    two reachability probes).
    """
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
                "Süsteemi tervis",
                _tooltip("Jena ja Postgres ühenduse staatus"),
                cls="card-title",
            )
        ),
        CardBody(body),
    )


def _last_sync_summary(last_sync: dict[str, Any] | None) -> str:
    """Format the ``Viimane sünk`` row for the aggregator card."""
    if last_sync is None:
        return "Sünki pole veel tehtud"
    status = last_sync.get("status") or "?"
    finished_at = last_sync.get("finished_at")
    if finished_at is None:
        return f"{status} (lõpetamata)"
    return f"{status} • {format_tallinn(finished_at)}"


def _bool_badge(ok: bool):
    """Render a green OK / red Ebaõnnestus pill for boolean health rows."""
    return StatusBadge("ok") if ok else StatusBadge("failed")


def _activity_badge(count: int):
    """Render a count as a neutral/primary status badge.

    Zero counts use the muted ``pending`` variant so the admin reads it
    as "nothing happening yet" rather than "broken"; non-zero counts
    use the primary running variant to mirror the live-traffic feel.
    """
    if count <= 0:
        return StatusBadge("pending")
    # Re-use the StatusBadge formatting but surface the actual number
    # rather than the canned "Töötab" label.
    return Span(  # noqa: F405
        Span("", cls="status-dot", aria_hidden="true"),  # noqa: F405
        str(count),
        cls="badge badge-primary status-badge status-running",
        role="status",
    )


def _overall_badge(status: str):
    """Map ``overall_status`` to a StatusBadge."""
    if status == "ok":
        return StatusBadge("ok")
    if status == "degraded":
        return StatusBadge("warning")
    return StatusBadge("failed")


def _health_aggregator_card():
    """Render the broad system-health rollup card for the admin dashboard.

    One row per subsystem with a status badge; the bottom carries a
    "Vaata üksikasju →" link to ``/admin/health/aggregator`` where
    timestamps and last-error messages are surfaced.
    """
    health = _get_system_health()
    last_sync = health["last_sync"]
    body = Dl(  # noqa: F405
        Dt("Postgres"),  # noqa: F405
        Dd(_bool_badge(health["postgres_ok"])),  # noqa: F405
        Dt("Jena"),  # noqa: F405
        Dd(_bool_badge(health["jena_ok"])),  # noqa: F405
        Dt("pgvector"),  # noqa: F405
        Dd(_bool_badge(health["pgvector_ok"])),  # noqa: F405
        Dt("Viimane sünk"),  # noqa: F405
        Dd(_last_sync_summary(last_sync)),  # noqa: F405
        Dt("Töötaja aktiivsus (5m)"),  # noqa: F405
        Dd(_activity_badge(int(health["worker_recent_activity"]))),  # noqa: F405
        Dt("Metrics (5m)"),  # noqa: F405
        Dd(_activity_badge(int(health["metrics_recent_count"]))),  # noqa: F405
        cls="info-list",
    )
    return Card(
        CardHeader(
            Div(  # noqa: F405
                H3(  # noqa: F405
                    "Süsteemi tervis (koond)",
                    _tooltip(
                        "Postgres, Jena, pgvector, viimane sünk, "
                        "töötaja aktiivsus ja üldine "
                        "veebiliiklus"
                    ),
                    cls="card-title",
                ),
                _overall_badge(str(health["overall_status"])),
                cls="card-title-row",
            )
        ),
        CardBody(
            body,
            P(  # noqa: F405
                A(  # noqa: F405
                    "Vaata üksikasju →",
                    href="/admin/health/aggregator",
                ),
                cls="card-link",
            ),
        ),
        id="health-aggregator-card",
    )


# ---------------------------------------------------------------------------
# Detail page
# ---------------------------------------------------------------------------


def _detail_row(label: str, value):  # type: ignore[no-untyped-def]
    """Render a single Dt/Dd pair for the aggregator detail page."""
    return (Dt(label), Dd(value))  # noqa: F405


def _format_duration(seconds: float | None) -> str:
    """Format a duration in seconds as ``M:SS`` or ``-`` when unknown."""
    if seconds is None:
        return "—"
    secs = int(seconds)
    minutes, rem = divmod(secs, 60)
    return f"{minutes}:{rem:02d}"


def _aggregator_detail_body(health: dict[str, Any]):
    """Render the rich detail view used by ``/admin/health/aggregator``."""
    last_sync = health["last_sync"]
    rows: list = []

    # Subsystem reachability
    rows.extend(_detail_row("Postgres", _bool_badge(health["postgres_ok"])))
    rows.extend(_detail_row("Jena", _bool_badge(health["jena_ok"])))
    rows.extend(_detail_row("pgvector", _bool_badge(health["pgvector_ok"])))

    # Sync status with timestamps + error message
    if last_sync is None:
        rows.extend(_detail_row("Viimane sünk", "Sünki pole veel tehtud"))
    else:
        sync_lines: list = [
            P(  # noqa: F405
                Strong("Staatus: "),  # noqa: F405
                str(last_sync.get("status") or "—"),
            ),
            P(  # noqa: F405
                Strong("Algusaeg: "),  # noqa: F405
                format_tallinn(last_sync.get("started_at")),
            ),
            P(  # noqa: F405
                Strong("Lõpetamise aeg: "),  # noqa: F405
                format_tallinn(last_sync.get("finished_at")),
            ),
            P(  # noqa: F405
                Strong("Kestus: "),  # noqa: F405
                _format_duration(last_sync.get("duration_s")),
            ),
        ]
        error_message = last_sync.get("error_message")
        if error_message:
            sync_lines.append(
                Details(  # noqa: F405
                    Summary("Veateade"),  # noqa: F405
                    Pre(str(error_message), cls="sync-error-full"),  # noqa: F405
                    cls="sync-error",
                )
            )
        rows.extend(_detail_row("Viimane sünk", Div(*sync_lines)))  # noqa: F405

    # Activity counts with the cutoff window made explicit
    rows.extend(
        _detail_row(
            "Töötaja aktiivsus (5m)",
            _activity_badge(int(health["worker_recent_activity"])),
        )
    )
    rows.extend(
        _detail_row(
            "Metrics kirjeid (5m)",
            _activity_badge(int(health["metrics_recent_count"])),
        )
    )

    # When the snapshot was taken — useful when the page is left open.
    rows.extend(
        _detail_row(
            "Snapshot võetud",
            format_tallinn(datetime.now(UTC)),
        )
    )

    return Dl(*rows, cls="info-list")  # noqa: F405


def admin_health_page(req: Request):
    """GET /admin/health/aggregator — system-health detail page.

    Renders the same signals as the dashboard card with timestamps,
    last sync error message, and a plain GET refresh button so an
    admin can re-poll without going back to ``/admin``.
    """
    auth = req.scope.get("auth")
    theme = get_theme_from_request(req)
    try:
        health = _get_system_health()
        content = (
            H1("Süsteemi tervis", cls="page-title"),  # noqa: F405
            P(A("← Tagasi adminipaneelile", href="/admin"), cls="back-link"),  # noqa: F405
            Card(
                CardHeader(
                    Div(  # noqa: F405
                        H3(  # noqa: F405
                            "Koondvaade",
                            cls="card-title",
                        ),
                        _overall_badge(str(health["overall_status"])),
                        cls="card-title-row",
                    )
                ),
                CardBody(
                    _aggregator_detail_body(health),
                    P(  # noqa: F405
                        A(  # noqa: F405
                            "Värskenda",
                            href="/admin/health/aggregator",
                            cls="btn btn-secondary",
                            role="button",
                        ),
                        cls="card-actions",
                    ),
                ),
                id="health-aggregator-detail",
            ),
        )
        return PageShell(
            *content,
            title="Süsteemi tervis",
            user=auth,
            theme=theme,
            active_nav="/admin",
        )
    except Exception:
        logger.exception("Failed to render admin health aggregator page")
        return _render_admin_error_page(title="Süsteemi tervis", user=auth, theme=theme)


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
