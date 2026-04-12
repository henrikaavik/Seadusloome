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
from app.version import read_version


def _version_footer():  # noqa: ANN202
    """Render the admin page footer showing app version, sha, and build time."""
    version = read_version()
    app_ver = version.get("app", "0.0.0")
    sha = version.get("sha", "unknown")
    built_at = version.get("built_at", "unknown")
    short_sha = sha[:7] if sha and not sha.startswith("dev") else sha
    # Link the sha to the GitHub commit when it looks like a real sha.
    is_real_sha = len(sha) >= 7 and not sha.startswith("dev") and sha != "unknown"
    sha_node = (
        A(  # noqa: F405
            short_sha,
            href=f"https://github.com/henrikaavik/Seadusloome/commit/{sha}",
            target="_blank",
            rel="noopener noreferrer",
            cls="admin-footer-sha",
        )
        if is_real_sha
        else Span(short_sha, cls="admin-footer-sha")  # noqa: F405
    )
    return Footer(  # noqa: F405
        Small(  # noqa: F405
            f"v{app_ver} · ",
            sha_node,
            f" · built {built_at}",
        ),
        cls="admin-footer",
    )


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
        _version_footer(),
    )

    return PageShell(
        *content,
        title="Administreerimise t\u00f6\u00f6laud",
        user=auth,
        theme=theme,
        active_nav="/admin",
    )
