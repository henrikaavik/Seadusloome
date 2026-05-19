"""Global search bar — visible in TopBar on every page (B1, epic #784).

Why a visible bar (not Cmd+K modal)
-----------------------------------
Ministry lawyers are familiar with Riigi Teataja, Eesti.ee, and Google — all
visible search bars in the page header. A command palette opened by a
keyboard shortcut you have to know about is the opposite of the
discoverability goal: we want every primary action (Normi mõjuahel, EL
ülevõtt, sanctions, …) reachable without the user knowing it exists. The
visible bar gives the same UX power as Cmd+K but is discoverable for
everyone, including touch and tablet users. ``Cmd+K`` / ``Ctrl+K`` still
focuses the bar — power-user shortcut, not a hidden affordance.

Responsive contract (Q4 of the plan section)
--------------------------------------------
* **Desktop ≥1024px** — full bar, 300-400px wide, two-row TopBar
  (search above the nav row).
* **Tablet 768-1024px** — 200px bar with the short placeholder.
* **Mobile ≤768px** — the bar collapses to a search-icon button in the
  single-row TopBar; tapping it opens the full-screen ``/search`` page
  with the input auto-focused so the on-screen keyboard appears.
* Touch targets ≥44×44px throughout (button + every dropdown row).

Accessibility
-------------
* The wrapper is ``role="combobox"`` (input owns the listbox), input has
  ``aria-autocomplete="list"`` and ``aria-controls="global-search-results"``.
* Dropdown is ``role="listbox"`` with ``role="group"`` sections (Entiteedid,
  Tegevused) and ``role="option"`` rows; keyboard navigation via ↑/↓/Enter/Esc
  lives in ``global_search.js``.
* ``aria-live="polite"`` region announces result counts ("3 entiteeti,
  2 tegevust") whenever the dropdown swaps.
* Focus-visible outlines come from the global ``:focus-visible`` rule in
  ``ui.css`` (the icon button gets the standard button focus ring).

Server contract
---------------
The input fires ``hx-get="/api/global-search"`` with a 200 ms debounce; the
endpoint returns HTML (the dropdown body) that swaps into
``#global-search-results``. The dropdown is opened by the ``htmx:afterSwap``
handler in ``global_search.js`` whenever the swap has a non-empty body, and
closed on Esc / outside-click / Enter on a row.
"""

from __future__ import annotations

from fasthtml.common import *  # noqa: F403

from app.ui.primitives.icon import Icon

# Placeholder strings — kept module-level so the mobile full-screen page can
# reuse the long form. Estonian-first; do not translate.
PLACEHOLDER_LONG = "Otsi sätet, akti, mõistet... või kirjuta tegevus"
PLACEHOLDER_SHORT = "Otsi või kirjuta tegevus"
ARIA_LABEL = "Globaalne otsing"


def GlobalSearchBar(  # noqa: ANN201
    *,
    autofocus: bool = False,
    placeholder: str | None = None,
    bar_id: str = "global-search",
):
    """Render the inline search bar used inside the desktop/tablet TopBar.

    Args:
        autofocus: If ``True``, the input claims focus on mount. Used by
            the mobile full-screen page so the on-screen keyboard opens
            immediately; *not* used in the TopBar (we don't want to
            steal focus from links on first page load).
        placeholder: Override the default placeholder. ``None`` (default)
            uses ``PLACEHOLDER_LONG`` — the tablet bar swaps in
            ``PLACEHOLDER_SHORT`` via CSS ``::placeholder`` on a narrower
            container; passing it explicitly is also supported for the
            mobile full-screen page.
        bar_id: Stable id used by tests and by the JS Cmd+K handler to
            locate the input. The mobile full-screen page passes a
            different id so both inputs can coexist if both render.
    """
    pl = placeholder if placeholder is not None else PLACEHOLDER_LONG

    input_attrs: dict = {
        "type": "search",
        "name": "q",
        "id": f"{bar_id}-input",
        "placeholder": pl,
        "aria_label": ARIA_LABEL,
        "aria_autocomplete": "list",
        "aria_controls": f"{bar_id}-results",
        "aria_expanded": "false",
        "autocomplete": "off",
        "spellcheck": "false",
        "cls": "global-search-input",
        # HTMX wiring: keyup with 200 ms debounce, swap into the dropdown.
        "hx_get": "/api/global-search",
        "hx_trigger": "keyup changed delay:200ms, search",
        "hx_target": f"#{bar_id}-results",
        "hx_swap": "innerHTML",
    }
    if autofocus:
        # #813: HTML4 string form survives FastHTML's HTTP renderer.
        input_attrs["autofocus"] = "autofocus"

    return Div(  # noqa: F405
        Div(  # noqa: F405
            Icon("search", cls="global-search-icon", aria_hidden=True),
            Input(**input_attrs),  # noqa: F405
            cls="global-search-input-wrap",
        ),
        # Live region — announces "N entiteeti, M tegevust" updates.
        # JS writes the count text on every htmx:afterSwap.
        Div(  # noqa: F405
            "",
            id=f"{bar_id}-status",
            cls="sr-only",
            aria_live="polite",
            aria_atomic="true",
        ),
        # Dropdown panel — populated by /api/global-search HTML response.
        Div(  # noqa: F405
            id=f"{bar_id}-results",
            cls="global-search-dropdown",
            role="listbox",
            aria_label="Otsingutulemused",
        ),
        id=bar_id,
        cls="global-search",
        role="combobox",
        aria_haspopup="listbox",
        aria_owns=f"{bar_id}-results",
        aria_expanded="false",
    )


def GlobalSearchMobileButton():  # noqa: ANN201
    """Search-icon button shown on ``≤768px`` in the single-row TopBar.

    Renders as a 44×44 button (touch-target floor) that links to the
    full-screen ``/search`` page. We use a plain anchor (not a JS-driven
    modal) so it works without JS, is keyboard-reachable trivially, and
    falls through cleanly when the page is opened in a new tab.
    """
    return A(  # noqa: F405
        Icon("search", aria_hidden=True),
        Span("Otsi", cls="sr-only"),  # noqa: F405
        href="/search",
        cls="global-search-mobile-trigger",
        aria_label=ARIA_LABEL,
    )


def render_dropdown(  # noqa: ANN201
    entities: list[dict],
    capabilities: list[dict],
    query: str,
):
    """Render the dropdown HTML returned by ``GET /api/global-search``.

    Returns a tuple of FT elements — FastHTML will concatenate them into
    the swap target (``#global-search-results``). Two groups, each with
    a sticky header, then either ``role="option"`` rows or an empty-state
    note. An empty query (or no matches at all) collapses to a tiny
    "Pole midagi näidata" line so the dropdown can stay closed.

    Args:
        entities: List of {uri, label, type} dicts from the entity search
            (top 5; the API caller is responsible for trimming).
        capabilities: List of {slug, name, description, url, icon} dicts
            from the capability matcher (top 4-6).
        query: The raw query string — used to compose action labels
            ("Käivita Normi mõjuahel <query> üle") server-side so the
            row hrefs work with pre-filled inputs.
    """
    if not query.strip():
        # Empty query: collapse to no body so JS keeps the dropdown closed.
        return ("",)

    n_ent = len(entities)
    n_cap = len(capabilities)

    # ARIA live announcement piggybacks via a data-attribute the JS reads
    # on htmx:afterSwap; this keeps the SR text out of the visual DOM.
    summary = _summary_estonian(n_ent, n_cap)
    summary_marker = Div(  # noqa: F405
        summary,
        cls="global-search-summary",
        data_summary=summary,
        aria_hidden="true",
    )

    if n_ent == 0 and n_cap == 0:
        return (
            summary_marker,
            Div(  # noqa: F405
                "Vastet ei leitud",
                cls="global-search-empty",
                role="status",
            ),
        )

    return (
        summary_marker,
        _entity_group(entities) if entities else "",
        _capability_group(capabilities) if capabilities else "",
    )


def _entity_group(entities: list[dict]):  # noqa: ANN202
    """Render the "Entiteedid" group of result rows."""
    rows = []
    for ent in entities:
        uri = ent.get("uri", "")
        label = ent.get("label", "") or uri
        type_uri = ent.get("type", "")
        short_type = type_uri.rsplit("#", 1)[-1].rsplit("/", 1)[-1] if type_uri else ""
        rows.append(
            A(  # noqa: F405
                Div(label, cls="global-search-row-label"),  # noqa: F405
                Div(short_type, cls="global-search-row-meta") if short_type else "",  # noqa: F405
                href=f"/explorer?focus={uri}",
                role="option",
                cls="global-search-row global-search-row--entity",
                tabindex="-1",
            )
        )
    return Div(  # noqa: F405
        Div(  # noqa: F405
            "Entiteedid",
            cls="global-search-group-header",
            role="presentation",
        ),
        *rows,
        role="group",
        aria_label="Entiteedid",
        cls="global-search-group",
    )


def _capability_group(capabilities: list[dict]):  # noqa: ANN202
    """Render the "Tegevused" group of capability action rows."""
    rows = []
    for cap in capabilities:
        rows.append(
            A(  # noqa: F405
                Icon(cap.get("icon", "search"), cls="global-search-row-icon", aria_hidden=True),
                Div(  # noqa: F405
                    Div(cap.get("name", ""), cls="global-search-row-label"),  # noqa: F405
                    Div(  # noqa: F405
                        cap.get("description", ""),
                        cls="global-search-row-meta",
                    )
                    if cap.get("description")
                    else "",
                    cls="global-search-row-body",
                ),
                href=cap.get("url", "/"),
                role="option",
                cls="global-search-row global-search-row--capability",
                tabindex="-1",
            )
        )
    return Div(  # noqa: F405
        Div(  # noqa: F405
            "Tegevused",
            cls="global-search-group-header",
            role="presentation",
        ),
        *rows,
        role="group",
        aria_label="Tegevused",
        cls="global-search-group",
    )


def _summary_estonian(n_ent: int, n_cap: int) -> str:
    """Build the ARIA-live text — Estonian noun agreement (1 vs. >1)."""

    def fmt(n: int, sing: str, plur: str) -> str:
        return f"{n} {sing if n == 1 else plur}"

    parts = []
    if n_ent:
        parts.append(fmt(n_ent, "entiteet", "entiteeti"))
    if n_cap:
        parts.append(fmt(n_cap, "tegevus", "tegevust"))
    if not parts:
        return "Vastet ei leitud"
    return ", ".join(parts)
