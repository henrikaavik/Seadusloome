"""Analüüsikeskus routes (#714).

For now this is a single placeholder page so the new ``Analüüsikeskus``
navigation entry resolves. The workflow directory (#720) replaces this
body; the individual workflows (#722 Normi mõjuahel, #723 EL ülevõtt)
register their own sub-routes here later.
"""

from fasthtml.common import *  # noqa: F403
from starlette.requests import Request

from app.ui.layout import PageShell
from app.ui.surfaces.info_box import InfoBox
from app.ui.theme import get_theme_from_request


def analyysikeskus_page(req: Request):
    """GET /analyysikeskus — placeholder for the legal-analysis workflow hub."""
    auth = req.scope.get("auth") or None
    theme = get_theme_from_request(req)
    return PageShell(
        H1("Analüüsikeskus", cls="page-title"),  # noqa: F405
        InfoBox(
            P(  # noqa: F405
                "Analüüsikeskus koondab õigusliku analüüsi töövood — "
                "normi mõjuahel, EL õiguse ülevõtt ja teised — ühte kohta. "
                "Tulekul."
            ),
            variant="info",
        ),
        title="Analüüsikeskus",
        user=auth,
        theme=theme,
        active_nav="/analyysikeskus",
    )


def register_analyysikeskus_routes(rt) -> None:  # type: ignore[no-untyped-def]
    """Register Analüüsikeskus routes on the FastHTML route decorator *rt*."""
    rt("/analyysikeskus", methods=["GET"])(analyysikeskus_page)
