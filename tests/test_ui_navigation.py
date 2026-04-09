"""Smoke tests for Breadcrumb, Tabs, and TabPanel navigation components."""

from fasthtml.common import to_xml

from app.ui.navigation import Breadcrumb, TabPanel, Tabs


def test_breadcrumb_renders_linked_items_and_current_page():
    html = to_xml(
        Breadcrumb(
            ("Avaleht", "/"),
            ("Eelnõud", "/drafts"),
            "Eelnõu nr 42",
        )
    )
    assert 'aria-label="Breadcrumb"' in html
    assert "<nav" in html and "<ol" in html
    assert 'href="/"' in html
    assert 'href="/drafts"' in html
    assert 'aria-current="page"' in html
    assert "Eelnõu nr 42" in html
    assert "\u203a" in html  # chevron separator


def test_breadcrumb_last_item_has_no_link_even_if_tuple():
    html = to_xml(Breadcrumb(("Avaleht", "/"), ("Praegune", "/now")))
    assert 'aria-current="page"' in html
    # The current item should not appear as a link even if given as tuple
    assert 'href="/now"' not in html


def test_tabs_defaults_first_tab_active():
    html = to_xml(Tabs([("overview", "Ülevaade"), ("details", "Detailid")]))
    assert 'role="tablist"' in html
    assert 'role="tab"' in html
    assert 'id="tab-overview"' in html
    assert 'aria-controls="panel-overview"' in html
    # First tab selected, second not
    assert 'aria-selected="true"' in html
    assert 'aria-selected="false"' in html
    assert 'tabindex="0"' in html
    assert 'tabindex="-1"' in html


def test_tabs_explicit_active_selects_given_tab():
    html = to_xml(
        Tabs(
            [("a", "Alpha"), ("b", "Beta"), ("c", "Gamma")],
            active="b",
        )
    )
    assert 'id="tab-b"' in html
    # The 'b' tab should carry aria-selected=true; assert via co-occurrence
    assert 'data-tab-id="b"' in html
    assert html.count('aria-selected="true"') == 1
    assert html.count('aria-selected="false"') == 2


def test_tabs_vertical_orientation_sets_classes():
    html = to_xml(Tabs([("a", "A"), ("b", "B")], orientation="vertical"))
    assert "tabs-vertical" in html
    assert "tablist-vertical" in html
    assert 'aria-orientation="vertical"' in html
    assert 'data-tabs="vertical"' in html


def test_tab_panel_hidden_when_not_active():
    html = to_xml(TabPanel("overview", "content"))
    assert 'role="tabpanel"' in html
    assert 'id="panel-overview"' in html
    assert 'aria-labelledby="tab-overview"' in html
    assert "hidden" in html


def test_tab_panel_visible_when_active():
    html = to_xml(TabPanel("overview", "content", active=True))
    assert 'id="panel-overview"' in html
    assert "hidden" not in html
