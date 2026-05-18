"""Tests for A6 — Töölaud "EL ülevõtu tähtajad" widget.

Covers:

* :func:`app.analyysikeskus.eu_transposition.list_overdue_or_upcoming_transpositions`
  — the SPARQL helper against a mocked Jena fixture with a mix of overdue,
  upcoming-within-horizon, and fully-transposed directives.
* :func:`app.analyysikeskus.eu_transposition._build_deadlines_query`
  — the query mentions all the right predicates and bakes the cutoff
  date as an ``xsd:date`` literal (cutoff is server-controlled, not user
  input).
* The Töölaud rendering branch in
  :mod:`app.templates.dashboard` — empty-state hides the widget entirely;
  populated state renders the right CELEX, deadline badge, status badge,
  and CTA link.

No live Jena — every test injects a ``MagicMock`` ``SparqlClient`` whose
``.query`` returns canned rows.
"""

from __future__ import annotations

from datetime import date
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from app.analyysikeskus.eu_transposition import (
    DEFAULT_TRANSPOSITION_HORIZON_DAYS,
    TranspositionDeadlineRow,
    _build_deadlines_query,
    _parse_deadline,
    list_overdue_or_upcoming_transpositions,
)

# ---------------------------------------------------------------------------
# Fixture URIs — mirror the shape the GDPR / AvTS rows take in
# tests/test_eu_transposition.py so the two test files read as a pair.
# ---------------------------------------------------------------------------


_DIR_OVERDUE = "https://data.riik.ee/ontology/estleg#EU-32022L0001"
_DIR_UPCOMING = "https://data.riik.ee/ontology/estleg#EU-32023L0002"
_DIR_TRANSPOSED = "https://data.riik.ee/ontology/estleg#EU-32020L0003"  # filtered out
_DIR_NO_ACT = "https://data.riik.ee/ontology/estleg#EU-32024L0004"  # puudub bucket

_ACT_PARTIAL = "https://data.riik.ee/ontology/estleg#act-partial"
_ACT_FULL = "https://data.riik.ee/ontology/estleg#act-full"
_ACT_OSALINE = "https://data.riik.ee/ontology/estleg#act-osaline"


def _client_returning(rows: list[dict[str, Any]]) -> MagicMock:
    client = MagicMock()
    client.query.return_value = rows
    return client


# ---------------------------------------------------------------------------
# _build_deadlines_query — predicate coverage + safe cutoff injection
# ---------------------------------------------------------------------------


class TestBuildDeadlinesQuery:
    def test_query_mentions_all_required_predicates(self):
        q = _build_deadlines_query(date(2026, 8, 13))
        # The directive deadline edge — mandatory on every row.
        assert "estleg:transpositionDeadline" in q
        # Both transposition directions covered via UNION.
        assert "estleg:transposesDirective" in q
        assert "estleg:transposedBy" in q
        # Status literal is OPTIONAL but must be projected.
        assert "estleg:transpositionStatus" in q
        assert "?status" in q
        # CELEX + label are optional but projected so the widget has labels.
        assert "estleg:celexNumber" in q
        assert "rdfs:label" in q

    def test_query_is_entity_centered_not_graph_scoped(self):
        # Same shape as docs/impact/eu_transposition.py — no GRAPH wrapper.
        q = _build_deadlines_query(date(2026, 8, 13))
        assert "GRAPH" not in q
        assert "?euAct" in q

    def test_cutoff_baked_as_xsd_date_literal(self):
        q = _build_deadlines_query(date(2026, 8, 13))
        assert '"2026-08-13"^^xsd:date' in q
        # And the filter applies it the right way around.
        assert "FILTER(?deadline < " in q

    def test_query_orders_by_deadline_ascending_and_caps_rows(self):
        q = _build_deadlines_query(date(2026, 8, 13))
        assert "ORDER BY ASC(?deadline)" in q
        assert "LIMIT " in q

    def test_query_floors_deadlines_to_drop_pre_1980_sentinels(self):
        """Bug #800: the ontology carries ~50 sentinel rows with
        ``"1001-01-01"`` deadlines that previously exhausted the
        ``LIMIT 50`` before any real overdue row surfaced. The WHERE
        clause now floors the deadline server-side."""
        q = _build_deadlines_query(date(2026, 8, 13))
        assert 'FILTER(?deadline >= "1980-01-01"^^xsd:date)' in q

    def test_query_scopes_to_in_force_directives(self):
        """Bug #800: repealed EU acts should not surface as live
        transposition debt. ``estleg:inForce true`` is the predicate
        present in prod Jena (26,313 boolean triples)."""
        q = _build_deadlines_query(date(2026, 8, 13))
        assert "estleg:inForce true" in q


# ---------------------------------------------------------------------------
# _parse_deadline — sentinel-year floor (bug #800)
# ---------------------------------------------------------------------------


class TestParseDeadlineSentinelFloor:
    """Defence-in-depth Python-side floor for pre-1980 sentinel dates.

    The server-side SPARQL filter already drops these, but the parser
    enforces the same rule so the widget never re-renders a year-1001
    row even if the ontology drifts.
    """

    def test_pre_1980_sentinel_year_1001_returns_none(self):
        # The literal that produced "01.01.1001 · 374511 p möödunud" rows
        # on /dashboard before the fix.
        assert _parse_deadline("1001-01-01") is None

    def test_year_just_below_floor_returns_none(self):
        # Boundary: 1979-12-31 is the last day below the 1980 floor.
        assert _parse_deadline("1979-12-31") is None

    def test_year_at_floor_parses(self):
        # Boundary: 1980-01-01 is the first day at the floor and parses
        # normally.
        assert _parse_deadline("1980-01-01") == date(1980, 1, 1)

    def test_modern_year_parses(self):
        # Sanity check: a normal post-2000 directive deadline parses
        # cleanly.
        assert _parse_deadline("2025-06-15") == date(2025, 6, 15)


# ---------------------------------------------------------------------------
# list_overdue_or_upcoming_transpositions — fixture graph
# ---------------------------------------------------------------------------


class TestListOverdueOrUpcomingTranspositions:
    """Fixture graph with a mix of overdue / upcoming / transposed directives.

    Note the SPARQL query already filters by ``FILTER(?deadline < cutoff)``
    server-side; we mirror that here by *only* returning rows whose
    deadline really is within horizon. The "far future" directive
    therefore does not appear in the fixture — that's the server-side
    behaviour. The transposed and no-act directives are returned by the
    mock so the Python-side status rollup is exercised.
    """

    def _fixture_rows(self) -> list[dict[str, str]]:
        return [
            # Overdue (deadline in the past) + partial transposition →
            # surfaces with status "osaline".
            {
                "euAct": _DIR_OVERDUE,
                "euLabel": "Direktiiv A",
                "celex": "32022L0001",
                "deadline": "2026-04-01",
                "eeAct": _ACT_PARTIAL,
                "status": "partial",
            },
            # Upcoming (deadline in horizon) + no status literal → "ebaselge".
            {
                "euAct": _DIR_UPCOMING,
                "euLabel": "Direktiiv B",
                "celex": "32023L0002",
                "deadline": "2026-06-15",
                "eeAct": _ACT_PARTIAL,
                # no status literal
            },
            # Upcoming (deadline in horizon) + fully transposed → filtered OUT.
            {
                "euAct": _DIR_TRANSPOSED,
                "euLabel": "Direktiiv C",
                "celex": "32020L0003",
                "deadline": "2026-05-30",
                "eeAct": _ACT_FULL,
                "status": "complete",
            },
            # Upcoming + no transposing act (eeAct unbound) → "puudub".
            {
                "euAct": _DIR_NO_ACT,
                "euLabel": "Direktiiv D",
                "celex": "32024L0004",
                "deadline": "2026-07-01",
            },
        ]

    def test_returns_rows_for_overdue_and_upcoming_only(self):
        rows = list_overdue_or_upcoming_transpositions(
            horizon_days=90,
            sparql_client=_client_returning(self._fixture_rows()),
            today=date(2026, 5, 15),
        )
        # 4 fixture directives → 3 surfaced (the fully-transposed one drops out).
        assert len(rows) == 3
        celexes = [r.celex for r in rows]
        assert "32022L0001" in celexes  # overdue
        assert "32023L0002" in celexes  # upcoming partial
        assert "32024L0004" in celexes  # puudub
        # The fully-transposed directive is filtered out by the Python rollup.
        assert "32020L0003" not in celexes

    def test_rows_ordered_by_deadline_ascending(self):
        rows = list_overdue_or_upcoming_transpositions(
            horizon_days=90,
            sparql_client=_client_returning(self._fixture_rows()),
            today=date(2026, 5, 15),
        )
        deadlines = [r.deadline for r in rows]
        assert deadlines == sorted(deadlines)
        # Most overdue first.
        assert rows[0].deadline == date(2026, 4, 1)

    def test_days_remaining_signed(self):
        rows = list_overdue_or_upcoming_transpositions(
            horizon_days=90,
            sparql_client=_client_returning(self._fixture_rows()),
            today=date(2026, 5, 15),
        )
        # Overdue (2026-04-01 from 2026-05-15) → −44 days.
        overdue = next(r for r in rows if r.celex == "32022L0001")
        assert overdue.days_remaining == -44
        # Upcoming (2026-06-15) → +31 days.
        upcoming = next(r for r in rows if r.celex == "32023L0002")
        assert upcoming.days_remaining == 31

    def test_status_mapping(self):
        rows = list_overdue_or_upcoming_transpositions(
            horizon_days=90,
            sparql_client=_client_returning(self._fixture_rows()),
            today=date(2026, 5, 15),
        )
        by_celex = {r.celex: r for r in rows}
        # "partial" raw → "osaline".
        assert by_celex["32022L0001"].status == "osaline"
        # No status literal but has a transposing act → "ebaselge".
        assert by_celex["32023L0002"].status == "ebaselge"
        # No transposing act at all → "puudub".
        assert by_celex["32024L0004"].status == "puudub"

    def test_transposing_acts_populated(self):
        rows = list_overdue_or_upcoming_transpositions(
            horizon_days=90,
            sparql_client=_client_returning(self._fixture_rows()),
            today=date(2026, 5, 15),
        )
        by_celex = {r.celex: r for r in rows}
        # Rows with eeAct bound → the URI is captured.
        assert by_celex["32022L0001"].transposing_acts == [_ACT_PARTIAL]
        assert by_celex["32023L0002"].transposing_acts == [_ACT_PARTIAL]
        # "puudub" row has no transposing act.
        assert by_celex["32024L0004"].transposing_acts == []

    def test_multi_act_directive_rolls_up_to_worst_status(self):
        """A directive transposed by *two* acts — one full, one partial — rolls
        up to "osaline" so the widget never lets a single ``complete`` row
        hide an incomplete sibling."""
        rows_in = [
            {
                "euAct": _DIR_UPCOMING,
                "euLabel": "Direktiiv B",
                "celex": "32023L0002",
                "deadline": "2026-06-15",
                "eeAct": _ACT_FULL,
                "status": "complete",
            },
            {
                "euAct": _DIR_UPCOMING,
                "euLabel": "Direktiiv B",
                "celex": "32023L0002",
                "deadline": "2026-06-15",
                "eeAct": _ACT_OSALINE,
                "status": "osaline",
            },
        ]
        rows = list_overdue_or_upcoming_transpositions(
            horizon_days=90,
            sparql_client=_client_returning(rows_in),
            today=date(2026, 5, 15),
        )
        assert len(rows) == 1
        assert rows[0].status == "osaline"
        # Both transposing acts captured, deduped, in order.
        assert set(rows[0].transposing_acts) == {_ACT_FULL, _ACT_OSALINE}

    def test_empty_jena_response_returns_empty_list(self):
        rows = list_overdue_or_upcoming_transpositions(
            horizon_days=90,
            sparql_client=_client_returning([]),
            today=date(2026, 5, 15),
        )
        assert rows == []

    def test_sparql_error_returns_empty_list(self):
        client = MagicMock()
        client.query.side_effect = RuntimeError("jena unreachable")
        rows = list_overdue_or_upcoming_transpositions(
            horizon_days=90,
            sparql_client=client,
            today=date(2026, 5, 15),
        )
        assert rows == []

    def test_org_id_is_accepted_but_ignored(self):
        """The ``org_id`` parameter is reserved for future ministry scoping
        (no ``responsibleMinistry`` predicate in the ontology yet)."""
        client = _client_returning(self._fixture_rows())
        rows_with = list_overdue_or_upcoming_transpositions(
            horizon_days=90,
            org_id="11111111-1111-1111-1111-111111111111",
            sparql_client=client,
            today=date(2026, 5, 15),
        )
        client2 = _client_returning(self._fixture_rows())
        rows_without = list_overdue_or_upcoming_transpositions(
            horizon_days=90,
            org_id=None,
            sparql_client=client2,
            today=date(2026, 5, 15),
        )
        # Both calls return the same surfaced directives.
        assert {r.celex for r in rows_with} == {r.celex for r in rows_without}

    def test_unparseable_deadline_skipped(self):
        rows_in = [
            {
                "euAct": _DIR_OVERDUE,
                "euLabel": "Direktiiv A",
                "celex": "32022L0001",
                "deadline": "not-a-date",  # malformed
                "eeAct": _ACT_PARTIAL,
                "status": "partial",
            },
            {
                "euAct": _DIR_UPCOMING,
                "euLabel": "Direktiiv B",
                "celex": "32023L0002",
                "deadline": "2026-06-15",
                "eeAct": _ACT_PARTIAL,
                "status": "partial",
            },
        ]
        rows = list_overdue_or_upcoming_transpositions(
            horizon_days=90,
            sparql_client=_client_returning(rows_in),
            today=date(2026, 5, 15),
        )
        # The malformed row drops; the valid one survives.
        assert len(rows) == 1
        assert rows[0].celex == "32023L0002"

    def test_pre_1980_sentinel_deadline_dropped_at_aggregation(self):
        """Bug #800: even if a sentinel ``"1001-01-01"`` literal somehow
        slips past the server-side floor (e.g. a future schema drift),
        the Python-side parser rejects it and ``_aggregate_rows`` drops
        the row — the widget never re-renders ``"01.01.1001 · 374511 p
        möödunud"``."""
        rows_in = [
            {
                "euAct": _DIR_OVERDUE,
                "euLabel": "Direktiiv (sentinel)",
                "celex": "31970L0001",
                "deadline": "1001-01-01",  # sentinel
                "eeAct": _ACT_PARTIAL,
                "status": "partial",
            },
            {
                "euAct": _DIR_UPCOMING,
                "euLabel": "Direktiiv B",
                "celex": "32023L0002",
                "deadline": "2026-06-15",
                "eeAct": _ACT_PARTIAL,
                "status": "partial",
            },
        ]
        rows = list_overdue_or_upcoming_transpositions(
            horizon_days=90,
            sparql_client=_client_returning(rows_in),
            today=date(2026, 5, 15),
        )
        # Sentinel row drops; the modern row survives.
        assert len(rows) == 1
        assert rows[0].celex == "32023L0002"

    def test_default_horizon_is_90_days(self):
        """The module-level constant gates the default — verifies the
        signature default isn't out of sync with the constant."""
        assert DEFAULT_TRANSPOSITION_HORIZON_DAYS == 90
        # Smoke-check the default flows through (cutoff appears in the query
        # the client receives).
        client = _client_returning([])
        list_overdue_or_upcoming_transpositions(
            sparql_client=client,
            today=date(2026, 5, 15),
        )
        sent_query = client.query.call_args.args[0]
        # 2026-05-15 + 90 days = 2026-08-13
        assert '"2026-08-13"^^xsd:date' in sent_query


# ---------------------------------------------------------------------------
# Dashboard widget — empty + populated rendering smoke tests
# ---------------------------------------------------------------------------


_ORG_INFO = {"org_name": "Justiitsministeerium", "role": "drafter", "member_count": 4}


def _make_dashboard_request():
    """Build a minimal ASGI ``Request`` carrying an ``auth`` scope.

    Mirrors ``tests/test_dashboard.py::_make_dashboard_request`` so the
    dashboard route can be invoked without a TestClient round-trip.
    """
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


_DASHBOARD_WIDGET_HELPERS = (
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


def _render_dashboard(returns: dict[str, object]) -> str:
    """Render ``dashboard_page`` with every widget helper patched."""
    from contextlib import ExitStack

    from fasthtml.common import to_xml

    from app.templates.dashboard import dashboard_page

    with ExitStack() as stack:
        for name in _DASHBOARD_WIDGET_HELPERS:
            default: object = None if name == "_get_user_org_info" else []
            stack.enter_context(
                patch(f"app.templates.dashboard.{name}", return_value=returns.get(name, default))
            )
        result = dashboard_page(_make_dashboard_request())
    return to_xml(result)


class TestDashboardWidgetRendering:
    """Empty + populated rendering of the EL ülevõtu tähtajad widget."""

    def test_empty_state_hides_widget_entirely(self):
        """When the helper returns no rows the entire card is omitted —
        no header, no empty-state placeholder, no card body. The dashboard
        already runs long; an empty "no upcoming transpositions" row would
        be noise."""
        html = _render_dashboard({"_get_user_org_info": _ORG_INFO})
        # The card heading text MUST NOT appear when the widget is hidden.
        assert "EL ülevõtu tähtajad" not in html

    def test_populated_state_renders_table_with_celex_and_link(self):
        rows = [
            TranspositionDeadlineRow(
                celex="32022L0001",
                directive_label_et="Direktiiv andmekaitse kohta",
                deadline=date(2026, 4, 1),
                days_remaining=-44,
                status="osaline",
                transposing_acts=["https://data.riik.ee/ontology/estleg#act-1"],
            ),
            TranspositionDeadlineRow(
                celex="32024L0004",
                directive_label_et="Direktiiv D",
                deadline=date(2026, 7, 1),
                days_remaining=47,
                status="puudub",
                transposing_acts=[],
            ),
        ]
        html = _render_dashboard(
            {
                "_get_user_org_info": _ORG_INFO,
                "_get_eu_transposition_deadlines": rows,
            }
        )
        # Card header present.
        assert "EL ülevõtu tähtajad" in html
        # Both CELEX numbers and labels are surfaced.
        assert "32022L0001" in html
        assert "Direktiiv andmekaitse kohta" in html
        assert "32024L0004" in html
        # Estonian status labels per the design spec.
        assert "Ülevõtt osaline" in html
        assert "Ülevõtt puudub" in html
        assert "Tähtaeg möödunud" in html
        # CTA — operational, Estonian, points at the EL ülevõtt workflow
        # pre-filled with the CELEX.
        assert "Vaata ülevõttu" in html
        assert "/analyysikeskus/el-ulevott?sisend=32022L0001" in html
        assert "/analyysikeskus/el-ulevott?sisend=32024L0004" in html
        # Deadline formatted as DD.MM.YYYY.
        assert "01.04.2026" in html
        assert "01.07.2026" in html

    def test_show_all_link_appears_when_more_than_five_rows(self):
        # Six rows → top 5 inline + "Näita kõiki (6)" link.
        rows = [
            TranspositionDeadlineRow(
                celex=f"32022L{i:04d}",
                directive_label_et=f"Direktiiv {i}",
                deadline=date(2026, 6, i + 1),
                days_remaining=20 + i,
                status="osaline",
                transposing_acts=[],
            )
            for i in range(6)
        ]
        html = _render_dashboard(
            {
                "_get_user_org_info": _ORG_INFO,
                "_get_eu_transposition_deadlines": rows,
            }
        )
        assert "Näita kõiki (6)" in html
        assert "/analyysikeskus/el-ulevott?vaade=tahtajad" in html

    def test_no_show_all_link_when_exactly_five_or_fewer(self):
        rows = [
            TranspositionDeadlineRow(
                celex=f"32022L{i:04d}",
                directive_label_et=f"Direktiiv {i}",
                deadline=date(2026, 6, i + 1),
                days_remaining=20 + i,
                status="osaline",
                transposing_acts=[],
            )
            for i in range(3)
        ]
        html = _render_dashboard(
            {
                "_get_user_org_info": _ORG_INFO,
                "_get_eu_transposition_deadlines": rows,
            }
        )
        assert "Näita kõiki" not in html

    def test_widget_placement_between_high_risk_and_stale(self):
        """A6 spec: place the widget after 'high-risk impact findings' and
        before 'recent drafts'. The closest stand-in for 'recent drafts'
        on this dashboard is 'Aegunud analüüsid' (stale drafts)."""
        rows = [
            TranspositionDeadlineRow(
                celex="32022L0001",
                directive_label_et="Direktiiv andmekaitse kohta",
                deadline=date(2026, 4, 1),
                days_remaining=-44,
                status="osaline",
                transposing_acts=[],
            ),
        ]
        html = _render_dashboard(
            {
                "_get_user_org_info": _ORG_INFO,
                "_get_eu_transposition_deadlines": rows,
            }
        )
        # The widget heading lands after the "Kõrge riskiga leiud" heading
        # and before the "Aegunud analüüsid" heading.
        hr_idx = html.index("Kõrge riskiga leiud")
        eu_idx = html.index("EL ülevõtu tähtajad")
        stale_idx = html.index("Aegunud analüüsid")
        assert hr_idx < eu_idx < stale_idx


# ---------------------------------------------------------------------------
# _get_eu_transposition_deadlines — timeout gating
# ---------------------------------------------------------------------------


class TestDeadlinesHelperTimeout:
    """The dashboard wraps the SPARQL call in a soft timeout; when Jena is
    slow / stuck, the helper returns ``[]`` so the widget hides rather than
    blocking the page render."""

    def test_returns_rows_when_query_completes_in_time(self):
        from app.templates.dashboard import _get_eu_transposition_deadlines

        canned = [
            TranspositionDeadlineRow(
                celex="32022L0001",
                directive_label_et="Direktiiv A",
                deadline=date(2026, 4, 1),
                days_remaining=-44,
                status="osaline",
                transposing_acts=[],
            ),
        ]

        with patch(
            "app.templates.dashboard.list_overdue_or_upcoming_transpositions",
            return_value=canned,
        ):
            out = _get_eu_transposition_deadlines(
                "11111111-1111-1111-1111-111111111111", timeout_s=2.0
            )
        assert out == canned

    def test_returns_empty_when_query_exceeds_timeout(self):
        """A genuinely slow upstream is replaced by ``[]`` after the
        timeout fires (the widget then hides on the page)."""
        import time as _time

        from app.templates.dashboard import _get_eu_transposition_deadlines

        def _slow_query(*args, **kwargs):
            _time.sleep(0.5)
            return [
                TranspositionDeadlineRow(
                    celex="32022L0001",
                    directive_label_et="Direktiiv A",
                    deadline=date(2026, 4, 1),
                    days_remaining=-44,
                    status="osaline",
                    transposing_acts=[],
                ),
            ]

        with patch(
            "app.templates.dashboard.list_overdue_or_upcoming_transpositions",
            side_effect=_slow_query,
        ):
            out = _get_eu_transposition_deadlines(
                "11111111-1111-1111-1111-111111111111",
                timeout_s=0.05,
            )
        assert out == []

    def test_returns_empty_on_exception(self):
        from app.templates.dashboard import _get_eu_transposition_deadlines

        with patch(
            "app.templates.dashboard.list_overdue_or_upcoming_transpositions",
            side_effect=RuntimeError("kabloom"),
        ):
            out = _get_eu_transposition_deadlines(
                "11111111-1111-1111-1111-111111111111", timeout_s=2.0
            )
        assert out == []

    def test_returns_within_timeout_when_query_is_slow(self):
        """Regression for F1 (2026-05-15 review): the timeout must actually
        bound wall-clock time. Previously the function used ``with
        ThreadPoolExecutor(...)`` which calls ``shutdown(wait=True)`` on
        exit, blocking the return until the slow query finished — so the
        function ran for ~slow_query_s instead of ~timeout_s. The fix
        switches to a manual lifecycle with ``shutdown(wait=False,
        cancel_futures=True)`` so the dashboard render is bounded.
        """
        import time as _time

        from app.templates.dashboard import _get_eu_transposition_deadlines

        slow_query_s = 1.5
        timeout_s = 0.1
        # CI sometimes runs slow; allow up to ~0.5s of scheduling jitter
        # before declaring the timeout broken. The bug case would be ~1.5s.
        tolerance_s = 0.5

        def _very_slow_query(*args, **kwargs):
            _time.sleep(slow_query_s)
            return []

        start = _time.perf_counter()
        with patch(
            "app.templates.dashboard.list_overdue_or_upcoming_transpositions",
            side_effect=_very_slow_query,
        ):
            out = _get_eu_transposition_deadlines(
                "11111111-1111-1111-1111-111111111111",
                timeout_s=timeout_s,
            )
        elapsed = _time.perf_counter() - start

        assert out == []
        assert elapsed < timeout_s + tolerance_s, (
            f"Expected return within ~{timeout_s}s + jitter but got {elapsed:.2f}s. "
            f"Slow query was {slow_query_s}s — if elapsed ≈ that, the "
            "ThreadPoolExecutor shutdown is blocking again (regression of F1)."
        )


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
