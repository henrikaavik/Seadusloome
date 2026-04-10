"""Admin dashboard page that composes all admin cards."""

from __future__ import annotations

from fasthtml.common import *  # noqa: F403
from starlette.requests import Request

from app.admin.health import _check_postgres, _health_card, jena_check_health
from app.admin.jobs import _job_queue_card
from app.admin.llm_usage import _llm_usage_card
from app.admin.rate_limits import _rate_limit_card
from app.admin.sync import _get_sync_logs, _sync_card
from app.admin.users import _get_user_stats, _quick_links_card, _user_stats_card
from app.ui.layout import PageShell
from app.ui.primitives.button import Button  # noqa: F401, F811  -- shadow guard
from app.ui.surfaces.info_box import InfoBox
from app.ui.theme import get_theme_from_request


def admin_dashboard_page(req: Request):
    """GET /admin — admin dashboard with system overview."""
    auth = req.scope.get("auth")
    theme = get_theme_from_request(req)

    jena_ok = jena_check_health()
    pg_ok = _check_postgres()
    sync_logs = _get_sync_logs()
    user_stats = _get_user_stats()

    content = (
        H1("Administreerimise t\u00f6\u00f6laud", cls="page-title"),  # noqa: F405
        InfoBox(
            P(  # noqa: F405
                "Administreerimise t\u00f6\u00f6laud n\u00e4itab s\u00fcsteemi "
                "tervist, s\u00fcnkroniseerimise staatust, taustajobisid, "
                "LLM kasutust ja kasutajate statistikat."
            ),
            variant="info",
            dismissible=True,
        ),
        _health_card(jena_ok, pg_ok),
        _sync_card(sync_logs),
        _job_queue_card(),
        _llm_usage_card(),
        _rate_limit_card(),
        _user_stats_card(user_stats),
        _quick_links_card(),
    )

    return PageShell(
        *content,
        title="Administreerimise t\u00f6\u00f6laud",
        user=auth,
        theme=theme,
        active_nav="/admin",
    )
