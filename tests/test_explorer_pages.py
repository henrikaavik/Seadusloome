"""Smoke tests for the Õiguskaart (explorer) page (#714 / #718 / #746).

Renders ``explorer_page`` directly via ``to_xml()`` and asserts on the
markup — no TestClient, no DB. Post-#746 the page is wrapped in the standard
``PageShell`` (sidebar + topbar + user menu) with the graph controls as a
horizontal toolbar; these tests pin both "the standard chrome is present"
and "the bespoke chrome is gone".
"""

from __future__ import annotations

from fasthtml.common import to_xml
from starlette.requests import Request


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


def test_explorer_page_no_focus_keeps_the_draft_tip():
    html = _html()
    assert "window.__explorerFocus" not in html
    assert "window.__explorerSearch" not in html
    # The cold-open hint about ?draft=ID is rendered in the toolbar.
    assert "?draft=ID" in html
    assert 'id="explorer-tip-banner"' in html


def test_explorer_page_search_blob_is_escaped():
    # A `</script>` injection attempt in the term must be neutralised in the
    # embedded blob exactly like the ?focus= blob is.
    html = _html("search=</script><b>x")
    assert "</script><b>x" not in html
    assert "window.__explorerSearch" in html
