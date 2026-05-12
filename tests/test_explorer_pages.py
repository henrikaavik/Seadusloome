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

from unittest.mock import patch

import pytest
from fasthtml.common import to_xml
from starlette.requests import Request


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
    # bypass the start panel and render the classic graph view (#755 handles
    # the proper draft subgraph — for #754 the panel just gets skipped).
    html = _html("draft=not-a-uuid")
    assert 'id="explorer-start-panel"' not in html
    assert "window.__explorerStartPanel" not in html
    # The graph toolbar is the visible chrome here (not hidden behind a panel).
    assert 'id="explorer-toolbar"' in html


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
