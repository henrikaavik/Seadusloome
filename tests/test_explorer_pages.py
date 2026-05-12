"""Smoke tests for the Õiguskaart (explorer) page (#714 / #718 / #746 / #754).

Renders ``explorer_page`` directly via ``to_xml()`` and asserts on the
markup — no TestClient. The start-panel DB queries (#754) are stubbed out by
an autouse fixture so these stay DB-free. Post-#746 the page is wrapped in the
standard ``PageShell`` (sidebar + topbar + user menu) with the graph controls
as a horizontal toolbar; post-#754 a "cold" open (no ?focus / ?draft / ?search /
?vaade=koik) renders a contextual start panel over the idle graph chrome and
does NOT auto-load the 90k overview.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest
from fasthtml.common import to_xml
from starlette.requests import Request

_REPO_ROOT = Path(__file__).resolve().parent.parent
_EXPLORER_JS = _REPO_ROOT / "app" / "static" / "js" / "explorer.js"


@pytest.fixture(autouse=True)
def _stub_start_panel_data():
    """#754: keep these markup tests DB-free.

    ``explorer_page`` calls :func:`app.explorer.start_panel.load_start_panel_data`
    on a cold open; without this stub every cold-open test would attempt three
    real ``psycopg.connect`` calls. The individual queries already degrade to
    ``[]`` on a DB error, but stubbing the bundle keeps the tests fast and
    deterministic. Tests that want populated sections patch it themselves.
    """
    empty: dict = {"bookmarks": [], "high_risk_reports": [], "recent_drafts": []}
    with patch("app.explorer.pages.load_start_panel_data", return_value=empty):
        yield


def _req(query: str = "") -> Request:
    return Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/explorer",
            "query_string": query.encode(),
            "headers": [],
            "scheme": "http",
            "server": ("testserver", 80),
            "client": ("127.0.0.1", 12345),
            "auth": {
                "id": "u-1",
                "email": "u@example.ee",
                "full_name": "Test User",
                "role": "drafter",
                "org_id": "11111111-1111-1111-1111-111111111111",
            },
        }
    )


def _html(query: str = "") -> str:
    from app.explorer.pages import explorer_page

    return to_xml(explorer_page(_req(query)))


# ---------------------------------------------------------------------------
# Standard chrome present (PageShell sidebar + topbar)
# ---------------------------------------------------------------------------


def test_explorer_page_renders_standard_sidebar():
    html = _html()
    # Standard nav labels from app.ui.layout.sidebar.NAV_ITEMS.
    for label in ("Töölaud", "Analüüsikeskus", "Eelnõud", "Õiguskaart", "Koostaja", "Nõustaja"):
        assert label in html, label
    assert "sidebar" in html
    # active_nav="/explorer" → the Õiguskaart item is marked current.
    assert 'aria-current="page"' in html
    assert "sidebar-link active" in html


def test_explorer_page_renders_standard_topbar():
    html = _html()
    # The "Seadusloome" logo + the user menu (with the test user's name).
    assert "Seadusloome" in html
    assert "user-menu" in html
    assert "Test User" in html
    # Logout form posts to /auth/logout (part of the standard TopBar).
    assert 'action="/auth/logout"' in html


def test_explorer_page_pulls_d3_and_explorer_css_into_head():
    html = _html()
    # D3 v7 from the CDN — only the Õiguskaart page loads it (extra_head=).
    assert "d3/7.9.0/d3.min.js" in html
    assert 'integrity="sha512-' in html
    assert 'crossorigin="anonymous"' in html
    # The explorer stylesheet, also page-scoped.
    assert "/static/css/explorer.css" in html
    # The page's own JS bundle is still loaded.
    assert "/static/js/explorer.js" in html


# ---------------------------------------------------------------------------
# Bespoke chrome removed
# ---------------------------------------------------------------------------


def test_explorer_page_drops_bespoke_topbar_and_nav_buttons():
    html = _html()  # a "cold" open — no ?focus / ?draft, so no toolbar-back link
    # The old bespoke top bar block + its tagline / "D3.js" badge are gone.
    assert 'id="topbar"' not in html
    assert "explorer-tagline" not in html
    # The bespoke left rail mixed graph controls with site-nav `.nav-btn`
    # anchors and a `.ctrl-divider`. The real Sidebar replaces the site nav;
    # the only `.nav-btn` left is the (conditional) toolbar back link, which
    # is absent on a cold open.
    assert "nav-btn" not in html
    assert "ctrl-divider" not in html
    # The old vertical control rail id is gone (replaced by #explorer-toolbar).
    assert 'id="controls"' not in html
    # Old site-nav anchors that lived in #controls (e.g. an <a href="/dashboard">
    # styled as a button) are not in the explorer's own markup — those are now
    # the Sidebar's sidebar-link anchors instead.
    assert 'class="nav-btn"' not in html


# ---------------------------------------------------------------------------
# The graph toolbar
# ---------------------------------------------------------------------------


def test_explorer_page_has_graph_toolbar_with_search_and_view_settings():
    html = _html()
    assert 'id="explorer-toolbar"' in html
    assert 'aria-label="Õiguskaardi tööriistad"' in html
    # The search box moved out of the deleted #topbar into the toolbar.
    assert 'id="search-box"' in html
    assert 'id="search-input"' in html
    assert 'id="search-btn"' in html
    # Legal-work view actions + the "Vaate seaded" disclosure.
    assert "Ülevaade" in html
    assert "Lähtesta vaade" in html
    assert "Vaate seaded" in html
    assert "Lähtesta paigutus" in html
    assert "Näita/peida seosenimed" in html
    assert "Rühmita liigi järgi" in html
    # Old force-simulation vocabulary stays gone from the control surface.
    assert "Taaskäivita simulatsioon" not in html
    assert "Lülita silte" not in html
    assert "Rühm. kategooria järgi" not in html


def test_explorer_page_timeline_is_clearly_labelled():
    html = _html()
    assert "Ajaline vaade" in html
    assert "timeline-slider" in html
    # "Keelatud" was a confusing label for "no time filter active".
    assert "Keelatud" not in html


# ---------------------------------------------------------------------------
# Branding
# ---------------------------------------------------------------------------


def test_explorer_page_is_branded_oiguskaart():
    html = _html()
    assert "Õiguskaart" in html
    # The old "Uurija" name is gone everywhere on the page.
    assert "Uurija" not in html


def test_explorer_page_title_is_oiguskaart():
    html = _html()
    # PageShell renders "<title>Õiguskaart — Seadusloome</title>".
    assert "Õiguskaart — Seadusloome" in html


# ---------------------------------------------------------------------------
# Deep links: ?focus= / ?search= / cold-open draft tip
# ---------------------------------------------------------------------------


def test_explorer_page_focus_param_is_handed_to_js():
    uri = "https://data.riik.ee/ontology/estleg#KarS_par_133"
    # ``focus`` arrives already URL-decoded in req.query_params.
    html = _html(f"focus={uri}")
    assert "window.__explorerFocus" in html
    assert uri in html
    # The "?draft=ID …" tip is suppressed when the user came to look at a
    # specific entity.
    assert "?draft=ID" not in html
    # The detail-panel back link element is in the DOM (explorer.js unhides it),
    # and the toolbar-level back link is rendered too.
    assert 'id="panel-back"' in html
    assert 'id="toolbar-back"' in html
    # ?focus= wins over ?search= when both are present.
    html2 = _html(f"focus={uri}&search=karistus")
    assert "window.__explorerFocus" in html2
    assert "window.__explorerSearch" not in html2


def test_explorer_page_search_param_is_handed_to_js():
    html = _html("search=andmekaitse")
    assert "window.__explorerSearch" in html
    assert "andmekaitse" in html
    # A bare ?search= is still a "cold" open as far as the back link goes —
    # but the draft tip is still shown (no draft/focus/overlay).
    assert "?draft=ID" in html


def test_explorer_page_search_blob_is_escaped():
    # A `</script>` injection attempt in the term must be neutralised in the
    # embedded blob exactly like the ?focus= blob is.
    html = _html("search=</script><b>x")
    assert "</script><b>x" not in html
    assert "window.__explorerSearch" in html


# ---------------------------------------------------------------------------
# #754 — contextual start panel on a "cold" open
# ---------------------------------------------------------------------------


def test_explorer_cold_open_renders_start_panel():
    """A bare /explorer (no ?focus / ?draft / ?search / ?vaade=koik) shows the
    contextual start panel — search, bookmarks, high-risk findings, recent
    drafts, Normi mõjuahel, "Sirvi liikide kaupa" — not the cold 90k graph."""
    html = _html()
    assert 'id="explorer-start-panel"' in html
    assert 'aria-label="Õiguskaardi avapaneel"' in html
    # The panel's sections (Estonian copy with proper diacritics).
    assert "Sinu järjehoidjad" in html
    assert "Hiljutised kõrge riskiga leiud" in html
    assert "Sinu hiljutised eelnõud" in html
    assert "Alusta Normi mõjuahelat" in html
    assert "Sirvi liikide kaupa" in html
    # The panel's own search box (a real <form method=get action=/explorer>).
    assert 'id="start-panel-search-form"' in html
    assert 'id="start-panel-search-input"' in html
    assert 'action="/explorer"' in html
    # The Normi mõjuahel shortcut points at the Analüüsikeskus workflow.
    assert "/analyysikeskus/normi-mojuahel" in html
    # The old cold-open ?draft= tip / its banner are gone — the panel replaces it.
    assert "?draft=ID" not in html
    assert 'id="explorer-tip-banner"' not in html


def test_explorer_cold_open_does_not_bootstrap_the_graph():
    """The whole point of #754: explorer.js must NOT fetch the 90k graph data
    on a cold open. The server signals that via window.__explorerStartPanel."""
    html = _html()
    assert "window.__explorerStartPanel" in html
    # No deep-link bridge blobs on a cold open.
    assert "window.__explorerFocus" not in html
    assert "window.__explorerSearch" not in html
    # The graph DOM + JS are still on the page (explorer.js needs them once the
    # user picks "Sirvi liikide kaupa"), and "Näita kogu kaarti" is offered in
    # the Vaate seaded dropdown too.
    assert "/static/js/explorer.js" in html
    assert 'id="canvas"' in html
    assert "Näita kogu kaarti" in html


def test_explorer_focus_param_skips_the_start_panel():
    uri = "https://data.riik.ee/ontology/estleg#KarS_par_133"
    html = _html(f"focus={uri}")
    # Deep-linked entry → the graph view, no start panel, no start-panel flag.
    assert 'id="explorer-start-panel"' not in html
    assert "window.__explorerStartPanel" not in html
    assert "window.__explorerFocus" in html


def test_explorer_draft_param_skips_the_start_panel():
    # ?draft= (even a malformed/cross-org one whose overlay is dropped) must
    # bypass the start panel and render the classic graph view (#755's subgraph
    # mode only kicks in for a resolvable, org-owned draft — for an unresolvable
    # one the panel just gets skipped and the classic chrome is shown).
    html = _html("draft=not-a-uuid")
    assert 'id="explorer-start-panel"' not in html
    assert "window.__explorerStartPanel" not in html
    # No subgraph blob either — the draft id didn't resolve.
    assert "window.__explorerDraftSubgraph" not in html
    # The graph toolbar is the visible chrome here (not hidden behind a panel).
    assert 'id="explorer-toolbar"' in html


# ---------------------------------------------------------------------------
# #755 — ?draft=<id> renders the draft's impact subgraph (epic #762, ws B)
# ---------------------------------------------------------------------------


# A draft connection stub for ``app.explorer.pages._resolve_draft_for_subgraph``
# (``SELECT org_id, title FROM drafts`` then ``SELECT 1 FROM impact_reports``).
class _DraftResolveCur:
    def __init__(self, draft_row, report_row):
        self._rows = [draft_row, report_row]
        self._i = -1

    def __call__(self, sql, params=()):
        self._i += 1
        return self

    def fetchone(self):
        return self._rows[self._i] if 0 <= self._i < len(self._rows) else None


class _DraftResolveConn:
    def __init__(self, draft_row, report_row):
        self.execute = _DraftResolveCur(draft_row, report_row)

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False


_SUBGRAPH_DRAFT_ID = "44444444-4444-4444-4444-444444444444"
_SUBGRAPH_ORG_ID = "11111111-1111-1111-1111-111111111111"  # matches _req()'s auth


def _draft_subgraph_html(draft_id: str, *, draft_row, report_row) -> str:
    """Render ``explorer_page`` for ``?draft=<id>`` with a mocked draft conn."""
    from app.explorer.pages import explorer_page

    with patch(
        "app.explorer.pages._connect",
        return_value=_DraftResolveConn(draft_row, report_row),
    ):
        return to_xml(explorer_page(_req(f"draft={draft_id}")))


def test_explorer_draft_param_renders_impact_subgraph_blob():
    """?draft=<id> for an org-owned draft with an impact report switches
    explorer.js into subgraph mode — it gets a window.__explorerDraftSubgraph
    blob pointing at /explorer/draft-subgraph/<id>, NOT the legacy
    draft-overlay-data blob, NOT the start panel, NOT the cold-graph bootstrap."""
    html = _draft_subgraph_html(
        _SUBGRAPH_DRAFT_ID,
        draft_row=(_SUBGRAPH_ORG_ID, "Liiklusseaduse muutmise eelnõu"),
        report_row=(1,),
    )
    # The subgraph bridge blob — carries the data endpoint + back links.
    assert "window.__explorerDraftSubgraph" in html
    assert f"/explorer/draft-subgraph/{_SUBGRAPH_DRAFT_ID}" in html
    assert f"/drafts/{_SUBGRAPH_DRAFT_ID}" in html  # draftUrl / reportUrl
    assert '"hasReport": true' in html or '"hasReport":true' in html
    # NOT the legacy node-highlight overlay (that's the ?focus=&draft= path).
    assert 'id="draft-overlay-data"' not in html
    # NOT the start panel, NOT the cold-graph flag.
    assert 'id="explorer-start-panel"' not in html
    assert "window.__explorerStartPanel" not in html
    # The toolbar back link is rendered (explorer.js wires it to the draft).
    assert 'id="toolbar-back"' in html
    assert 'id="panel-back"' in html
    # The graph DOM + JS are still present (explorer.js fetches the subgraph).
    assert 'id="canvas"' in html
    assert "/static/js/explorer.js" in html


def test_explorer_draft_param_no_report_still_enters_subgraph_mode():
    """A valid, org-owned draft with no impact report yet still switches into
    subgraph mode (hasReport=false) — explorer.js shows a "run the analysis"
    fallback rather than the cold 90k graph. No 500, no start panel."""
    html = _draft_subgraph_html(
        _SUBGRAPH_DRAFT_ID,
        draft_row=(_SUBGRAPH_ORG_ID, "Analüüsimata eelnõu"),
        report_row=None,
    )
    assert "window.__explorerDraftSubgraph" in html
    assert '"hasReport": false' in html or '"hasReport":false' in html
    assert f"/explorer/draft-subgraph/{_SUBGRAPH_DRAFT_ID}" in html
    assert 'id="explorer-start-panel"' not in html
    assert "window.__explorerStartPanel" not in html


def test_explorer_draft_param_cross_org_falls_back_no_subgraph():
    """A draft owned by a different org → _resolve_draft_for_subgraph returns
    None, so no subgraph blob is emitted. The page falls back to the classic
    graph view (never a 500, the draft's existence isn't revealed)."""
    html = _draft_subgraph_html(
        _SUBGRAPH_DRAFT_ID,
        draft_row=("99999999-9999-9999-9999-999999999999", "Teise asutuse eelnõu"),
        report_row=None,
    )
    assert "window.__explorerDraftSubgraph" not in html
    assert 'id="draft-overlay-data"' not in html
    # Classic graph chrome (no start panel, no subgraph blob).
    assert 'id="explorer-start-panel"' not in html
    assert 'id="explorer-toolbar"' in html
    assert 'id="canvas"' in html


def test_explorer_draft_param_db_error_falls_back_no_subgraph():
    """A DB error while resolving the draft → no subgraph blob, classic graph
    view, never a 500."""
    from app.explorer.pages import explorer_page

    with patch("app.explorer.pages._connect", side_effect=RuntimeError("no db")):
        html = to_xml(explorer_page(_req(f"draft={_SUBGRAPH_DRAFT_ID}")))
    assert "window.__explorerDraftSubgraph" not in html
    assert 'id="explorer-toolbar"' in html


def test_explorer_focus_and_draft_uses_legacy_overlay_not_subgraph():
    """When ?focus= is *also* present (a report deep link), the legacy
    node-highlight overlay path wins — the draft-overlay-data blob is embedded,
    NOT the subgraph blob (the user wants the entity neighbourhood, not the
    whole impact subgraph)."""
    uri = "https://data.riik.ee/ontology/estleg#KarS_par_133"
    # _fetch_draft_overlay does SELECT org_id then SELECT report_data.
    report_data = {"affected_entities": [{"uri": "urn:x:1"}]}
    with patch(
        "app.explorer.pages._connect",
        return_value=_DraftResolveConn((_SUBGRAPH_ORG_ID,), (report_data,)),
    ):
        from app.explorer.pages import explorer_page

        html = to_xml(explorer_page(_req(f"focus={uri}&draft={_SUBGRAPH_DRAFT_ID}")))
    assert 'id="draft-overlay-data"' in html
    assert "urn:x:1" in html
    assert "window.__explorerDraftSubgraph" not in html
    assert "window.__explorerFocus" in html


def test_explorer_search_param_skips_the_start_panel():
    html = _html("search=andmekaitse")
    assert 'id="explorer-start-panel"' not in html
    assert "window.__explorerStartPanel" not in html
    assert "window.__explorerSearch" in html


def test_explorer_vaade_koik_forces_the_graph_view():
    """The "Näita kogu kaarti" / "Sirvi liikide kaupa" buttons navigate to
    /explorer?vaade=koik — that URL renders the classic graph view, not the
    start panel."""
    html = _html("vaade=koik")
    assert 'id="explorer-start-panel"' not in html
    assert "window.__explorerStartPanel" not in html
    assert 'id="explorer-toolbar"' in html
    assert 'id="canvas"' in html


def test_explorer_start_panel_lists_bookmarks_reports_and_drafts():
    """When the org-scoped queries return rows, the panel renders them as
    links — bookmarks/drafts focus the entity / draft overlay; high-risk
    rows link to the report (and offer "Ava mõjukaart")."""
    bm_url = "/explorer?focus=https%3A%2F%2Fdata.riik.ee%2Fontology%2Festleg%23KarS"
    populated: dict = {
        "bookmarks": [
            {
                "id": "b-1",
                "entity_uri": "https://data.riik.ee/ontology/estleg#KarS",
                "label": "Karistusseadustik",
                "explorer_url": bm_url,
            }
        ],
        "high_risk_reports": [
            {
                "draft_id": "d-9",
                "title": "Andmekaitse seaduse muutmine",
                "impact_score": 72,
                "band": "high",
                "band_label": "Kõrge risk",
                "conflict_count": 3,
                "affected_count": 12,
                "gap_count": 1,
                "generated_at": None,
                "report_url": "/drafts/d-9/report",
                "explorer_url": "/explorer?draft=d-9",
            }
        ],
        "recent_drafts": [
            {
                "draft_id": "d-2",
                "title": "Liiklusseaduse eelnõu",
                "status": "analyzed",
                "updated_at": None,
                "detail_url": "/drafts/d-2",
                "explorer_url": "/explorer?draft=d-2",
            }
        ],
    }
    from app.explorer.pages import explorer_page

    with patch("app.explorer.pages.load_start_panel_data", return_value=populated):
        html = to_xml(explorer_page(_req()))
    # Bookmark → focuses that entity.
    assert "Karistusseadustik" in html
    assert bm_url in html
    # High-risk report → links to the report page + "Ava mõjukaart" (?draft=).
    assert "Andmekaitse seaduse muutmine" in html
    assert "/drafts/d-9/report" in html
    assert "/explorer?draft=d-9" in html
    assert "Kõrge risk" in html
    # Recent draft → detail page + "Ava mõjukaart".
    assert "Liiklusseaduse eelnõu" in html
    assert "/drafts/d-2" in html
    assert "/explorer?draft=d-2" in html
    assert "Ava mõjukaart" in html


# ---------------------------------------------------------------------------
# #756 — legal-view preset chips in the toolbar (?vaade=<slug>)
# ---------------------------------------------------------------------------

# Keep this in sync with ``_LEGAL_VIEW_PRESETS`` in app/explorer/pages.py.
_PRESET_SLUGS = ("kehtiv-oigus", "eelnou-mojud", "el-seosed", "kohtupraktika", "ajalugu")
_PRESET_LABELS = ("Kehtiv õigus", "Eelnõu mõjud", "EL seosed", "Kohtupraktika", "Ajalugu")


def test_explorer_toolbar_renders_the_five_legal_view_preset_chips():
    """The toolbar offers the five legal-view presets as a labelled chip group;
    the raw graph knobs stay under "Vaate seaded". Cold open is fine — the
    chips live in the (idle) graph chrome behind the start panel."""
    html = _html()
    # The chip group + its accessible labelling.
    assert 'id="explorer-presets"' in html
    assert 'aria-label="Õiguskaardi vaated"' in html
    assert "Õigusvaated" in html
    # All five preset labels + their slugs, each as a clickable chip.
    for label in _PRESET_LABELS:
        assert label in html, label
    for slug in _PRESET_SLUGS:
        assert f'data-vaade="{slug}"' in html, slug
        assert f"explorerApplyPreset('{slug}')" in html, slug
    # The raw simulation knobs are still under "Vaate seaded", not surfaced
    # at the top level alongside the presets.
    assert "Vaate seaded" in html
    assert "Lähtesta paigutus" in html
    # explorer.js gets the preset table (and no active slug on a cold open).
    assert "window.__explorerPresets" in html
    assert "window.__explorerVaade" not in html
    # No preset chip is active on a cold open.
    assert "preset-chip active" not in html
    assert 'data-active-vaade=""' in html


def test_explorer_known_vaade_slug_marks_that_preset_active():
    """``/explorer?vaade=el-seosed`` renders the graph view with the EL-seosed
    chip active and hands the slug to explorer.js."""
    html = _html("vaade=el-seosed")
    # The graph view (no start panel — a preset needs the graph to filter).
    assert 'id="explorer-start-panel"' not in html
    assert "window.__explorerStartPanel" not in html
    assert 'id="canvas"' in html
    # The matching chip is active; explorer.js gets the resolved slug.
    assert 'data-active-vaade="el-seosed"' in html
    assert 'data-vaade="el-seosed"' in html
    assert "preset-chip active" in html
    assert 'aria-pressed="true"' in html
    assert "window.__explorerVaade" in html
    assert "el-seosed" in html
    assert "window.__explorerPresets" in html


def test_explorer_each_known_vaade_slug_is_addressable():
    """Every preset slug, given as ?vaade=, marks its own chip active."""
    for slug in _PRESET_SLUGS:
        html = _html(f"vaade={slug}")
        assert f'data-active-vaade="{slug}"' in html, slug
        assert "preset-chip active" in html, slug
        assert "window.__explorerVaade" in html, slug
        # A preset bypasses the start panel.
        assert "window.__explorerStartPanel" not in html, slug


def test_explorer_vaade_preset_is_handed_to_js_with_full_table():
    """The preset table embedded for explorer.js carries every slug's config
    keys (categories / relKeywords / timeline)."""
    html = _html("vaade=kohtupraktika")
    assert "window.__explorerPresets" in html
    # The table mentions each slug + the per-preset keys.
    for slug in _PRESET_SLUGS:
        assert slug in html, slug
    assert "categories" in html
    assert "relKeywords" in html
    assert "timeline" in html
    # A representative ontology category + relation keyword from the configs.
    assert "CourtDecision" in html
    assert "interpret" in html


def test_explorer_unknown_vaade_value_is_ignored():
    """An unrecognised ``?vaade=`` value renders the page as if it were absent:
    a cold open (start panel + no preset active), no JS preset blob."""
    html = _html("vaade=ei-ole-olemas")
    # Falls through to the #754 cold-open behaviour.
    assert 'id="explorer-start-panel"' in html
    assert "window.__explorerStartPanel" in html
    # No preset is active and no slug is handed to JS.
    assert "preset-chip active" not in html
    assert 'data-active-vaade=""' in html
    assert "window.__explorerVaade" not in html
    # The chip group + the preset table are still present (the chips just sit
    # idle behind the start panel).
    assert 'id="explorer-presets"' in html
    assert "window.__explorerPresets" in html


def test_explorer_vaade_koik_is_not_a_preset():
    """``?vaade=koik`` keeps its #754 meaning (force the full graph view) — it
    does not mark a preset chip active."""
    html = _html("vaade=koik")
    assert 'id="explorer-start-panel"' not in html
    assert "preset-chip active" not in html
    assert 'data-active-vaade=""' in html
    assert "window.__explorerVaade" not in html


def test_explorer_focus_param_wins_over_vaade_preset():
    """When both ?focus= and ?vaade=<preset> are present, the focus deep link
    is the more specific intent — ?vaade= is ignored entirely (no active chip,
    no preset blob)."""
    uri = "https://data.riik.ee/ontology/estleg#KarS_par_133"
    html = _html(f"focus={uri}&vaade=el-seosed")
    assert "window.__explorerFocus" in html
    assert "window.__explorerVaade" not in html
    assert "preset-chip active" not in html
    assert 'data-active-vaade=""' in html
    # The chip group itself is still present (the chips just sit idle).
    assert 'id="explorer-presets"' in html


def test_explorer_search_param_wins_over_vaade_preset():
    """Same as the ?focus= case: ?search= is the more specific intent, so a
    co-present ?vaade= is ignored."""
    html = _html("search=andmekaitse&vaade=kohtupraktika")
    assert "window.__explorerSearch" in html
    assert "window.__explorerVaade" not in html
    assert "preset-chip active" not in html


# ---------------------------------------------------------------------------
# #757 — evidence-card detail panel (epic #762, design doc workstream D)
# ---------------------------------------------------------------------------


def test_explorer_detail_panel_renders_evidence_card_structure():
    """The node detail panel exposes the evidence-card slots — Allikas,
    Kuupäev / versioon, Seose liik, "Miks see oluline on" — plus the heading
    of the Tegevused (actions) group. explorer.js fills + (un)hides each one."""
    # ?focus= still renders the detail panel DOM (explorer.js unhides it).
    uri = "https://data.riik.ee/ontology/estleg#KarS_par_133"
    html = _html(f"focus={uri}")
    assert 'id="detail-panel"' in html
    # The four evidence-card section containers + their fill targets.
    assert 'id="evidence-source-section"' in html
    assert 'id="panel-source-row"' in html
    assert 'id="evidence-date-section"' in html
    assert 'id="panel-date-info"' in html
    assert 'id="evidence-relation-section"' in html
    assert 'id="panel-relation"' in html
    assert 'id="evidence-why-section"' in html
    assert 'id="panel-why"' in html
    # The Estonian section headings.
    assert "Allikas" in html
    assert "Kuupäev / versioon" in html
    assert "Seose liik" in html
    assert "Miks see oluline on" in html
    assert "Tegevused" in html
    # The evidence sections start hidden (explorer.js shows the ones with data).
    assert "evidence-section" in html
    assert "display:none" in html


def test_explorer_detail_panel_has_four_actions_wired():
    """The evidence card's four actions point at the right places:
    1) Küsi nõustajalt → POST /chat/seed (the single-use pending_chat_seed flow);
    2) Ava analüüsikeskuses → /analyysikeskus/normi-mojuahel;
    3) Lisa märkus → the entity-level annotation button (#panel-annotation-btn);
    4) Lisa järjehoidja → the #743 XHR bookmark button (#panel-bookmark-btn)."""
    html = _html("focus=https://data.riik.ee/ontology/estleg#KarS")
    # (1) Küsi nõustajalt selle kohta — a <form method=post action=/chat/seed>
    # with the seed_text + draft_id hidden inputs explorer.js fills in.
    assert "Küsi nõustajalt selle kohta" in html
    assert 'id="panel-chat-seed-form"' in html
    assert 'action="/chat/seed"' in html
    assert 'name="seed_text"' in html
    assert 'name="draft_id"' in html
    # (2) Ava analüüsikeskuses — links to the Normi mõjuahel workflow (the
    # ?sisend=<uri> is appended client-side by explorer.js).
    assert "Ava analüüsikeskuses" in html
    assert 'id="panel-analyysikeskus-link"' in html
    assert "/analyysikeskus/normi-mojuahel" in html
    # (3) Lisa märkus — the entity-level annotation slot, filled by
    # _PANEL_ANNOTATION_SCRIPT (which POSTs annotations the standard way).
    assert 'id="panel-annotation-btn"' in html
    # (4) Lisa järjehoidja — the existing XHR bookmark button (kept working).
    assert "Lisa järjehoidja" in html
    assert 'id="panel-bookmark-btn"' in html
    assert 'onclick="explorerBookmark()"' in html
    # The bookmark XHR endpoint is still the one the #743 path uses (the JS
    # POSTs /api/bookmarks with X-Requested-With) — assert the JS bundle is
    # still wired in (the actual fetch lives in explorer.js).
    assert "/static/js/explorer.js" in html


def test_explorer_detail_panel_keeps_back_link_and_metadata_sections():
    """The evidence-card restyle is additive — the panel still carries the
    #719 back link, the metadata dump, the version history, and the relations
    list (so explorer.js' existing populate logic keeps working)."""
    html = _html("focus=https://data.riik.ee/ontology/estleg#KarS")
    assert 'id="panel-back"' in html
    assert 'id="panel-meta"' in html
    assert 'id="version-history-section"' in html
    assert 'id="panel-neighbors"' in html
    assert 'id="panel-title"' in html
    assert 'id="panel-link"' in html


# ---------------------------------------------------------------------------
# #754 — app/explorer/start_panel.py: the org-scoped data queries
# ---------------------------------------------------------------------------


class _Cur:
    """Tiny cursor stub: records the SQL+params, replays canned rows."""

    def __init__(self, rows: list, log: list):
        self._rows = rows
        self._log = log

    def __call__(self, sql: str, params: tuple = ()):  # conn.execute(...)
        self._log.append((sql, params))
        return self

    def fetchall(self) -> list:
        return self._rows


class _Conn:
    """Context-manager connection stub returning a fixed result set."""

    def __init__(self, rows: list, log: list):
        self.execute = _Cur(rows, log)

    def __enter__(self):  # noqa: ANN204
        return self

    def __exit__(self, *_):  # noqa: ANN002
        return False


class TestStartPanelBookmarks:
    def test_scoped_by_user_id_in_sql(self):
        from app.explorer import start_panel

        log: list = []
        rows = [("b-1", "urn:x:1", "Esimene"), ("b-2", "urn:x:2", None)]
        with patch.object(start_panel, "_connect", return_value=_Conn(rows, log)):
            out = start_panel.get_user_bookmarks("user-42")
        # The WHERE clause filters on the caller's user_id — never a bare SELECT.
        assert len(log) == 1
        sql, params = log[0]
        assert "where user_id = %s" in sql.lower()
        assert params[0] == "user-42"
        # Rows are mapped, with a fallback label and a ?focus= explorer link.
        assert out[0]["label"] == "Esimene"
        assert out[1]["label"] == "urn:x:2"  # NULL label → URI
        assert out[0]["explorer_url"] == "/explorer?focus=urn%3Ax%3A1"

    def test_missing_user_returns_empty_without_db(self):
        from app.explorer import start_panel

        with patch.object(start_panel, "_connect") as mock_connect:
            assert start_panel.get_user_bookmarks(None) == []
            assert start_panel.get_user_bookmarks("") == []
            mock_connect.assert_not_called()

    def test_db_error_degrades_to_empty(self):
        from app.explorer import start_panel

        with patch.object(start_panel, "_connect", side_effect=RuntimeError("no db")):
            assert start_panel.get_user_bookmarks("user-42") == []


class TestStartPanelHighRiskReports:
    def test_scoped_by_org_id_in_sql(self):
        from app.explorer import start_panel

        log: list = []
        # (draft_id, title, impact_score, conflict, affected, gap, generated_at)
        rows = [("d-7", "Suur eelnõu", 84, 2, 9, 1, None)]
        with patch.object(start_panel, "_connect", return_value=_Conn(rows, log)):
            out = start_panel.get_high_risk_reports("org-9")
        assert len(log) == 1
        sql, params = log[0]
        # The org filter is on drafts.org_id inside the JOIN.
        assert "d.org_id = %s" in sql
        assert params[0] == "org-9"
        assert out[0]["draft_id"] == "d-7"
        assert out[0]["band"] == "critical"
        assert out[0]["report_url"] == "/drafts/d-7/report"
        assert out[0]["explorer_url"] == "/explorer?draft=d-7"

    def test_low_band_rows_are_filtered_out(self):
        from app.explorer import start_panel

        # A row that slipped past the SQL ``> 50`` gate (defensive band check).
        rows = [("d-1", "Väike", 10, 0, 1, 0, None)]
        with patch.object(start_panel, "_connect", return_value=_Conn(rows, [])):
            assert start_panel.get_high_risk_reports("org-9") == []

    def test_missing_org_returns_empty_without_db(self):
        from app.explorer import start_panel

        with patch.object(start_panel, "_connect") as mock_connect:
            assert start_panel.get_high_risk_reports(None) == []
            mock_connect.assert_not_called()

    def test_db_error_degrades_to_empty(self):
        from app.explorer import start_panel

        with patch.object(start_panel, "_connect", side_effect=RuntimeError("no db")):
            assert start_panel.get_high_risk_reports("org-9") == []


class TestStartPanelRecentDrafts:
    def test_scoped_by_org_id_in_sql(self):
        from app.explorer import start_panel

        log: list = []
        rows = [("d-3", "Mõni eelnõu", "analyzed", None)]
        with patch.object(start_panel, "_connect", return_value=_Conn(rows, log)):
            out = start_panel.get_recent_drafts("org-5")
        assert len(log) == 1
        sql, params = log[0]
        assert "where org_id = %s" in sql.lower()
        assert params[0] == "org-5"
        assert out[0]["draft_id"] == "d-3"
        assert out[0]["detail_url"] == "/drafts/d-3"
        assert out[0]["explorer_url"] == "/explorer?draft=d-3"

    def test_missing_org_returns_empty_without_db(self):
        from app.explorer import start_panel

        with patch.object(start_panel, "_connect") as mock_connect:
            assert start_panel.get_recent_drafts(None) == []
            mock_connect.assert_not_called()

    def test_db_error_degrades_to_empty(self):
        from app.explorer import start_panel

        with patch.object(start_panel, "_connect", side_effect=RuntimeError("no db")):
            assert start_panel.get_recent_drafts("org-5") == []


class TestStartPanelBundle:
    def test_load_start_panel_data_fans_out(self):
        from app.explorer import start_panel

        with (
            patch.object(start_panel, "get_user_bookmarks", return_value=["bm"]) as gb,
            patch.object(start_panel, "get_high_risk_reports", return_value=["hr"]) as gh,
            patch.object(start_panel, "get_recent_drafts", return_value=["dr"]) as gd,
        ):
            data = start_panel.load_start_panel_data("u-1", "o-1")
        gb.assert_called_once_with("u-1")
        gh.assert_called_once_with("o-1")
        gd.assert_called_once_with("o-1")
        assert data == {
            "bookmarks": ["bm"],
            "high_risk_reports": ["hr"],
            "recent_drafts": ["dr"],
        }


# ---------------------------------------------------------------------------
# #758 — spatial-map polish: mini-map DOM container + stable-layout seed
# ---------------------------------------------------------------------------


def test_explorer_page_renders_minimap_container():
    """#758: the mini-map's DOM container (a <div id="minimap"> wrapping
    <svg id="minimap-svg">) is present near the canvas so explorer.js can
    render the overview panel into it. It's there on a graph-view open..."""
    html = _html("vaade=koik")
    assert 'id="minimap"' in html
    assert 'id="minimap-svg"' in html
    assert 'aria-label="Õiguskaardi miniülevaade"' in html
    # ...and also on a cold open (the graph DOM is rendered behind the start
    # panel so explorer.js' getElementById() calls don't hit nulls).
    cold = _html()
    assert 'id="minimap"' in cold
    assert 'id="minimap-svg"' in cold
    # And on a focus deep-link.
    focused = _html("focus=https://data.riik.ee/ontology/estleg#KarS_par_133")
    assert 'id="minimap"' in focused


# --- The layout-seed function: deterministic (same node id → same position) ---

# Pull the pure constants + helper functions ("#758: spatial-map polish —
# constants for the mini-map" up to the State section) straight out of
# explorer.js, so the determinism test exercises the *shipped* implementation.
_SEED_BLOCK_RE = re.compile(
    r"// #758: spatial-map polish — constants for the mini-map.*?"
    r"(?=// -+\n// State\n// -+)",
    re.DOTALL,
)


def _extract_seed_block() -> str:
    src = _EXPLORER_JS.read_text(encoding="utf-8")
    m = _SEED_BLOCK_RE.search(src)
    assert m is not None, "could not locate the #758 layout-seed block in explorer.js"
    return m.group(0)


def test_explorer_js_exposes_the_layout_seed_block():
    """Guard the regex above: the block must contain the two pure helpers."""
    block = _extract_seed_block()
    assert "function hashStringToInt(" in block
    assert "function seedPosition(" in block
    # The fixed PRNG seed for d3-force lives in the same block.
    assert "LAYOUT_PRNG_SEED" in block


@pytest.mark.skipif(shutil.which("node") is None, reason="node not available")
def test_layout_seed_is_deterministic_per_node_id(tmp_path: Path):
    """#758 (stable layout): seedPosition(id) is a pure function — the same node
    id always maps to the same initial (x, y), so re-opening the same query
    re-creates the same picture. Run the shipped function in Node and check it
    twice over a spread of ids."""
    block = _extract_seed_block()
    ids = [
        "https://data.riik.ee/ontology/estleg#KarS_par_133",
        "cat:EnactedLaw",
        "https://eur-lex.europa.eu/eli/dir/2016/680",
        "",  # empty id must still produce a finite, stable point
        "Mõni eestikeelne id õ ä ü",  # non-ASCII id
    ]
    driver = block + (
        "\nconst ids = " + json.dumps(ids) + ";\n"
        "const once = ids.map((id) => seedPosition(id, LAYOUT_SEED_RADIUS));\n"
        "const twice = ids.map((id) => seedPosition(id, LAYOUT_SEED_RADIUS));\n"
        # Also check that distinct ids generally land on distinct points.
        "const hashes = ids.filter((x) => x).map(hashStringToInt);\n"
        "process.stdout.write(JSON.stringify({once, twice, hashes}));\n"
    )
    script = tmp_path / "seed_check.js"
    script.write_text(driver, encoding="utf-8")
    out = subprocess.run(
        ["node", str(script)],
        capture_output=True,
        text=True,
        timeout=20,
        check=True,
    )
    data = json.loads(out.stdout)
    once, twice, hashes = data["once"], data["twice"], data["hashes"]
    # Determinism: identical results across two independent calls.
    assert once == twice
    # Every coordinate is a finite number.
    for pt in once:
        assert isinstance(pt["x"], (int, float)) and isinstance(pt["y"], (int, float))
        assert pt["x"] == pt["x"] and pt["y"] == pt["y"]  # not NaN
        assert abs(pt["x"]) < 1e6 and abs(pt["y"]) < 1e6
    # The hash spreads ids out — no all-collide degenerate case.
    assert len(set(hashes)) == len(hashes)
    # A known anchor point won't drift between runs of the test suite either:
    # recompute it here in Python (mirror of hashStringToInt + seedPosition) and
    # compare. This pins the algorithm, not just self-consistency.
    expected_first = _py_seed_position(ids[0])
    assert abs(once[0]["x"] - expected_first[0]) < 1e-6
    assert abs(once[0]["y"] - expected_first[1]) < 1e-6


def _py_hash_string_to_int(s: str) -> int:
    """Pure-Python mirror of explorer.js' hashStringToInt (FNV-1a, 32-bit).

    The JS does ``h ^= s.charCodeAt(i)`` — a UTF-16 code unit. This mirror is
    only used to compare against ids that live in the BMP (``ord(ch) ==
    charCodeAt``); astral-plane ids are exercised by the Node-side test only.
    """
    h = 0x811C9DC5
    for ch in s:
        h ^= ord(ch)
        h = (h + ((h << 1) + (h << 4) + (h << 7) + (h << 8) + (h << 24))) & 0xFFFFFFFF
    return h & 0xFFFFFFFF


def _py_seed_position(node_id: str, radius: float = 480.0) -> tuple[float, float]:
    """Pure-Python mirror of explorer.js' seedPosition."""
    import math

    h = _py_hash_string_to_int(node_id)
    a = (h & 0xFFFF) / 0x10000
    b = ((h >> 16) & 0xFFFF) / 0x10000
    angle = a * math.pi * 2
    dist = math.sqrt(b) * radius
    return (math.cos(angle) * dist, math.sin(angle) * dist)


def test_py_seed_mirror_matches_for_ascii_ids():
    """Sanity-check the Python mirror against a couple of hand-computable cases
    so a future tweak to the JS algorithm trips this test, not just the Node one."""
    # FNV-1a of the empty string is the offset basis → both fractions are
    # derived from 0x811c9dc5.
    h = 0x811C9DC5
    a = (h & 0xFFFF) / 0x10000
    b = ((h >> 16) & 0xFFFF) / 0x10000
    import math

    assert _py_hash_string_to_int("") == h
    x, y = _py_seed_position("")
    assert abs(x - math.cos(a * math.pi * 2) * math.sqrt(b) * 480.0) < 1e-9
    assert abs(y - math.sin(a * math.pi * 2) * math.sqrt(b) * 480.0) < 1e-9


# ---------------------------------------------------------------------------
# #760 — responsive + accessibility QA pass (epic #762, workstream G)
# ---------------------------------------------------------------------------

_EXPLORER_CSS = _REPO_ROOT / "app" / "static" / "css" / "explorer.css"


def _css() -> str:
    return _EXPLORER_CSS.read_text(encoding="utf-8")


def _js() -> str:
    return _EXPLORER_JS.read_text(encoding="utf-8")


def test_explorer_landmark_roles_and_labels_present():
    """#760: the new surfaces carry sensible landmark roles + labels — the
    toolbar (already "Õiguskaardi tööriistad"), the search box (role=search),
    the preset chip group (role=group + aria-label, aria-pressed on chips),
    the breadcrumb nav, the detail panel (role=region, starts aria-hidden),
    and the mini-map (role=img + label, its inner svg aria-hidden)."""
    html = _html()  # cold open — start panel + the (idle) graph chrome
    # Toolbar (pre-existing) + the new search-box landmark.
    assert 'aria-label="Õiguskaardi tööriistad"' in html
    assert 'id="search-box"' in html
    assert 'role="search"' in html  # both the toolbar #search-box and the panel form
    # The legal-view preset chips: a labelled group with aria-pressed on chips.
    assert 'id="explorer-presets"' in html
    assert 'role="group"' in html
    assert 'aria-label="Õiguskaardi vaated"' in html
    assert 'aria-pressed="false"' in html  # no chip active on a cold open
    # The breadcrumb is a labelled <nav> landmark even while empty.
    assert 'id="breadcrumb"' in html
    assert 'aria-label="Asukoht õiguskaardil"' in html
    # The detail panel is a labelled region, hidden from AT until opened.
    assert 'id="detail-panel"' in html
    assert 'aria-label="Üksuse üksikasjad"' in html
    assert 'aria-hidden="true"' in html
    # The mini-map: role=img + label on the wrapper; the inner svg is AT-hidden
    # and not focusable (so it can't trap keyboard focus).
    assert 'id="minimap"' in html
    assert 'role="img"' in html
    assert 'aria-label="Õiguskaardi miniülevaade"' in html
    assert 'focusable="false"' in html
    # The start panel is a labelled region too.
    assert 'id="explorer-start-panel"' in html
    assert 'aria-label="Õiguskaardi avapaneel"' in html


def test_explorer_active_preset_chip_is_aria_pressed_true():
    """#760 (+ #756): the active legal-view preset chip reports aria-pressed=true
    so AT announces which view is on."""
    html = _html("vaade=el-seosed")
    assert 'aria-pressed="true"' in html
    assert "preset-chip active" in html


def test_explorer_css_has_focus_visible_rings_for_chrome_controls():
    """#760: every bespoke interactive control gets a visible :focus-visible
    ring (reusing the design system's --color-focus-ring token)."""
    css = _css()
    assert ":focus-visible" in css
    assert "--color-focus-ring" in css
    # The ring covers the toolbar buttons, the preset chips, the search box,
    # the "Vaate seaded" summary, the detail-panel actions, and the start panel.
    for needle in (
        ".ctrl-btn:focus-visible",
        "summary.ctrl-settings-summary:focus-visible",
        "#search-input:focus-visible",
        "#detail-close:focus-visible",
        "#detail-panel .evidence-action:focus-visible",
        "#explorer-start-panel a:focus-visible",
        "#timeline-slider:focus-visible",
        "#minimap-svg:focus-visible",
    ):
        assert needle in css, needle


def test_explorer_css_respects_prefers_reduced_motion():
    """#760: animations are wrapped in @media (prefers-reduced-motion: reduce) —
    the #758 "you are here" pulsing ring, the loading spinner, the toasts, and
    the detail-panel slide."""
    css = _css()
    assert css.count("@media (prefers-reduced-motion: reduce)") >= 3
    # The you-are-here ring stops pulsing.
    assert "you-are-here-ring { animation: none" in css
    # The spinner stops spinning.
    assert ".spinner { animation: none; }" in css


def test_explorer_css_responsive_blocks_cover_new_surfaces():
    """#760: the @media blocks handle the preset chips, the evidence card, the
    mini-map (shrunk ≤768px, hidden on the smallest phones), and the start
    panel — at 768px and at ~360px."""
    css = _css()
    assert "@media (max-width: 768px)" in css
    assert "@media (max-width: 400px)" in css
    # Preset chips: full-width wrapping line that can scroll-x on a phone.
    assert ".preset-group {" in css
    # The mini-map is shrunk on a phone and hidden on the smallest screens.
    assert "#minimap, #minimap.visible { display: none; }" in css
    # The detail panel becomes a full-content-width overlay on a phone.
    assert "#detail-panel { width: 100%; max-width: 100%;" in css


def test_explorer_css_closed_detail_panel_leaves_the_tab_order():
    """#760: the closed (off-screen) detail panel is ``visibility: hidden`` so
    its action buttons aren't a "ghost focus" trap; it becomes ``visible`` only
    when ``.open``."""
    css = _css()
    # Match irrespective of internal whitespace.
    detail_block = re.search(r"#detail-panel\s*\{[^}]*\}", css)
    assert detail_block is not None
    assert "visibility: hidden" in detail_block.group(0)
    open_block = re.search(r"#detail-panel\.open\s*\{[^}]*\}", css)
    assert open_block is not None
    assert "visibility: visible" in open_block.group(0)


def test_explorer_js_has_keyboard_and_focus_helpers():
    """#760: explorer.js exposes the keyboard-activation helper (used for the
    breadcrumb crumbs + the neighbour-list rows), manages detail-panel focus +
    aria-hidden, and inerts the chrome behind the contextual start panel."""
    js = _js()
    # The keyboard helper + its use on the two click-only surfaces.
    assert "function makeKeyActivatable(" in js
    assert "makeKeyActivatable(overview" in js  # breadcrumb crumb
    assert js.count("makeKeyActivatable(") >= 3  # helper def + breadcrumb + neighbour + cat
    # Detail-panel focus management.
    assert "function openDetailPanel(" in js
    assert "removeAttribute('aria-hidden')" in js
    assert "setAttribute('aria-hidden', 'true')" in js
    # The start panel inerts the chrome behind it.
    assert "function _setBehindPanelInert(" in js
    assert "setAttribute('inert'" in js
