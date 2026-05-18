"""Tests for the Sanktsioonide indeks workflow (A1 v1 standalone).

Covers:

1. The SPARQL helper layer in :mod:`app.analyysikeskus.sanctions` —
   row → :class:`SanctionRow` conversion, empty / dead-Jena paths,
   the act / provision / similar-sanction query shapes, and the
   range-overlap filter that scopes :func:`find_similar_sanctions`.
2. The Estonian display-label helpers (``sanction_type_label`` /
   ``sanction_unit_label``) — covers the explicit mappings and the
   raw-string fallback.
3. The route layer in :mod:`app.analyysikeskus.routes` — the
   ``/analyysikeskus/sanktsioonid`` endpoint: the auth gate, the
   landing page (no ``sisend``), the resolved-provision happy path,
   the disambiguation branch, and the unresolved branch.

Tests follow the same shape as ``test_analyysikeskus_routes.py`` — the
SPARQL client / ReferenceResolver / RAG retriever are patched
*where used* (the patch-path contract).
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock, patch

# ---------------------------------------------------------------------------
# Test fixtures — shared SanctionRow instances and SPARQL row stubs
# ---------------------------------------------------------------------------

_KARS_URI = "https://data.riik.ee/ontology/estleg#karistusseadustik"
_KARS_P211_URI = "https://data.riik.ee/ontology/estleg#KarS-p211"
_KARS_P211_SANCTION_URI = "https://data.riik.ee/ontology/estleg#KarS-p211-Sanction"

_KMS_URI = "https://data.riik.ee/ontology/estleg#kaibemaksuseadus"
_KMS_P30_URI = "https://data.riik.ee/ontology/estleg#KMS-p30"
_KMS_P30_SANCTION_URI = "https://data.riik.ee/ontology/estleg#KMS-p30-Sanction"


def _kars_p211_row() -> dict[str, str]:
    """SPARQL JSON-extractor row for a sample KarS §211 imprisonment sanction.

    Mirrors the prod shape (post-2026-05-18 Wave 2 Step 5): the act
    join is the literal ``estleg:sourceAct`` title in ``actLit`` —
    there is no act URI / actLabel column. The row-builder fills
    :class:`SanctionRow.act_label` from this literal and leaves
    ``act_uri`` empty.
    """
    return {
        "sanction": _KARS_P211_SANCTION_URI,
        "provision": _KARS_P211_URI,
        "provisionLabel": "KarS § 211",
        "actLit": "Karistusseadustik",
        "sanctionType": "imprisonment",
        "minAmount": "1",
        "maxAmount": "5",
        "minUnit": "years",
        "maxUnit": "years",
        "minCurrency": "",
        "maxCurrency": "",
        "enforcedAtLevel": "act",
        "isStatutoryDefault": "true",
    }


def _kms_p30_row() -> dict[str, str]:
    """SPARQL JSON-extractor row for a comparison fine sanction in KMS."""
    return {
        "sanction": _KMS_P30_SANCTION_URI,
        "provision": _KMS_P30_URI,
        "provisionLabel": "KMS § 30",
        "actLit": "Käibemaksuseadus",
        "sanctionType": "fine",
        "minAmount": "100",
        "maxAmount": "5000",
        "minUnit": "monetary",
        "maxUnit": "monetary",
        "minCurrency": "EUR",
        "maxCurrency": "EUR",
        "enforcedAtLevel": "act",
        "isStatutoryDefault": "false",
    }


# ---------------------------------------------------------------------------
# 1. Label helpers
# ---------------------------------------------------------------------------


class TestLabelHelpers:
    def test_sanction_type_label_known_values(self):
        from app.analyysikeskus.sanctions import sanction_type_label

        assert sanction_type_label("imprisonment") == "Vangistus"
        assert sanction_type_label("fine") == "Rahatrahv"
        assert sanction_type_label("pecuniary_punishment") == "Rahaline karistus"
        assert sanction_type_label("arrest") == "Arest"
        assert sanction_type_label("coercive_payment") == "Sunniraha"

    def test_sanction_type_label_unknown_falls_back(self):
        from app.analyysikeskus.sanctions import sanction_type_label

        # An unmapped value comes through verbatim — keeps a future
        # ontology extension legible without a code change.
        assert sanction_type_label("forfeiture") == "forfeiture"

    def test_sanction_type_label_empty(self):
        from app.analyysikeskus.sanctions import sanction_type_label

        assert sanction_type_label("") == "Sanktsioon"
        assert sanction_type_label("   ") == "Sanktsioon"

    def test_sanction_unit_label_time_units(self):
        from app.analyysikeskus.sanctions import sanction_unit_label

        assert sanction_unit_label("years") == "aastat"
        assert sanction_unit_label("months") == "kuud"
        assert sanction_unit_label("days") == "päeva"
        assert sanction_unit_label("daily_rates") == "päevamäära"
        assert sanction_unit_label("fine_units") == "trahvi-ühikut"

    def test_sanction_unit_label_monetary_uses_currency(self):
        from app.analyysikeskus.sanctions import sanction_unit_label

        # ``"monetary"`` → currency code rather than literal "monetary".
        assert sanction_unit_label("monetary", "EUR") == "EUR"
        assert sanction_unit_label("monetary", None) == "EUR"  # fallback default
        assert sanction_unit_label("monetary", "") == "EUR"

    def test_sanction_unit_label_unknown_falls_back(self):
        from app.analyysikeskus.sanctions import sanction_unit_label

        assert sanction_unit_label("seconds") == "seconds"


# ---------------------------------------------------------------------------
# 2. SPARQL helpers — list_sanctions_for_provision / _for_act / find_similar
# ---------------------------------------------------------------------------


class TestListSanctionsForProvision:
    def test_returns_parsed_rows(self):
        from app.analyysikeskus.sanctions import (
            list_sanctions_for_provision,
        )

        stub_client = MagicMock()
        stub_client.query.return_value = [_kars_p211_row()]

        rows = list_sanctions_for_provision(_KARS_P211_URI, sparql_client=stub_client)
        assert len(rows) == 1
        row = rows[0]
        assert row.sanction_uri == _KARS_P211_SANCTION_URI
        assert row.provision_uri == _KARS_P211_URI
        assert row.provision_label == "KarS § 211"
        # Wave 2 Step 5: ``sourceAct`` is a literal in prod, so
        # ``act_uri`` is always empty and ``act_label`` carries the
        # literal title.
        assert row.act_uri == ""
        assert row.act_label == "Karistusseadustik"
        assert row.sanction_type == "imprisonment"
        assert row.min_amount == 1.0
        assert row.max_amount == 5.0
        assert row.min_unit == "years"
        assert row.max_unit == "years"
        # Empty strings on currency are converted to None.
        assert row.min_currency is None
        assert row.max_currency is None
        assert row.enforced_at_level == "act"
        assert row.is_statutory_default is True

    def test_blank_uri_returns_empty_without_hitting_jena(self):
        from app.analyysikeskus.sanctions import list_sanctions_for_provision

        stub_client = MagicMock()
        rows = list_sanctions_for_provision("", sparql_client=stub_client)
        assert rows == []
        stub_client.query.assert_not_called()

    def test_sparql_error_returns_empty(self):
        from app.analyysikeskus.sanctions import list_sanctions_for_provision

        stub_client = MagicMock()
        stub_client.query.side_effect = RuntimeError("jena unreachable")
        rows = list_sanctions_for_provision(_KARS_P211_URI, sparql_client=stub_client)
        assert rows == []

    def test_binds_provision_as_uri(self):
        """The URI must travel via :meth:`SparqlClient._inject_uri_bindings` (safe)."""
        from app.analyysikeskus.sanctions import list_sanctions_for_provision

        stub_client = MagicMock()
        stub_client.query.return_value = []
        list_sanctions_for_provision(_KARS_P211_URI, sparql_client=stub_client)
        kwargs = stub_client.query.call_args.kwargs
        assert "uri_bindings" in kwargs
        assert kwargs["uri_bindings"] == {"provision": _KARS_P211_URI}


class TestListSanctionsForAct:
    def test_aggregates_act_member_sanctions(self):
        from app.analyysikeskus.sanctions import list_sanctions_for_act

        stub_client = MagicMock()
        stub_client.query.return_value = [_kars_p211_row()]

        # Wave 2 Step 5: param is the literal ``estleg:sourceAct`` title.
        rows = list_sanctions_for_act("Karistusseadustik", sparql_client=stub_client)
        assert len(rows) == 1
        # ``act_uri`` is always empty in prod (no act URIs on
        # provisions); ``act_label`` carries the literal title.
        assert rows[0].act_uri == ""
        assert rows[0].act_label == "Karistusseadustik"
        assert rows[0].provision_uri == _KARS_P211_URI

    def test_blank_title_returns_empty_without_hitting_jena(self):
        """Whitespace / empty title must short-circuit — no Jena round-trip."""
        from app.analyysikeskus.sanctions import list_sanctions_for_act

        stub_client = MagicMock()
        assert list_sanctions_for_act("", sparql_client=stub_client) == []
        assert list_sanctions_for_act("   ", sparql_client=stub_client) == []
        stub_client.query.assert_not_called()

    def test_dead_jena_returns_empty(self):
        from app.analyysikeskus.sanctions import list_sanctions_for_act

        stub_client = MagicMock()
        stub_client.query.side_effect = RuntimeError("jena down")
        rows = list_sanctions_for_act("Karistusseadustik", sparql_client=stub_client)
        assert rows == []

    def test_binds_act_title_as_literal(self):
        """The title must travel via ``bindings`` (string-literal VALUES), not ``uri_bindings``.

        Pins the parameter contract documented in
        ``app/analyysikeskus/sanctions.py`` (Wave 2 Step 5): the act
        join is a literal because the prod ontology has no act URIs on
        provisions.
        """
        from app.analyysikeskus.sanctions import list_sanctions_for_act

        stub_client = MagicMock()
        stub_client.query.return_value = []
        list_sanctions_for_act("Karistusseadustik", sparql_client=stub_client)
        kwargs = stub_client.query.call_args.kwargs
        assert "bindings" in kwargs
        assert kwargs["bindings"] == {"actLit": "Karistusseadustik"}
        # Not via uri_bindings — that's reserved for genuine URI joins.
        assert "uri_bindings" not in kwargs or not kwargs["uri_bindings"]

    def test_uri_input_returns_empty_gracefully(self):
        """A caller passing a URI by mistake degrades to "no rows", not a 500.

        The new SPARQL query joins on ``?provision estleg:sourceAct
        ?actLit`` where ``?actLit`` is bound as a string literal. If
        a caller passes a URI (e.g. legacy code that wasn't updated),
        the literal VALUES binding will simply not match any triples
        in prod (no provision has a URI on the right-hand side of
        ``sourceAct``). The function returns ``[]`` rather than
        crashing.
        """
        from app.analyysikeskus.sanctions import list_sanctions_for_act

        stub_client = MagicMock()
        stub_client.query.return_value = []
        rows = list_sanctions_for_act(_KARS_URI, sparql_client=stub_client)
        assert rows == []


class TestFindSimilarSanctions:
    def test_no_type_yields_empty_without_hitting_jena(self):
        from app.analyysikeskus.sanctions import SanctionRow, find_similar_sanctions

        stub_client = MagicMock()
        seed = SanctionRow(sanction_type="")  # empty type → short-circuit
        rows = find_similar_sanctions(seed, sparql_client=stub_client)
        assert rows == []
        stub_client.query.assert_not_called()

    def test_excludes_seed_act_defence_in_depth(self):
        """Even if SPARQL leaks a row with the seed's act title, Python filters it.

        Wave 2 Step 5: the "other acts" exclusion now keys on the
        literal ``estleg:sourceAct`` title carried in
        :class:`SanctionRow.act_label` because the prod corpus has no
        act URIs on provisions.
        """
        from app.analyysikeskus.sanctions import SanctionRow, find_similar_sanctions

        seed = SanctionRow(
            sanction_type="fine",
            act_label="Karistusseadustik",
            min_amount=100.0,
            max_amount=500.0,
        )
        stub_client = MagicMock()
        # SPARQL "leaks" a row in the same act — Python filter drops it.
        stub_client.query.return_value = [
            {
                "sanction": "https://data.riik.ee/ontology/estleg#KarS-other-Sanction",
                "provision": "https://data.riik.ee/ontology/estleg#KarS-other",
                "actLit": "Karistusseadustik",  # same as seed title → must drop
                "sanctionType": "fine",
                "minAmount": "200",
                "maxAmount": "400",
            },
            _kms_p30_row(),  # different act title → kept
        ]

        rows = find_similar_sanctions(seed, limit=10, sparql_client=stub_client)
        assert len(rows) == 1
        assert rows[0].act_label == "Käibemaksuseadus"
        assert rows[0].act_uri == ""

    def test_passes_seed_bounds_to_sparql_bindings(self):
        from app.analyysikeskus.sanctions import SanctionRow, find_similar_sanctions

        seed = SanctionRow(
            sanction_type="fine",
            act_label="Karistusseadustik",
            min_amount=100.0,
            max_amount=500.0,
        )
        stub_client = MagicMock()
        stub_client.query.return_value = []
        find_similar_sanctions(seed, sparql_client=stub_client)
        bindings = stub_client.query.call_args.kwargs["bindings"]
        assert bindings["type"] == "fine"
        # ``_xsd_decimal_literal`` strips trailing ``.0`` from integral
        # floats so the binding stays a tight xsd:decimal lexical form.
        assert float(bindings["seedMin"]) == 100.0
        assert float(bindings["seedMax"]) == 500.0
        assert "e" not in bindings["seedMin"].lower()
        assert "e" not in bindings["seedMax"].lower()
        # Wave 2 Step 5: the seed-act binding is the literal title
        # (``seedActLit``), not the act URI — the prod corpus has no
        # act URIs to compare against.
        assert bindings["seedActLit"] == "Karistusseadustik"
        assert "seedAct" not in bindings

    def test_missing_seed_bounds_default_to_open_range(self):
        """A seed with no numeric bounds must not blow up — bounds → 0 / +inf."""
        from app.analyysikeskus.sanctions import SanctionRow, find_similar_sanctions

        seed = SanctionRow(
            sanction_type="fine",
            act_label="Karistusseadustik",
        )  # no min/max
        stub_client = MagicMock()
        stub_client.query.return_value = []
        find_similar_sanctions(seed, sparql_client=stub_client)
        bindings = stub_client.query.call_args.kwargs["bindings"]
        # F7 regression: seedMax sentinel must serialise as a plain
        # integer string ("1000000000000000000"), never exponential.
        # Apache Jena's xsd:decimal(...) constructor rejects "1e+18".
        assert bindings["seedMin"] == "0"
        assert bindings["seedMax"] == "1000000000000000000"
        assert "e" not in bindings["seedMax"].lower()
        assert float(bindings["seedMax"]) >= 1e17

    def test_xsd_decimal_literal_helper(self):
        """Direct unit test for the F7 helper: plain integer strings, no
        exponent, no trailing zeros."""
        from app.analyysikeskus.sanctions import _xsd_decimal_literal

        assert _xsd_decimal_literal(0) == "0"
        assert _xsd_decimal_literal(0.0) == "0"
        assert _xsd_decimal_literal(100) == "100"
        assert _xsd_decimal_literal(100.0) == "100"
        assert _xsd_decimal_literal(100.5) == "100.5"
        assert _xsd_decimal_literal(10**18) == "1000000000000000000"
        # Float 1e18 is the original bug case — must never emit "1e+18".
        assert "e" not in _xsd_decimal_literal(1.0e18).lower()
        assert _xsd_decimal_literal(1.0e18) == "1000000000000000000"

    def test_min_only_seed_finds_overlap_against_rdflib(self):
        """End-to-end regression for F7 against rdflib.

        With only ``min_amount`` set (``max_amount = None``), the sentinel
        upper bound used to render as ``"1e+18"`` which Apache Jena
        rejects with the ``xsd:decimal()`` cast — the filter then dropped
        every overlapping row. The F7 fix uses ``_xsd_decimal_literal``
        which emits a plain integer string. This test exercises the same
        path with rdflib (which is also strict about the constructor).

        Updated for Wave 2 Step 5: the SPARQL now joins on the literal
        ``estleg:sourceAct`` title instead of an ``estleg:partOf`` URI
        edge (the prod corpus has no act URIs on provisions). The
        in-test graph + VALUES block reflect that shape.
        """
        from rdflib import Graph

        from app.analyysikeskus.sanctions import (
            _SIMILAR_SANCTIONS_QUERY,
            SanctionRow,
            _xsd_decimal_literal,
        )

        graph_data = """
        @prefix estleg: <https://data.riik.ee/ontology/estleg#> .
        @prefix xsd: <http://www.w3.org/2001/XMLSchema#> .
        @prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .

        estleg:OtherProvision rdfs:label "§1 OtherAct" ;
            estleg:sourceAct "Different act" ;
            estleg:hasSanction estleg:OtherSanction .
        estleg:OtherSanction estleg:sanctionType "fine" ;
            estleg:minPenaltyAmount "150"^^xsd:decimal ;
            estleg:maxPenaltyAmount "300"^^xsd:decimal .
        """

        g = Graph()
        g.parse(data=graph_data, format="turtle")

        seed = SanctionRow(
            sanction_type="fine",
            act_label="Seed act title",
            min_amount=100.0,
            max_amount=None,  # the F7 case — sentinel kicks in
        )
        seed_min_str = _xsd_decimal_literal(
            seed.min_amount if seed.min_amount is not None else 0.0
        )
        seed_max_str = _xsd_decimal_literal(
            seed.max_amount if seed.max_amount is not None else 10**18
        )
        values_block = (
            'VALUES ?type { "fine" }\n'
            f'VALUES ?seedMin {{ "{seed_min_str}" }}\n'
            f'VALUES ?seedMax {{ "{seed_max_str}" }}\n'
            'VALUES ?seedActLit { "Seed act title" }\n'
        )
        last_brace = _SIMILAR_SANCTIONS_QUERY.rfind("}")
        query = (
            _SIMILAR_SANCTIONS_QUERY[:last_brace]
            + "\n"
            + values_block
            + "\n"
            + _SIMILAR_SANCTIONS_QUERY[last_brace:]
        )

        results = list(g.query(query))
        # min-only seed [100, +inf] overlaps candidate [150, 300] → expect 1.
        # With the F7 bug (str(1e18) == "1e+18") this was 0.
        assert len(results) == 1

    def test_respects_limit(self):
        from app.analyysikeskus.sanctions import SanctionRow, find_similar_sanctions

        seed = SanctionRow(sanction_type="fine", act_label="Karistusseadustik")
        stub_client = MagicMock()
        stub_client.query.return_value = [_kms_p30_row()] * 25
        rows = find_similar_sanctions(seed, limit=3, sparql_client=stub_client)
        assert len(rows) == 3

    def test_sparql_error_returns_empty(self):
        from app.analyysikeskus.sanctions import SanctionRow, find_similar_sanctions

        seed = SanctionRow(sanction_type="fine")
        stub_client = MagicMock()
        stub_client.query.side_effect = RuntimeError("jena down")
        assert find_similar_sanctions(seed, sparql_client=stub_client) == []

    def test_query_template_casts_seed_bounds_to_xsd_decimal(self):
        """Regression for F2 (2026-05-15 review).

        The SPARQL bindings injection emits VALUES with string literals
        (``"100.0"``, not ``"100.0"^^xsd:decimal``). The FILTER must
        therefore cast through ``xsd:decimal()`` before comparing
        against the candidate row's decimal ``?maxAmount`` /
        ``?minAmount``. Without the cast the comparison evaluates as
        ``string <= decimal``, which silently returns no rows even
        when the ranges genuinely overlap.
        """
        from app.analyysikeskus.sanctions import _SIMILAR_SANCTIONS_QUERY

        assert "xsd:decimal(?seedMin)" in _SIMILAR_SANCTIONS_QUERY
        assert "xsd:decimal(?seedMax)" in _SIMILAR_SANCTIONS_QUERY

    def test_similar_sanctions_query_finds_overlap_against_rdflib(self):
        """End-to-end regression for F2 against an in-memory rdflib graph.

        Mirrors :class:`app.ontology.SparqlClient`'s VALUES injection
        (string literals) and asserts the FILTER returns the
        overlapping row. Catches the original bug: without the
        ``xsd:decimal`` casts, string-vs-decimal comparison drops the
        row even though [100, 500] genuinely overlaps [150, 300].

        Updated for Wave 2 Step 5: provision → act join is on the
        literal ``estleg:sourceAct`` title (no act URIs in prod).
        """
        from rdflib import Graph

        from app.analyysikeskus.sanctions import _SIMILAR_SANCTIONS_QUERY

        graph_data = """
        @prefix estleg: <https://data.riik.ee/ontology/estleg#> .
        @prefix xsd: <http://www.w3.org/2001/XMLSchema#> .
        @prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .

        estleg:OtherProvision rdfs:label "§1 OtherAct" ;
            estleg:sourceAct "Different act" ;
            estleg:hasSanction estleg:OtherSanction .
        estleg:OtherSanction estleg:sanctionType "fine" ;
            estleg:minPenaltyAmount "150"^^xsd:decimal ;
            estleg:maxPenaltyAmount "300"^^xsd:decimal .
        """

        g = Graph()
        g.parse(data=graph_data, format="turtle")

        # Mirror ``SparqlClient._inject_bindings``: string literals via VALUES.
        values_block = (
            'VALUES ?type { "fine" }\n'
            'VALUES ?seedMin { "100.0" }\n'
            'VALUES ?seedMax { "500.0" }\n'
            'VALUES ?seedActLit { "Seed act title" }\n'
        )
        last_brace = _SIMILAR_SANCTIONS_QUERY.rfind("}")
        query = (
            _SIMILAR_SANCTIONS_QUERY[:last_brace]
            + "\n"
            + values_block
            + "\n"
            + _SIMILAR_SANCTIONS_QUERY[last_brace:]
        )

        results = list(g.query(query))
        # Seed range [100, 500] overlaps candidate [150, 300] → expect 1.
        # With the F2 bug (no ``xsd:decimal`` cast) this is 0.
        assert len(results) == 1


# ---------------------------------------------------------------------------
# 3. Input parser pinning — A1 must accept the same input vocabulary as Normi
# ---------------------------------------------------------------------------


class TestInputParserAcceptsA1Inputs:
    """Pins the Normi-style inputs that A1 must accept (no per-workflow parser).

    Re-exercises the rule-based regex parser so the A1 route can confidently
    assume that §-refs / CELEX / case numbers travel into the workflow as
    structured refs.
    """

    def test_section_ref_parses_for_kars_211(self):
        from app.analyysikeskus.input_parser import parse_user_reference

        refs = parse_user_reference("KarS § 211")
        types = [r.ref_type for r in refs]
        assert types == ["provision", "law"]

    def test_celex_parses(self):
        from app.analyysikeskus.input_parser import parse_user_reference

        refs = parse_user_reference("32016R0679")
        assert [r.ref_type for r in refs] == ["eu_act"]

    def test_plain_prose_returns_empty(self):
        from app.analyysikeskus.input_parser import parse_user_reference

        # An unrecognised input ⇒ no refs ⇒ the route shows the
        # "Ei tuvastanud õiguslikku viidet" warning.
        assert parse_user_reference("mis sanktsioonid kehtivad varguse eest") == []


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
    The route picks that title up and routes to ``list_sanctions_for_act``.
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


def _canned_sanction_rows():
    from app.analyysikeskus.sanctions import SanctionRow

    # Wave 2 Step 5: ``act_uri`` is always empty in the prod shape
    # because the corpus carries no provision → act URI edge;
    # ``act_label`` holds the literal ``estleg:sourceAct`` title.
    return [
        SanctionRow(
            sanction_uri=_KARS_P211_SANCTION_URI,
            provision_uri=_KARS_P211_URI,
            provision_label="KarS § 211",
            act_uri="",
            act_label="Karistusseadustik",
            sanction_type="imprisonment",
            min_amount=1.0,
            max_amount=5.0,
            min_unit="years",
            max_unit="years",
            enforced_at_level="act",
            is_statutory_default=True,
        )
    ]


def test_sanctions_redirects_unauthenticated():
    from starlette.testclient import TestClient

    from app.main import app

    client = TestClient(app, follow_redirects=False)
    resp = client.get("/analyysikeskus/sanktsioonid")
    assert resp.status_code == 303
    assert resp.headers["location"] == "/auth/login"


@patch("app.auth.middleware._get_provider")
def test_sanctions_landing_renders_input_form(mock_provider: MagicMock):
    """GET /analyysikeskus/sanktsioonid with no sisend renders the landing shell."""
    mock_provider.return_value = _stub_provider()
    client = _authed_client()
    resp = client.get("/analyysikeskus/sanktsioonid")
    assert resp.status_code == 200
    body = resp.text
    # Title + the 5-card shell headings.
    assert "Sanktsioonide indeks" in body
    for heading in ("Sisend", "Ulatus", "Tulemused", "Tõendid", "Soovitatud tegevused"):
        assert heading in body, heading
    # Landing has its own input form posting back to the same endpoint.
    assert 'action="/analyysikeskus/sanktsioonid"' in body
    assert "Otsi sanktsioone" in body
    # No table rendered yet.
    assert "Sisestage päring" in body


@patch("app.analyysikeskus.routes.find_similar_sanctions", return_value=[])
@patch("app.analyysikeskus.routes.list_sanctions_for_provision")
@patch("app.docs.reference_resolver.ReferenceResolver.resolve")
@patch("app.auth.middleware._get_provider")
def test_sanctions_resolved_provision_renders_full_result(
    mock_provider: MagicMock,
    mock_resolve: MagicMock,
    mock_list: MagicMock,
    mock_similar: MagicMock,
):
    """A resolved §-reference renders the sanctions table + Tõendid rows."""
    mock_provider.return_value = _stub_provider()
    mock_resolve.return_value = [_canned_resolved_provision_ref()]
    mock_list.return_value = _canned_sanction_rows()

    client = _authed_client()
    # "KarS § 211" — url-encoded
    resp = client.get("/analyysikeskus/sanktsioonid?sisend=KarS+%C2%A7+211")
    assert resp.status_code == 200
    body = resp.text

    # Page title and the 5-card shell.
    assert "Sanktsioonide indeks" in body
    for heading in ("Sisend", "Ulatus", "Tulemused", "Tõendid", "Soovitatud tegevused"):
        assert heading in body, heading

    # The resolved label appears in Sisend.
    assert "KarS § 211 — Karistusseadustik" in body

    # The Tulemused summary line names the sanction type in Estonian.
    assert "Vangistus" in body  # imprisonment → "Vangistus"
    assert "1 sanktsiooni" in body  # singular sanctions count

    # The Tõendid row links to the sanction URI in the Õiguskaart.
    # The KarS-p211 URI's "#" survives into the query string as %23.
    assert "/explorer?focus=" in body
    assert "%23KarS-p211-Sanction" in body or "%23KarS-p211" in body

    # The "Küsi nõustajalt" per-row form is present (pattern from #724).
    assert 'action="/chat/seed"' in body
    assert 'name="seed_text"' in body
    assert "Küsi nõustajalt" in body

    # Static "Soovitatud tegevused" — comparison toggle + court-practice link.
    assert "Võrdle sarnaste aktide sanktsioonidega" in body
    assert "Vaata sätte kohtupraktikat" in body

    # The provision branch was called, similar query was NOT called
    # (no vordle_sarnaste_aktidega param on this request).
    mock_list.assert_called_once_with(_KARS_P211_URI)
    mock_similar.assert_not_called()


@patch("app.analyysikeskus.routes.find_similar_sanctions")
@patch("app.analyysikeskus.routes.list_sanctions_for_provision")
@patch("app.docs.reference_resolver.ReferenceResolver.resolve")
@patch("app.auth.middleware._get_provider")
def test_sanctions_comparison_flag_runs_similar_query(
    mock_provider: MagicMock,
    mock_resolve: MagicMock,
    mock_list: MagicMock,
    mock_similar: MagicMock,
):
    """``vordle_sarnaste_aktidega=1`` triggers :func:`find_similar_sanctions`."""
    mock_provider.return_value = _stub_provider()
    mock_resolve.return_value = [_canned_resolved_provision_ref()]
    mock_list.return_value = _canned_sanction_rows()
    mock_similar.return_value = _canned_sanction_rows()  # one comparison row

    client = _authed_client()
    resp = client.get(
        "/analyysikeskus/sanktsioonid?sisend=KarS+%C2%A7+211&vordle_sarnaste_aktidega=1"
    )
    assert resp.status_code == 200
    body = resp.text

    # The comparison section heading is present.
    assert "Sarnaste aktide sanktsioonid" in body
    mock_similar.assert_called_once()


@patch("app.analyysikeskus.routes.list_sanctions_for_act")
@patch("app.docs.reference_resolver.ReferenceResolver.resolve")
@patch("app.auth.middleware._get_provider")
def test_sanctions_resolved_law_uses_act_query(
    mock_provider: MagicMock,
    mock_resolve: MagicMock,
    mock_list_act: MagicMock,
):
    """A resolved ``law`` ref ⇒ the act-level query branch.

    Wave 2 Step 5: the route picks the literal act title from the
    resolver's ``partial_match`` payload and passes that to
    :func:`list_sanctions_for_act` — the prod corpus has no act URIs.
    """
    mock_provider.return_value = _stub_provider()
    mock_resolve.return_value = [_canned_resolved_law_ref()]
    mock_list_act.return_value = _canned_sanction_rows()

    client = _authed_client()
    # The router exercises the resolver branch when there's a single
    # resolved ref; for a law-typed ref we go through list_sanctions_for_act.
    # We send a §-ref input so parse_user_reference emits two refs;
    # mock_resolve returns only the law ref so the route enters the
    # act branch.
    resp = client.get("/analyysikeskus/sanktsioonid?sisend=KarS+%C2%A7+211")
    assert resp.status_code == 200
    # The route passes the literal act title via partial_match.
    mock_list_act.assert_called_once_with("Karistusseadustik")


@patch("app.analyysikeskus.routes.find_similar_sanctions", return_value=[])
@patch("app.analyysikeskus.routes.list_sanctions_for_act")
@patch("app.analyysikeskus.routes._rag_candidates", return_value=[])
@patch("app.docs.reference_resolver.ReferenceResolver.resolve")
@patch("app.auth.middleware._get_provider")
def test_sanctions_bare_law_input_routes_to_for_act(
    mock_provider: MagicMock,
    mock_resolve: MagicMock,
    mock_rag: MagicMock,
    mock_list_act: MagicMock,
    mock_similar: MagicMock,
):
    """Wave 2 Step 5: a bare law sisend (``KarS``) routes to list_sanctions_for_act.

    Exercises the full path: parse_user_reference recognises ``KarS``
    as a curated alias → emits one ``law`` ExtractedRef → resolver
    returns the partial-match shape (no URI, title literal in
    ``partial_match``) → route picks the title and calls
    ``list_sanctions_for_act("Karistusseadustik")``. The route does
    NOT fall through to the unresolved/RAG path.
    """
    mock_provider.return_value = _stub_provider()
    mock_resolve.return_value = [_canned_resolved_law_ref()]
    mock_list_act.return_value = _canned_sanction_rows()

    client = _authed_client()
    # Bare law input — no § ref. The new bare-law branch in
    # parse_user_reference emits a single ``law`` ExtractedRef.
    resp = client.get("/analyysikeskus/sanktsioonid?sisend=KarS")
    assert resp.status_code == 200
    body = resp.text

    # The act-level helper was called with the literal title.
    mock_list_act.assert_called_once_with("Karistusseadustik")
    # The RAG fallback was NOT consulted.
    mock_rag.assert_not_called()
    # The "Ei tuvastanud" warning is absent — we resolved (partially).
    assert "Ei tuvastanud õiguslikku viidet" not in body


@patch("app.analyysikeskus.routes._rag_candidates", return_value=[])
@patch("app.docs.reference_resolver.ReferenceResolver.resolve", return_value=[])
@patch("app.auth.middleware._get_provider")
def test_sanctions_unresolved_input_shows_warning(
    mock_provider: MagicMock,
    mock_resolve: MagicMock,
    mock_rag: MagicMock,
):
    """An unrecognised input renders the friendly warning + the result shell."""
    mock_provider.return_value = _stub_provider()
    client = _authed_client()
    resp = client.get("/analyysikeskus/sanktsioonid?sisend=mingi+suvaline+jutt")
    assert resp.status_code == 200
    body = resp.text
    # The friendly warning is shown.
    assert "Ei tuvastanud õiguslikku viidet" in body
    # Still a full result shell.
    for heading in ("Sisend", "Ulatus", "Tulemused", "Tõendid", "Soovitatud tegevused"):
        assert heading in body, heading


@patch("app.analyysikeskus.routes.list_sanctions_for_provision")
@patch("app.docs.reference_resolver.ReferenceResolver.resolve")
@patch("app.auth.middleware._get_provider")
def test_sanctions_disambiguation_when_multiple_resolutions(
    mock_provider: MagicMock,
    mock_resolve: MagicMock,
    mock_list: MagicMock,
):
    """Multiple distinct URI-resolved refs ⇒ a disambiguation card.

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
    resp = client.get("/analyysikeskus/sanktsioonid?sisend=KarS+%C2%A7+211")
    assert resp.status_code == 200
    body = resp.text
    # The disambiguation banner is shown.
    assert "Sisend võib viidata mitmele üksusele" in body
    # Both candidate labels are linked back into the workflow.
    assert "KarS § 211 — Karistusseadustik" in body
    assert "KarS § 133 — Karistusseadustik" in body
    # No sanctions list rendering — disambiguation short-circuits.
    mock_list.assert_not_called()


@patch("app.analyysikeskus.routes.list_sanctions_for_provision", return_value=[])
@patch("app.docs.reference_resolver.ReferenceResolver.resolve")
@patch("app.auth.middleware._get_provider")
def test_sanctions_empty_result_renders_friendly_message(
    mock_provider: MagicMock,
    mock_resolve: MagicMock,
    mock_list: MagicMock,
):
    """No sanctions returned ⇒ a friendly "ei leitud" line, not a 500."""
    mock_provider.return_value = _stub_provider()
    mock_resolve.return_value = [_canned_resolved_provision_ref()]

    client = _authed_client()
    resp = client.get("/analyysikeskus/sanktsioonid?sisend=KarS+%C2%A7+211")
    assert resp.status_code == 200
    assert "Sanktsioone ei leitud" in resp.text


# ---------------------------------------------------------------------------
# 5. Existing routes still register — no breakage
# ---------------------------------------------------------------------------


def test_existing_workflows_still_registered():
    """A1 only appends — the Normi / EL routes must still respond."""
    from starlette.testclient import TestClient

    from app.main import app

    client = TestClient(app, follow_redirects=False)
    for path in (
        "/analyysikeskus",
        "/analyysikeskus/normi-mojuahel",
        "/analyysikeskus/el-ulevott",
        "/analyysikeskus/sanktsioonid",
    ):
        resp = client.get(path)
        # All four redirect to login (auth gate) when unauthenticated.
        assert resp.status_code == 303, path
        assert resp.headers["location"] == "/auth/login", path
