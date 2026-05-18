"""Tests for the Kohtupraktika sätte kohta workflow (C3, plan section 5).

Covers:

1. The SPARQL helper layer in :mod:`app.analyysikeskus.court_practice` —
   row → :class:`CourtDecisionRow` conversion, empty / dead-Jena paths,
   the provision / act query shapes, and the canonical-predicate usage.
2. The court-classification + grouping helpers — bucket assignment from
   type URI / court label / case number, citation counts, sparse
   year-bucket trends.
3. Fixture-graph SPARQL — runs the two queries against an in-memory
   rdflib graph seeded with the canonical fixture (with CourtDecision +
   interpretsLaw edges added) so the templates prove they actually fire
   on a real triplestore.
4. The route layer in :mod:`app.analyysikeskus.routes` — the
   ``/analyysikeskus/kohtupraktika`` endpoint: the auth gate, the
   landing page (no ``sisend``), the resolved-provision happy path with
   multi-court grouping + citation counts, the empty-state, the
   disambiguation branch, the unresolved branch, and the scope-carrying
   URL pattern.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

# ---------------------------------------------------------------------------
# Test fixtures — shared CourtDecisionRow instances and SPARQL row stubs
# ---------------------------------------------------------------------------

_AVTS_URI = "https://data.riik.ee/ontology/estleg#avts"
_AVTS_P35_URI = "https://data.riik.ee/ontology/estleg#AvTS-p35"

_DEC_1_URI = "https://data.riik.ee/ontology/estleg#CourtDecision_RK_1"
_DEC_2_URI = "https://data.riik.ee/ontology/estleg#CourtDecision_RK_2"
_DEC_3_URI = "https://data.riik.ee/ontology/estleg#CourtDecision_EU_1"
_DEC_4_URI = "https://data.riik.ee/ontology/estleg#CourtDecision_RING_1"

_RIIGIKOHUS_URI = "https://data.riik.ee/ontology/estleg#Court_Riigikohus"
_EUROOPAKOHUS_URI = "https://data.riik.ee/ontology/estleg#Court_EuroopaKohus"
_RINGKONNAKOHUS_URI = "https://data.riik.ee/ontology/estleg#Court_Tallinn_Ringkonnakohus"


def _dec_riigikohus_row(case: str = "3-1-1-63-15", year: str = "2015") -> dict[str, str]:
    return {
        "decision": _DEC_1_URI,
        "decisionLabel": "Riigikohtu lahend 1",
        "caseNumber": case,
        "decisionDate": f"{year}-11-30",
        "court": _RIIGIKOHUS_URI,
        "courtLabel": "Riigikohus",
        "type": "https://data.riik.ee/ontology/estleg#CourtDecision",
        "provision": _AVTS_P35_URI,
        "provisionLabel": "AvTS § 35",
    }


def _dec_riigikohus_row_2() -> dict[str, str]:
    return {
        "decision": _DEC_2_URI,
        "decisionLabel": "Riigikohtu lahend 2",
        "caseNumber": "3-1-1-10-20",
        "decisionDate": "2020-06-12",
        "court": _RIIGIKOHUS_URI,
        "courtLabel": "Riigikohus",
        "type": "https://data.riik.ee/ontology/estleg#CourtDecision",
        "provision": _AVTS_P35_URI,
        "provisionLabel": "AvTS § 35",
    }


def _dec_euroopa_kohus_row() -> dict[str, str]:
    return {
        "decision": _DEC_3_URI,
        "decisionLabel": "EU Court Decision",
        "caseNumber": "C-131/12",
        "decisionDate": "2014-05-13",
        "court": _EUROOPAKOHUS_URI,
        "courtLabel": "Euroopa Kohus",
        "type": "https://data.riik.ee/ontology/estleg#EUCourtDecision",
        "provision": _AVTS_P35_URI,
        "provisionLabel": "AvTS § 35",
    }


def _dec_ringkonnakohus_row() -> dict[str, str]:
    return {
        "decision": _DEC_4_URI,
        "decisionLabel": "Tallinna Ringkonnakohtu lahend",
        "caseNumber": "",
        "decisionDate": "2018-03-22",
        "court": _RINGKONNAKOHUS_URI,
        "courtLabel": "Tallinna Ringkonnakohus",
        "type": "https://data.riik.ee/ontology/estleg#CourtDecision",
        "provision": _AVTS_P35_URI,
        "provisionLabel": "AvTS § 35",
    }


# ---------------------------------------------------------------------------
# 1. classify_court — bucket assignment
# ---------------------------------------------------------------------------


class TestClassifyCourt:
    def test_eu_court_decision_type_wins(self):
        from app.analyysikeskus.court_practice import classify_court

        assert (
            classify_court(
                type_uri="https://data.riik.ee/ontology/estleg#EUCourtDecision",
            )
            == "euroopa_kohus"
        )

    def test_riigikohus_label(self):
        from app.analyysikeskus.court_practice import classify_court

        assert classify_court(court_label="Riigikohus") == "riigikohus"
        assert classify_court(court_label="riigikohus") == "riigikohus"

    def test_ringkonnakohus_label(self):
        from app.analyysikeskus.court_practice import classify_court

        assert classify_court(court_label="Tallinna Ringkonnakohus") == "ringkonnakohus"

    def test_euroopa_kohus_label_variants(self):
        from app.analyysikeskus.court_practice import classify_court

        for label in ("Euroopa Kohus", "EL Kohus", "CJEU", "ECJ", "Court of Justice"):
            assert classify_court(court_label=label) == "euroopa_kohus", label

    def test_ee_case_number_implies_riigikohus(self):
        from app.analyysikeskus.court_practice import classify_court

        assert classify_court(case_number="3-1-1-63-15") == "riigikohus"
        assert classify_court(case_number="5-19-1-2") == "riigikohus"

    def test_eu_case_number_implies_euroopa_kohus(self):
        from app.analyysikeskus.court_practice import classify_court

        assert classify_court(case_number="C-131/12") == "euroopa_kohus"
        assert classify_court(case_number="T-99/04") == "euroopa_kohus"

    def test_unknown_falls_back_to_muu(self):
        from app.analyysikeskus.court_practice import classify_court

        assert classify_court() == "muu"
        assert classify_court(court_label="Mingi muu kohus") == "muu"


# ---------------------------------------------------------------------------
# 2. year_of — defensive date parsing
# ---------------------------------------------------------------------------


class TestYearOf:
    def test_iso_date(self):
        from app.analyysikeskus.court_practice import year_of

        assert year_of("2020-06-12") == 2020

    def test_iso_datetime(self):
        from app.analyysikeskus.court_practice import year_of

        assert year_of("2020-06-12T10:30:00Z") == 2020

    def test_blank_returns_none(self):
        from app.analyysikeskus.court_practice import year_of

        assert year_of("") is None
        assert year_of(" ") is None

    def test_malformed_returns_none(self):
        from app.analyysikeskus.court_practice import year_of

        assert year_of("not a date") is None
        assert year_of("ab12-01-01") is None

    def test_out_of_range_returns_none(self):
        from app.analyysikeskus.court_practice import year_of

        # Older than 1900 — likely a glitch.
        assert year_of("0001-01-01") is None
        assert year_of("3000-01-01") is None


# ---------------------------------------------------------------------------
# 3. SPARQL helpers — list_decisions_for_provision / _for_act
# ---------------------------------------------------------------------------


class TestListDecisionsForProvision:
    def test_returns_parsed_rows(self):
        from app.analyysikeskus.court_practice import list_decisions_for_provision

        stub_client = MagicMock()
        stub_client.query.return_value = [_dec_riigikohus_row()]

        rows = list_decisions_for_provision(_AVTS_P35_URI, sparql_client=stub_client)
        assert len(rows) == 1
        row = rows[0]
        assert row.decision_uri == _DEC_1_URI
        assert row.decision_label == "Riigikohtu lahend 1"
        assert row.case_number == "3-1-1-63-15"
        assert row.decision_date == "2015-11-30"
        assert row.court_uri == _RIIGIKOHUS_URI
        assert row.court_label == "Riigikohus"
        assert row.provision_uri == _AVTS_P35_URI
        assert row.provision_label == "AvTS § 35"
        # Convenience properties.
        assert row.bucket == "riigikohus"
        assert row.year == 2015

    def test_blank_uri_returns_empty_without_hitting_jena(self):
        from app.analyysikeskus.court_practice import list_decisions_for_provision

        stub_client = MagicMock()
        rows = list_decisions_for_provision("", sparql_client=stub_client)
        assert rows == []
        stub_client.query.assert_not_called()

    def test_sparql_error_returns_empty(self):
        from app.analyysikeskus.court_practice import list_decisions_for_provision

        stub_client = MagicMock()
        stub_client.query.side_effect = RuntimeError("jena unreachable")
        rows = list_decisions_for_provision(_AVTS_P35_URI, sparql_client=stub_client)
        assert rows == []

    def test_binds_provision_as_uri(self):
        """The URI must travel via :meth:`SparqlClient._inject_uri_bindings`."""
        from app.analyysikeskus.court_practice import list_decisions_for_provision

        stub_client = MagicMock()
        stub_client.query.return_value = []
        list_decisions_for_provision(_AVTS_P35_URI, sparql_client=stub_client)
        kwargs = stub_client.query.call_args.kwargs
        assert "uri_bindings" in kwargs
        assert kwargs["uri_bindings"] == {"provision": _AVTS_P35_URI}

    def test_dedupes_decision_per_provision(self):
        """A decision returned by both UNION arms only counts once."""
        from app.analyysikeskus.court_practice import list_decisions_for_provision

        stub_client = MagicMock()
        # Same decision URI + provision URI twice — once per UNION direction.
        stub_client.query.return_value = [
            _dec_riigikohus_row(),
            _dec_riigikohus_row(),
        ]
        rows = list_decisions_for_provision(_AVTS_P35_URI, sparql_client=stub_client)
        assert len(rows) == 1

    def test_query_uses_canonical_predicate_uris(self):
        """The SPARQL template must use ``app.ontology.relations.PREDICATES``."""
        from app.analyysikeskus.court_practice import _PROVISION_DECISIONS_QUERY
        from app.ontology.relations import PREDICATES

        assert PREDICATES.INTERPRETS_LAW in _PROVISION_DECISIONS_QUERY
        assert PREDICATES.INTERPRETED_BY in _PROVISION_DECISIONS_QUERY


class TestListDecisionsForAct:
    def test_literal_title_uses_string_binding_directly(self):
        """The prod contract: caller passes a literal act title string.

        No reverse-lookup needed — the literal goes straight into
        ``?actLit`` via ``bindings={"actLit": ...}`` and the SPARQL
        joins ``?provision estleg:sourceAct ?actLit``.
        """
        from app.analyysikeskus.court_practice import list_decisions_for_act

        stub_client = MagicMock()
        stub_client.query.return_value = [_dec_riigikohus_row()]

        rows = list_decisions_for_act("Avaliku teabe seadus", sparql_client=stub_client)
        assert len(rows) == 1
        assert rows[0].decision_uri == _DEC_1_URI
        # Exactly one SPARQL call (no reverse-lookup pre-step for literals).
        assert stub_client.query.call_count == 1
        # And the literal travelled via the string-binding API, not the
        # URI-binding API.
        kwargs = stub_client.query.call_args.kwargs
        assert kwargs.get("bindings") == {"actLit": "Avaliku teabe seadus"}
        assert "uri_bindings" not in kwargs or not kwargs.get("uri_bindings")

    def test_uri_input_reverse_looks_up_label(self):
        """Legacy/fixture contract: URI input ⇒ reverse-lookup ``rdfs:label``.

        The first SPARQL call peeks the URI's label; the second
        decisions query runs with the resolved literal as ``?actLit``.
        """
        from app.analyysikeskus.court_practice import list_decisions_for_act

        stub_client = MagicMock()
        # First call → label lookup; second call → decisions.
        stub_client.query.side_effect = [
            [{"label": "Avaliku teabe seadus"}],
            [_dec_riigikohus_row()],
        ]

        rows = list_decisions_for_act(_AVTS_URI, sparql_client=stub_client)
        assert len(rows) == 1
        assert rows[0].decision_uri == _DEC_1_URI
        assert stub_client.query.call_count == 2
        # Second call uses the resolved literal.
        second_kwargs = stub_client.query.call_args_list[1].kwargs
        assert second_kwargs.get("bindings") == {"actLit": "Avaliku teabe seadus"}

    def test_uri_with_no_label_returns_empty_without_main_query(self):
        """A URI input that has no ``rdfs:label`` ⇒ skip the main query and return ``[]``.

        Prevents a spurious empty-literal join (``?actLit = ""``) that
        could match nothing anyway but should never be sent.
        """
        from app.analyysikeskus.court_practice import list_decisions_for_act

        stub_client = MagicMock()
        # Label lookup returns no rows → resolver returns "".
        stub_client.query.return_value = []

        rows = list_decisions_for_act(_AVTS_URI, sparql_client=stub_client)
        assert rows == []
        # Only the label-lookup query ran — no decisions query.
        assert stub_client.query.call_count == 1

    def test_blank_input_returns_empty(self):
        from app.analyysikeskus.court_practice import list_decisions_for_act

        stub_client = MagicMock()
        rows = list_decisions_for_act("", sparql_client=stub_client)
        assert rows == []
        stub_client.query.assert_not_called()

    def test_dead_jena_returns_empty(self):
        from app.analyysikeskus.court_practice import list_decisions_for_act

        stub_client = MagicMock()
        stub_client.query.side_effect = RuntimeError("jena down")
        rows = list_decisions_for_act("Avaliku teabe seadus", sparql_client=stub_client)
        assert rows == []

    def test_query_uses_canonical_predicate_uris(self):
        from app.analyysikeskus.court_practice import _ACT_DECISIONS_QUERY
        from app.ontology.relations import PREDICATES

        assert PREDICATES.INTERPRETS_LAW in _ACT_DECISIONS_QUERY
        assert PREDICATES.INTERPRETED_BY in _ACT_DECISIONS_QUERY

    def test_query_drops_partof_arm(self):
        """The prod-shape rewrite drops the legacy ``estleg:partOf`` UNION arm.

        Acceptance criterion 1 of the bugfix plan, Step 5: no active
        SPARQL string references ``estleg:partOf`` /
        ``estleg:partOfAct``.
        """
        from app.analyysikeskus.court_practice import _ACT_DECISIONS_QUERY

        assert "estleg:partOf" not in _ACT_DECISIONS_QUERY
        assert "estleg:partOfAct" not in _ACT_DECISIONS_QUERY
        # And the new query uses ``?actLit`` as the literal binding.
        assert "?actLit" in _ACT_DECISIONS_QUERY
        assert "estleg:sourceAct ?actLit" in _ACT_DECISIONS_QUERY


# ---------------------------------------------------------------------------
# 4. group_by_court — multi-court grouping, citation counts, year trends
# ---------------------------------------------------------------------------


class TestGroupByCourt:
    def test_empty_input_returns_empty(self):
        from app.analyysikeskus.court_practice import group_by_court

        assert group_by_court([]) == []

    def test_multi_court_grouping_with_counts_and_trend(self):
        from app.analyysikeskus.court_practice import (
            CourtDecisionRow,
            group_by_court,
        )

        rows = [
            CourtDecisionRow(
                decision_uri=_DEC_1_URI,
                court_label="Riigikohus",
                decision_date="2015-11-30",
            ),
            CourtDecisionRow(
                decision_uri=_DEC_2_URI,
                court_label="Riigikohus",
                decision_date="2020-06-12",
            ),
            CourtDecisionRow(
                decision_uri=_DEC_3_URI,
                type_uri="https://data.riik.ee/ontology/estleg#EUCourtDecision",
                court_label="Euroopa Kohus",
                decision_date="2014-05-13",
            ),
            CourtDecisionRow(
                decision_uri=_DEC_4_URI,
                court_label="Tallinna Ringkonnakohus",
                decision_date="2018-03-22",
            ),
        ]
        groups = group_by_court(rows)

        # Three distinct buckets present, in the canonical order.
        assert [g.bucket for g in groups] == [
            "riigikohus",
            "euroopa_kohus",
            "ringkonnakohus",
        ]

        rk = groups[0]
        assert rk.label_et == "Riigikohus"
        assert rk.citation_count == 2
        # Newest first.
        assert rk.rows[0].decision_uri == _DEC_2_URI
        assert rk.rows[1].decision_uri == _DEC_1_URI
        # Year trend ascending, sparse.
        assert rk.year_trend == {2015: 1, 2020: 1}

        eu = groups[1]
        assert eu.bucket == "euroopa_kohus"
        assert eu.citation_count == 1
        assert eu.year_trend == {2014: 1}

        ring = groups[2]
        assert ring.bucket == "ringkonnakohus"
        assert ring.citation_count == 1

    def test_decision_without_date_sinks_to_bottom(self):
        from app.analyysikeskus.court_practice import (
            CourtDecisionRow,
            group_by_court,
        )

        rows = [
            CourtDecisionRow(
                decision_uri=_DEC_2_URI,
                court_label="Riigikohus",
                decision_date="",  # missing
            ),
            CourtDecisionRow(
                decision_uri=_DEC_1_URI,
                court_label="Riigikohus",
                decision_date="2015-11-30",
            ),
        ]
        groups = group_by_court(rows)
        assert len(groups) == 1
        # Dated row first, undated last.
        assert groups[0].rows[0].decision_uri == _DEC_1_URI
        assert groups[0].rows[1].decision_uri == _DEC_2_URI
        # Year trend excludes the undated row.
        assert groups[0].year_trend == {2015: 1}

    def test_dedupes_same_decision_uri(self):
        """A single decision returned twice (different OPTIONAL bindings)
        is grouped once."""
        from app.analyysikeskus.court_practice import (
            CourtDecisionRow,
            group_by_court,
        )

        rows = [
            CourtDecisionRow(
                decision_uri=_DEC_1_URI,
                court_label="Riigikohus",
                decision_date="2015-11-30",
            ),
            CourtDecisionRow(
                decision_uri=_DEC_1_URI,
                court_label="Riigikohus",
                decision_date="2015-11-30",
            ),
        ]
        groups = group_by_court(rows)
        assert len(groups) == 1
        assert groups[0].citation_count == 1


# ---------------------------------------------------------------------------
# 5. Fixture-graph SPARQL — runs against canonical Turtle via rdflib
# ---------------------------------------------------------------------------


_FIXTURE_PATH = Path(__file__).parent / "fixtures" / "ontology_canonical.ttl"


def _load_fixture_graph():
    from rdflib import Graph

    g = Graph()
    g.parse(str(_FIXTURE_PATH), format="turtle")
    return g


def _query_against_fixture(template: str, *, provision_uri: str | None = None):
    """Run ``template`` against the canonical fixture graph via rdflib.

    Mirrors :class:`SparqlClient._inject_uri_bindings` — injects a
    ``VALUES`` block with the ``<uri>`` literal before the closing brace.
    Returns the raw ``rdflib.query.Result`` so callers can use ``.vars``.
    """
    g = _load_fixture_graph()
    if provision_uri:
        last_brace = template.rfind("}")
        values_block = f"  VALUES ?provision {{ <{provision_uri}> }}"
        template = template[:last_brace] + "\n" + values_block + "\n" + template[last_brace:]
    return g.query(template)


class TestFixtureGraph:
    """End-to-end regressions: the SPARQL templates fire on a real graph."""

    def test_provision_query_returns_court_decisions_for_provision_1(self):
        from app.analyysikeskus.court_practice import _PROVISION_DECISIONS_QUERY

        results = _query_against_fixture(
            _PROVISION_DECISIONS_QUERY,
            provision_uri="https://data.riik.ee/ontology/estleg#Provision_1",
        )
        # The fixture has 3 CourtDecisions tied to Provision_1:
        #   CourtDecision_1 via interpretedBy (inverse direction)
        #   CourtDecision_2 via interpretsLaw (forward)
        #   CourtDecision_3 via interpretsLaw (forward, EUCourtDecision)
        decision_uris = {str(row.decision) for row in results}  # type: ignore[attr-defined,union-attr]
        assert any(uri.endswith("CourtDecision_1") for uri in decision_uris)
        assert any(uri.endswith("CourtDecision_2") for uri in decision_uris)
        assert any(uri.endswith("CourtDecision_3") for uri in decision_uris)

    def test_eu_court_decision_classified_as_euroopa_kohus(self):
        from app.analyysikeskus.court_practice import (
            _PROVISION_DECISIONS_QUERY,
            _rows_to_decisions,
            group_by_court,
        )

        results = _query_against_fixture(
            _PROVISION_DECISIONS_QUERY,
            provision_uri="https://data.riik.ee/ontology/estleg#Provision_1",
        )
        # Convert rdflib rows → dict[str, str] shaped like the SparqlClient
        # JSON extractor would emit.
        as_dicts: list[dict[str, str]] = [
            {
                str(var): str(value)
                for var, value in zip(results.vars or [], row)  # type: ignore[arg-type]
            }
            for row in results
        ]
        rows = _rows_to_decisions(as_dicts)
        groups = group_by_court(rows)
        bucket_keys = [g.bucket for g in groups]
        # At least one EUCourtDecision (CourtDecision_3) must classify as EU.
        assert "euroopa_kohus" in bucket_keys


# ---------------------------------------------------------------------------
# 6. Route smoke tests (test client end-to-end)
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
            ref_text="AvTS § 35",
            ref_type="provision",
            confidence=1.0,
            location={"source": "analyysikeskus_input"},
        ),
        entity_uri=_AVTS_P35_URI,
        matched_label="AvTS § 35 — Avaliku teabe seadus",
        match_score=1.0,
    )


def _canned_resolved_law_ref():
    from app.docs.entity_extractor import ExtractedRef
    from app.docs.reference_resolver import ResolvedRef

    return ResolvedRef(
        extracted=ExtractedRef(
            ref_text="AvTS",
            ref_type="law",
            confidence=1.0,
            location={"source": "analyysikeskus_input"},
        ),
        entity_uri=_AVTS_URI,
        matched_label="Avaliku teabe seadus",
        match_score=1.0,
    )


def _canned_decision_rows():
    from app.analyysikeskus.court_practice import CourtDecisionRow

    return [
        CourtDecisionRow(
            decision_uri=_DEC_1_URI,
            decision_label="Riigikohtu lahend 1",
            case_number="3-1-1-63-15",
            decision_date="2015-11-30",
            court_uri=_RIIGIKOHUS_URI,
            court_label="Riigikohus",
            type_uri="https://data.riik.ee/ontology/estleg#CourtDecision",
            provision_uri=_AVTS_P35_URI,
            provision_label="AvTS § 35",
        ),
        CourtDecisionRow(
            decision_uri=_DEC_2_URI,
            decision_label="Riigikohtu lahend 2",
            case_number="3-1-1-10-20",
            decision_date="2020-06-12",
            court_uri=_RIIGIKOHUS_URI,
            court_label="Riigikohus",
            type_uri="https://data.riik.ee/ontology/estleg#CourtDecision",
            provision_uri=_AVTS_P35_URI,
            provision_label="AvTS § 35",
        ),
        CourtDecisionRow(
            decision_uri=_DEC_3_URI,
            decision_label="EU Court Decision",
            case_number="C-131/12",
            decision_date="2014-05-13",
            court_uri=_EUROOPAKOHUS_URI,
            court_label="Euroopa Kohus",
            type_uri="https://data.riik.ee/ontology/estleg#EUCourtDecision",
            provision_uri=_AVTS_P35_URI,
            provision_label="AvTS § 35",
        ),
    ]


def test_kohtupraktika_redirects_unauthenticated():
    from starlette.testclient import TestClient

    from app.main import app

    client = TestClient(app, follow_redirects=False)
    resp = client.get("/analyysikeskus/kohtupraktika")
    assert resp.status_code == 303
    assert resp.headers["location"] == "/auth/login"


@patch("app.auth.middleware._get_provider")
def test_kohtupraktika_landing_renders_input_form(mock_provider: MagicMock):
    """GET /analyysikeskus/kohtupraktika with no sisend renders the landing shell."""
    mock_provider.return_value = _stub_provider()
    client = _authed_client()
    resp = client.get("/analyysikeskus/kohtupraktika")
    assert resp.status_code == 200
    body = resp.text
    # Title + the 5-card shell headings.
    assert "Kohtupraktika sätte kohta" in body
    for heading in ("Sisend", "Ulatus", "Tulemused", "Tõendid", "Soovitatud tegevused"):
        assert heading in body, heading
    # Landing has its own input form posting back to the same endpoint.
    assert 'action="/analyysikeskus/kohtupraktika"' in body
    assert "Otsi kohtupraktikat" in body
    # No table rendered yet.
    assert "Sisestage päring" in body


@patch("app.analyysikeskus.routes.list_decisions_for_provision")
@patch("app.docs.reference_resolver.ReferenceResolver.resolve")
@patch("app.auth.middleware._get_provider")
def test_kohtupraktika_resolved_provision_multi_court_grouping(
    mock_provider: MagicMock,
    mock_resolve: MagicMock,
    mock_list: MagicMock,
):
    """A resolved §-reference renders per-court sections with citation counts."""
    mock_provider.return_value = _stub_provider()
    mock_resolve.return_value = [_canned_resolved_provision_ref()]
    mock_list.return_value = _canned_decision_rows()

    client = _authed_client()
    # "AvTS § 35" — url-encoded
    resp = client.get("/analyysikeskus/kohtupraktika?sisend=AvTS+%C2%A7+35")
    assert resp.status_code == 200
    body = resp.text

    # Page title + the 5-card shell.
    assert "Kohtupraktika sätte kohta" in body
    for heading in ("Sisend", "Ulatus", "Tulemused", "Tõendid", "Soovitatud tegevused"):
        assert heading in body, heading

    # Resolved label visible in Sisend.
    assert "AvTS § 35 — Avaliku teabe seadus" in body

    # Multi-court grouping rendered — both bucket labels show up.
    assert "Riigikohus" in body
    assert "Euroopa Kohus" in body

    # Citation counts present (2 Riigikohus + 1 EU = 3 total).
    assert "3 lahendit" in body
    assert "2 lahendit" in body  # Riigikohus group line
    assert "1 lahendit" in body  # EU group line

    # Year-bucket trend rendered (the helper joins with " · ").
    assert "Aastate kaupa" in body

    # The provision branch was called (not the act branch).
    mock_list.assert_called_once_with(_AVTS_P35_URI)


@patch("app.analyysikeskus.routes.list_decisions_for_act")
@patch("app.docs.reference_resolver.ReferenceResolver.resolve")
@patch("app.auth.middleware._get_provider")
def test_kohtupraktika_resolved_law_uses_act_query(
    mock_provider: MagicMock,
    mock_resolve: MagicMock,
    mock_list_act: MagicMock,
):
    """A resolved ``law`` ref ⇒ the act-level query branch."""
    mock_provider.return_value = _stub_provider()
    mock_resolve.return_value = [_canned_resolved_law_ref()]
    mock_list_act.return_value = _canned_decision_rows()

    client = _authed_client()
    resp = client.get("/analyysikeskus/kohtupraktika?sisend=AvTS+%C2%A7+35")
    assert resp.status_code == 200
    mock_list_act.assert_called_once_with(_AVTS_URI)


@patch("app.analyysikeskus.routes.list_decisions_for_provision", return_value=[])
@patch("app.docs.reference_resolver.ReferenceResolver.resolve")
@patch("app.auth.middleware._get_provider")
def test_kohtupraktika_empty_result_renders_friendly_message(
    mock_provider: MagicMock,
    mock_resolve: MagicMock,
    mock_list: MagicMock,
):
    """No decisions returned ⇒ a friendly "ei leitud" line, not a 500."""
    mock_provider.return_value = _stub_provider()
    mock_resolve.return_value = [_canned_resolved_provision_ref()]

    client = _authed_client()
    resp = client.get("/analyysikeskus/kohtupraktika?sisend=AvTS+%C2%A7+35")
    assert resp.status_code == 200
    assert "Kohtupraktikat ei leitud" in resp.text


@patch("app.analyysikeskus.routes.list_decisions_for_provision")
@patch("app.docs.reference_resolver.ReferenceResolver.resolve")
@patch("app.auth.middleware._get_provider")
def test_kohtupraktika_disambiguation_when_multiple_resolutions(
    mock_provider: MagicMock,
    mock_resolve: MagicMock,
    mock_list: MagicMock,
):
    """Multiple resolved entities ⇒ a disambiguation card with clickable candidates."""
    mock_provider.return_value = _stub_provider()
    mock_resolve.return_value = [
        _canned_resolved_provision_ref(),
        _canned_resolved_law_ref(),
    ]

    client = _authed_client()
    resp = client.get("/analyysikeskus/kohtupraktika?sisend=AvTS+%C2%A7+35")
    assert resp.status_code == 200
    body = resp.text
    assert "Sisend võib viidata mitmele üksusele" in body
    assert "AvTS § 35 — Avaliku teabe seadus" in body
    assert "Avaliku teabe seadus" in body
    mock_list.assert_not_called()


@patch("app.analyysikeskus.routes._rag_candidates", return_value=[])
@patch("app.docs.reference_resolver.ReferenceResolver.resolve", return_value=[])
@patch("app.auth.middleware._get_provider")
def test_kohtupraktika_unresolved_input_shows_warning(
    mock_provider: MagicMock,
    mock_resolve: MagicMock,
    mock_rag: MagicMock,
):
    """An unrecognised input renders the friendly warning + the result shell."""
    mock_provider.return_value = _stub_provider()
    client = _authed_client()
    resp = client.get("/analyysikeskus/kohtupraktika?sisend=mingi+suvaline+jutt")
    assert resp.status_code == 200
    body = resp.text
    assert "Ei tuvastanud õiguslikku viidet" in body
    for heading in ("Sisend", "Ulatus", "Tulemused", "Tõendid", "Soovitatud tegevused"):
        assert heading in body, heading


@patch("app.analyysikeskus.routes.list_decisions_for_provision")
@patch("app.docs.reference_resolver.ReferenceResolver.resolve")
@patch("app.auth.middleware._get_provider")
def test_kohtupraktika_evidence_rows_use_legal_phrase(
    mock_provider: MagicMock,
    mock_resolve: MagicMock,
    mock_list: MagicMock,
):
    """Tõendid rows use the canonical "tõlgendab" legal phrase from C0."""
    mock_provider.return_value = _stub_provider()
    mock_resolve.return_value = [_canned_resolved_provision_ref()]
    mock_list.return_value = _canned_decision_rows()

    client = _authed_client()
    resp = client.get("/analyysikeskus/kohtupraktika?sisend=AvTS+%C2%A7+35")
    assert resp.status_code == 200
    body = resp.text
    assert "tõlgendab" in body
    # The per-row "Küsi nõustajalt" form is present (the shared #724 pattern).
    assert 'action="/chat/seed"' in body


@patch("app.analyysikeskus.routes.list_decisions_for_provision")
@patch("app.docs.reference_resolver.ReferenceResolver.resolve")
@patch("app.auth.middleware._get_provider")
def test_kohtupraktika_actions_carry_scope(
    mock_provider: MagicMock,
    mock_resolve: MagicMock,
    mock_list: MagicMock,
):
    """The Soovitatud tegevused links carry the sisend through (scope-carrying URLs)."""
    mock_provider.return_value = _stub_provider()
    mock_resolve.return_value = [_canned_resolved_provision_ref()]
    mock_list.return_value = _canned_decision_rows()

    client = _authed_client()
    resp = client.get("/analyysikeskus/kohtupraktika?sisend=AvTS+%C2%A7+35")
    assert resp.status_code == 200
    body = resp.text
    # The "Vaata sätte sanktsioone" link re-routes into the sanctions workflow
    # with the same input — scope is carried forward as a sisend= param.
    assert "/analyysikeskus/sanktsioonid?" in body
    assert "sisend=AvTS" in body


# ---------------------------------------------------------------------------
# 7. Existing routes still register — no breakage
# ---------------------------------------------------------------------------


def test_existing_workflows_still_registered():
    """C3 only appends — the prior routes must still respond."""
    from starlette.testclient import TestClient

    from app.main import app

    client = TestClient(app, follow_redirects=False)
    for path in (
        "/analyysikeskus",
        "/analyysikeskus/normi-mojuahel",
        "/analyysikeskus/el-ulevott",
        "/analyysikeskus/sanktsioonid",
        "/analyysikeskus/kohtupraktika",
    ):
        resp = client.get(path)
        assert resp.status_code == 303, path
        assert resp.headers["location"] == "/auth/login", path
