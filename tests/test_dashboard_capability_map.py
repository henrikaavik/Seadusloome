"""Tests for the Töölaud "Mida soovid teha?" capability map (B2, issue #793).

The map sits at the top of ``/dashboard``, above the existing work-queue
widgets (which must still render unchanged). Card list is derived from the
B3 capability dictionary, filtered to a curated subset, and rendered as a
collapsible <details> with localStorage persistence.
"""

from __future__ import annotations

from unittest.mock import patch

from fasthtml.common import to_xml

from app.templates.dashboard import (
    _CAPABILITY_MAP_EXCLUDED_SLUGS,
    _MAX_CAPABILITY_CARDS,
    _capability_map_section,
    _dashboard_capabilities,
)
from app.ui.capabilities import CAPABILITIES, get_capability
from app.ui.components.capability_card import CapabilityCard, capability_href

# ---------------------------------------------------------------------------
# Shared helpers (mirror tests/test_dashboard.py so the assertion style
# stays consistent across the dashboard test surface).
# ---------------------------------------------------------------------------

_WIDGET_HELPERS = (
    "_get_active_drafter_sessions",
    "_get_high_risk_reports",
    "_get_unviewed_reports",
    "_get_stale_analysis_drafts",
    "_get_recent_syncs",
    "_get_recent_exports",
    "_get_unresolved_annotation_drafts",
    "_get_bookmarks",
    "_get_user_org_info",
    "_get_eu_transposition_deadlines",
)

_ORG_INFO = {"org_name": "Justiitsministeerium", "role": "drafter", "member_count": 4}


def _make_dashboard_request():
    """Build a minimal ASGI request carrying an ``auth`` scope."""
    from starlette.requests import Request

    scope = {
        "type": "http",
        "method": "GET",
        "path": "/dashboard",
        "headers": [],
        "query_string": b"",
        "scheme": "http",
        "server": ("testserver", 80),
        "client": ("127.0.0.1", 12345),
        "auth": {
            "id": "33333333-3333-3333-3333-333333333333",
            "email": "kasutaja@seadusloome.ee",
            "full_name": "Test Kasutaja",
            "role": "drafter",
            "org_id": "11111111-1111-1111-1111-111111111111",
        },
    }
    return Request(scope)


def _render_dashboard(returns: dict[str, object] | None = None) -> str:
    """Render ``dashboard_page`` with every DB-touching widget patched."""
    from contextlib import ExitStack

    from app.templates.dashboard import dashboard_page

    returns = returns or {}
    with ExitStack() as stack:
        for name in _WIDGET_HELPERS:
            default: object = None if name == "_get_user_org_info" else []
            stack.enter_context(
                patch(
                    f"app.templates.dashboard.{name}",
                    return_value=returns.get(name, default),
                )
            )
        result = dashboard_page(_make_dashboard_request())
    return to_xml(result)


# ---------------------------------------------------------------------------
# Capability filter (B2-specific)
# ---------------------------------------------------------------------------


class TestDashboardCapabilityFilter:
    def test_returns_between_6_and_max_cards(self):
        """Plan calls for 6-9 cards; filter must respect the upper bound."""
        caps = _dashboard_capabilities()
        assert 6 <= len(caps) <= _MAX_CAPABILITY_CARDS == 9

    def test_excludes_globaalne_otsing_and_el_tahtajad(self):
        """The two excluded slugs would duplicate existing dashboard chrome
        (top-bar search and the EU deadlines widget)."""
        slugs = {c.slug for c in _dashboard_capabilities()}
        assert "globaalne-otsing" not in slugs
        assert "el-tahtajad" not in slugs
        assert _CAPABILITY_MAP_EXCLUDED_SLUGS == frozenset({"globaalne-otsing", "el-tahtajad"})

    def test_live_capabilities_render_before_planned(self):
        """A new lawyer should see real entry points first; planned ones
        with a Tulekul badge come after."""
        caps = _dashboard_capabilities()
        statuses = [c.status for c in caps]
        if "planned" in statuses:
            first_planned = statuses.index("planned")
            # Every entry before the first planned must be live.
            assert all(s == "live" for s in statuses[:first_planned])

    def test_every_card_has_a_known_capability(self):
        """Filter result must be drawn from the canonical CAPABILITIES list."""
        slugs = {c.slug for c in CAPABILITIES}
        for cap in _dashboard_capabilities():
            assert cap.slug in slugs


# ---------------------------------------------------------------------------
# Card href / deep-link builder
# ---------------------------------------------------------------------------


class TestCapabilityHref:
    def test_analyysikeskus_with_example_prefills_sisend(self):
        cap = get_capability("normi-mojuahel")
        assert cap is not None and cap.example_input is not None
        href = capability_href(cap)
        assert href.startswith("/analyysikeskus/normi-mojuahel?")
        assert "sisend=AvTS" in href

    def test_chat_target_does_not_get_sisend(self):
        """``/chat/new`` would ignore ``?sisend=`` (chat seeds need POST), so
        the card just deep-links to the bare URL."""
        cap = get_capability("noustaja")
        assert cap is not None
        href = capability_href(cap)
        assert href == "/chat/new"

    def test_explorer_target_keeps_bare_url(self):
        cap = get_capability("oiguskaart")
        assert cap is not None
        href = capability_href(cap)
        assert href == "/explorer"

    def test_drafts_target_keeps_bare_url(self):
        cap = get_capability("eelnou-impact")
        assert cap is not None
        href = capability_href(cap)
        assert href == "/drafts"


# ---------------------------------------------------------------------------
# CapabilityCard FT component
# ---------------------------------------------------------------------------


class TestCapabilityCard:
    def test_renders_anchor_with_href_and_aria_label(self):
        cap = get_capability("normi-mojuahel")
        assert cap is not None
        html = to_xml(CapabilityCard(cap))
        assert "<a " in html
        assert 'href="/analyysikeskus/normi-mojuahel?sisend=' in html
        assert cap.canonical_name_et in html
        assert cap.one_line_description_et in html
        # aria-label combines title + description for screen readers.
        assert f"{cap.canonical_name_et} — {cap.one_line_description_et}" in html
        # Stable slug data attribute helps tests + downstream analytics.
        assert 'data-capability-slug="normi-mojuahel"' in html

    def test_planned_capability_shows_tulekul_badge(self):
        # Pick any planned capability still present in the dictionary.
        cap = next((c for c in CAPABILITIES if c.status == "planned"), None)
        assert cap is not None
        html = to_xml(CapabilityCard(cap))
        assert "Tulekul" in html
        assert "capability-card--planned" in html

    def test_live_capability_omits_tulekul_badge(self):
        cap = get_capability("normi-mojuahel")
        assert cap is not None and cap.status == "live"
        html = to_xml(CapabilityCard(cap))
        assert "Tulekul" not in html

    def test_example_input_renders_in_card(self):
        cap = get_capability("normi-mojuahel")
        assert cap is not None and cap.example_input is not None
        html = to_xml(CapabilityCard(cap))
        assert "Näide:" in html
        assert cap.example_input in html

    def test_no_example_when_input_is_none(self):
        cap = get_capability("oiguskaart")
        assert cap is not None and cap.example_input is None
        html = to_xml(CapabilityCard(cap))
        assert "Näide:" not in html


# ---------------------------------------------------------------------------
# Section render — collapsible <details>, grid, script
# ---------------------------------------------------------------------------


class TestCapabilityMapSection:
    def test_section_renders_details_summary_with_estonian_title(self):
        section = _capability_map_section(_dashboard_capabilities())
        assert section is not None
        html = to_xml(section)
        assert "<details" in html
        assert "<summary" in html
        assert "Mida soovid teha?" in html
        assert 'id="capability-map"' in html

    def test_section_renders_every_card_in_filter(self):
        caps = _dashboard_capabilities()
        section = _capability_map_section(caps)
        assert section is not None
        html = to_xml(section)
        for cap in caps:
            assert f'data-capability-slug="{cap.slug}"' in html, cap.slug

    def test_section_returns_none_when_empty(self):
        """No capabilities → no decorative box (matches A6 widget policy)."""
        assert _capability_map_section([]) is None

    def test_section_inlines_localstorage_script(self):
        section = _capability_map_section(_dashboard_capabilities())
        assert section is not None
        html = to_xml(section)
        assert "dashboard.capabilityMap.open" in html
        # Default-open-on-desktop / collapsed-on-mobile heuristic.
        assert "min-width: 769px" in html
        assert "localStorage" in html

    def test_grid_class_applied(self):
        section = _capability_map_section(_dashboard_capabilities())
        assert section is not None
        html = to_xml(section)
        assert "capability-map__grid" in html


# ---------------------------------------------------------------------------
# Full /dashboard page integration — new section + untouched legacy widgets
# ---------------------------------------------------------------------------


class TestDashboardPageIntegration:
    def test_capability_map_section_renders_at_top(self):
        """B2 spec: "Mida soovid teha?" sits above the work queue."""
        html = _render_dashboard({"_get_user_org_info": _ORG_INFO})
        assert "Mida soovid teha?" in html
        # Capability map appears before the first work-queue widget.
        cap_idx = html.index("Mida soovid teha?")
        queue_idx = html.index("Minu järgmised tegevused")
        assert cap_idx < queue_idx, "Capability map must precede the work queue on the page."

    def test_existing_work_queue_widgets_still_render(self):
        """The existing dashboard widgets (queue + bookmarks) stay intact."""
        html = _render_dashboard({"_get_user_org_info": _ORG_INFO})
        for header in (
            "Minu järgmised tegevused",
            "Kõrge riskiga leiud",
            "Aegunud analüüsid",
            "Uued ontoloogia muudatused",
            "Hiljutised ekspordid",
            "Eelnõud lahtiste märkustega",
            "Järjehoidjad",
        ):
            assert header in html, header

    def test_eu_deadlines_widget_still_renders_after_capability_map(self):
        """A6's EU transposition deadlines widget must still render below the
        capability map when rows exist."""
        from datetime import date

        from app.analyysikeskus.eu_transposition import TranspositionDeadlineRow

        row = TranspositionDeadlineRow(
            celex="32016R0679",
            directive_label_et="Andmekaitse üldmäärus",
            deadline=date(2026, 6, 1),
            days_remaining=16,
            status="puudub",
        )
        html = _render_dashboard(
            {
                "_get_user_org_info": _ORG_INFO,
                "_get_eu_transposition_deadlines": [row],
            }
        )
        assert "EL ülevõtu tähtajad" in html
        # Capability map still appears above the EU widget.
        assert html.index("Mida soovid teha?") < html.index("EL ülevõtu tähtajad")

    def test_card_links_use_capability_href(self):
        html = _render_dashboard({"_get_user_org_info": _ORG_INFO})
        # Sanity: at least one Analüüsikeskus deep-link with a prefilled
        # ``sisend`` shows up; the chat card uses its bare URL.
        assert "/analyysikeskus/normi-mojuahel?sisend=" in html
        assert 'href="/chat/new"' in html
        assert 'href="/explorer"' in html

    def test_capability_map_present_even_with_empty_queue(self):
        """The whole point of B2: a brand-new user with no drafts and no
        findings still sees the discovery section."""
        html = _render_dashboard({})  # everything empty incl. org_info=None
        assert "Mida soovid teha?" in html
        assert "Hetkel pole midagi ootel." in html  # empty queue confirmed
