"""Explorer page — serves the full-page D3.js ontology graph explorer."""

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


def explorer_page(req: Request):
    """GET /explorer -- full-screen interactive ontology graph.

    When called with ``?draft=<uuid>`` and the caller's org owns that
    draft, the affected-entity URIs from its latest impact report are
    embedded as a JSON blob in a ``<script id="draft-overlay-data">``
    tag. The explorer JS reads this blob on load and applies the
    ``d3-node-highlighted`` class to matching nodes. Cross-org or
    malformed draft params are silently dropped (no error UI) so the
    page stays usable as a generic ontology browser even when the
    overlay can't be applied. The page itself requires authentication
    via the global ``auth_before`` middleware (see #442).
    """
    draft_param = req.query_params.get("draft", "").strip()
    overlay_uris: list[str] = []
    if draft_param:
        overlay_uris = _fetch_draft_overlay(req, draft_param)

    overlay_tags: list = []
    if overlay_uris:
        # #464: escape any closing-tag and Unicode line-separator
        # sequences in the JSON payload before embedding in a
        # ``<script>`` tag. ``</`` would otherwise terminate the
        # script element early and let an attacker inject HTML;
        # U+2028 / U+2029 are valid in JSON strings but not in
        # JavaScript string literals, so leaving them in causes a
        # parse error in older browsers and confuses some inline
        # script parsers. JSON natively decodes ``<\/`` back to
        # ``</`` so json.loads still works on the rendered payload.
        payload = json.dumps({"uris": overlay_uris})
        payload = (
            payload.replace("</", "<\\/").replace("\u2028", "\\u2028").replace("\u2029", "\\u2029")
        )
        overlay_tags.append(
            Script(
                payload,
                id="draft-overlay-data",
                type="application/json",
            )
        )
        # Tiny inline init: read the JSON blob, build a Set of URIs,
        # and apply the highlight class to any node whose datum.uri or
        # datum.id is in the set. Runs every 500ms so we catch nodes
        # that arrive after the initial render via lazy-loading.
        overlay_tags.append(
            Script(
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
        )

    # Build the help banner elements that sit above the top bar.
    help_banner = Div(
        Div(
            Span(
                "\u2139\ufe0f",
                cls="info-box-icon",
                aria_hidden="true",
            ),
            Div(
                "See on Eesti \u00f5iguse ontoloogia uurija. "
                "Kl\u00f5psake kategooriatele, et uurida seadusi, kohtuotsuseid "
                "ja EL-i \u00f5igusakte. Kasutage otsingut konkreetsete "
                "s\u00e4tete leidmiseks.",
                cls="info-box-content",
            ),
            Button(
                "\u00d7",
                type="button",
                cls="info-box-dismiss",
                aria_label="Sulge",
                onclick="this.parentElement.parentElement.remove()",
            ),
            cls="info-box info-box-info",
            role="note",
        ),
        id="explorer-help-banner",
        style="padding:0.75rem 1rem 0;",
    )

    # Optional draft tip
    draft_tip = None
    if not overlay_uris and not draft_param:
        draft_tip = Div(
            Div(
                Span(
                    "\U0001f4a1",
                    cls="info-box-icon",
                    aria_hidden="true",
                ),
                Div(
                    "N\u00e4pun\u00e4ide: lisage ?draft=ID URL-ile, "
                    "et n\u00e4ha eeln\u00f5u m\u00f5jutatud \u00fcksusi graafikul.",
                    cls="info-box-content",
                ),
                Button(
                    "\u00d7",
                    type="button",
                    cls="info-box-dismiss",
                    aria_label="Sulge",
                    onclick="this.parentElement.parentElement.remove()",
                ),
                cls="info-box info-box-tip",
                role="note",
            ),
            style="padding:0 1rem;",
        )

    return Html(
        Head(
            Meta(charset="UTF-8"),
            Meta(name="viewport", content="width=device-width, initial-scale=1.0"),
            Title("Eesti \u00f5iguse ontoloogia \u2014 Explorer"),
            # D3.js v7
            Script(
                src="https://cdnjs.cloudflare.com/ajax/libs/d3/7.9.0/d3.min.js",
                integrity="sha512-vc58qvvBdrDR4etbxMdlTt4GBQk1qjvyORR2nrsPsFPyrs+/u5c3+1Ct6upOgdZoIl7eq6k3a1UPDSNAQi/32A==",
                crossorigin="anonymous",
            ),
            # Design tokens (CSS custom properties) — required by ui.css
            Link(rel="stylesheet", href="/static/css/tokens.css"),
            # Explorer styles
            Link(rel="stylesheet", href="/static/css/explorer.css"),
            # Phase 2 additions (#446): the .d3-node-highlighted rule
            # used by the draft overlay lives in ui.css. Pull the
            # whole stylesheet in so future Phase 2 additions take
            # effect on the explorer page without per-rule duplication.
            Link(rel="stylesheet", href="/static/css/ui.css"),
        ),
        Body(
            # ----- Help banner -----
            help_banner,
            draft_tip if draft_tip else "",
            # ----- Top bar -----
            Div(
                H1("Estonian Legal Ontology"),
                Span("Explorer", cls="badge"),
                Span("D3.js", cls="badge"),
                # Search box
                Div(
                    Input(
                        id="search-input",
                        type="text",
                        placeholder="Otsi seadust, eeln\u00f5u, lahendit\u2026",
                    ),
                    Button(
                        "Otsi",
                        id="search-btn",
                        onclick="explorerSearch()",
                    ),
                    id="search-box",
                ),
                id="topbar",
            ),
            # ----- Controls -----
            Div(
                Button(
                    "Taask\u00e4ivita simulatsioon",
                    cls="ctrl-btn",
                    onclick="explorerReheat()",
                ),
                Button(
                    "L\u00fclita silte",
                    cls="ctrl-btn",
                    onclick="explorerToggleLabels()",
                ),
                Button(
                    "R\u00fchm. kategooria j\u00e4rgi",
                    cls="ctrl-btn",
                    onclick="explorerGroupByCategory()",
                ),
                Button(
                    "L\u00e4htesta vaade",
                    cls="ctrl-btn",
                    onclick="explorerResetView()",
                ),
                Button(
                    "\u00dclevaade",
                    cls="ctrl-btn",
                    onclick="explorerCollapseToOverview()",
                ),
                # ----- Divider -----
                Div(cls="ctrl-divider"),
                # ----- Navigation -----
                Button(
                    "Töölaud",
                    cls="nav-btn",
                    onclick="location.href='/dashboard'",
                ),
                Button(
                    "Eelnõud",
                    cls="nav-btn",
                    onclick="location.href='/drafts'",
                ),
                Button(
                    "Koostaja",
                    cls="nav-btn",
                    onclick="location.href='/drafter'",
                ),
                Button(
                    "Vestlus",
                    cls="nav-btn",
                    onclick="location.href='/chat'",
                ),
                Button(
                    "Admin",
                    cls="nav-btn",
                    onclick="location.href='/admin'",
                ),
                id="controls",
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
                    "Enacted Law",
                    cls="legend-item",
                ),
                Div(
                    Div(cls="legend-dot", style="background:#a78bfa"),
                    "Draft Legislation",
                    cls="legend-item",
                ),
                Div(
                    Div(cls="legend-dot", style="background:#fb923c"),
                    "Court Decisions",
                    cls="legend-item",
                ),
                Div(
                    Div(cls="legend-dot", style="background:#34d399"),
                    "EU Legislation",
                    cls="legend-item",
                ),
                Div(
                    Div(cls="legend-dot", style="background:#f472b6"),
                    "EU Court Decisions",
                    cls="legend-item",
                ),
                id="legend",
            ),
            # ----- Instructions -----
            Div(
                "Lohista s\u00f5lmpunkte \u00fcmber \u00b7 Kerige suumimiseks "
                "\u00b7 Kl\u00f5psa ja lohista tausta panoraamimiseks",
                Br(),
                "H\u00f5lju s\u00f5lmpunktil detailide n\u00e4gemiseks "
                "\u00b7 Kl\u00f5psa kategooriat avamiseks "
                "\u00b7 Kl\u00f5psa olemit kinnitamiseks",
                id="instructions",
            ),
            # ----- Loading overlay -----
            Div(
                Div(cls="spinner"),
                id="loading-overlay",
            ),
            # ----- Timeline slider -----
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
                    ),
                    Span(str(datetime.now().year + 1), cls="tl-label"),
                    cls="tl-slider-row",
                ),
                Div(
                    Span("Ajafilter: ", cls="tl-prefix"),
                    Span("Keelatud", id="timeline-value", cls="tl-value"),
                    Button(
                        "L\u00e4htesta",
                        id="timeline-reset",
                        cls="tl-reset-btn",
                        onclick="explorerResetTimeline()",
                    ),
                    cls="tl-info-row",
                ),
                id="timeline-bar",
            ),
            # ----- Toast container -----
            Div(id="toast-container"),
            # ----- Detail panel (right sidebar) -----
            Div(
                Div(
                    H2(id="panel-title"),
                    Button(
                        "\u00d7",
                        id="detail-close",
                        onclick="explorerCloseDetail()",
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
                        "Lisa j\u00e4rjehoidjatesse",
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
            # ----- SVG canvas -----
            NotStr('<svg id="canvas"></svg>'),
            # ----- Explorer JS (after DOM) -----
            Script(src="/static/js/explorer.js"),
            # ----- Annotation button wiring for detail panel -----
            # When explorerShowDetail() sets #panel-title, this
            # observer injects an AnnotationButton via HTMX into
            # #panel-annotation-btn. The MutationObserver pattern
            # avoids patching explorer.js directly.
            Script(
                "(function(){"
                "var panelBtn=document.getElementById('panel-annotation-btn');"
                "var panelTitle=document.getElementById('panel-title');"
                "if(!panelBtn||!panelTitle){return;}"
                "var obs=new MutationObserver(function(){"
                "var uri=panelTitle.dataset.entityUri||panelTitle.textContent.trim();"
                "if(!uri){panelBtn.style.display='none';return;}"
                "var safeId=encodeURIComponent(uri).replace(/'/g,'%27');"
                "panelBtn.innerHTML="
                '\'<div class="annotation-button-wrapper">'
                '<button type="button" class="annotation-button"'
                " hx-get=\"/api/annotations?target_type=entity&target_id='+safeId+'\""
                " hx-target=\"#annotation-popover-entity-'+safeId+'\""
                ' hx-swap="innerHTML"'
                ' aria-label="Markused"'
                ' title="Markused">&#128172;</button>'
                "<div id=\"annotation-popover-entity-'+safeId+'\""
                ' class="annotation-popover-container"></div></div>\';'
                "panelBtn.style.display='block';"
                "if(typeof htmx!=='undefined'){htmx.process(panelBtn);}"
                "});"
                "obs.observe(panelTitle,{childList:true,characterData:true,subtree:true});"
                "})();"
            ),
            # ----- Optional draft overlay (Phase 2 Batch 4) -----
            *overlay_tags,
            cls="explorer-page",
        ),
        data_theme="dark",
    )


# ---------------------------------------------------------------------------
# Route registration
# ---------------------------------------------------------------------------


def register_explorer_pages(rt) -> None:  # type: ignore[no-untyped-def]
    """Register explorer page routes on the FastHTML route decorator *rt*."""
    rt("/explorer", methods=["GET"])(explorer_page)
