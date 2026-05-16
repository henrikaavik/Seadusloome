"""C2 — Koostaja Step 3 research cards: relation-typed groupings.

Two layers of coverage:

1. **SPARQL layer.** Run the four research SELECT templates from
   :mod:`app.drafter.handlers` against the canonical-predicate fixture
   loaded into an in-memory :class:`rdflib.Graph`, and assert each query
   projects a ``?relation`` column carrying a canonical
   ``estleg:`` URI.

2. **Renderer layer.** Feed mixed-relation items into
   ``_research_category_card`` and assert the rendered HTML contains
   per-relation sub-headers populated by ``legal_phrase`` (e.g.
   ``muudab``, ``tõlgendab``, ``võtab üle direktiivi``), while
   preserving an overall total.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fasthtml.common import to_xml
from rdflib import Graph

from app.drafter._step_renderers import _research_category_card
from app.drafter.handlers import (
    _COURT_DECISIONS_QUERY,
    _EU_DIRECTIVES_QUERY,
    _PROVISIONS_BY_KEYWORD_QUERY,
    _TOPIC_CLUSTERS_QUERY,
    _safe_keyword,
)
from app.ontology.relations import PREDICATES

FIXTURE_PATH = Path(__file__).parent / "fixtures" / "ontology_canonical.ttl"


# ---------------------------------------------------------------------------
# SPARQL fixture
# ---------------------------------------------------------------------------


@pytest.fixture
def seeded_graph() -> Graph:
    """Canonical-predicate Turtle fixture loaded as a plain rdflib Graph.

    The research SPARQL templates don't use a ``GRAPH`` envelope (unlike
    the impact engine), so a plain :class:`Graph` is enough — no need
    for the named-graph :class:`Dataset` machinery used by the impact
    canonical tests.
    """
    g = Graph()
    g.parse(FIXTURE_PATH, format="turtle")
    return g


def _rows(g: Graph, query: str) -> list[dict[str, str]]:
    """Run a SELECT and return rows as plain str dicts."""
    out: list[dict[str, str]] = []
    result = g.query(query)
    for row in result:
        d: dict[str, str] = {}
        for var in row.labels:  # type: ignore[attr-defined,union-attr]
            value = row[var]  # type: ignore[index]
            d[str(var)] = str(value) if value is not None else ""
        out.append(d)
    return out


# ---------------------------------------------------------------------------
# Provisions query
# ---------------------------------------------------------------------------


class TestProvisionsQuery:
    """The provision card query projects ``?relation`` per row.

    The fixture has ``Provision_1`` labelled ``"Provision 1 — fixture"``
    — keyword ``"provision"`` matches all three provisions; we assert
    the canonical relations the fixture wires up appear as ``?relation``
    values.
    """

    def test_returns_relation_column(self, seeded_graph: Graph):
        query = _PROVISIONS_BY_KEYWORD_QUERY.format(keyword=_safe_keyword("provision"))
        rows = _rows(seeded_graph, query)
        assert rows, "expected at least one keyword match"
        for row in rows:
            assert "relation" in row, f"row missing relation: {row}"
            assert row["relation"], f"row has empty relation: {row}"

    def test_classifies_amends_branch(self, seeded_graph: Graph):
        # Provision_1 has an inbound AmendmentEvent_1 estleg:amends edge.
        query = _PROVISIONS_BY_KEYWORD_QUERY.format(keyword=_safe_keyword("provision"))
        rows = _rows(seeded_graph, query)
        amends = [r for r in rows if r["relation"] == PREDICATES.AMENDS]
        assert any(r["provision"].endswith("Provision_1") for r in amends), (
            f"expected Provision_1 under amends; got {[r['provision'] for r in amends]}"
        )

    def test_classifies_amended_by_branch(self, seeded_graph: Graph):
        # Provision_1 estleg:amendedBy Act_2.
        query = _PROVISIONS_BY_KEYWORD_QUERY.format(keyword=_safe_keyword("provision"))
        rows = _rows(seeded_graph, query)
        amended_by = [r for r in rows if r["relation"] == PREDICATES.AMENDED_BY]
        assert any(r["provision"].endswith("Provision_1") for r in amended_by)

    def test_classifies_transposes_directive_branch(self, seeded_graph: Graph):
        # Provision_1 estleg:transposesDirective EU_Dir_1.
        query = _PROVISIONS_BY_KEYWORD_QUERY.format(keyword=_safe_keyword("provision"))
        rows = _rows(seeded_graph, query)
        td = [r for r in rows if r["relation"] == PREDICATES.TRANSPOSES_DIRECTIVE]
        assert any(r["provision"].endswith("Provision_1") for r in td)

    def test_classifies_interpreted_by_branch(self, seeded_graph: Graph):
        # Provision_1 estleg:interpretedBy CourtDecision_1.
        query = _PROVISIONS_BY_KEYWORD_QUERY.format(keyword=_safe_keyword("provision"))
        rows = _rows(seeded_graph, query)
        ib = [r for r in rows if r["relation"] == PREDICATES.INTERPRETED_BY]
        assert any(r["provision"].endswith("Provision_1") for r in ib)

    def test_fallback_references_branch_for_unrelated_match(self, seeded_graph: Graph):
        # Every keyword match shows up under the "references" fallback at
        # least once — ensures the card never silently drops items.
        query = _PROVISIONS_BY_KEYWORD_QUERY.format(keyword=_safe_keyword("provision"))
        rows = _rows(seeded_graph, query)
        refs = [r for r in rows if r["relation"] == PREDICATES.REFERENCES]
        # Every distinct keyword-matched provision must have a fallback row.
        ref_provisions = {r["provision"] for r in refs}
        all_provisions = {r["provision"] for r in rows}
        assert ref_provisions == all_provisions, (
            "every keyword-matched provision should appear under references; "
            f"missing: {all_provisions - ref_provisions}"
        )


# ---------------------------------------------------------------------------
# EU directives query
# ---------------------------------------------------------------------------


class TestEuDirectivesQuery:
    def test_returns_relation_column(self, seeded_graph: Graph):
        query = _EU_DIRECTIVES_QUERY.format(keyword=_safe_keyword("directive"))
        rows = _rows(seeded_graph, query)
        assert rows
        for row in rows:
            assert "relation" in row
            assert row["relation"]

    def test_classifies_transposes_directive_branch(self, seeded_graph: Graph):
        # Act_1 estleg:transposesDirective EU_Dir_2.
        query = _EU_DIRECTIVES_QUERY.format(keyword=_safe_keyword("directive"))
        rows = _rows(seeded_graph, query)
        td = [r for r in rows if r["relation"] == PREDICATES.TRANSPOSES_DIRECTIVE]
        assert any(r["directive"].endswith("EU_Dir_2") for r in td)
        # Also: Provision_1 transposesDirective EU_Dir_1.
        assert any(r["directive"].endswith("EU_Dir_1") for r in td)

    def test_classifies_transposed_by_branch(self, seeded_graph: Graph):
        # EU_Dir_1 estleg:transposedBy Act_1.
        query = _EU_DIRECTIVES_QUERY.format(keyword=_safe_keyword("directive"))
        rows = _rows(seeded_graph, query)
        tb = [r for r in rows if r["relation"] == PREDICATES.TRANSPOSED_BY]
        assert any(r["directive"].endswith("EU_Dir_1") for r in tb)

    def test_classifies_harmonised_with_branch(self, seeded_graph: Graph):
        # Provision_1 estleg:harmonisedWith EU_Dir_1.
        query = _EU_DIRECTIVES_QUERY.format(keyword=_safe_keyword("directive"))
        rows = _rows(seeded_graph, query)
        hw = [r for r in rows if r["relation"] == PREDICATES.HARMONISED_WITH]
        assert any(r["directive"].endswith("EU_Dir_1") for r in hw)


# ---------------------------------------------------------------------------
# Court decisions query
# ---------------------------------------------------------------------------


class TestCourtDecisionsQuery:
    def test_returns_relation_column(self, seeded_graph: Graph):
        query = _COURT_DECISIONS_QUERY.format(keyword=_safe_keyword("court"))
        rows = _rows(seeded_graph, query)
        assert rows
        for row in rows:
            assert "relation" in row
            assert row["relation"]

    def test_classifies_interprets_law_branch(self, seeded_graph: Graph):
        # CourtDecision_1 estleg:interpretsLaw Provision_1.
        query = _COURT_DECISIONS_QUERY.format(keyword=_safe_keyword("court"))
        rows = _rows(seeded_graph, query)
        il = [r for r in rows if r["relation"] == PREDICATES.INTERPRETS_LAW]
        assert any(r["decision"].endswith("CourtDecision_1") for r in il)

    def test_classifies_interpreted_by_branch(self, seeded_graph: Graph):
        # Provision_1 estleg:interpretedBy CourtDecision_1.
        query = _COURT_DECISIONS_QUERY.format(keyword=_safe_keyword("court"))
        rows = _rows(seeded_graph, query)
        ib = [r for r in rows if r["relation"] == PREDICATES.INTERPRETED_BY]
        assert any(r["decision"].endswith("CourtDecision_1") for r in ib)


# ---------------------------------------------------------------------------
# Topic clusters query
# ---------------------------------------------------------------------------


class TestTopicClustersQuery:
    def test_returns_relation_column(self, seeded_graph: Graph):
        query = _TOPIC_CLUSTERS_QUERY.format(keyword=_safe_keyword("cluster"))
        rows = _rows(seeded_graph, query)
        assert rows
        for row in rows:
            assert "relation" in row
            assert row["relation"]

    def test_classifies_requested_cluster_branch(self, seeded_graph: Graph):
        # Provision_1 estleg:requestedCluster Cluster_1.
        query = _TOPIC_CLUSTERS_QUERY.format(keyword=_safe_keyword("cluster"))
        rows = _rows(seeded_graph, query)
        rc = [r for r in rows if r["relation"] == PREDICATES.REQUESTED_CLUSTER]
        assert any(r["cluster"].endswith("Cluster_1") for r in rc)

    def test_classifies_topic_cluster_branch(self, seeded_graph: Graph):
        # Provision_2 estleg:topicCluster Cluster_1.
        query = _TOPIC_CLUSTERS_QUERY.format(keyword=_safe_keyword("cluster"))
        rows = _rows(seeded_graph, query)
        tc = [r for r in rows if r["relation"] == PREDICATES.TOPIC_CLUSTER]
        assert any(r["cluster"].endswith("Cluster_1") for r in tc)


# ---------------------------------------------------------------------------
# Renderer
# ---------------------------------------------------------------------------


class TestResearchCategoryCardGrouping:
    """The card renderer groups items by Estonian legal phrase."""

    def test_empty_items_renders_zero(self):
        html = to_xml(_research_category_card("Sätted", [], "provision"))
        assert "Sätted: 0" in html
        assert "Tulemusi ei leitud" in html

    def test_renders_overall_count(self):
        items = [
            {"uri": "uri:1", "label": "P1", "relation": PREDICATES.AMENDS},
            {"uri": "uri:2", "label": "P2", "relation": PREDICATES.INTERPRETED_BY},
            {"uri": "uri:3", "label": "P3", "relation": PREDICATES.TRANSPOSES_DIRECTIVE},
        ]
        html = to_xml(_research_category_card("Sätted", items, "provision"))
        assert "Sätted: 3" in html

    def test_groups_by_legal_phrase_and_shows_per_group_count(self):
        items = [
            {"uri": "uri:1", "label": "P1", "relation": PREDICATES.AMENDS},
            {"uri": "uri:2", "label": "P2", "relation": PREDICATES.AMENDS},
            {"uri": "uri:3", "label": "P3", "relation": PREDICATES.INTERPRETED_BY},
            {"uri": "uri:4", "label": "P4", "relation": PREDICATES.TRANSPOSES_DIRECTIVE},
        ]
        html = to_xml(_research_category_card("Sätted", items, "provision"))
        # Estonian legal phrases from app.ontology.relations.LEGAL_PHRASES
        assert "muudab: 2" in html
        assert "on tõlgendatud: 1" in html
        assert "võtab üle direktiivi: 1" in html

    def test_legacy_items_without_relation_fall_back_to_viitab(self):
        # Backwards-compatibility: cached payloads without a relation
        # field still render under the neutral "viitab" group.
        items = [
            {"uri": "uri:1", "label": "P1"},
            {"uri": "uri:2", "label": "P2"},
        ]
        html = to_xml(_research_category_card("Sätted", items, "provision"))
        assert "Sätted: 2" in html
        assert "viitab: 2" in html

    def test_includes_explorer_focus_links_per_item(self):
        items = [
            {
                "uri": "https://data.riik.ee/ontology/estleg#Provision_1",
                "label": "Säte 1",
                "relation": PREDICATES.AMENDS,
            },
        ]
        html = to_xml(_research_category_card("Sätted", items, "provision"))
        assert "Ava õiguskaardil" in html

    def test_topic_cluster_phrase_is_kuulub_teemavaldkonda(self):
        items = [
            {"uri": "uri:c1", "label": "Cluster 1", "relation": PREDICATES.REQUESTED_CLUSTER},
        ]
        html = to_xml(_research_category_card("Teemaklastrid", items, "cluster"))
        assert "kuulub teemavaldkonda: 1" in html

    def test_eu_relation_phrases(self):
        items = [
            {"uri": "uri:e1", "label": "Dir 1", "relation": PREDICATES.TRANSPOSES_DIRECTIVE},
            {"uri": "uri:e2", "label": "Dir 2", "relation": PREDICATES.HARMONISED_WITH},
        ]
        html = to_xml(_research_category_card("EL-i õigusaktid", items, "eu"))
        assert "võtab üle direktiivi: 1" in html
        assert "on harmoneeritud aktiga: 1" in html

    def test_preserves_top_10_per_group(self):
        # 15 items in one group → only 10 should render as <li>.
        items = [
            {"uri": f"uri:{i}", "label": f"P{i}", "relation": PREDICATES.AMENDS} for i in range(15)
        ]
        html = to_xml(_research_category_card("Sätted", items, "provision"))
        # Overall + per-group count both reflect the full 15.
        assert "Sätted: 15" in html
        assert "muudab: 15" in html
        # Only the first 10 items render in the <ul>.
        assert "P0" in html
        assert "P9" in html
        assert "P10" not in html
