"""Tests for the Ajalooline kehtivus workflow (A4 v1).

Covers:

1. The SPARQL / DB helper layer in :mod:`app.analyysikeskus.history` —
   ``ActTimeline``, AmendmentEvent aggregation, court-decision /
   pending-draft list helpers, impact_reports DB read, and the
   :func:`get_history_bundle` aggregator. Includes a rdflib end-to-end
   regression against the extended canonical fixture so the SPARQL
   templates run against a real (in-memory) triplestore.
2. The Estonian display-label helper (``temporal_status_label``) —
   covers the explicit mappings and the raw-string fallback.
3. The route layer in :mod:`app.analyysikeskus.routes` — the
   ``/analyysikeskus/ajalugu`` endpoint: auth gate, landing page,
   resolved-provision page (with the v1 limitation banner),
   resolved-act page (banner suppressed), disambiguation branch, and
   the unresolved branch.

The critical test is ``test_ajalugu_provision_input_shows_banner`` /
``test_ajalugu_act_input_hides_banner``: the v1 limitation banner
must appear for Provision inputs and never for Act inputs.

Tests follow the same shape as ``test_analyysikeskus_sanctions.py``
— external dependencies (SPARQL client, ReferenceResolver, RAG
retriever, get_history_bundle) are patched **where used** per the
patch-path contract.
"""

from __future__ import annotations

from datetime import date, datetime
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Fixtures — shared URIs + canned rows
# ---------------------------------------------------------------------------

_ACT_URI = "https://data.riik.ee/ontology/estleg#Act_1"
_PROVISION_URI = "https://data.riik.ee/ontology/estleg#Provision_1"
_EVENT_URI = "https://data.riik.ee/ontology/estleg#AmendmentEvent_1"
_COURT_URI = "https://data.riik.ee/ontology/estleg#CourtDecision_1"
_DRAFT_URI = "https://data.riik.ee/ontology/estleg#Draft_1"

# A made-up KarS-style URI used by the route tests (resolver is mocked).
_KARS_URI = "https://data.riik.ee/ontology/estleg#karistusseadustik"
_KARS_P211_URI = "https://data.riik.ee/ontology/estleg#KarS-p211"


# ---------------------------------------------------------------------------
# 1. Label helper
# ---------------------------------------------------------------------------


class TestTemporalStatusLabel:
    def test_known_values(self):
        from app.analyysikeskus.history import temporal_status_label

        assert temporal_status_label("in_force") == "Kehtib"
        assert temporal_status_label("repealed") == "Tunnistatud kehtetuks"
        assert temporal_status_label("pending") == "Jõustumata"
        assert temporal_status_label("expired") == "Aegunud"

    def test_unknown_falls_back(self):
        from app.analyysikeskus.history import temporal_status_label

        assert temporal_status_label("draft_review") == "draft_review"

    def test_empty(self):
        from app.analyysikeskus.history import temporal_status_label

        assert temporal_status_label("") == "—"
        assert temporal_status_label("   ") == "—"


# ---------------------------------------------------------------------------
# 2. SPARQL helpers — owning-act / timeline / amendment / court / pending
# ---------------------------------------------------------------------------


class TestResolveOwningAct:
    def test_returns_act_uri_legacy_key(self):
        """Legacy ``?act`` projection (older test stubs) still works."""
        from app.analyysikeskus.history import resolve_owning_act

        stub = MagicMock()
        stub.query.return_value = [{"act": _ACT_URI}]
        assert resolve_owning_act(_PROVISION_URI, sparql_client=stub) == _ACT_URI

    def test_returns_literal_title_from_prod_key(self):
        """Prod contract: ``?actLabel`` projects the ``estleg:sourceAct`` literal.

        Per the 2026-05-18 ontology probe, ``estleg:sourceAct`` is a
        literal title (e.g. ``"Avaliku teabe seadus"``) — never an
        act URI. The resolver returns the literal directly.
        """
        from app.analyysikeskus.history import resolve_owning_act

        stub = MagicMock()
        stub.query.return_value = [{"actLabel": "Avaliku teabe seadus"}]
        assert resolve_owning_act(_PROVISION_URI, sparql_client=stub) == "Avaliku teabe seadus"

    def test_blank_uri_skips_jena(self):
        from app.analyysikeskus.history import resolve_owning_act

        stub = MagicMock()
        assert resolve_owning_act("", sparql_client=stub) == ""
        stub.query.assert_not_called()

    def test_sparql_error_returns_empty(self):
        from app.analyysikeskus.history import resolve_owning_act

        stub = MagicMock()
        stub.query.side_effect = RuntimeError("jena down")
        assert resolve_owning_act(_PROVISION_URI, sparql_client=stub) == ""

    def test_owning_act_query_drops_partof(self):
        """The owning-act query no longer references ``estleg:partOf``.

        Acceptance criterion 1 of Wave 2 Step 5: no active SPARQL
        string references ``estleg:partOf`` / ``estleg:partOfAct``.
        """
        from app.analyysikeskus.history import _PROVISION_OWNING_ACT_QUERY

        assert "estleg:partOf" not in _PROVISION_OWNING_ACT_QUERY
        assert "estleg:partOfAct" not in _PROVISION_OWNING_ACT_QUERY
        assert "estleg:sourceAct" in _PROVISION_OWNING_ACT_QUERY


class TestGetActTimeline:
    def test_returns_parsed_timeline(self):
        from app.analyysikeskus.history import get_act_timeline

        stub = MagicMock()
        stub.query.return_value = [
            {
                "act": _ACT_URI,
                "actLabel": "Act 1 — fixture host",
                "entryIntoForce": "2010-01-01",
                "lastAmendmentDate": "2023-03-15",
                "temporalStatus": "in_force",
            }
        ]
        timeline = get_act_timeline(_ACT_URI, sparql_client=stub)
        assert timeline.act_uri == _ACT_URI
        assert timeline.act_label == "Act 1 — fixture host"
        assert timeline.entry_into_force == date(2010, 1, 1)
        assert timeline.last_amendment_date == date(2023, 3, 15)
        assert timeline.repeal_date is None
        assert timeline.temporal_status == "in_force"

    def test_no_rows_returns_empty_with_uri(self):
        from app.analyysikeskus.history import get_act_timeline

        stub = MagicMock()
        stub.query.return_value = []
        timeline = get_act_timeline(_ACT_URI, sparql_client=stub)
        assert timeline.act_uri == _ACT_URI
        assert timeline.entry_into_force is None

    def test_blank_uri_yields_empty(self):
        from app.analyysikeskus.history import ActTimeline, get_act_timeline

        stub = MagicMock()
        assert get_act_timeline("", sparql_client=stub) == ActTimeline()
        stub.query.assert_not_called()

    def test_sparql_error_returns_empty_with_uri(self):
        from app.analyysikeskus.history import get_act_timeline

        stub = MagicMock()
        stub.query.side_effect = RuntimeError("boom")
        timeline = get_act_timeline(_ACT_URI, sparql_client=stub)
        assert timeline.act_uri == _ACT_URI
        assert timeline.entry_into_force is None


class TestActLiteralFor:
    """Internal helper that coerces URI input to a literal act title."""

    def test_literal_input_short_circuits(self):
        from app.analyysikeskus.history import _act_literal_for

        stub = MagicMock()
        assert _act_literal_for("Avaliku teabe seadus", client=stub) == "Avaliku teabe seadus"
        # No SPARQL call needed for an already-literal title.
        stub.query.assert_not_called()

    def test_uri_input_reverse_looks_up_label(self):
        from app.analyysikeskus.history import _act_literal_for

        stub = MagicMock()
        stub.query.return_value = [{"label": "Avaliku teabe seadus"}]
        assert _act_literal_for(_ACT_URI, client=stub) == "Avaliku teabe seadus"
        stub.query.assert_called_once()

    def test_uri_with_no_label_returns_empty(self):
        from app.analyysikeskus.history import _act_literal_for

        stub = MagicMock()
        stub.query.return_value = []
        assert _act_literal_for(_ACT_URI, client=stub) == ""

    def test_blank_input_returns_empty(self):
        from app.analyysikeskus.history import _act_literal_for

        stub = MagicMock()
        assert _act_literal_for("", client=stub) == ""
        assert _act_literal_for("   ", client=stub) == ""
        stub.query.assert_not_called()


class TestAmendmentEventsLiteralContract:
    """The prod-shape join contract: bind ``?actLit`` as a string literal."""

    def test_literal_act_title_uses_string_binding(self):
        """A literal-title input flows straight to ``bindings={'actLit': ...}``."""
        from app.analyysikeskus.history import list_amendment_events

        stub = MagicMock()
        stub.query.return_value = [
            {
                "event": _EVENT_URI,
                "eventLabel": "Amendment Event 1",
                "eventDate": "2023-03-15",
                "rtReference": "RT I, 04.01.2023, 12",
                "affectedProvision": _PROVISION_URI,
                "affectedLabel": "Provision 1",
            }
        ]

        rows = list_amendment_events(_PROVISION_URI, "Avaliku teabe seadus", sparql_client=stub)
        assert len(rows) == 1
        assert rows[0].event_uri == _EVENT_URI
        # Exactly one SPARQL call (no rdfs:label reverse-lookup pre-step).
        assert stub.query.call_count == 1
        kwargs = stub.query.call_args.kwargs
        # ``?inputUri`` is bound as a URI; ``?actLit`` as a string literal.
        assert kwargs.get("uri_bindings") == {"inputUri": _PROVISION_URI}
        assert kwargs.get("bindings") == {"actLit": "Avaliku teabe seadus"}

    def test_amendment_query_drops_partof(self):
        from app.analyysikeskus.history import _AMENDMENT_EVENTS_QUERY

        assert "estleg:partOf" not in _AMENDMENT_EVENTS_QUERY
        assert "estleg:partOfAct" not in _AMENDMENT_EVENTS_QUERY
        # And the new sibling-via-sourceAct arm is present.
        assert "?actLit" in _AMENDMENT_EVENTS_QUERY
        assert "estleg:sourceAct ?actLit" in _AMENDMENT_EVENTS_QUERY


class TestListAmendmentEvents:
    def test_aggregates_per_event(self):
        from app.analyysikeskus.history import list_amendment_events

        stub = MagicMock()
        # Two raw rows for the same event but different affected provisions.
        stub.query.return_value = [
            {
                "event": _EVENT_URI,
                "eventLabel": "Amendment Event 1",
                "eventDate": "2023-03-15",
                "entryIntoForceDate": "2023-04-01",
                "rtReference": "RT I, 04.01.2023, 12",
                "affectedProvision": _PROVISION_URI,
                "affectedLabel": "Provision 1",
            },
            {
                "event": _EVENT_URI,
                "eventLabel": "Amendment Event 1",
                "eventDate": "2023-03-15",
                "entryIntoForceDate": "2023-04-01",
                "rtReference": "RT I, 04.01.2023, 12",
                "affectedProvision": "https://data.riik.ee/ontology/estleg#Provision_X",
                "affectedLabel": "Provision X",
            },
        ]
        rows = list_amendment_events(_PROVISION_URI, _ACT_URI, sparql_client=stub)
        assert len(rows) == 1
        ev = rows[0]
        assert ev.event_uri == _EVENT_URI
        assert ev.event_date == date(2023, 3, 15)
        assert ev.entry_into_force_date == date(2023, 4, 1)
        assert ev.rt_reference == "RT I, 04.01.2023, 12"
        # Both affected provisions surface, ordered by first-seen.
        assert [u for u, _ in ev.affected_provisions] == [
            _PROVISION_URI,
            "https://data.riik.ee/ontology/estleg#Provision_X",
        ]

    def test_blank_input_returns_empty(self):
        from app.analyysikeskus.history import list_amendment_events

        stub = MagicMock()
        assert list_amendment_events("", _ACT_URI, sparql_client=stub) == []
        stub.query.assert_not_called()

    def test_sparql_error_returns_empty(self):
        from app.analyysikeskus.history import list_amendment_events

        stub = MagicMock()
        stub.query.side_effect = RuntimeError("dead")
        assert list_amendment_events(_PROVISION_URI, _ACT_URI, sparql_client=stub) == []


class TestCourtDecisionsLiteralContract:
    """The prod-shape join contract for the court-decisions query."""

    def test_literal_act_title_uses_string_binding(self):
        from app.analyysikeskus.history import list_court_decisions

        stub = MagicMock()
        stub.query.return_value = [
            {
                "decision": _COURT_URI,
                "decisionLabel": "Court Decision 1",
                "decisionDate": "2022-06-10",
                "interpretsUri": _PROVISION_URI,
                "interpretsLabel": "Provision 1",
            }
        ]

        rows = list_court_decisions(_PROVISION_URI, "Avaliku teabe seadus", sparql_client=stub)
        assert len(rows) == 1
        assert stub.query.call_count == 1
        kwargs = stub.query.call_args.kwargs
        assert kwargs.get("uri_bindings") == {"inputUri": _PROVISION_URI}
        assert kwargs.get("bindings") == {"actLit": "Avaliku teabe seadus"}

    def test_court_query_drops_partof(self):
        from app.analyysikeskus.history import _COURT_DECISIONS_QUERY

        assert "estleg:partOf" not in _COURT_DECISIONS_QUERY
        assert "estleg:partOfAct" not in _COURT_DECISIONS_QUERY
        assert "?actLit" in _COURT_DECISIONS_QUERY
        assert "estleg:sourceAct ?actLit" in _COURT_DECISIONS_QUERY


class TestPendingDraftsLiteralContract:
    """The prod-shape join contract for the pending-drafts query."""

    def test_literal_act_title_uses_string_binding(self):
        from app.analyysikeskus.history import list_pending_drafts

        stub = MagicMock()
        stub.query.return_value = [
            {
                "draft": _DRAFT_URI,
                "draftLabel": "Eelnõu 1",
                "draftType": "DraftLegislation",
                "submittedDate": "2024-02-20",
            }
        ]

        rows = list_pending_drafts(_PROVISION_URI, "Avaliku teabe seadus", sparql_client=stub)
        assert len(rows) == 1
        assert rows[0].draft_uri == _DRAFT_URI
        assert stub.query.call_count == 1
        kwargs = stub.query.call_args.kwargs
        assert kwargs.get("uri_bindings") == {"inputUri": _PROVISION_URI}
        assert kwargs.get("bindings") == {"actLit": "Avaliku teabe seadus"}

    def test_drafts_query_drops_partof_and_act_uri_arm(self):
        from app.analyysikeskus.history import _PENDING_DRAFTS_QUERY

        assert "estleg:partOf" not in _PENDING_DRAFTS_QUERY
        assert "estleg:partOfAct" not in _PENDING_DRAFTS_QUERY
        # The legacy ``?draft estleg:amends ?actUri`` arm is gone.
        assert "?actUri" not in _PENDING_DRAFTS_QUERY
        assert "?actLit" in _PENDING_DRAFTS_QUERY


class TestHistoryBundleProdShape:
    """End-to-end regression: the bundle flow against prod-shaped data.

    Walks a Provision URI through ``get_history_bundle`` using
    prod-shape SPARQL responses (``sourceAct`` returns a literal title;
    no ``partOf`` rows; no atomic act URI for the timeline). The
    amendment chain must still surface through the new literal-join.
    """

    def test_provision_amendment_chain_resolves_through_literal(self):
        from app.analyysikeskus.history import get_history_bundle

        stub_sparql = MagicMock()
        # Prod-shape SPARQL responses:
        # 1. resolve_owning_act → returns the literal title via ?actLabel.
        # 2. get_act_timeline → sees a literal, short-circuits without
        #    a SPARQL call (no atomic act URI exists in prod).
        # 3. amendments query → returns a sibling-affected event row.
        # 4. court query → empty.
        # 5. pending-drafts query → empty.
        stub_sparql.query.side_effect = [
            [{"actLabel": "Avaliku teabe seadus"}],
            [
                {
                    "event": _EVENT_URI,
                    "eventLabel": "AvTS muudatus 2024",
                    "eventDate": "2024-02-15",
                    "rtReference": "RT I, 15.02.2024, 4",
                    "affectedProvision": _PROVISION_URI,
                    "affectedLabel": "AvTS § 35",
                }
            ],
            [],  # court
            [],  # pending
        ]
        stub_conn = MagicMock()
        stub_cur = MagicMock()
        stub_conn.cursor.return_value.__enter__.return_value = stub_cur
        stub_conn.cursor.return_value.__exit__.return_value = None
        stub_cur.fetchall.return_value = []

        bundle = get_history_bundle(
            _PROVISION_URI,
            input_type="provision",
            sparql_client=stub_sparql,
            db_connection=stub_conn,
        )

        # The act-level envelope holds the literal title (no atomic
        # Act URI exists in prod, so the timeline call short-circuits
        # without a SPARQL hit).
        assert bundle.act_timeline.act_label == "Avaliku teabe seadus"
        assert bundle.act_timeline.entry_into_force is None
        # The amendment chain resolved through the literal-join shape.
        assert len(bundle.amendments) == 1
        assert bundle.amendments[0].rt_reference == "RT I, 15.02.2024, 4"
        # Exactly 4 SPARQL calls (owning-act + 3 section queries; the
        # timeline call is skipped for the literal-title branch).
        assert stub_sparql.query.call_count == 4
        # All section queries received the literal title via
        # string-binding, not URI-binding.
        for call in stub_sparql.query.call_args_list[1:]:
            kwargs = call.kwargs
            assert kwargs.get("bindings") == {"actLit": "Avaliku teabe seadus"}


class TestListCourtDecisions:
    def test_parses_and_dedups(self):
        from app.analyysikeskus.history import list_court_decisions

        stub = MagicMock()
        stub.query.return_value = [
            {
                "decision": _COURT_URI,
                "decisionLabel": "Court Decision 1",
                "decisionDate": "2022-06-10",
                "interpretsUri": _PROVISION_URI,
                "interpretsLabel": "Provision 1",
            },
            # Duplicate (same decision URI) → dropped.
            {
                "decision": _COURT_URI,
                "decisionLabel": "Court Decision 1",
                "decisionDate": "2022-06-10",
                "interpretsUri": _PROVISION_URI,
                "interpretsLabel": "Provision 1",
            },
        ]
        rows = list_court_decisions(_PROVISION_URI, _ACT_URI, sparql_client=stub)
        assert len(rows) == 1
        d = rows[0]
        assert d.decision_uri == _COURT_URI
        assert d.decision_date == date(2022, 6, 10)
        assert d.interprets_label == "Provision 1"

    def test_blank_input_returns_empty(self):
        from app.analyysikeskus.history import list_court_decisions

        stub = MagicMock()
        assert list_court_decisions("", "", sparql_client=stub) == []
        stub.query.assert_not_called()


class TestListPendingDrafts:
    def test_parses_and_sorts_newest_first(self):
        from app.analyysikeskus.history import list_pending_drafts

        stub = MagicMock()
        stub.query.return_value = [
            {
                "draft": "https://data.riik.ee/ontology/estleg#Draft_old",
                "draftLabel": "Old draft",
                "draftType": "DraftLegislation",
                "submittedDate": "2020-01-01",
            },
            {
                "draft": _DRAFT_URI,
                "draftLabel": "Eelnõu 1",
                "draftType": "DraftLegislation",
                "submittedDate": "2024-02-20",
            },
        ]
        rows = list_pending_drafts(_PROVISION_URI, _ACT_URI, sparql_client=stub)
        assert [r.draft_uri for r in rows] == [
            _DRAFT_URI,
            "https://data.riik.ee/ontology/estleg#Draft_old",
        ]
        assert rows[0].submitted_date == date(2024, 2, 20)

    def test_sparql_error_returns_empty(self):
        from app.analyysikeskus.history import list_pending_drafts

        stub = MagicMock()
        stub.query.side_effect = RuntimeError("boom")
        assert list_pending_drafts(_PROVISION_URI, _ACT_URI, sparql_client=stub) == []


class TestListImpactReports:
    def test_returns_parsed_rows(self):
        from app.analyysikeskus.history import list_impact_reports

        stub_conn = MagicMock()
        stub_cur = MagicMock()
        stub_conn.cursor.return_value.__enter__.return_value = stub_cur
        stub_conn.cursor.return_value.__exit__.return_value = None
        gen_at = datetime(2024, 3, 1, 12, 30)
        stub_cur.fetchall.return_value = [
            (
                "11111111-1111-1111-1111-111111111111",  # report id
                "22222222-2222-2222-2222-222222222222",  # draft id
                "Eelnõu pealkiri",
                gen_at,
                3,
            )
        ]
        rows = list_impact_reports(_PROVISION_URI, db_connection=stub_conn)
        assert len(rows) == 1
        r = rows[0]
        assert r.report_id == "11111111-1111-1111-1111-111111111111"
        assert r.draft_id == "22222222-2222-2222-2222-222222222222"
        assert r.draft_title == "Eelnõu pealkiri"
        assert r.version_number == 3
        assert r.generated_at == gen_at

    def test_blank_uri_skips_query(self):
        from app.analyysikeskus.history import list_impact_reports

        stub_conn = MagicMock()
        assert list_impact_reports("", db_connection=stub_conn) == []
        stub_conn.cursor.assert_not_called()

    def test_db_error_returns_empty(self):
        from app.analyysikeskus.history import list_impact_reports

        stub_conn = MagicMock()
        stub_conn.cursor.side_effect = RuntimeError("db down")
        assert list_impact_reports(_PROVISION_URI, db_connection=stub_conn) == []


class TestGetHistoryBundle:
    def test_aggregates_all_sections_for_provision(self):
        from app.analyysikeskus.history import get_history_bundle

        stub_sparql = MagicMock()
        # Per-call SPARQL responses, in order of the helpers invoked:
        # resolve_owning_act, get_act_timeline, list_amendment_events,
        # list_court_decisions, list_pending_drafts.
        stub_sparql.query.side_effect = [
            [{"act": _ACT_URI}],  # owning-act
            [  # act timeline
                {
                    "act": _ACT_URI,
                    "actLabel": "Act 1",
                    "entryIntoForce": "2010-01-01",
                    "temporalStatus": "in_force",
                }
            ],
            [  # amendments
                {
                    "event": _EVENT_URI,
                    "eventLabel": "Amendment Event 1",
                    "eventDate": "2023-03-15",
                    "rtReference": "RT I, 04.01.2023, 12",
                    "affectedProvision": _PROVISION_URI,
                    "affectedLabel": "Provision 1",
                }
            ],
            [  # court decisions
                {
                    "decision": _COURT_URI,
                    "decisionLabel": "Court Decision 1",
                    "decisionDate": "2022-06-10",
                    "interpretsUri": _PROVISION_URI,
                }
            ],
            [  # pending drafts
                {
                    "draft": _DRAFT_URI,
                    "draftLabel": "Eelnõu 1",
                    "draftType": "DraftLegislation",
                    "submittedDate": "2024-02-20",
                }
            ],
        ]
        # DB returns no impact reports for simplicity.
        stub_conn = MagicMock()
        stub_cur = MagicMock()
        stub_conn.cursor.return_value.__enter__.return_value = stub_cur
        stub_conn.cursor.return_value.__exit__.return_value = None
        stub_cur.fetchall.return_value = []

        bundle = get_history_bundle(
            _PROVISION_URI,
            input_type="provision",
            sparql_client=stub_sparql,
            db_connection=stub_conn,
        )
        assert bundle.input_type == "provision"
        assert bundle.act_timeline.act_uri == _ACT_URI
        assert len(bundle.amendments) == 1
        assert len(bundle.court_decisions) == 1
        assert len(bundle.pending_drafts) == 1
        assert bundle.impact_reports == []

    def test_act_input_uses_self_as_act(self):
        """An act-typed input should NOT call resolve_owning_act (uses input itself)."""
        from app.analyysikeskus.history import get_history_bundle

        stub_sparql = MagicMock()
        # 4 queries: timeline / amendments / court / pending (no owning-act resolution).
        stub_sparql.query.side_effect = [[], [], [], []]
        stub_conn = MagicMock()
        stub_cur = MagicMock()
        stub_conn.cursor.return_value.__enter__.return_value = stub_cur
        stub_conn.cursor.return_value.__exit__.return_value = None
        stub_cur.fetchall.return_value = []

        bundle = get_history_bundle(
            _ACT_URI,
            input_type="act",
            sparql_client=stub_sparql,
            db_connection=stub_conn,
        )
        assert bundle.input_type == "act"
        # Exactly 4 SPARQL calls (timeline / amendments / court / pending).
        assert stub_sparql.query.call_count == 4

    def test_blank_input_short_circuits(self):
        from app.analyysikeskus.history import get_history_bundle

        bundle = get_history_bundle("", input_type="provision")
        assert bundle.input_uri == ""
        assert bundle.amendments == []


# ---------------------------------------------------------------------------
# 3. rdflib end-to-end regression — SPARQL queries run against canonical fixture
# ---------------------------------------------------------------------------


class TestAgainstCanonicalFixture:
    """Run the A4 queries against an in-memory rdflib graph loaded from the
    extended canonical fixture so the templates are exercised end-to-end."""

    @pytest.fixture
    def graph(self):
        try:
            from rdflib import Graph
        except ImportError:
            pytest.skip("rdflib not installed")
        fixture = Path(__file__).parent / "fixtures" / "ontology_canonical.ttl"
        g = Graph()
        g.parse(str(fixture), format="turtle")
        return g

    def test_act_timeline_query_runs(self, graph):
        from rdflib import URIRef

        from app.analyysikeskus.history import _ACT_TIMELINE_QUERY

        query = _ACT_TIMELINE_QUERY + f"\nVALUES ?act {{ <{_ACT_URI}> }}\n"
        # rdflib doesn't accept extra VALUES after WHERE close — inject
        # before the trailing brace like the SparqlClient does.
        last_brace = _ACT_TIMELINE_QUERY.rfind("}")
        query = (
            _ACT_TIMELINE_QUERY[:last_brace]
            + f"\nVALUES ?act {{ <{_ACT_URI}> }}\n"
            + _ACT_TIMELINE_QUERY[last_brace:]
        )
        rows = list(graph.query(query))
        assert any(row.entryIntoForce is not None for row in rows)
        # And the act label is in the row.
        labels = {str(r.actLabel) for r in rows if r.actLabel}
        assert any("Act 1" in lbl for lbl in labels)
        # Silence unused import warning.
        assert URIRef

    def test_amendment_events_query_finds_event(self, graph):
        from app.analyysikeskus.history import _AMENDMENT_EVENTS_QUERY

        last_brace = _AMENDMENT_EVENTS_QUERY.rfind("}")
        query = (
            _AMENDMENT_EVENTS_QUERY[:last_brace]
            + f"\nVALUES ?inputUri {{ <{_PROVISION_URI}> }}\n"
            + f"VALUES ?actUri {{ <{_ACT_URI}> }}\n"
            + _AMENDMENT_EVENTS_QUERY[last_brace:]
        )
        rows = list(graph.query(query))
        # AmendmentEvent_1 amends Provision_1 — must appear.
        event_uris = {str(r.event) for r in rows}
        assert _EVENT_URI in event_uris

    def test_pending_drafts_query_finds_draft(self, graph):
        from app.analyysikeskus.history import _PENDING_DRAFTS_QUERY

        last_brace = _PENDING_DRAFTS_QUERY.rfind("}")
        query = (
            _PENDING_DRAFTS_QUERY[:last_brace]
            + f"\nVALUES ?inputUri {{ <{_PROVISION_URI}> }}\n"
            + f"VALUES ?actUri {{ <{_ACT_URI}> }}\n"
            + _PENDING_DRAFTS_QUERY[last_brace:]
        )
        rows = list(graph.query(query))
        assert any(str(r.draft) == _DRAFT_URI for r in rows)


# ---------------------------------------------------------------------------
# 4. Route smoke tests (test client end-to-end)
# ---------------------------------------------------------------------------


def _authed_user() -> dict[str, Any]:
    return {
        "id": "33333333-3333-3333-3333-333333333333",
        "email": "kasutaja@seadusloome.ee",
        "full_name": "Test Kasutaja",
        "role": "drafter",
        "org_id": "11111111-1111-1111-1111-111111111111",
    }


def _stub_provider() -> MagicMock:
    provider = MagicMock()
    provider.get_current_user.return_value = _authed_user()
    return provider


def _authed_client(*, raise_server_exceptions: bool = True):
    from starlette.testclient import TestClient

    client = TestClient(
        __import__("app.main", fromlist=["app"]).app,
        follow_redirects=False,
        raise_server_exceptions=raise_server_exceptions,
    )
    client.cookies.set("access_token", "stub-token")
    return client


def _canned_resolved_provision_ref():
    from app.docs.entity_extractor import ExtractedRef
    from app.docs.reference_resolver import ResolvedRef

    return ResolvedRef(
        extracted=ExtractedRef(
            ref_text="KarS § 211",
            ref_type="provision",
            confidence=1.0,
            location={"source": "analyysikeskus_input"},
        ),
        entity_uri=_KARS_P211_URI,
        matched_label="KarS § 211 — Karistusseadustik",
        match_score=1.0,
    )


def _canned_resolved_law_ref():
    """Build a ResolvedRef matching the real Wave 2 Step 2 resolver shape.

    Post-Wave-2 the resolver returns ``entity_uri=None`` for law-only
    refs and rides the canonical act title literal on ``partial_match``.
    The route picks that title up and passes it to ``get_history_bundle``
    with ``input_type="act"``.
    """
    from app.docs.entity_extractor import ExtractedRef
    from app.docs.reference_resolver import ResolvedRef

    return ResolvedRef(
        extracted=ExtractedRef(
            ref_text="KarS",
            ref_type="law",
            confidence=1.0,
            location={"source": "analyysikeskus_input"},
        ),
        entity_uri=None,
        matched_label="Karistusseadustik",
        match_score=1.0,
        partial_match={
            "act_token": "KRIMIN",
            "act_title": "Karistusseadustik",
            "section": None,
        },
    )


def _canned_bundle_with_amendment():
    """A HistoryBundle with one AmendmentEvent so the Tõendid row renders."""
    from app.analyysikeskus.history import (
        ActTimeline,
        AmendmentEventRow,
        HistoryBundle,
    )

    return HistoryBundle(
        input_uri=_KARS_P211_URI,
        input_type="provision",
        act_timeline=ActTimeline(
            act_uri=_KARS_URI,
            act_label="Karistusseadustik",
            entry_into_force=date(2002, 9, 1),
            last_amendment_date=date(2023, 3, 15),
            temporal_status="in_force",
        ),
        amendments=[
            AmendmentEventRow(
                event_uri="https://data.riik.ee/ontology/estleg#KarS-Amendment-2023",
                event_label="KarS § 211 muudatus",
                event_date=date(2023, 3, 15),
                entry_into_force_date=date(2023, 4, 1),
                rt_reference="RT I, 04.01.2023, 12",
                affected_provisions=[(_KARS_P211_URI, "KarS § 211")],
            )
        ],
    )


def _canned_bundle_act_input():
    from app.analyysikeskus.history import ActTimeline, HistoryBundle

    return HistoryBundle(
        input_uri=_KARS_URI,
        input_type="act",
        act_timeline=ActTimeline(
            act_uri=_KARS_URI,
            act_label="Karistusseadustik",
            entry_into_force=date(2002, 9, 1),
            temporal_status="in_force",
        ),
    )


def test_ajalugu_redirects_unauthenticated():
    from starlette.testclient import TestClient

    from app.main import app

    client = TestClient(app, follow_redirects=False)
    resp = client.get("/analyysikeskus/ajalugu")
    assert resp.status_code == 303
    assert resp.headers["location"] == "/auth/login"


@patch("app.auth.middleware._get_provider")
def test_ajalugu_landing_renders_input_form(mock_provider: MagicMock):
    mock_provider.return_value = _stub_provider()
    client = _authed_client()
    resp = client.get("/analyysikeskus/ajalugu")
    assert resp.status_code == 200
    body = resp.text
    assert "Ajalooline kehtivus" in body
    for heading in ("Sisend", "Ulatus", "Tulemused", "Tõendid", "Soovitatud tegevused"):
        assert heading in body, heading
    assert 'action="/analyysikeskus/ajalugu"' in body
    assert "Vaata ajalugu" in body
    assert "Sisestage päring" in body
    # Banner is NOT rendered on the landing page.
    assert "Ainult akti tasandi ajalugu" not in body


@patch("app.analyysikeskus.routes._ajalugu.get_history_bundle")
@patch("app.docs.reference_resolver.ReferenceResolver.resolve")
@patch("app.auth.middleware._get_provider")
def test_ajalugu_provision_input_shows_banner(
    mock_provider: MagicMock,
    mock_resolve: MagicMock,
    mock_bundle: MagicMock,
):
    """CRITICAL — provision input MUST render the v1 limitation banner."""
    mock_provider.return_value = _stub_provider()
    mock_resolve.return_value = [_canned_resolved_provision_ref()]
    mock_bundle.return_value = _canned_bundle_with_amendment()

    client = _authed_client()
    resp = client.get("/analyysikeskus/ajalugu?sisend=KarS+%C2%A7+211")
    assert resp.status_code == 200
    body = resp.text

    # The banner heading + body copy must be present, verbatim.
    assert "Ainult akti tasandi ajalugu" in body
    assert "Sätte tasandi versioonid" in body
    assert "github.com/henrikaavik/estonian-legal-ontology/issues/208" in body

    # Resolved label appears in Sisend.
    assert "KarS § 211 — Karistusseadustik" in body

    # The amendment row surfaces in Muudatused.
    assert "Muudatused" in body
    assert "RT I, 04.01.2023, 12" in body

    # The Tõendid row + "Küsi nõustajalt" form is present.
    assert 'action="/chat/seed"' in body
    assert "Küsi nõustajalt" in body

    # The bundle was called with input_type="provision".
    args, kwargs = mock_bundle.call_args
    assert kwargs.get("input_type") == "provision"


@patch("app.analyysikeskus.routes._ajalugu.get_history_bundle")
@patch("app.docs.reference_resolver.ReferenceResolver.resolve")
@patch("app.auth.middleware._get_provider")
def test_ajalugu_act_input_hides_banner(
    mock_provider: MagicMock,
    mock_resolve: MagicMock,
    mock_bundle: MagicMock,
):
    """CRITICAL — act-level input must NOT render the v1 limitation banner."""
    mock_provider.return_value = _stub_provider()
    mock_resolve.return_value = [_canned_resolved_law_ref()]
    mock_bundle.return_value = _canned_bundle_act_input()

    client = _authed_client()
    # Use "KarS § 211" so the parser emits structured refs; the
    # resolver mock returns only the law-typed ref so the route
    # enters the act-input branch.
    resp = client.get("/analyysikeskus/ajalugu?sisend=KarS+%C2%A7+211")
    assert resp.status_code == 200
    body = resp.text

    # Banner heading must NOT appear.
    assert "Ainult akti tasandi ajalugu" not in body

    # But the page still renders the 5-card shell.
    for heading in ("Sisend", "Ulatus", "Tulemused", "Tõendid", "Soovitatud tegevused"):
        assert heading in body, heading

    # Bundle was called with input_type="act".
    args, kwargs = mock_bundle.call_args
    assert kwargs.get("input_type") == "act"


@patch("app.analyysikeskus.routes._ajalugu.get_history_bundle")
@patch("app.analyysikeskus.routes._ajalugu._rag_candidates", return_value=[])
@patch("app.docs.reference_resolver.ReferenceResolver.resolve")
@patch("app.auth.middleware._get_provider")
def test_ajalugu_bare_law_input_routes_to_act_bundle(
    mock_provider: MagicMock,
    mock_resolve: MagicMock,
    mock_rag: MagicMock,
    mock_bundle: MagicMock,
):
    """Wave 2 Step 5: bare law sisend (``KarS``) routes to act-level bundle.

    The route picks the act title from the resolver's ``partial_match``
    payload and passes it to ``get_history_bundle`` with
    ``input_type="act"``. The route does NOT fall through to the
    unresolved/RAG branch.
    """
    mock_provider.return_value = _stub_provider()
    mock_resolve.return_value = [_canned_resolved_law_ref()]
    mock_bundle.return_value = _canned_bundle_act_input()

    client = _authed_client()
    resp = client.get("/analyysikeskus/ajalugu?sisend=KarS")
    assert resp.status_code == 200
    body = resp.text

    # Bundle was invoked with the literal title and input_type="act".
    mock_bundle.assert_called_once()
    args, kwargs = mock_bundle.call_args
    # First positional arg is the bundle input (title literal, not URI).
    bundle_arg = args[0] if args else kwargs.get("input_uri")
    assert bundle_arg == "Karistusseadustik"
    assert kwargs.get("input_type") == "act"

    # The route did not fall through to the unresolved/RAG branch.
    mock_rag.assert_not_called()
    assert "Ei tuvastanud õiguslikku viidet" not in body


@patch("app.analyysikeskus.routes._ajalugu.get_history_bundle")
@patch("app.docs.reference_resolver.ReferenceResolver.resolve")
@patch("app.auth.middleware._get_provider")
def test_ajalugu_disambiguation_when_multiple_resolutions(
    mock_provider: MagicMock,
    mock_resolve: MagicMock,
    mock_bundle: MagicMock,
):
    """Multiple distinct URI-resolved refs ⇒ disambiguation card.

    Wave 2 Step 5: the route now prefers URI-resolved refs over
    partial-match refs when both are present. To force the
    disambiguation branch we mock TWO distinct URI-resolved refs.
    """
    from app.docs.entity_extractor import ExtractedRef
    from app.docs.reference_resolver import ResolvedRef

    other_uri = "https://data.riik.ee/ontology/estleg#KarS-p133"
    other_ref = ResolvedRef(
        extracted=ExtractedRef(
            ref_text="KarS § 133",
            ref_type="provision",
            confidence=1.0,
            location={"source": "analyysikeskus_input"},
        ),
        entity_uri=other_uri,
        matched_label="KarS § 133 — Karistusseadustik",
        match_score=1.0,
    )

    mock_provider.return_value = _stub_provider()
    mock_resolve.return_value = [
        _canned_resolved_provision_ref(),
        other_ref,
    ]

    client = _authed_client()
    resp = client.get("/analyysikeskus/ajalugu?sisend=KarS+%C2%A7+211")
    assert resp.status_code == 200
    body = resp.text
    assert "Sisend võib viidata mitmele üksusele" in body
    assert "KarS § 211 — Karistusseadustik" in body
    assert "KarS § 133 — Karistusseadustik" in body
    mock_bundle.assert_not_called()


@patch("app.analyysikeskus.routes._ajalugu._rag_candidates", return_value=[])
@patch("app.docs.reference_resolver.ReferenceResolver.resolve", return_value=[])
@patch("app.auth.middleware._get_provider")
def test_ajalugu_unresolved_input_shows_warning(
    mock_provider: MagicMock,
    mock_resolve: MagicMock,
    mock_rag: MagicMock,
):
    mock_provider.return_value = _stub_provider()
    client = _authed_client()
    resp = client.get("/analyysikeskus/ajalugu?sisend=mingi+suvaline+jutt")
    assert resp.status_code == 200
    body = resp.text
    assert "Ei tuvastanud õiguslikku viidet" in body
    for heading in ("Sisend", "Ulatus", "Tulemused", "Tõendid", "Soovitatud tegevused"):
        assert heading in body, heading


# ---------------------------------------------------------------------------
# 5. Capability dictionary + route registration
# ---------------------------------------------------------------------------


def test_ajalugu_capability_is_live():
    """The ``ajalugu`` capability must be live (status field absent ⇒ live)."""
    from app.ui.capabilities import get_capability

    cap = get_capability("ajalugu")
    assert cap is not None
    assert cap.status == "live"
    assert cap.target_url == "/analyysikeskus/ajalugu"


def test_existing_workflows_still_registered():
    """A4 only appends — the existing routes must still respond."""
    from starlette.testclient import TestClient

    from app.main import app

    client = TestClient(app, follow_redirects=False)
    for path in (
        "/analyysikeskus",
        "/analyysikeskus/normi-mojuahel",
        "/analyysikeskus/el-ulevott",
        "/analyysikeskus/sanktsioonid",
        "/analyysikeskus/ajalugu",
    ):
        resp = client.get(path)
        # All five redirect to login (auth gate) when unauthenticated.
        assert resp.status_code == 303, path
        assert resp.headers["location"] == "/auth/login", path
