"""Routes for the global search bar (B1, epic #784).

This module exposes two HTTP entry-points consumed by
:mod:`app.ui.components.global_search` and the mobile full-screen page:

``GET /api/global-search?q=<query>``
    HTMX target for the inline dropdown. Returns the dropdown body HTML
    (FT fragments). Combines:

    * **Entiteedid** — top 5 entity matches from the existing explorer
      SPARQL search. Reuses ``app.explorer.routes.explorer_search`` so
      the regex sanitisation, limit cap, and label/type shape stay
      consistent.
    * **Tegevused** — top 4-6 capability matches from
      ``app.ui.capabilities``. Matching is case- and diacritic-tolerant
      substring search over canonical name, description, slug, and
      example input. The query is woven into the capability label so
      the user sees "Käivita Normi mõjuahel: <query>" — pre-filled
      action verbs.

``GET /search``
    Full-screen mobile search page. The TopBar's mobile button links
    here so the on-screen keyboard opens immediately (the input is
    server-rendered with ``autofocus``). Uses the same dropdown
    endpoint via HTMX.

The HTML response from ``/api/global-search`` is intentionally a
fragment (no PageShell, no doctype) so HTMX can swap it directly into
``#global-search-results``. The ``/search`` page wraps the same input
component in PageShell.
"""

from __future__ import annotations

import unicodedata
from typing import Any

from starlette.requests import Request

from app.ui.capabilities import CAPABILITIES, Capability
from app.ui.components.global_search import (
    PLACEHOLDER_LONG,
    GlobalSearchBar,
    render_dropdown,
)
from app.ui.layout.page_shell import PageShell

# Hard limits — keep the dropdown to a glanceable height.
MAX_ENTITY_RESULTS = 5
MAX_CAPABILITY_RESULTS = 6

# Capabilities whose target_url should be visited verbatim (no query weave)
# even when the user typed something. ``/chat/new`` is the only current
# case — the user can paste their query into the chat input themselves;
# we don't pre-fill via URL because the chat input is WS-backed.
_NO_QUERY_WEAVE: set[str] = {"noustaja"}


def register_search_routes(rt) -> None:  # type: ignore[no-untyped-def]
    """Register the global-search routes on FastHTML's ``rt`` decorator."""
    rt("/api/global-search", methods=["GET"])(global_search_endpoint)
    rt("/search", methods=["GET"])(mobile_search_page)


# ---------------------------------------------------------------------------
# /api/global-search — HTMX dropdown body
# ---------------------------------------------------------------------------


def global_search_endpoint(req: Request):  # noqa: ANN201
    """GET /api/global-search?q=<query> — dropdown HTML for the bar.

    Returns FT fragments (no PageShell), which HTMX swaps into
    ``#global-search-results``. An empty query short-circuits to an
    empty body so the dropdown auto-closes.
    """
    query = (req.query_params.get("q") or "").strip()
    if not query:
        return render_dropdown(entities=[], capabilities=[], query="")

    entities = _entity_matches(query)
    capability_rows = _capability_matches(query)

    return render_dropdown(
        entities=entities,
        capabilities=capability_rows,
        query=query,
    )


def _entity_matches(query: str) -> list[dict]:
    """Call the existing explorer SPARQL search with a small limit.

    Defensively swallow errors so a Jena hiccup never blanks the whole
    dropdown — the user still gets the capability rows. The explorer
    search returns ``{entity, label, type}``; we re-key to ``uri`` to
    match the dropdown row contract.
    """
    try:
        # Lazy imports — keep the global-search module light for tests
        # that don't need the ontology stack (and let tests patch the
        # explorer SPARQL client surface as usual).
        from app.explorer.routes import (  # noqa: PLC0415
            _get_client,
            _sanitize_regex,
        )
        from app.ontology.queries import SEARCH_ENTITIES  # noqa: PLC0415
    except Exception:
        return []

    try:
        safe = _sanitize_regex(query).replace("\\", "\\\\").replace('"', '\\"')
        sparql = SEARCH_ENTITIES.format(search_pattern=safe, limit=MAX_ENTITY_RESULTS)
        rows = _get_client().query(sparql)
    except Exception:
        return []

    out: list[dict] = []
    for row in rows[:MAX_ENTITY_RESULTS]:
        out.append(
            {
                "uri": row.get("entity", ""),
                "label": row.get("label", ""),
                "type": row.get("type", ""),
            }
        )
    return out


# ---------------------------------------------------------------------------
# Capability matching
# ---------------------------------------------------------------------------


def _fold(s: str) -> str:
    """Lowercase + strip diacritics for substring matching.

    Ministry lawyers will type "polevkivi" as often as "põlevkivi"; we
    fold the haystack and the needle so both hit.
    """
    nfkd = unicodedata.normalize("NFKD", s)
    no_marks = "".join(c for c in nfkd if not unicodedata.combining(c))
    return no_marks.lower()


def _capability_matches(query: str) -> list[dict[str, Any]]:
    """Return up to ``MAX_CAPABILITY_RESULTS`` capability rows for ``query``.

    Order:
        1. prefix match on name (best),
        2. substring match on name,
        3. substring match on description / example_input / slug.

    Within each tier we preserve the source list order from
    :mod:`app.ui.capabilities` so adjacent tiers don't shuffle.
    """
    folded_q = _fold(query)
    if not folded_q:
        return []

    prefix_hits: list[Capability] = []
    name_hits: list[Capability] = []
    other_hits: list[Capability] = []

    for cap in CAPABILITIES:
        name_f = _fold(cap.canonical_name_et)
        if name_f.startswith(folded_q):
            prefix_hits.append(cap)
        elif folded_q in name_f:
            name_hits.append(cap)
        else:
            haystack = " ".join(
                [
                    _fold(cap.one_line_description_et),
                    _fold(cap.example_input or ""),
                    cap.slug,
                ]
            )
            if folded_q in haystack:
                other_hits.append(cap)

    ordered = prefix_hits + name_hits + other_hits
    ordered = ordered[:MAX_CAPABILITY_RESULTS]

    return [_capability_row(c, query) for c in ordered]


def _capability_row(cap: Capability, query: str) -> dict[str, Any]:
    """Project a ``Capability`` to the dict the dropdown row expects.

    Weaves the user's query into the action label (so they see a
    verb-shaped hint like ``Käivita Normi mõjuahel: <query>``) and
    pre-fills the input on the workflow page via ``?sisend=<query>``.
    """
    weave = cap.slug not in _NO_QUERY_WEAVE and cap.example_input is not None
    if weave and query:
        name = f"{cap.canonical_name_et}: {query}"
    else:
        name = cap.canonical_name_et

    url = cap.target_url
    if weave and query:
        sep = "&" if "?" in url else "?"
        url = f"{url}{sep}sisend={_url_encode(query)}"

    return {
        "slug": cap.slug,
        "name": name,
        "description": cap.one_line_description_et,
        "url": url,
        "icon": cap.icon,
    }


def _url_encode(s: str) -> str:
    """Lazy import of urllib.parse.quote — keeps import surface flat."""
    from urllib.parse import quote  # noqa: PLC0415

    return quote(s, safe="")


# ---------------------------------------------------------------------------
# /search — mobile full-screen page
# ---------------------------------------------------------------------------


def mobile_search_page(req: Request):  # noqa: ANN201
    """GET /search — full-screen search page used by the mobile TopBar button.

    Auto-focused input so the on-screen keyboard appears on tap-through.
    Uses the same ``GlobalSearchBar`` component (with a distinct id so
    both inputs can coexist if the user resizes the viewport from mobile
    to desktop mid-session — both still wire to ``/api/global-search``).
    """
    user = req.scope.get("auth")

    from fasthtml.common import H1, Div  # noqa: PLC0415, F401

    return PageShell(
        Div(
            H1("Otsing", cls="sr-only"),
            GlobalSearchBar(
                autofocus=True,
                placeholder=PLACEHOLDER_LONG,
                bar_id="global-search-mobile",
            ),
            cls="mobile-search-page",
        ),
        title="Otsing",
        user=user,
        active_nav=None,
        request=req,
    )
