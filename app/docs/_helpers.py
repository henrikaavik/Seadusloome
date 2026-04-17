"""Small shared helpers for the drafts module (#671).

Extracted out of :mod:`app.docs.routes` so that sibling modules
(notably :mod:`app.docs.retry_handler`) can import them without
creating an import cycle at module-load time.

Keep this file **dependency-light**: anything imported here must not
depend on :mod:`app.docs.routes` itself, or the cycle returns.
"""

from __future__ import annotations

import uuid

from fasthtml.common import H1, A, P  # noqa: F401
from starlette.requests import Request

from app.ui.layout import PageShell
from app.ui.surfaces.alert import Alert
from app.ui.theme import get_theme_from_request


def _parse_uuid(raw: str) -> uuid.UUID | None:
    """Return a ``UUID`` parsed from *raw*, or ``None`` if invalid."""
    try:
        return uuid.UUID(raw)
    except (ValueError, TypeError):
        return None


def _not_found_page(req: Request):
    """Render the 404 page used whenever a draft is missing or out of scope."""
    auth = req.scope.get("auth")
    theme = get_theme_from_request(req)
    return PageShell(
        H1("Eelnõu ei leitud", cls="page-title"),
        Alert(
            "Otsitud eelnõu ei ole olemas või Te ei oma selle vaatamise õigust.",
            variant="warning",
        ),
        P(A("← Tagasi eelnõude nimekirja", href="/drafts"), cls="back-link"),
        title="Eelnõu ei leitud",
        user=auth,
        theme=theme,
        active_nav="/drafts",
        request=req,
    )
