"""Unit tests for :mod:`app.ontology.relations` (C0 part 1).

Two layers:

* **Pure-Python lookups** — the legal-phrase / inverse / group helpers
  resolve every canonical predicate plus the legacy aliases the
  explorer historically surfaced. No SPARQL, no rdflib.

* **Fixture-graph SPARQL** — load ``tests/fixtures/ontology_canonical.ttl``
  into a transient ``rdflib.Graph`` and assert that every canonical
  predicate is queryable in the resulting store. Catches regressions
  where the URI constants drift from the actual ontology vocabulary.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from rdflib import Graph

from app.ontology.relations import (
    ESTLEG_NS,
    INVERSES,
    LEGAL_PHRASES,
    PREDICATES,
    RELATION_GROUPS,
    group_of,
    inverse_of,
    is_amendment_relation,
    is_interpretation_relation,
    is_transposition_relation,
    legal_phrase,
    predicate_for_label,
)

FIXTURE_PATH = Path(__file__).parent / "fixtures" / "ontology_canonical.ttl"


# ---------------------------------------------------------------------------
# PREDICATES — canonical URI constants
# ---------------------------------------------------------------------------


class TestPredicateConstants:
    """The PREDICATES namespace exposes every URI the plan calls for."""

    def test_required_predicates_present(self):
        # The list mirrors the task spec — anything in scope of C0 must be here.
        required = [
            "INTERPRETS_LAW",
            "INTERPRETED_BY",
            "AMENDS",
            "AMENDED_BY",
            "TOPIC_CLUSTER",
            "REQUESTED_CLUSTER",
            "TRANSPOSES_DIRECTIVE",
            "TRANSPOSED_BY",
            "HARMONISED_WITH",
            "REFERENCES",
            "SEMANTICALLY_SIMILAR_TO",
            "DEFINES_CONCEPT",
            "DEFINES_TERM",
            "COMPETENT_AUTHORITY",
        ]
        for name in required:
            assert hasattr(PREDICATES, name), f"PREDICATES missing {name}"
            value = getattr(PREDICATES, name)
            assert isinstance(value, str)
            assert value.startswith(ESTLEG_NS), f"{name} not in estleg namespace"

    def test_interprets_law_full_uri(self):
        assert PREDICATES.INTERPRETS_LAW == ESTLEG_NS + "interpretsLaw"

    def test_amends_and_amended_by_distinct(self):
        assert PREDICATES.AMENDS != PREDICATES.AMENDED_BY
        assert PREDICATES.AMENDS.endswith("#amends")
        assert PREDICATES.AMENDED_BY.endswith("#amendedBy")


# ---------------------------------------------------------------------------
# INVERSES — forward / inverse pairing
# ---------------------------------------------------------------------------


class TestInverses:
    """Every inverse is symmetric and matches the canonical pairs."""

    def test_interprets_law_and_interpreted_by_are_inverses(self):
        assert INVERSES[PREDICATES.INTERPRETS_LAW] == PREDICATES.INTERPRETED_BY
        assert INVERSES[PREDICATES.INTERPRETED_BY] == PREDICATES.INTERPRETS_LAW

    def test_amends_and_amended_by_are_inverses(self):
        assert INVERSES[PREDICATES.AMENDS] == PREDICATES.AMENDED_BY
        assert INVERSES[PREDICATES.AMENDED_BY] == PREDICATES.AMENDS

    def test_transposes_directive_inverse(self):
        assert INVERSES[PREDICATES.TRANSPOSES_DIRECTIVE] == PREDICATES.TRANSPOSED_BY
        assert INVERSES[PREDICATES.TRANSPOSED_BY] == PREDICATES.TRANSPOSES_DIRECTIVE

    def test_inverses_are_symmetric(self):
        # For every recorded inverse pair, the reverse mapping must also exist.
        for forward, backward in INVERSES.items():
            assert INVERSES.get(backward) == forward

    def test_inverse_of_helper(self):
        assert inverse_of(PREDICATES.AMENDS) == PREDICATES.AMENDED_BY
        # Accept prefixed name.
        assert inverse_of("estleg:amends") == PREDICATES.AMENDED_BY
        # Accept bare local name.
        assert inverse_of("amends") == PREDICATES.AMENDED_BY
        # Accept legacy alias (amendsProvision → amends → amendedBy).
        assert inverse_of("amendsProvision") == PREDICATES.AMENDED_BY
        # No inverse → None.
        assert inverse_of(PREDICATES.HARMONISED_WITH) is None
        # Empty input.
        assert inverse_of("") is None


# ---------------------------------------------------------------------------
# LEGAL_PHRASES — predicate → Estonian legal phrase
# ---------------------------------------------------------------------------


class TestLegalPhrase:
    """The Estonian legal-language label for each predicate."""

    def test_core_phrases(self):
        assert legal_phrase(PREDICATES.AMENDS) == "muudab"
        assert legal_phrase(PREDICATES.INTERPRETS_LAW) == "tõlgendab"
        assert legal_phrase(PREDICATES.REFERENCES) == "viitab"
        assert legal_phrase(PREDICATES.TRANSPOSES_DIRECTIVE) == "võtab üle direktiivi"
        assert legal_phrase(PREDICATES.DEFINES_CONCEPT) == "defineerib mõistet"
        assert legal_phrase(PREDICATES.HARMONISED_WITH) == "on harmoneeritud aktiga"

    def test_legacy_aliases_resolve(self):
        # The previous (buggy) Seadusloome names must still resolve so
        # cached UI payloads keep showing phrases until they are refreshed.
        assert legal_phrase("amendsProvision") == "muudab"
        assert legal_phrase("interpretsProvision") == "tõlgendab"
        assert legal_phrase("hasTopic") == "kuulub teemavaldkonda"
        assert legal_phrase("implementsEU") == "võtab üle direktiivi"
        assert legal_phrase("transposes") == "võtab üle direktiivi"

    def test_full_uri_accepted(self):
        iri = ESTLEG_NS + "amends"
        assert legal_phrase(iri) == "muudab"

    def test_prefixed_name_accepted(self):
        assert legal_phrase("estleg:amends") == "muudab"

    def test_unknown_predicate_falls_back_to_local_name(self):
        assert legal_phrase("estleg:somethingNobodyMapped") == "somethingNobodyMapped"

    def test_empty_returns_empty(self):
        assert legal_phrase("") == ""
        assert legal_phrase(None) == ""  # type: ignore[arg-type]

    def test_explorer_long_tail_predicates_still_resolve(self):
        # These aren't canonical predicates but the explorer still surfaces
        # them; the helper must keep parity with the historical table.
        assert legal_phrase("sourceAct") == "kuulub õigusakti"
        assert legal_phrase("appliesProvision") == "kohaldab"
        assert legal_phrase("relatedTo") == "on seotud"
        assert legal_phrase("partOf") == "on osa"


# ---------------------------------------------------------------------------
# RELATION_GROUPS — semantic grouping
# ---------------------------------------------------------------------------


class TestRelationGroups:
    """The semantic group label for each predicate."""

    def test_amendment_group(self):
        assert group_of(PREDICATES.AMENDS) == "amendment"
        assert group_of(PREDICATES.AMENDED_BY) == "amendment"
        assert group_of(PREDICATES.REPEALS) == "amendment"

    def test_interpretation_group(self):
        assert group_of(PREDICATES.INTERPRETS_LAW) == "interpretation"
        assert group_of(PREDICATES.INTERPRETED_BY) == "interpretation"

    def test_transposition_group(self):
        assert group_of(PREDICATES.TRANSPOSES_DIRECTIVE) == "transposition"
        assert group_of(PREDICATES.TRANSPOSED_BY) == "transposition"
        assert group_of(PREDICATES.HARMONISED_WITH) == "transposition"

    def test_similarity_group(self):
        assert group_of(PREDICATES.SEMANTICALLY_SIMILAR_TO) == "similarity"

    def test_concept_group(self):
        assert group_of(PREDICATES.DEFINES_CONCEPT) == "concept"
        assert group_of(PREDICATES.DEFINES_TERM) == "concept"
        assert group_of(PREDICATES.TOPIC_CLUSTER) == "concept"
        assert group_of(PREDICATES.REQUESTED_CLUSTER) == "concept"

    def test_competence_group(self):
        assert group_of(PREDICATES.COMPETENT_AUTHORITY) == "competence"

    def test_unknown_predicate_returns_none(self):
        assert group_of("estleg:somethingNobodyMapped") is None
        assert group_of("") is None

    def test_legacy_alias_resolves_group(self):
        # ``interpretsProvision`` is a legacy alias for INTERPRETS_LAW —
        # the group lookup must still classify it.
        assert group_of("interpretsProvision") == "interpretation"
        assert group_of("amendsProvision") == "amendment"

    def test_groups_table_covers_every_canonical_phrase(self):
        # If a phrase is registered, the predicate should also have a
        # group entry — keeps the two tables aligned.
        for uri in LEGAL_PHRASES:
            assert uri in RELATION_GROUPS, f"{uri} in LEGAL_PHRASES but not RELATION_GROUPS"


# ---------------------------------------------------------------------------
# Convenience predicates
# ---------------------------------------------------------------------------


class TestGroupPredicates:
    """Tiny helpers callers may use as filter functions."""

    def test_is_amendment_relation(self):
        assert is_amendment_relation(PREDICATES.AMENDS)
        assert is_amendment_relation("amendsProvision")
        assert not is_amendment_relation(PREDICATES.TRANSPOSES_DIRECTIVE)
        assert not is_amendment_relation("")

    def test_is_interpretation_relation(self):
        assert is_interpretation_relation(PREDICATES.INTERPRETS_LAW)
        assert is_interpretation_relation("interpretsProvision")
        assert not is_interpretation_relation(PREDICATES.AMENDS)

    def test_is_transposition_relation(self):
        assert is_transposition_relation(PREDICATES.TRANSPOSES_DIRECTIVE)
        assert is_transposition_relation(PREDICATES.HARMONISED_WITH)
        assert is_transposition_relation("implementsEU")
        assert not is_transposition_relation(PREDICATES.AMENDS)


# ---------------------------------------------------------------------------
# Reverse lookup
# ---------------------------------------------------------------------------


class TestPredicateForLabel:
    """Reverse lookup: legal phrase → canonical predicate URI."""

    def test_known_phrase(self):
        assert predicate_for_label("muudab") == PREDICATES.AMENDS
        assert predicate_for_label("tõlgendab") == PREDICATES.INTERPRETS_LAW
        assert predicate_for_label("viitab") == PREDICATES.REFERENCES

    def test_case_insensitive(self):
        assert predicate_for_label("Muudab") == PREDICATES.AMENDS
        assert predicate_for_label("MUUDAB") == PREDICATES.AMENDS

    def test_unknown_phrase_returns_none(self):
        assert predicate_for_label("inexistent phrase") is None
        assert predicate_for_label("") is None


# ---------------------------------------------------------------------------
# Fixture-graph SPARQL — every canonical predicate is queryable
# ---------------------------------------------------------------------------


@pytest.fixture
def fixture_graph() -> Graph:
    """Load the canonical-predicates fixture graph."""
    g = Graph()
    g.parse(FIXTURE_PATH, format="turtle")
    return g


class TestFixtureGraphLoadability:
    """Smoke test that the Turtle fixture parses cleanly with rdflib."""

    def test_graph_parses(self, fixture_graph: Graph):
        # A handful of triples per predicate; 25-40 total is the right ballpark.
        assert len(fixture_graph) >= 15

    def test_each_canonical_predicate_appears(self, fixture_graph: Graph):
        # For every URI in LEGAL_PHRASES, the fixture should have at
        # least one matching triple. Catches predicate renames that
        # haven't been mirrored into the fixture.
        for predicate_uri in LEGAL_PHRASES:
            from rdflib import URIRef

            count = sum(1 for _ in fixture_graph.triples((None, URIRef(predicate_uri), None)))
            # REPEALS / REPEALED_BY / CITED_BY are tracked in the
            # phrase table but not exercised in the fixture (they're
            # uncommon; the unit table above covers their phrases).
            if predicate_uri in (
                PREDICATES.REPEALS,
                PREDICATES.REPEALED_BY,
                PREDICATES.CITED_BY,
            ):
                continue
            assert count >= 1, (
                f"Fixture missing a triple for {predicate_uri} — "
                f"either the predicate name drifted from the ontology or "
                f"the fixture forgot to add an example."
            )


class TestFixtureGraphSparql:
    """Run small SELECT queries against the fixture; assert canonical edges."""

    def test_interprets_law_query(self, fixture_graph: Graph):
        rows = list(
            fixture_graph.query(
                """
                PREFIX estleg: <https://data.riik.ee/ontology/estleg#>
                SELECT ?court ?prov WHERE {
                    ?court estleg:interpretsLaw ?prov .
                }
                """
            )
        )
        # The fixture seeds CourtDecision_1 → Provision_1; C3 added
        # CourtDecision_2 / CourtDecision_3 with explicit interpretsLaw
        # edges so the Kohtupraktika workflow has multi-court coverage.
        # Assert canonical-edge presence rather than pinning a row count.
        assert len(rows) >= 1
        pairs = {(str(r.court), str(r.prov)) for r in rows}  # type: ignore[attr-defined]
        assert any(
            court.endswith("CourtDecision_1") and prov.endswith("Provision_1")
            for court, prov in pairs
        )

    def test_interpreted_by_inverse_query(self, fixture_graph: Graph):
        rows = list(
            fixture_graph.query(
                """
                PREFIX estleg: <https://data.riik.ee/ontology/estleg#>
                SELECT ?prov ?court WHERE {
                    ?prov estleg:interpretedBy ?court .
                }
                """
            )
        )
        assert len(rows) == 1
        assert str(rows[0].prov).endswith("Provision_1")  # type: ignore[attr-defined]

    def test_amends_query(self, fixture_graph: Graph):
        rows = list(
            fixture_graph.query(
                """
                PREFIX estleg: <https://data.riik.ee/ontology/estleg#>
                SELECT ?ev ?prov WHERE {
                    ?ev estleg:amends ?prov .
                }
                """
            )
        )
        assert len(rows) == 1
        assert str(rows[0].ev).endswith("AmendmentEvent_1")  # type: ignore[attr-defined]

    def test_amended_by_query(self, fixture_graph: Graph):
        rows = list(
            fixture_graph.query(
                """
                PREFIX estleg: <https://data.riik.ee/ontology/estleg#>
                SELECT ?prov ?act WHERE {
                    ?prov estleg:amendedBy ?act .
                }
                """
            )
        )
        assert len(rows) == 1

    def test_requested_cluster_query(self, fixture_graph: Graph):
        rows = list(
            fixture_graph.query(
                """
                PREFIX estleg: <https://data.riik.ee/ontology/estleg#>
                SELECT ?prov ?cluster WHERE {
                    ?prov estleg:requestedCluster ?cluster .
                }
                """
            )
        )
        assert len(rows) == 1

    def test_topic_cluster_alias_present(self, fixture_graph: Graph):
        # SHACL-defined alias — populated only on Provision_2 in this
        # fixture; the real corpus has it empty today.
        rows = list(
            fixture_graph.query(
                """
                PREFIX estleg: <https://data.riik.ee/ontology/estleg#>
                SELECT ?prov ?cluster WHERE {
                    ?prov estleg:topicCluster ?cluster .
                }
                """
            )
        )
        assert len(rows) == 1

    def test_transposes_directive_and_inverse(self, fixture_graph: Graph):
        # The fixture seeds both shapes the SHACL allows:
        #   Provision_1 estleg:transposesDirective EU_Dir_1   (SHACL 158-163)
        #   Act_1       estleg:transposesDirective EU_Dir_2   (SHACL 62-66)
        # ...so a free ``?subject estleg:transposesDirective ?eu`` query
        # matches both. Tighten each branch with a specific subject so the
        # test stays diagnostic when more transposition data is added.
        provision_level = list(
            fixture_graph.query(
                """
                PREFIX estleg: <https://data.riik.ee/ontology/estleg#>
                SELECT ?eu WHERE {
                    estleg:Provision_1 estleg:transposesDirective ?eu .
                }
                """
            )
        )
        act_level = list(
            fixture_graph.query(
                """
                PREFIX estleg: <https://data.riik.ee/ontology/estleg#>
                SELECT ?eu WHERE {
                    estleg:Act_1 estleg:transposesDirective ?eu .
                }
                """
            )
        )
        inverse = list(
            fixture_graph.query(
                """
                PREFIX estleg: <https://data.riik.ee/ontology/estleg#>
                SELECT ?eu ?act WHERE {
                    ?eu estleg:transposedBy ?act .
                }
                """
            )
        )
        assert len(provision_level) == 1
        assert len(act_level) == 1
        assert len(inverse) == 1

    def test_harmonised_with_query(self, fixture_graph: Graph):
        rows = list(
            fixture_graph.query(
                """
                PREFIX estleg: <https://data.riik.ee/ontology/estleg#>
                SELECT ?prov ?eu WHERE {
                    ?prov estleg:harmonisedWith ?eu .
                }
                """
            )
        )
        assert len(rows) == 1

    def test_references_and_similarity(self, fixture_graph: Graph):
        refs = list(
            fixture_graph.query(
                """
                PREFIX estleg: <https://data.riik.ee/ontology/estleg#>
                SELECT ?a ?b WHERE {
                    ?a estleg:references ?b .
                }
                """
            )
        )
        sim = list(
            fixture_graph.query(
                """
                PREFIX estleg: <https://data.riik.ee/ontology/estleg#>
                SELECT ?a ?b WHERE {
                    ?a estleg:semanticallySimilarTo ?b .
                }
                """
            )
        )
        assert len(refs) == 1
        assert len(sim) == 1

    def test_defines_concept_and_term(self, fixture_graph: Graph):
        concepts = list(
            fixture_graph.query(
                """
                PREFIX estleg: <https://data.riik.ee/ontology/estleg#>
                SELECT ?p ?c WHERE { ?p estleg:definesConcept ?c . }
                """
            )
        )
        terms = list(
            fixture_graph.query(
                """
                PREFIX estleg: <https://data.riik.ee/ontology/estleg#>
                SELECT ?p ?t WHERE { ?p estleg:definesTerm ?t . }
                """
            )
        )
        assert len(concepts) == 1
        assert len(terms) == 1

    def test_competent_authority_query(self, fixture_graph: Graph):
        # Pin the canonical edge for the original PREDICATE smoke; the
        # fixture also carries A3 (Pädevuste kaardistus) edges so the
        # row count is > 1. We assert that the Provision_1 →
        # Institution_1 edge is present (the canonical fixture row).
        rows = list(
            fixture_graph.query(
                """
                PREFIX estleg: <https://data.riik.ee/ontology/estleg#>
                SELECT ?p ?i WHERE { ?p estleg:competentAuthority ?i . }
                """
            )
        )
        assert len(rows) >= 1
        pairs = {(str(r.p), str(r.i)) for r in rows}  # type: ignore[attr-defined]
        assert (
            "https://data.riik.ee/ontology/estleg#Provision_1",
            "https://data.riik.ee/ontology/estleg#Institution_1",
        ) in pairs
