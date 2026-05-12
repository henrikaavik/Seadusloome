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

# Re-import our design-system Button after the wildcard so the symbol does
# not silently fall back to FastHTML's unstyled Button (#419).
from app.ui.layout import PageShell
from app.ui.primitives.button import Button  # noqa: E402

logger = logging.getLogger(__name__)


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


def _explorer_toolbar(*, has_back_context: bool, draft_tip: bool):  # noqa: ANN202
    """The thin horizontal graph toolbar across the top of the canvas.

    Holds the legal-work view actions (``Ülevaade`` / ``Lähtesta vaade``),
    the ``Vaate seaded`` disclosure (technical layout controls), the search
    input + ``Otsi`` button (moved here from the deleted bespoke topbar),
    and — when the page was opened from a report/analysis — a "← Tagasi
    aruandesse" link. explorer.js wires the ``onclick`` handlers and unhides
    the panel back link based on ``document.referrer``.
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
        # Technical layout controls behind a disclosure so the toolbar
        # reads in legal-work language, not force-simulation jargon
        # (#714 / #718). The menu drops down (position: absolute) so it
        # doesn't change the toolbar's height.
        Details(
            Summary("Vaate seaded ▾", cls="ctrl-btn ctrl-settings-summary"),
            Div(
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
    """
    auth = req.scope.get("auth")
    draft_param = req.query_params.get("draft", "").strip()
    focus_param = req.query_params.get("focus", "").strip()
    search_param = req.query_params.get("search", "").strip()
    overlay_uris: list[str] = []
    if draft_param:
        overlay_uris = _fetch_draft_overlay(req, draft_param)

    # ---- Server → JS bridge: ?focus= / ?search= / draft-overlay blobs ----
    bridge_tags: list = []
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
    # A "cold" open (no focus / draft / overlay) gets the small ?draft= tip.
    show_draft_tip = not overlay_uris and not draft_param and not focus_param

    content = (
        # ----- Graph toolbar (across the top of the canvas) -----
        _explorer_toolbar(has_back_context=has_back_context, draft_tip=show_draft_tip),
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
        # ----- Explorer JS (after the DOM it touches) -----
        Script(src="/static/js/explorer.js"),
        # ----- Auto-hide the (dismissed) draft tip -----
        Script(_TIP_AUTOHIDE_SCRIPT),
        # ----- Detail-panel annotation button wiring -----
        Script(_PANEL_ANNOTATION_SCRIPT),
        # ----- ?focus= / ?search= / draft-overlay bridge blobs -----
        *bridge_tags,
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
