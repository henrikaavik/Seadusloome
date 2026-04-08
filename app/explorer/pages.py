"""Explorer page — serves the full-page D3.js ontology graph explorer."""

from __future__ import annotations

from fasthtml.common import *
from starlette.requests import Request


def explorer_page(req: Request):
    """GET /explorer -- full-screen interactive ontology graph."""
    return (
        Html(
            Head(
                Meta(charset="UTF-8"),
                Meta(name="viewport", content="width=device-width, initial-scale=1.0"),
                Title("Eesti \u00f5iguse ontoloogia \u2014 Explorer"),
                # D3.js v7
                Script(
                    src="https://cdnjs.cloudflare.com/ajax/libs/d3/7.9.0/d3.min.js",
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
                    Div(
                        H4("Seosed"),
                        Ul(id="panel-neighbors", cls="neighbor-list"),
                        cls="meta-section",
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
                cls="explorer-page",
            ),
        )
    )


# ---------------------------------------------------------------------------
# Route registration
# ---------------------------------------------------------------------------


def register_explorer_pages(rt) -> None:  # type: ignore[no-untyped-def]
    """Register explorer page routes on the FastHTML route decorator *rt*."""
    rt("/explorer", methods=["GET"])(explorer_page)
