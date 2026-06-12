"""Admin Sentry errors panel card and detail page.

Read-only Sentry API integration (#324) that surfaces the most recent
issues from the project's Sentry instance on the admin dashboard, plus
a dedicated ``/admin/sentry`` detail page with a refresh button.

Design notes:
    * **Optional integration.** The Sentry API call is only attempted when
      all three of ``SENTRY_API_TOKEN``, ``SENTRY_ORG_SLUG``, and
      ``SENTRY_PROJECT_SLUG`` are set in the environment. Any missing
      variable yields ``None`` from :func:`_get_recent_sentry_errors`
      and the card renders an "ei ole konfigureeritud" empty state.
    * **Graceful degradation.** Every external HTTP call is wrapped in
      try/except so a Sentry outage, timeout, or 5xx response degrades
      to "Sentry API ei vasta" rather than a 500. The 3-second timeout
      keeps the admin page snappy even when Sentry is slow.
    * **Secret hygiene.** ``SENTRY_API_TOKEN`` is sent in the
      ``Authorization`` header and never logged. Only the public org +
      project slugs are embedded in the "Vaata Sentrys" link.
    * **No new deps.** ``httpx`` is already a project dependency (used
      by other modules) and the official Sentry SDK is *not* required
      for this read-only panel.
"""

from __future__ import annotations

import logging
from typing import Any

import httpx
from fasthtml.common import *  # noqa: F403
from starlette.requests import Request

from app import config
from app.admin._shared import _tooltip
from app.ui.data.data_table import Column, DataTable
from app.ui.layout import PageShell
from app.ui.primitives.badge import Badge, StatusBadge
from app.ui.primitives.button import Button  # noqa: F401, F811  -- shadow guard
from app.ui.surfaces.card import Card, CardBody, CardHeader
from app.ui.theme import get_theme_from_request

logger = logging.getLogger(__name__)

_SENTRY_API_TIMEOUT_SECONDS = 3.0
_SENTRY_MAX_ISSUES = 5


def _sentry_env() -> tuple[str, str, str] | None:
    """Return ``(token, org, project)`` if all three env vars are set, else ``None``.

    Centralising this lookup makes it trivial to unit-test the
    not-configured branch with ``monkeypatch.delenv`` and keeps the
    token-vs-slug distinction local to this module.
    """
    token = config.env_str("SENTRY_API_TOKEN")
    org = config.env_str("SENTRY_ORG_SLUG")
    project = config.env_str("SENTRY_PROJECT_SLUG")
    if not (token and org and project):
        return None
    return token, org, project


def _sentry_issues_url(org: str, project: str) -> str:
    """Return the public Sentry issues URL for the configured project.

    Only the slugs are interpolated — no token. Safe to render in HTML.
    """
    return f"https://{org}.sentry.io/issues/?project={project}"


def _get_recent_sentry_errors() -> list[dict[str, Any]] | None:
    """Query the Sentry API for recent issues for the configured project.

    Returns:
        * ``None`` when any of ``SENTRY_API_TOKEN``, ``SENTRY_ORG_SLUG``,
          ``SENTRY_PROJECT_SLUG`` is missing — signals "not configured".
        * ``[]`` (empty list) when Sentry returned 0 issues OR when the
          API call failed (timeout, non-2xx, network error). The caller
          renders an empty/error state from this — failures degrade
          rather than raising.
        * ``list[dict]`` of at most ``_SENTRY_MAX_ISSUES`` entries with
          keys ``title``, ``last_seen``, ``count``, ``level``, ``link``.
    """
    env = _sentry_env()
    if env is None:
        return None
    token, org, project = env

    url = f"https://sentry.io/api/0/projects/{org}/{project}/issues/"
    params = {"query": "is:unresolved", "limit": str(_SENTRY_MAX_ISSUES)}
    headers = {"Authorization": f"Bearer {token}"}

    try:
        response = httpx.get(
            url,
            params=params,
            headers=headers,
            timeout=_SENTRY_API_TIMEOUT_SECONDS,
        )
    except Exception:
        # NEVER include the headers/token in the log message.
        logger.exception("Sentry API request failed for project=%s/%s", org, project)
        return []

    if response.status_code != 200:
        logger.warning(
            "Sentry API returned status=%s for project=%s/%s",
            response.status_code,
            org,
            project,
        )
        return []

    try:
        payload = response.json()
    except Exception:
        logger.exception("Sentry API returned non-JSON body for project=%s/%s", org, project)
        return []

    if not isinstance(payload, list):
        logger.warning("Sentry API returned unexpected payload type for %s/%s", org, project)
        return []

    issues: list[dict[str, Any]] = []
    for raw in payload[:_SENTRY_MAX_ISSUES]:
        if not isinstance(raw, dict):
            continue
        issues.append(
            {
                "title": str(raw.get("title") or raw.get("culprit") or "(pealkiri puudub)"),
                "last_seen": str(raw.get("lastSeen") or ""),
                "count": str(raw.get("count") or "0"),
                "level": str(raw.get("level") or "error"),
                "link": str(raw.get("permalink") or _sentry_issues_url(org, project)),
            }
        )
    return issues


def _level_badge(level: str):
    """Map a Sentry level string to a coloured Badge."""
    variant: str = "default"
    if level in ("fatal", "error"):
        variant = "danger"
    elif level == "warning":
        variant = "warning"
    elif level == "info":
        variant = "primary"
    return Badge(level, variant=variant)  # type: ignore[arg-type]


def _sentry_table(issues: list[dict[str, Any]]):
    """Render the issues list as a DataTable.

    ``level`` and ``link`` columns use a ``render`` callback so the
    Badge/anchor FT nodes are emitted directly instead of being
    stringified by DataTable's default ``str(row[key])`` path.
    """
    rows = [
        {
            "title": issue["title"],
            "level": _level_badge(issue["level"]),
            "count": issue["count"],
            "last_seen": issue["last_seen"],
            "link": A(  # noqa: F405
                "Ava Sentrys →",
                href=issue["link"],
                target="_blank",
                rel="noopener noreferrer",
            ),
        }
        for issue in issues
    ]
    columns = [
        Column(key="title", label="Pealkiri", sortable=False),
        Column(key="level", label="Tase", sortable=False, render=lambda r: r["level"]),
        Column(key="count", label="Esinemisi", sortable=False),
        Column(key="last_seen", label="Viimati nähti", sortable=False),
        Column(key="link", label="Vaata", sortable=False, render=lambda r: r["link"]),
    ]
    return DataTable(columns=columns, rows=rows)


def _sentry_panel_card():
    """Render the Sentry errors card for the admin dashboard.

    States rendered:
        1. **Not configured** (``_get_recent_sentry_errors()`` is ``None``):
           an info note pointing to the setup doc.
        2. **No recent errors** (empty list): a green StatusBadge plus
           "Hiljutisi vigu pole.".
        3. **Issues present**: a 5-row DataTable plus a "Vaata Sentrys"
           link to the project's issues page.
    """
    issues = _get_recent_sentry_errors()

    if issues is None:
        body: object = Div(  # noqa: F405
            P(  # noqa: F405
                "Sentry pole konfigureeritud. Seadista keskkonnamuutujad "
                "SENTRY_API_TOKEN, SENTRY_ORG_SLUG ja SENTRY_PROJECT_SLUG "
                "(vt docs/superpowers/specs/2026-04-09-phase4-design.md "
                "Sentry seadistuse jaoks).",
                cls="muted-text",
            ),
        )
    elif not issues:
        body = Div(  # noqa: F405
            P(  # noqa: F405
                StatusBadge("ok"),
                " Hiljutisi vigu pole.",
            ),
        )
    else:
        env = _sentry_env()
        # env cannot be None here because issues was not None.
        assert env is not None
        _token, org, project = env
        body = Div(  # noqa: F405
            _sentry_table(issues),
            P(  # noqa: F405
                A(  # noqa: F405
                    "Vaata Sentrys →",
                    href=_sentry_issues_url(org, project),
                    target="_blank",
                    rel="noopener noreferrer",
                    cls="back-link",
                ),
            ),
        )

    return Card(
        CardHeader(
            H3(  # noqa: F405
                "Sentry vead",
                _tooltip("Viimased lahendamata vead Sentry projektis"),
                cls="card-title",
            )
        ),
        CardBody(body),
        id="sentry-panel-card",
    )


def admin_sentry_page(req: Request):
    """GET /admin/sentry -- detail view of recent Sentry errors with a refresh button.

    Helpers are imported as locals to stay consistent with the rest of
    the admin sub-modules; the outer try/except renders a styled error
    banner instead of letting an unexpected failure bubble up as a raw
    500.
    """
    auth = req.scope.get("auth")
    theme = get_theme_from_request(req)
    try:
        from app.admin.sentry_panel import _sentry_panel_card

        content = (
            H1("Sentry vead", cls="page-title"),  # noqa: F405
            P(A("← Tagasi adminipaneelile", href="/admin"), cls="back-link"),  # noqa: F405
            P(  # noqa: F405
                A(  # noqa: F405
                    "Värskenda",
                    href="/admin/sentry",
                    cls="btn btn-secondary btn-sm",
                ),
            ),
            _sentry_panel_card(),
        )

        return PageShell(
            *content,
            title="Sentry vead",
            user=auth,
            theme=theme,
            active_nav="/admin",
        )
    except Exception:
        logger.exception("Failed to render admin sentry page")
        from app.admin._shared import _render_admin_error_page

        return _render_admin_error_page(title="Sentry vead", user=auth, theme=theme)
