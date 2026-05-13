"""Õiguskaart page — the D3.js ontology graph, wearing the standard app chrome.

Issue #746 (design rationale: ``docs/2026-05-12-ui-plan-explorer-home.html``
and ``docs/2026-05-11-ministry-lawyer-ui-structure.md``): the explorer no
longer ships its own bespoke ``<html>`` document with a custom topbar and a
left rail that mixes graph controls with site navigation. It is rendered
inside :func:`app.ui.layout.PageShell` with ``full_bleed=True`` — the
standard sidebar (with "Õiguskaart" highlighted) + topbar + user menu, and
the D3 canvas filling the content area. The former control rail becomes a
thin horizontal **toolbar** across the top of the canvas; the search box
moves into it. The D3 v7 CDN ``<script>`` (~270 KB) and ``explorer.css`` are
pushed into ``<head>`` via ``extra_head=`` so they load on this page only.

Issue #754 (epic #762, design doc ``docs/2026-05-12-oiguskaart-evidence-map.md``,
workstream A): a "cold" open — ``/explorer`` with no ``?focus=`` / ``?draft=`` /
``?search=`` and not the explicit ``?vaade=koik`` opt-in — no longer auto-loads
the 90k-entity category overview. It renders a compact **contextual start panel**
(search box, your bookmarks, recent high-risk reports for your org, recent drafts,
a ``Normi mõjuahel`` shortcut, "Sirvi liikide kaupa") inside the otherwise-empty
graph area, and tells ``explorer.js`` *not* to fetch the graph data. Any of
``?focus=`` / ``?draft=`` / ``?search=`` / ``?vaade=koik`` bypasses the panel and
loads the graph exactly as before. The org-scoped DB queries that back the panel
live in :mod:`app.explorer.start_panel`.

Issue #756 (epic #762, same design doc, workstream C): the toolbar grows
**legal-view preset chips** — ``Kehtiv õigus`` · ``Eelnõu mõjud`` · ``EL
seosed`` · ``Kohtupraktika`` · ``Ajalugu``. Each preset is a named bundle of
the knobs the explorer already has (which entity types / relation types to keep
on screen + whether the timeline is on); the raw simulation knobs stay under
``Vaate seaded ▾``. Presets are URL-addressable — ``/explorer?vaade=<slug>``
applies one on load (and, like ``?vaade=koik``, skips the start panel);
``explorer.js`` mirrors a clicked preset into the URL. See
:data:`_LEGAL_VIEW_PRESETS`.
"""

from __future__ import annotations

import json
import logging
import threading
import time
import uuid
from datetime import datetime

from fasthtml.common import *
from starlette.requests import Request

from app.db import get_connection as _connect
from app.explorer.start_panel import StartPanelData, load_start_panel_data

# Re-import our design-system Button after the wildcard so the symbol does
# not silently fall back to FastHTML's unstyled Button (#419).
from app.ui.layout import PageShell
from app.ui.primitives.button import Button  # noqa: E402
from app.ui.time import format_tallinn

logger = logging.getLogger(__name__)

# The explicit "show me the whole 90k map" opt-in. The start panel's "Näita
# kogu kaarti" / "Sirvi liikide kaupa" buttons (and the toolbar's "Näita kogu
# kaarti" in the Vaate seaded menu) navigate to ``/explorer?vaade=koik``; that
# URL renders the graph view (no panel) and lets ``explorer.js`` load the
# overview as it always did.
_VAADE_KOIK = "koik"

# #756 (epic #762, design doc ``docs/2026-05-12-oiguskaart-evidence-map.md``,
# workstream C): legal-view presets in the toolbar. Each preset is a *named
# bundle* of the knobs the explorer already has — which entity types (graph
# categories) to keep on screen, which relation types to keep (matched on the
# edge labels explorer.js already renders, i.e. the predicate names), and
# whether the timeline ("ajaline vaade") is on. The raw simulation knobs
# (``Lähtesta paigutus`` / ``Näita-peida seosenimed`` / ``Rühmita liigi
# järgi`` / the timeline slider) stay under the existing ``Vaate seaded ▾``
# disclosure — presets are the *legal-work* surface.
#
# Presets are URL-addressable: ``/explorer?vaade=<slug>`` applies the preset on
# load (and, like ``?vaade=koik``, forces the graph view past the #754 start
# panel — a preset is meaningless without the graph). explorer.js mirrors a
# clicked preset back into the URL via ``history.replaceState``. An unknown
# ``?vaade=`` value is ignored gracefully (the page renders as if no ``vaade``
# param were present).
#
# The mapping is best-effort against the ontology's actual predicates
# (``estleg:references`` / ``interpretsProvision`` / ``transposesDirective`` /
# ``amendsProvision`` / ``transposedBy`` / ``harmonisedWith`` — see
# ``app/docs/impact/queries.py`` / ``app/analyysikeskus/eu_lookup.py``); where a
# clean relation-type grouping doesn't exist the keyword list errs on the side
# of "show a bit more". Category keys match ``CATEGORY_COLORS`` in
# ``app/static/js/explorer.js``.
#
# Each entry: ``slug -> {"label", "title", "categories", "rel_keywords",
# "timeline"}``. ``categories`` / ``rel_keywords`` empty ⇒ "no filter on that
# axis" (used by ``ajalugu``, which keeps every type and just turns the
# timeline on).
_LEGAL_VIEW_PRESETS: dict[str, dict] = {
    "kehtiv-oigus": {
        "label": "Kehtiv õigus",
        "title": "Kehtivad seadused ja õigusnormid ning nende struktuursed seosed",
        "categories": [
            "EnactedLaw",
            "LegalProvision",
            "Section",
            "Division",
            "Chapter",
            "Subdivision",
            "LegalPart",
        ],
        # Structural containment relations between an act and its provisions —
        # no impact/EU/case predicates. ``hasInstance`` is the synthetic
        # category→entity edge explorer.js draws on drill-down.
        "rel_keywords": ["contains", "haspart", "ispartof", "hasprovision", "hasinstance"],
        "timeline": False,
    },
    "eelnou-mojud": {
        "label": "Eelnõu mõjud",
        "title": "Eelnõud ja nende mõjuseosed (mõjutab / on vastuolus / muudab)",
        "categories": ["DraftLegislation", "LegalProvision", "EnactedLaw"],
        "rel_keywords": [
            "reference",
            "viit",
            "affect",
            "mojut",
            "conflict",
            "vastuolu",
            "amend",
            "muut",
        ],
        "timeline": False,
    },
    "el-seosed": {
        "label": "EL seosed",
        "title": "EL õigusaktid ja ülevõtmisseosed (võtab üle direktiivi / on harmoneeritud)",
        "categories": ["EULegislation", "LegalProvision", "EnactedLaw"],
        "rel_keywords": [
            "transpos",
            "ulevot",
            "directive",
            "direktiiv",
            "harmonis",
            "harmonee",
            "implement",
        ],
        "timeline": False,
    },
    "kohtupraktika": {
        "label": "Kohtupraktika",
        "title": "Kohtulahendid ja nende tõlgendus-/kohaldamisseosed",
        "categories": ["CourtDecision", "EUCourtDecision", "LegalProvision"],
        "rel_keywords": ["interpret", "tolgenda", "appl", "kohalda", "cite", "viit"],
        "timeline": False,
    },
    "ajalugu": {
        "label": "Ajalugu",
        "title": "Versioonid ja muudatused — ajaline vaade sees",
        # No category filter — versions/amendments span every entity type.
        "categories": [],
        "rel_keywords": ["amend", "muut", "version", "versioon", "supersede", "asend", "replace"],
        "timeline": True,
    },
}


def _legal_view_presets_for_js() -> dict[str, dict]:
    """Trim the preset table to the keys ``explorer.js`` needs (no labels)."""
    return {
        slug: {
            "categories": p["categories"],
            "relKeywords": p["rel_keywords"],
            "timeline": p["timeline"],
        }
        for slug, p in _LEGAL_VIEW_PRESETS.items()
    }


# #475: the explorer page previously re-queried ``drafts`` and
# ``impact_reports`` on every request, including from HTMX polling
# fragments. The data is per-draft-per-org and changes only when a new
# impact_report row is inserted at the end of the analyze_impact
# handler — the cache TTL is short enough (60s) that an admin staring
# at the explorer sees fresh data within a minute, but long enough to
# absorb the typical "user opens the page 5 times in a row" pattern.
#
# Cache key is ``(draft_id, org_id)`` to avoid cross-org leakage even
# in the unlikely event of a collision on draft UUIDs.
_OVERLAY_CACHE_TTL_SECONDS = 60.0
_overlay_cache: dict[tuple[str, str], tuple[float, list[str]]] = {}
_overlay_cache_lock = threading.Lock()


def _overlay_cache_get(key: tuple[str, str]) -> list[str] | None:
    """Return a cached overlay list if present and still fresh."""
    now = time.monotonic()
    with _overlay_cache_lock:
        entry = _overlay_cache.get(key)
        if entry is None:
            return None
        stored_at, uris = entry
        if (now - stored_at) > _OVERLAY_CACHE_TTL_SECONDS:
            # Lazy eviction — stale entries stay until a hit prunes them.
            _overlay_cache.pop(key, None)
            return None
        return uris


def _overlay_cache_put(key: tuple[str, str], uris: list[str]) -> None:
    """Store a freshly-computed overlay list in the cache."""
    now = time.monotonic()
    with _overlay_cache_lock:
        # Keep the cache bounded — if it grows beyond ~256 entries,
        # drop the oldest half. This is a pragmatic cap for an admin
        # tool with a handful of concurrent users, not a full LRU.
        if len(_overlay_cache) > 256:
            keys_to_drop = list(_overlay_cache.keys())[:128]
            for k in keys_to_drop:
                _overlay_cache.pop(k, None)
        _overlay_cache[key] = (now, uris)


def _overlay_cache_clear() -> None:
    """Drop every cached overlay entry. Exposed for tests."""
    with _overlay_cache_lock:
        _overlay_cache.clear()


def _fetch_draft_overlay(req: Request, draft_id_raw: str) -> list[str]:
    """Return affected entity URIs for the draft, or an empty list.

    Access control rules:

    - Unauthenticated requests get an empty list (defensive — the
      auth middleware should already have redirected to login because
      ``/explorer`` is no longer in ``SKIP_PATHS`` (#442); this branch
      survives as a belt-and-braces guard).
    - Drafts owned by another org get an empty list (silently dropped
      so the URL stays browseable without revealing existence).
    - Malformed UUIDs / missing reports also yield an empty list.
    - Any DB error is logged and treated as "no overlay" so the
      explorer still renders normally.
    """
    auth = req.scope.get("auth")
    if not auth or not auth.get("org_id"):
        return []
    try:
        draft_uuid = uuid.UUID(draft_id_raw)
    except (TypeError, ValueError):
        return []

    # #475: check the TTL cache before hitting the DB. Hits here dodge
    # two SELECTs (one for the draft, one for the impact_report) plus
    # the JSON deserialisation below. See ``_overlay_cache_get`` for
    # the eviction semantics.
    org_id = str(auth.get("org_id"))
    cache_key = (str(draft_uuid), org_id)
    cached = _overlay_cache_get(cache_key)
    if cached is not None:
        return cached

    try:
        with _connect() as conn:
            draft_row = conn.execute(
                "SELECT org_id FROM drafts WHERE id = %s",
                (str(draft_uuid),),
            ).fetchone()
            if draft_row is None:
                _overlay_cache_put(cache_key, [])
                return []
            if str(draft_row[0]) != org_id:
                _overlay_cache_put(cache_key, [])
                return []

            report_row = conn.execute(
                """
                SELECT report_data
                FROM impact_reports
                WHERE draft_id = %s
                ORDER BY generated_at DESC
                LIMIT 1
                """,
                (str(draft_uuid),),
            ).fetchone()
    except Exception:
        logger.exception("explorer overlay: DB error for draft=%s", draft_id_raw)
        return []

    if report_row is None:
        _overlay_cache_put(cache_key, [])
        return []

    raw = report_row[0]
    if isinstance(raw, str):
        try:
            data = json.loads(raw)
        except (TypeError, ValueError):
            _overlay_cache_put(cache_key, [])
            return []
    elif isinstance(raw, dict):
        data = raw
    else:
        _overlay_cache_put(cache_key, [])
        return []

    affected = data.get("affected_entities") or []
    uris: list[str] = []
    for row in affected:
        if isinstance(row, dict):
            uri = row.get("uri")
            if uri:
                uris.append(str(uri))
    _overlay_cache_put(cache_key, uris)
    return uris


# #755: epic #762, workstream B — ``?draft=<id>`` renders the draft's impact
# subgraph (its affected / conflicting / gap provisions + the inter-relations)
# as the actual graph content, not the full 90k overview. The subgraph data
# itself is fetched client-side from ``/explorer/draft-subgraph/{draft_id}``
# (a small auth-gated, org-scoped JSON endpoint in :mod:`app.explorer.routes`);
# this page handler just needs to know *whether* to switch explorer.js into
# subgraph mode — which depends on the draft being resolvable + owned by the
# caller's org. A draft with no impact report yet still switches into the mode
# (the JS shows a "run the analysis" fallback rather than the cold 90k graph).
#
# Distinct from :func:`_fetch_draft_overlay` (which conflates "cross-org" and
# "no report" into an empty list and feeds the legacy node-highlight overlay):
# here we need the three-way result {cross-org → fall back to start panel,
# resolvable+report → subgraph, resolvable+no report → subgraph w/ fallback msg}.


def _resolve_draft_for_subgraph(req: Request, draft_id_raw: str) -> dict | None:
    """Return ``{draft_id, title, draft_url, report_url, has_report}`` or ``None``.

    ``None`` means "don't switch into subgraph mode" — i.e. unauthenticated,
    non-UUID id, missing draft, or a draft owned by another org. In those cases
    the caller falls back to the normal flow (start panel on a cold open). A
    valid, org-owned draft returns the dict even when it has no impact report
    yet (``has_report=False``); explorer.js then shows the graceful fallback.

    Any DB error is logged and treated as "can't resolve" → ``None`` so the
    explorer still renders (degrading to the start panel / overview).
    """
    auth = req.scope.get("auth")
    if not auth or not auth.get("org_id"):
        return None
    try:
        draft_uuid = uuid.UUID(draft_id_raw)
    except (TypeError, ValueError):
        return None
    org_id = str(auth.get("org_id"))
    try:
        with _connect() as conn:
            draft_row = conn.execute(
                "SELECT org_id, title FROM drafts WHERE id = %s",
                (str(draft_uuid),),
            ).fetchone()
            if draft_row is None or str(draft_row[0]) != org_id:
                return None
            title = str(draft_row[1] or "Eelnõu")
            report_row = conn.execute(
                "SELECT 1 FROM impact_reports WHERE draft_id = %s LIMIT 1",
                (str(draft_uuid),),
            ).fetchone()
    except Exception:
        logger.exception("explorer draft-subgraph resolve: DB error for draft=%s", draft_id_raw)
        return None
    return {
        "draft_id": str(draft_uuid),
        "title": title,
        "draft_url": f"/drafts/{draft_uuid}",
        "report_url": f"/drafts/{draft_uuid}/report",
        "has_report": report_row is not None,
    }


# ---------------------------------------------------------------------------
# <head> resources — D3 v7 CDN + the explorer stylesheet. Kept off the global
# ``_HDRS`` in app/main.py: D3 is ~270 KB and only the Õiguskaart page uses
# it, and explorer.css only styles this page's overlays.
# ---------------------------------------------------------------------------
_EXPLORER_HEAD: list = [
    Script(
        src="https://cdnjs.cloudflare.com/ajax/libs/d3/7.9.0/d3.min.js",
        integrity=(
            "sha512-vc58qvvBdrDR4etbxMdlTt4GBQk1qjvyORR2nrsPsFPyrs+/u5c3+1Ct6upOgdZoIl7eq6k3a1UPDSNAQi/32A=="
        ),
        crossorigin="anonymous",
    ),
    Link(rel="stylesheet", href="/static/css/explorer.css"),
]

# Auto-hide the (dismissed-once) draft-tip banner — reads the localStorage
# flag the toolbar's "×" button sets and removes the element on next load.
_TIP_AUTOHIDE_SCRIPT = (
    "(function(){"
    "if(localStorage.getItem('explorer-tip-dismissed')){"
    "var t=document.getElementById('explorer-tip-banner');"
    "if(t)t.remove();"
    "}"
    "})();"
)

# Tiny inline init for the ?draft= overlay: read the JSON blob, build a Set
# of URIs, and apply the highlight class to any node whose datum.uri /
# datum.id is in the set. Runs every 500ms so nodes that arrive after the
# first render via lazy-loading still get highlighted (#446 / Phase 2).
_DRAFT_OVERLAY_INIT_SCRIPT = (
    "(function(){"
    "var el=document.getElementById('draft-overlay-data');"
    "if(!el){return;}"
    "var data;try{data=JSON.parse(el.textContent||'{}');}catch(e){return;}"
    "var uris=new Set((data&&data.uris)||[]);"
    "if(!uris.size){return;}"
    "function apply(){"
    "if(typeof d3==='undefined'){return;}"
    "d3.selectAll('g.node').each(function(d){"
    "if(!d){return;}"
    "var key=d.uri||d.id;"
    "var hit=uris.has(key);"
    "d3.select(this).classed('d3-node-highlighted',hit);"
    "d3.select(this).select('circle.outer').classed('d3-node-highlighted',hit);"
    "});"
    "}"
    "var overlayInterval=setInterval(apply,500);"
    "apply();"
    "if(typeof htmx!=='undefined'){"
    "htmx.on('htmx:beforeSwap',function(){clearInterval(overlayInterval);});"
    "}"
    "})();"
)

# Detail-panel annotation button wiring. When explorerShowDetail() sets
# #panel-title, this MutationObserver injects an AnnotationButton (+ its
# popover container) into #panel-annotation-btn via HTMX — keeping
# explorer.js itself untouched. Built with DOM methods (no innerHTML);
# hx-get defaults its swap to innerHTML so no hx-swap attribute is needed.
#
# #773: the popover container uses a STATIC id (``panel-annotation-popover``)
# instead of an id derived from the entity URI. Only one detail panel is
# open at any moment, so a single, fixed CSS-safe id is the simplest fix
# for "raw URI characters in CSS selectors break HTMX hx-target". The
# raw URI is still encoded for the ?target_id= query string via
# encodeURIComponent so the GET payload reaches the server intact.
_PANEL_ANNOTATION_SCRIPT = (
    "(function(){"
    "var panelBtn=document.getElementById('panel-annotation-btn');"
    "var panelTitle=document.getElementById('panel-title');"
    "if(!panelBtn||!panelTitle){return;}"
    "var obs=new MutationObserver(function(){"
    "var uri=panelTitle.dataset.entityUri||panelTitle.textContent.trim();"
    "if(!uri){panelBtn.style.display='none';return;}"
    # encodeURIComponent for the query-string value only; the popover
    # container id below is a fixed string so CSS selectors stay valid.
    "var encUri=encodeURIComponent(uri);"
    "while(panelBtn.firstChild){panelBtn.removeChild(panelBtn.firstChild);}"
    "var wrap=document.createElement('div');"
    "wrap.className='annotation-button-wrapper';"
    # data-target-id carries the original URI so JS callers can recover
    # the raw identity without re-decoding.
    "wrap.setAttribute('data-target-type','entity');"
    "wrap.setAttribute('data-target-id',uri);"
    "var btn=document.createElement('button');"
    "btn.type='button';btn.className='annotation-button';"
    "btn.setAttribute('hx-get','/api/annotations?target_type=entity&target_id='+encUri);"
    "btn.setAttribute('hx-target','#panel-annotation-popover');"
    "btn.setAttribute('hx-swap','innerHTML');"
    "btn.setAttribute('aria-label','Markused');"
    "btn.setAttribute('title','Markused');"
    "btn.textContent='\\uD83D\\uDCAC';"
    "var pop=document.createElement('div');"
    # Fixed CSS-safe id — see header comment.  hx-target above resolves
    # to this element regardless of which entity is focused.
    "pop.id='panel-annotation-popover';"
    "pop.className='annotation-popover-container';"
    "wrap.appendChild(btn);wrap.appendChild(pop);"
    "panelBtn.appendChild(wrap);"
    "panelBtn.style.display='block';"
    "if(typeof htmx!=='undefined'){htmx.process(panelBtn);}"
    "});"
    "obs.observe(panelTitle,{childList:true,characterData:true,subtree:true});"
    "})();"
)


def _escape_for_script(payload: str) -> str:
    """Escape a JSON string so it is safe to embed inside a ``<script>``.

    ``</`` would otherwise terminate the script element early; the
    line-separator characters U+2028 / U+2029 are valid inside JSON strings
    but break JavaScript string literals in older parsers, so they are
    rewritten to their ``\\u`` escapes. JSON natively decodes ``<\\/`` back
    to ``</`` so ``json.loads`` still works on the rendered payload (#464).
    """
    return payload.replace("</", "<\\/").replace(" ", "\\u2028").replace(" ", "\\u2029")


# #756: the legal-view preset chips, rendered as one group in the toolbar.
def _legal_view_preset_chips(active_preset: str | None):  # noqa: ANN202
    """Build the ``Õigusvaated`` chip row for the toolbar.

    One ``.preset-chip`` button per entry in :data:`_LEGAL_VIEW_PRESETS`; the
    chip matching *active_preset* (the resolved ``?vaade=`` slug, if it names a
    real preset) carries the ``active`` class so the page reflects which view is
    on without waiting for JS. explorer.js wires the ``onclick`` (it calls
    ``explorerApplyPreset(slug)``), re-paints the active chip when a preset is
    clicked, and mirrors the slug into the URL.
    """
    chips: list = [Span("Õigusvaated:", cls="preset-group-label")]
    for slug, preset in _LEGAL_VIEW_PRESETS.items():
        is_active = slug == active_preset
        chips.append(
            Button(
                preset["label"],
                type="button",
                cls="ctrl-btn preset-chip active" if is_active else "ctrl-btn preset-chip",
                # explorer.js reads this to find the button when reflecting the
                # URL / re-painting the active state.
                data_vaade=slug,
                onclick=f"explorerApplyPreset('{slug}')",
                title=preset["title"],
                aria_pressed="true" if is_active else "false",
            )
        )
    return Div(
        *chips,
        id="explorer-presets",
        cls="preset-group",
        role="group",
        aria_label="Õiguskaardi vaated",
        data_active_vaade=active_preset or "",
    )


def _explorer_toolbar(
    *, has_back_context: bool, draft_tip: bool, active_preset: str | None = None
):  # noqa: ANN202
    """The thin horizontal graph toolbar across the top of the canvas.

    Holds the legal-work view actions (``Ülevaade`` / ``Lähtesta vaade``), the
    legal-view preset chips (#756 — ``Kehtiv õigus`` · ``Eelnõu mõjud`` · ``EL
    seosed`` · ``Kohtupraktika`` · ``Ajalugu``), the ``Vaate seaded``
    disclosure (technical layout controls), the search input + ``Otsi`` button
    (moved here from the deleted bespoke topbar), and — when the page was
    opened from a report/analysis — a "← Tagasi aruandesse" link. explorer.js
    wires the ``onclick`` handlers and unhides the panel back link based on
    ``document.referrer``. *active_preset* is the resolved ``?vaade=`` slug when
    it names a real preset (else ``None``); the matching chip renders active.
    """
    items: list = [
        # A small page-identity label — the highlighted sidebar item and
        # the browser tab title carry the rest. Compact, per the wireframe.
        Span("Õiguskaart", cls="toolbar-title"),
        Button(
            "Ülevaade",
            type="button",
            cls="ctrl-btn",
            onclick="explorerCollapseToOverview()",
            title="Näita kõigi liikide ülevaadet",
        ),
        Button(
            "Lähtesta vaade",
            type="button",
            cls="ctrl-btn",
            onclick="explorerResetView()",
            title="Lähtesta suum ja valik",
        ),
        # #756: legal-view presets — the legal-work surface; the raw graph
        # knobs stay under "Vaate seaded ▾" below.
        _legal_view_preset_chips(active_preset),
        # Technical layout controls behind a disclosure so the toolbar
        # reads in legal-work language, not force-simulation jargon
        # (#714 / #718). The menu drops down (position: absolute) so it
        # doesn't change the toolbar's height.
        Details(
            Summary("Vaate seaded ▾", cls="ctrl-btn ctrl-settings-summary"),
            Div(
                # #754: an explicit way back to the full 90k category overview
                # from inside the graph view — pairs with the start panel's
                # "Näita kogu kaarti" button. Loads the overview via JS (no
                # navigation) so it doesn't disturb the current focus state.
                Button(
                    "Näita kogu kaarti",
                    type="button",
                    cls="ctrl-btn",
                    onclick="explorerShowFullMap()",
                    title="Lae kogu õiguskaardi liikide ülevaade",
                ),
                Button(
                    "Lähtesta paigutus",
                    type="button",
                    cls="ctrl-btn",
                    onclick="explorerReheat()",
                    title="Arvuta sõlmpunktide paigutus uuesti",
                ),
                Button(
                    "Näita/peida seosenimed",
                    type="button",
                    cls="ctrl-btn",
                    onclick="explorerToggleLabels()",
                ),
                Button(
                    "Rühmita liigi järgi",
                    type="button",
                    cls="ctrl-btn",
                    onclick="explorerGroupByCategory()",
                ),
                cls="ctrl-settings-menu",
            ),
            cls="ctrl-settings",
        ),
        # Search — moved out of the deleted #topbar into the toolbar.
        # ``role="search"`` makes it a landmark; the input + button inside are
        # plain tab stops (the input also responds to Enter — wired in JS).
        Div(
            Input(
                id="search-input",
                type="text",
                placeholder="Otsi seadust, eelnõu, lahendit…",
                aria_label="Otsi seadusi, eelnõusid, lahendeid",
            ),
            Button(
                "Otsi",
                type="button",
                id="search-btn",
                onclick="explorerSearch()",
            ),
            id="search-box",
            role="search",
        ),
    ]
    if has_back_context:
        # explorer.js rewrites the label ("← Tagasi aruandesse" /
        # "← Tagasi analüüsi") from document.referrer; this toolbar anchor
        # is the counterpart of the detail panel's #panel-back link.
        items.append(
            A(
                "← Tagasi aruandesse",
                id="toolbar-back",
                href="#",
                cls="toolbar-back",
            )
        )
    if draft_tip:
        # Surfaced only on a "cold" open (no ?focus / ?draft / overlay) —
        # a small dismissible hint that ?draft=ID overlays a draft's
        # affected entities. localStorage remembers the dismissal.
        items.append(
            Span(
                Span(
                    "Näpunäide: lisage ?draft=ID URL-ile, et näha eelnõu "
                    "mõjutatud üksusi graafikul.",
                    cls="toolbar-tip-text",
                ),
                Button(
                    "×",
                    type="button",
                    cls="toolbar-tip-dismiss",
                    aria_label="Sulge",
                    onclick=(
                        "localStorage.setItem('explorer-tip-dismissed','1');"
                        "this.parentElement.remove()"
                    ),
                ),
                id="explorer-tip-banner",
                cls="toolbar-tip",
            )
        )
    return Nav(
        *items,
        id="explorer-toolbar",
        aria_label="Õiguskaardi tööriistad",
    )


# ---------------------------------------------------------------------------
# #754 — the contextual start panel (shown on a "cold" open)
# ---------------------------------------------------------------------------


# A muted single-line "nothing here yet" row shared by the panel's sections.
def _panel_empty(text: str):  # noqa: ANN202
    return P(text, cls="start-panel-empty")


def _start_panel_section(title: str, body):  # noqa: ANN202
    """One ``<section>`` of the start panel — an ``<h3>`` + its body."""
    return Section(H3(title, cls="start-panel-section-title"), body, cls="start-panel-section")


def _start_panel_search():  # noqa: ANN202
    """The search box at the top of the start panel.

    Submitting navigates to ``/explorer?search=<term>`` — the same ``?search=``
    plumbing the toolbar's "Otsi" button and the deep-link bridge already use
    (#746). ``explorer.js`` wires the click/Enter handlers (``startPanelSearch``)
    so the page can carry a clean ``<input>`` with no inline JS.
    """
    return Form(
        Input(
            id="start-panel-search-input",
            name="search",
            type="search",
            placeholder="Otsi seadust, §-viidet, CELEX-numbrit, kohtuasja numbrit…",
            aria_label="Otsi seadusi, §-viiteid, CELEX-numbreid, kohtuasju",
            autocomplete="off",
        ),
        Button(
            "Otsi",
            type="submit",
            id="start-panel-search-btn",
            cls="start-panel-search-btn",
        ),
        # method=GET so a no-JS submit still lands on /explorer?search=… ;
        # explorer.js intercepts to reuse the in-page search flow.
        method="get",
        action="/explorer",
        id="start-panel-search-form",
        cls="start-panel-search",
        role="search",
    )


def _start_panel_bookmarks(bookmarks: list):  # noqa: ANN202
    if not bookmarks:
        return _start_panel_section(
            "Sinu järjehoidjad", _panel_empty("Sul pole veel ühtegi järjehoidjat.")
        )
    items = [
        Li(
            A(bm["label"], href=bm["explorer_url"], cls="start-panel-link"),
            cls="start-panel-item",
        )
        for bm in bookmarks
    ]
    return _start_panel_section("Sinu järjehoidjad", Ul(*items, cls="start-panel-list"))


def _start_panel_high_risk(reports: list):  # noqa: ANN202
    if not reports:
        return _start_panel_section(
            "Hiljutised kõrge riskiga leiud",
            _panel_empty("Kõrge riskiga mõjuaruandeid hetkel pole."),
        )
    items = []
    for r in reports:
        meta_bits = f"{r['band_label']} · skoor {r['impact_score']}/100"
        if r["conflict_count"]:
            meta_bits += f" · {r['conflict_count']} konflikti"
        when = format_tallinn(r["generated_at"])
        items.append(
            Li(
                A(r["title"], href=r["report_url"], cls="start-panel-link"),
                Span(meta_bits, cls="start-panel-meta"),
                Span(when, cls="start-panel-meta start-panel-meta-date"),
                A(
                    "Ava mõjukaart",
                    href=r["explorer_url"],
                    cls="start-panel-secondary-link",
                ),
                cls="start-panel-item start-panel-item--rich",
            )
        )
    return _start_panel_section(
        "Hiljutised kõrge riskiga leiud", Ul(*items, cls="start-panel-list")
    )


def _start_panel_recent_drafts(drafts: list):  # noqa: ANN202
    if not drafts:
        return _start_panel_section(
            "Sinu hiljutised eelnõud", _panel_empty("Hiljutisi eelnõusid pole.")
        )
    items = []
    for d in drafts:
        when = format_tallinn(d["updated_at"])
        items.append(
            Li(
                A(d["title"], href=d["detail_url"], cls="start-panel-link"),
                Span(when, cls="start-panel-meta start-panel-meta-date"),
                A(
                    "Ava mõjukaart",
                    href=d["explorer_url"],
                    cls="start-panel-secondary-link",
                ),
                cls="start-panel-item start-panel-item--rich",
            )
        )
    return _start_panel_section("Sinu hiljutised eelnõud", Ul(*items, cls="start-panel-list"))


def _start_panel_shortcuts():  # noqa: ANN202
    """The two "do something" rows at the foot of the panel.

    "Alusta Normi mõjuahelat" → ``/analyysikeskus/normi-mojuahel`` (a real
    navigation). "Sirvi liikide kaupa" → loads today's category overview *in
    place* (``explorer.js`` ``explorerShowFullMap()``), which is now opt-in.
    """
    return Section(
        Div(
            A(
                "Alusta Normi mõjuahelat",
                href="/analyysikeskus/normi-mojuahel",
                cls="start-panel-action start-panel-action--primary",
            ),
            Button(
                "Sirvi liikide kaupa",
                type="button",
                id="start-panel-browse-btn",
                cls="start-panel-action start-panel-action--secondary",
                onclick="explorerShowFullMap()",
                title="Lae kõigi õiguskaardi liikide ülevaade",
            ),
            cls="start-panel-actions",
        ),
        cls="start-panel-section start-panel-section--actions",
    )


def _start_panel(data: StartPanelData):  # noqa: ANN202
    """Build the full contextual start panel (#754).

    Sits inside ``#main-content`` (``position: relative; overflow: hidden``)
    like the other overlays — ``explorer.css`` anchors ``#explorer-start-panel``
    absolutely below the toolbar.
    """
    return Div(
        Div(
            H2("Õiguskaart", cls="start-panel-title"),
            P(
                "Vali, mida vaadata — otsi õigusakti, ava järjehoidja, vaata "
                "eelnõu mõjukaarti, või sirvi kogu kaarti liikide kaupa.",
                cls="start-panel-lead",
            ),
            _start_panel_search(),
            _start_panel_bookmarks(data["bookmarks"]),
            _start_panel_high_risk(data["high_risk_reports"]),
            _start_panel_recent_drafts(data["recent_drafts"]),
            _start_panel_shortcuts(),
            cls="start-panel-inner",
        ),
        id="explorer-start-panel",
        # A labelled landmark region — a screen-reader user can jump straight
        # to "Õiguskaardi avapaneel" rather than tabbing past the page chrome.
        role="region",
        aria_label="Õiguskaardi avapaneel",
    )


def explorer_page(req: Request):
    """GET /explorer -- the Õiguskaart graph inside the standard app chrome.

    #755 (epic #762, workstream B): when called with ``?draft=<uuid>`` and the
    caller's org owns that draft, the page switches ``explorer.js`` into
    **impact-subgraph mode** — it fetches ``/explorer/draft-subgraph/{id}`` (a
    small org-scoped JSON endpoint that reshapes the draft's *already-computed*
    latest ``impact_reports`` row into D3 ``{nodes, links}`` — no fresh 90k
    traversal) and renders only that subgraph (the draft itself + its affected /
    conflicting / gap provisions + the inter-relations), with a "← Tagasi eelnõu
    juurde" link back to the draft. A valid draft with no impact report yet
    still enters the mode (the JS shows a "run the analysis" fallback rather
    than the cold 90k graph). Cross-org / malformed / non-UUID ``?draft=``
    params don't switch modes — they fall through to the normal flow (the
    contextual start panel on a cold open), never a 500. When ``?focus=`` is
    *also* present it wins: that's the legacy node-highlight overlay path (the
    affected-entity URIs from the latest report are embedded as a JSON blob in a
    ``<script id="draft-overlay-data">`` tag and ``explorer.js`` applies the
    ``d3-node-highlighted`` class to matching nodes in the full graph).

    When called with ``?focus=<uri>`` (URL-encoded — see
    :func:`app.docs.report_routes.explorer_focus_url`) the JS loads that
    entity's neighbourhood and opens the detail panel on it. ``?search=``
    instead pre-runs the existing search flow with the given term; if both
    ``?focus=`` and ``?search=`` are present, ``focus`` wins. The page
    itself requires authentication via the global ``auth_before``
    middleware (see #442).

    #754: a "cold" open — none of ``?focus=`` / ``?draft=`` / ``?search=`` and
    not the explicit ``?vaade=koik`` opt-in — renders the **contextual start
    panel** over the (empty) graph area and tells ``explorer.js`` *not* to
    fetch the 90k overview. ``?vaade=koik`` (set by the start panel's "Näita
    kogu kaarti" / "Sirvi liikide kaupa" buttons and the toolbar's "Näita kogu
    kaarti") forces the classic graph view. Unauthenticated requests never
    reach here — ``auth_before`` redirects to ``/auth/login`` first (#442).

    #756: ``?vaade=<slug>`` where ``<slug>`` names a legal-view preset (see
    :data:`_LEGAL_VIEW_PRESETS`) renders the graph view with that preset's chip
    active and hands the preset config to ``explorer.js`` (``window.__explorerVaade``)
    so the filter combo (entity types + relation types + timeline) is applied on
    load. Like ``?vaade=koik`` a preset slug bypasses the start panel. An unknown
    ``?vaade=`` value is ignored (treated as if absent).
    """
    auth = req.scope.get("auth")
    draft_param = req.query_params.get("draft", "").strip()
    focus_param = req.query_params.get("focus", "").strip()
    search_param = req.query_params.get("search", "").strip()
    vaade_param = req.query_params.get("vaade", "").strip()
    # #756: resolve ?vaade= once. ``active_preset`` is set only when the slug
    # names a real legal-view preset; ``?vaade=koik`` (the #754 "show me
    # everything" opt-in) and unknown values both leave it ``None``. ``?focus=``
    # / ``?search=`` are more specific intents (a particular entity / query) —
    # when present, ``?vaade=`` is ignored entirely so the chip state and the
    # JS init path don't fight the focus/search deep link.
    active_preset: str | None = None
    if not focus_param and not search_param:
        active_preset = vaade_param if vaade_param in _LEGAL_VIEW_PRESETS else None
    overlay_uris: list[str] = []
    # #755: ``?draft=<id>`` (without ``?focus=``) → render the draft's impact
    # subgraph (not the 90k graph). Resolve the draft org-scoped here so we can
    # tell explorer.js to switch into subgraph mode; the subgraph data itself is
    # fetched client-side from /explorer/draft-subgraph/{id}. ``None`` (unknown
    # / cross-org / non-UUID) → fall through to the normal flow (the start panel
    # on a cold open). When ``?focus=`` is *also* present (a report's deep link —
    # see :func:`app.docs.report_routes.explorer_focus_url`) the legacy
    # node-highlight overlay path takes over instead: the affected-entity URIs
    # are embedded as the ``draft-overlay-data`` blob and highlighted on the
    # full graph around the focused entity.
    draft_subgraph: dict | None = None
    if draft_param and not focus_param:
        draft_subgraph = _resolve_draft_for_subgraph(req, draft_param)
    elif draft_param and focus_param:
        overlay_uris = _fetch_draft_overlay(req, draft_param)

    # #754 / #756: the start panel shows only when there is nothing to deep-link
    # to and the user hasn't explicitly asked for a full/preset view. Any of
    # ?focus= / ?draft= / ?search= / ?vaade=koik / ?vaade=<preset> bypasses it
    # and renders the classic graph view (so existing deep links / overlays
    # never regress, and a preset has the graph to filter).
    show_start_panel = not (
        focus_param
        or search_param
        or draft_param
        or vaade_param == _VAADE_KOIK
        or active_preset is not None
    )

    # ---- Server → JS bridge: ?focus= / ?search= / ?vaade= / draft-overlay / mode blobs ----
    bridge_tags: list = []
    if show_start_panel:
        # The flag explorer.js checks on init() to skip loadOverview() — the
        # whole point of #754 (don't fetch the cold blob until the user picks).
        bridge_tags.append(Script("window.__explorerStartPanel=true;"))
    # #756: hand the full preset table + the active slug to explorer.js. The
    # table is small static config (no user data) so plain json.dumps is fine;
    # _escape_for_script keeps it safe inside the <script> regardless.
    presets_payload = _escape_for_script(json.dumps(_legal_view_presets_for_js()))
    bridge_tags.append(Script(f"window.__explorerPresets={presets_payload};"))
    if active_preset is not None:
        vaade_payload = _escape_for_script(json.dumps(active_preset))
        bridge_tags.append(Script(f"window.__explorerVaade={vaade_payload};"))
    # #719: hand the focus URI to the JS (it's validated server-side at
    # /api/explorer/entity/{uri}; embedding it escaped keeps the contract
    # explicit so the JS need not re-parse location.search).
    if focus_param.startswith("http"):
        focus_payload = _escape_for_script(json.dumps(focus_param))
        bridge_tags.append(Script(f"window.__explorerFocus={focus_payload};"))
    elif search_param:
        # ?search= pre-runs the search on load — the same path the "Otsi"
        # button triggers. Suppressed when ?focus= is present (focus is
        # the more specific intent). Escaped exactly like the focus blob.
        search_payload = _escape_for_script(json.dumps(search_param))
        bridge_tags.append(Script(f"window.__explorerSearch={search_payload};"))
    # #755: when ``?draft=<id>`` resolves to an org-owned draft, switch
    # explorer.js into impact-subgraph mode (it fetches the subgraph from the
    # data endpoint below and renders *only* that). This takes priority over
    # the legacy node-highlight overlay — we don't load the 90k graph at all in
    # this mode — so the ``draft-overlay-data`` blob + its init script are
    # suppressed here. The blob is escaped exactly like the other bridge tags.
    if draft_subgraph is not None:
        subgraph_payload = _escape_for_script(
            json.dumps(
                {
                    "draftId": draft_subgraph["draft_id"],
                    "title": draft_subgraph["title"],
                    "dataUrl": f"/explorer/draft-subgraph/{draft_subgraph['draft_id']}",
                    "draftUrl": draft_subgraph["draft_url"],
                    "reportUrl": draft_subgraph["report_url"],
                    "hasReport": bool(draft_subgraph["has_report"]),
                }
            )
        )
        bridge_tags.append(Script(f"window.__explorerDraftSubgraph={subgraph_payload};"))
    elif overlay_uris:
        # #464: escape closing-tag + Unicode line-separator sequences in
        # the JSON payload before embedding in a <script>.
        payload = _escape_for_script(json.dumps({"uris": overlay_uris}))
        bridge_tags.append(Script(payload, id="draft-overlay-data", type="application/json"))
        bridge_tags.append(Script(_DRAFT_OVERLAY_INIT_SCRIPT))

    # "Back to report/analysis" context: the page was opened from an impact
    # report / analysis result (?focus= or ?draft=) — explorer.js detects
    # the same thing from document.referrer for the detail-panel back link.
    # #755: the draft-subgraph mode also wires "← Tagasi eelnõu juurde" from the
    # blob's draftUrl, so it counts as back-context too.
    has_back_context = (
        bool(focus_param) or bool(overlay_uris) or bool(draft_param) or draft_subgraph is not None
    )
    # The small ?draft= toolbar tip is only useful in the classic graph view
    # with no focus/draft/overlay — the start panel makes it redundant.
    show_draft_tip = (
        not show_start_panel and not overlay_uris and not draft_param and not focus_param
    )

    # #754: the start panel is an opaque overlay over the (idle) graph chrome.
    # We still render the full graph DOM so explorer.js' top-level
    # getElementById() calls don't hit nulls — it just doesn't fetch data.
    start_panel_tag: list = []
    if show_start_panel:
        user_id = (auth or {}).get("id")
        org_id = (auth or {}).get("org_id")
        panel_data = load_start_panel_data(user_id, org_id)
        start_panel_tag.append(_start_panel(panel_data))

    content = (
        # ----- Contextual start panel (#754) — opaque overlay, cold open only -----
        *start_panel_tag,
        # ----- Graph toolbar (across the top of the canvas) -----
        _explorer_toolbar(
            has_back_context=has_back_context,
            draft_tip=show_draft_tip,
            active_preset=active_preset,
        ),
        # ----- Breadcrumb (drill-down trail; populated by explorer.js) -----
        # ``aria-label`` names the landmark even while it's empty; the crumbs
        # explorer.js injects are keyboard-operable (role="button" + Enter/Space).
        Nav(id="breadcrumb", aria_label="Asukoht õiguskaardil"),
        # ----- Tooltip -----
        Div(
            H3(id="tt-title"),
            Span(cls="cat", id="tt-cat"),
            P(id="tt-desc"),
            Div(id="tt-stat", cls="stat"),
            id="tooltip",
        ),
        # ----- Legend -----
        Div(
            H3("Kategooriad"),
            Div(
                Div(cls="legend-dot", style="background:#38bdf8"),
                "Kehtiv seadus",
                cls="legend-item",
            ),
            Div(
                Div(cls="legend-dot", style="background:#a78bfa"),
                "Eelnõu",
                cls="legend-item",
            ),
            Div(
                Div(cls="legend-dot", style="background:#fb923c"),
                "Kohtulahend",
                cls="legend-item",
            ),
            Div(
                Div(cls="legend-dot", style="background:#34d399"),
                "EL õigusakt",
                cls="legend-item",
            ),
            Div(
                Div(cls="legend-dot", style="background:#f472b6"),
                "EL kohtulahend",
                cls="legend-item",
            ),
            id="legend",
        ),
        # ----- Instructions -----
        Div(
            "Lohista sõlmpunkte ümber · Kerige suumimiseks "
            "· Klõpsa ja lohista tausta panoraamimiseks",
            Br(),
            "Hõlju sõlmpunktil detailide nägemiseks "
            "· Klõpsa kategooriat avamiseks "
            "· Klõpsa olemit kinnitamiseks",
            id="instructions",
        ),
        # ----- Loading overlay -----
        Div(
            Div(cls="spinner"),
            id="loading-overlay",
        ),
        # ----- Timeline / "ajaline vaade" slider (bottom of the canvas) -----
        Div(
            Div(
                Span("1990", cls="tl-label"),
                Input(
                    id="timeline-slider",
                    type="range",
                    min="1990",
                    max=str(datetime.now().year + 1),
                    value=str(datetime.now().year + 1),
                    step="1",
                    aria_label="Ajaline vaade — aasta valik",
                ),
                Span(str(datetime.now().year + 1), cls="tl-label"),
                cls="tl-slider-row",
            ),
            Div(
                Span("Ajaline vaade: ", cls="tl-prefix"),
                Span("Väljas", id="timeline-value", cls="tl-value"),
                Button(
                    "Lähtesta",
                    type="button",
                    id="timeline-reset",
                    cls="tl-reset-btn",
                    onclick="explorerResetTimeline()",
                ),
                cls="tl-info-row",
            ),
            id="timeline-bar",
        ),
        # ----- Detail panel — the evidence card (#757; epic #762, design doc
        # docs/2026-05-12-oiguskaart-evidence-map.md, workstream D). Restructured
        # from a plain metadata dump into: Allikas (source act/draft/court) ·
        # Kuupäev / versioon · Seose liik (relation type in legal language) ·
        # Miks see oluline on (a deterministic one-line note) · Tegevused (four
        # action buttons). The original element IDs (#panel-title, #panel-meta,
        # #panel-neighbors, #panel-bookmark-btn, #panel-annotation-btn,
        # #panel-link, #panel-back, #panel-category, #panel-versions,
        # #version-history-section) are kept so explorer.js + the panel
        # annotation MutationObserver above keep working unchanged. -----
        Div(
            # #719: shown only when the page was opened via ?focus= (i.e.
            # from an impact report / analysis) — explorer.js unhides it
            # and sets the label/target.
            A(
                "← Tagasi",
                id="panel-back",
                href="#",
                cls="panel-back",
                style="display:none;",
            ),
            Div(
                H2(id="panel-title"),
                Button(
                    "×",
                    type="button",
                    id="detail-close",
                    onclick="explorerCloseDetail()",
                    aria_label="Sulge üksikasjade paneel",
                ),
                cls="panel-header",
            ),
            Span(id="panel-category", cls="panel-category"),
            # #757: Allikas — the parent law / draft / court the entity belongs
            # to. explorer.js fills #panel-source-row (and hides the section
            # when there's no derivable source).
            Div(
                H4("Allikas"),
                Div(id="panel-source-row", cls="evidence-source-row"),
                id="evidence-source-section",
                cls="meta-section evidence-section",
                style="display:none;",
            ),
            # #757: Kuupäev / versioon — the entity's date / version literals.
            Div(
                H4("Kuupäev / versioon"),
                Div(id="panel-date-info", cls="evidence-date-info"),
                id="evidence-date-section",
                cls="meta-section evidence-section",
                style="display:none;",
            ),
            # #757: Seose liik — the relation type, in legal language, to the
            # previously-focused node (or to whatever opened the panel).
            Div(
                H4("Seose liik"),
                Div(id="panel-relation", cls="evidence-relation"),
                id="evidence-relation-section",
                cls="meta-section evidence-section",
                style="display:none;",
            ),
            # #757: Miks see oluline on — a deterministic one-line note derived
            # from (relation type) + (impact band if known). Not an LLM call.
            Div(
                H4("Miks see oluline on"),
                P(id="panel-why", cls="evidence-why"),
                id="evidence-why-section",
                cls="meta-section evidence-section",
                style="display:none;",
            ),
            Div(
                H4("Metaandmed"),
                Div(id="panel-meta"),
                cls="meta-section",
            ),
            # ----- Version history section -----
            Div(
                H4("Versiooniajalugu"),
                Div(id="panel-versions"),
                id="version-history-section",
                cls="meta-section",
                style="display:none;",
            ),
            Div(
                H4("Seosed"),
                Ul(id="panel-neighbors", cls="neighbor-list"),
                cls="meta-section",
            ),
            # ----- #757: Tegevused — the evidence card's four action buttons.
            # 1) Küsi nõustajalt selle kohta — a tiny <form> POSTed to the
            #    existing /chat/seed single-use-token route (explorer.js fills
            #    the hidden seed_text/draft_id inputs before showing it).
            # 2) Ava analüüsikeskuses — link to /analyysikeskus/normi-mojuahel
            #    ?sisend=<entity-uri> (explorer.js sets the href).
            # 3) Lisa märkus — reuses #panel-annotation-btn (the entity-level
            #    AnnotationButton wired by _PANEL_ANNOTATION_SCRIPT above);
            #    hidden when the entity has no annotation target.
            # 4) Lisa järjehoidja — the existing #743 XHR bookmark button. -----
            Div(
                H4("Tegevused"),
                # (1) Küsi nõustajalt — POST /chat/seed (server-side token).
                Form(
                    Hidden(name="seed_text", value="", id="panel-chat-seed-text"),
                    Hidden(name="draft_id", value="", id="panel-chat-seed-draft"),
                    Button(
                        "Küsi nõustajalt selle kohta",
                        type="submit",
                        cls="evidence-action evidence-action-chat",
                    ),
                    method="post",
                    action="/chat/seed",
                    id="panel-chat-seed-form",
                    cls="evidence-action-form",
                ),
                # (2) Ava analüüsikeskuses — /analyysikeskus/normi-mojuahel?sisend=
                A(
                    "Ava analüüsikeskuses",
                    id="panel-analyysikeskus-link",
                    href="/analyysikeskus/normi-mojuahel",
                    cls="evidence-action evidence-action-analyysikeskus",
                ),
                # (3) Lisa märkus — the entity-level annotation button (filled
                # by _PANEL_ANNOTATION_SCRIPT; hidden when there's no target).
                Div(
                    id="panel-annotation-btn",
                    cls="annotation-section evidence-action-annotation",
                    style="display:none;",
                ),
                # (4) Lisa järjehoidja — the #743 XHR bookmark button.
                Button(
                    "Lisa järjehoidja",
                    type="button",
                    id="panel-bookmark-btn",
                    cls="bookmark-btn evidence-action evidence-action-bookmark",
                    onclick="explorerBookmark()",
                ),
                cls="meta-section evidence-actions-section",
            ),
            A(
                "Ava allikas",
                id="panel-link",
                cls="external-link",
                href="#",
                target="_blank",
                rel="noopener",
            ),
            id="detail-panel",
            # The evidence card is a labelled region; its H4-headed sections give
            # screen-reader users a heading-nav structure inside it. It starts
            # hidden from AT (it's off-screen via CSS until ``.open``) — explorer.js
            # flips ``aria-hidden`` and moves focus to the close button when it
            # opens, and restores focus to the opener when it closes (#760).
            role="region",
            aria_label="Üksuse üksikasjad",
            aria_hidden="true",
        ),
        # ----- SVG canvas (fills the content area; sized by explorer.js) -----
        NotStr('<svg id="canvas"></svg>'),
        # ----- #758: mini-map (overview panel, bottom-right corner) -----
        # A small <svg> overview of the whole current node set with a
        # draggable viewport rectangle; explorer.js renders into it, keeps the
        # rect in sync with the main d3-zoom transform, and pans the main view
        # on click/drag. Hidden until the graph is populated (explorer.js
        # toggles the ``visible`` class).
        # The mini-map is a visual duplicate of the main canvas + a mouse-only
        # pan affordance — ``role="img"`` + the label name it for AT without
        # exposing the (pointer-only) drag handle as a fake widget; it isn't a
        # tab stop, so it can't become a keyboard trap (#760).
        Div(
            NotStr('<svg id="minimap-svg" focusable="false" aria-hidden="true"></svg>'),
            id="minimap",
            role="img",
            aria_label="Õiguskaardi miniülevaade",
        ),
        # ----- Server → JS bridge blobs (mode flag, ?focus= / ?search=, draft
        # overlay) — emitted BEFORE explorer.js so the flags are visible to
        # init()'s *synchronous* prologue (the start-panel gate in particular
        # has to be read before loadOverview() is called). #719's ?focus= /
        # ?search= blobs are only consulted after an `await`, so this ordering
        # is also safe for them. -----
        *bridge_tags,
        # ----- Explorer JS (after the DOM it touches + the bridge blobs) -----
        Script(src="/static/js/explorer.js"),
        # ----- Auto-hide the (dismissed) draft tip -----
        Script(_TIP_AUTOHIDE_SCRIPT),
        # ----- Detail-panel annotation button wiring -----
        Script(_PANEL_ANNOTATION_SCRIPT),
    )

    return PageShell(
        *content,
        title="Õiguskaart",
        user=auth or None,
        active_nav="/explorer",
        request=req,
        full_bleed=True,
        extra_head=_EXPLORER_HEAD,
    )


# ---------------------------------------------------------------------------
# Route registration
# ---------------------------------------------------------------------------


def register_explorer_pages(rt) -> None:  # type: ignore[no-untyped-def]
    """Register explorer page routes on the FastHTML route decorator *rt*."""
    rt("/explorer", methods=["GET"])(explorer_page)
