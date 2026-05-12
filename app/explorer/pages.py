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
_PANEL_ANNOTATION_SCRIPT = (
    "(function(){"
    "var panelBtn=document.getElementById('panel-annotation-btn');"
    "var panelTitle=document.getElementById('panel-title');"
    "if(!panelBtn||!panelTitle){return;}"
    "var obs=new MutationObserver(function(){"
    "var uri=panelTitle.dataset.entityUri||panelTitle.textContent.trim();"
    "if(!uri){panelBtn.style.display='none';return;}"
    "var safeId=encodeURIComponent(uri).replace(/'/g,'%27');"
    "while(panelBtn.firstChild){panelBtn.removeChild(panelBtn.firstChild);}"
    "var wrap=document.createElement('div');"
    "wrap.className='annotation-button-wrapper';"
    "var btn=document.createElement('button');"
    "btn.type='button';btn.className='annotation-button';"
    "btn.setAttribute('hx-get','/api/annotations?target_type=entity&target_id='+safeId);"
    "btn.setAttribute('hx-target','#annotation-popover-entity-'+safeId);"
    "btn.setAttribute('aria-label','Markused');"
    "btn.setAttribute('title','Markused');"
    "btn.textContent='\\uD83D\\uDCAC';"
    "var pop=document.createElement('div');"
    "pop.id='annotation-popover-entity-'+safeId;"
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
        aria_label="Õiguskaardi avapaneel",
    )


def explorer_page(req: Request):
    """GET /explorer -- the Õiguskaart graph inside the standard app chrome.

    When called with ``?draft=<uuid>`` and the caller's org owns that
    draft, the affected-entity URIs from its latest impact report are
    embedded as a JSON blob in a ``<script id="draft-overlay-data">``
    tag. The explorer JS reads this blob on load and applies the
    ``d3-node-highlighted`` class to matching nodes. Cross-org or
    malformed draft params are silently dropped (no error UI) so the
    page stays usable as a generic ontology browser even when the
    overlay can't be applied.

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
    if draft_param:
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
    if overlay_uris:
        # #464: escape closing-tag + Unicode line-separator sequences in
        # the JSON payload before embedding in a <script>.
        payload = _escape_for_script(json.dumps({"uris": overlay_uris}))
        bridge_tags.append(Script(payload, id="draft-overlay-data", type="application/json"))
        bridge_tags.append(Script(_DRAFT_OVERLAY_INIT_SCRIPT))

    # "Back to report/analysis" context: the page was opened from an impact
    # report / analysis result (?focus= or ?draft=) — explorer.js detects
    # the same thing from document.referrer for the detail-panel back link.
    has_back_context = bool(focus_param) or bool(overlay_uris) or bool(draft_param)
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
        # ----- Breadcrumb -----
        Div(id="breadcrumb"),
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
        # ----- Detail panel (slides in from the right edge of the content) -----
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
            # ----- Annotation button (entity-level) -----
            Div(
                id="panel-annotation-btn",
                cls="annotation-section",
                style="display:none;",
            ),
            # ----- Bookmark button -----
            Div(
                Button(
                    "Lisa järjehoidjatesse",
                    type="button",
                    id="panel-bookmark-btn",
                    cls="bookmark-btn",
                    onclick="explorerBookmark()",
                ),
                cls="bookmark-section",
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
        ),
        # ----- SVG canvas (fills the content area; sized by explorer.js) -----
        NotStr('<svg id="canvas"></svg>'),
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
