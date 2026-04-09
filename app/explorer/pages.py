"""Explorer page — serves the full-page D3.js ontology graph explorer."""

from __future__ import annotations

import json
import logging
import uuid

from fasthtml.common import *
from starlette.requests import Request

from app.db import get_connection as _connect

# Re-import our design-system Button after the wildcard so the symbol does
# not silently fall back to FastHTML's unstyled Button (#419).
from app.ui.primitives.button import Button  # noqa: E402

logger = logging.getLogger(__name__)


def _fetch_draft_overlay(req: Request, draft_id_raw: str) -> list[str]:
    """Return affected entity URIs for the draft, or an empty list.

    Access control rules:

    - Unauthenticated requests get an empty list (the explorer page is
      public and we never want it to leak draft data).
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

    try:
        with _connect() as conn:
            draft_row = conn.execute(
                "SELECT org_id FROM drafts WHERE id = %s",
                (str(draft_uuid),),
            ).fetchone()
            if draft_row is None:
                return []
            if str(draft_row[0]) != str(auth.get("org_id")):
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
        return []

    raw = report_row[0]
    if isinstance(raw, str):
        try:
            data = json.loads(raw)
        except (TypeError, ValueError):
            return []
    elif isinstance(raw, dict):
        data = raw
    else:
        return []

    affected = data.get("affected_entities") or []
    uris: list[str] = []
    for row in affected:
        if isinstance(row, dict):
            uri = row.get("uri")
            if uri:
                uris.append(str(uri))
    return uris


def explorer_page(req: Request):
    """GET /explorer -- full-screen interactive ontology graph.

    When called with ``?draft=<uuid>`` and the caller's org owns that
    draft, the affected-entity URIs from its latest impact report are
    embedded as a JSON blob in a ``<script id="draft-overlay-data">``
    tag. The explorer JS reads this blob on load and applies the
    ``d3-node-highlighted`` class to matching nodes. Cross-org or
    malformed draft params are silently dropped (no error UI) so the
    page stays browseable for unauthenticated visitors as well.
    """
    draft_param = req.query_params.get("draft", "").strip()
    overlay_uris: list[str] = []
    if draft_param:
        overlay_uris = _fetch_draft_overlay(req, draft_param)

    overlay_tags: list = []
    if overlay_uris:
        overlay_tags.append(
            Script(
                json.dumps({"uris": overlay_uris}),
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
                "setInterval(apply,500);"
                "apply();"
                "})();"
            )
        )

    return Html(
        Head(
            Meta(charset="UTF-8"),
            Meta(name="viewport", content="width=device-width, initial-scale=1.0"),
            Title("Eesti \u00f5iguse ontoloogia \u2014 Explorer"),
            # D3.js v7
            Script(
                src="https://cdnjs.cloudflare.com/ajax/libs/d3/7.9.0/d3.min.js",
                integrity="sha512-jmDsOHNPGMbOkS50n+TJaZNJBaJCz5Z+3dqzsPe9C5Vs7BVrL/9r8g9EDR0+vRYHFjpFa2cmNqzO+wFLZBkKg==",
                crossorigin="anonymous",
            ),
            # Explorer styles
            Link(rel="stylesheet", href="/static/css/explorer.css"),
        ),
        Body(
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
                        max="2026",
                        value="2026",
                        step="1",
                    ),
                    Span("2026", cls="tl-label"),
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
            # ----- Optional draft overlay (Phase 2 Batch 4) -----
            *overlay_tags,
            cls="explorer-page",
        ),
    )


# ---------------------------------------------------------------------------
# Route registration
# ---------------------------------------------------------------------------


def register_explorer_pages(rt) -> None:  # type: ignore[no-untyped-def]
    """Register explorer page routes on the FastHTML route decorator *rt*."""
    rt("/explorer", methods=["GET"])(explorer_page)
