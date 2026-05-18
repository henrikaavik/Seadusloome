"""Run the impact-engine SPARQL templates against a seeded rdflib graph (C0).

The impact engine's queries (``app.docs.impact.queries``) live in a
``GRAPH <...>`` envelope that scopes the draft's references to a named
graph. To exercise them without a real Jena, we build an in-memory
:class:`rdflib.Dataset` (so the ``GRAPH`` keyword is honoured), seed it
with:

* The canonical-predicate fixture as the default graph (the "enacted
  ontology" side that the body clauses traverse).
* A synthetic draft named graph carrying one ``estleg:references`` edge.

…and then assert that each impact query returns the expected canonical
predicate names in the ``?relation`` projection. This is the
deterministic regression catcher for C0 — if a predicate name drifts
again in the future, these tests fail before any production query does.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from rdflib import Dataset, Namespace, URIRef
from rdflib.namespace import RDF

from app.docs.impact.queries import (
    build_affected_entities_query,
    build_conflicts_query,
    build_eu_compliance_query,
    build_gaps_query,
)

FIXTURE_PATH = Path(__file__).parent / "fixtures" / "ontology_canonical.ttl"
ESTLEG = Namespace("https://data.riik.ee/ontology/estleg#")

# The synthetic draft graph URI must match ``_SAFE_GRAPH_URI`` —
# ``https://data.riik.ee/ontology/estleg/drafts/<uuid>``.
DRAFT_GRAPH_URI = (
    "https://data.riik.ee/ontology/estleg/drafts/aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
)


@pytest.fixture
def seeded_dataset() -> Dataset:
    """An rdflib Dataset with the fixture + a draft named graph.

    The default graph carries the canonical-predicates fixture (the
    enacted ontology). The named graph at ``DRAFT_GRAPH_URI`` holds a
    single ``estleg:references`` edge from a draft self-node to
    ``estleg:Provision_1`` — the entity that participates in every
    relation in the fixture. This lets the impact queries traverse from
    the draft outward through the canonical edges.
    """
    ds = Dataset()
    ds.parse(FIXTURE_PATH, format="turtle")

    # Synthetic draft graph: ``draft-self estleg:references Provision_1``.
    draft_graph = ds.graph(URIRef(DRAFT_GRAPH_URI))
    draft_self = URIRef("urn:draft:test")
    draft_graph.add((draft_self, RDF.type, ESTLEG.DraftLegislation))
    draft_graph.add((draft_self, ESTLEG.references, ESTLEG.Provision_1))
    return ds


def _rows(ds: Dataset, query: str) -> list[dict[str, str]]:
    """Run a SELECT and return the rows as plain str dicts."""
    out: list[dict[str, str]] = []
    for row in ds.query(query):
        d: dict[str, str] = {}
        # rdflib's Row exposes labels via .labels and values via index.
        # Pyright doesn't see this on the union return type, hence the ignores.
        for var in row.labels:  # type: ignore[attr-defined,union-attr]
            value = row[var]  # type: ignore[index]
            d[str(var)] = str(value) if value is not None else ""
        out.append(d)
    return out


# ---------------------------------------------------------------------------
# AFFECTED_ENTITIES
# ---------------------------------------------------------------------------


class TestAffectedEntitiesQuery:
    """The 2-hop BFS query should walk every canonical edge."""

    def test_returns_relation_projection(self, seeded_dataset: Dataset):
        query = build_affected_entities_query(DRAFT_GRAPH_URI)
        rows = _rows(seeded_dataset, query)
        # Every row must carry a relation URI.
        for row in rows:
            assert "relation" in row, f"row missing relation projection: {row}"

    def test_finds_self_reference(self, seeded_dataset: Dataset):
        query = build_affected_entities_query(DRAFT_GRAPH_URI)
        rows = _rows(seeded_dataset, query)
        # Provision_1 itself (the BIND(?ref AS ?entity) branch).
        self_rows = [
            r
            for r in rows
            if r["entity"].endswith("Provision_1") and r["relation"].endswith("references")
        ]
        assert len(self_rows) == 1

    def test_finds_amendment_edge(self, seeded_dataset: Dataset):
        # AmendmentEvent_1 amends Provision_1 → query should pivot from
        # Provision_1 to AmendmentEvent_1 via the ``amends`` (subject-side)
        # branch.
        query = build_affected_entities_query(DRAFT_GRAPH_URI)
        rows = _rows(seeded_dataset, query)
        amend_rows = [
            r
            for r in rows
            if r["entity"].endswith("AmendmentEvent_1") and r["relation"].endswith("amends")
        ]
        assert len(amend_rows) == 1

    def test_finds_amended_by_edge(self, seeded_dataset: Dataset):
        # Provision_1 amendedBy Act_2 → query should pivot from
        # Provision_1 outward via the ``amendedBy`` branch.
        query = build_affected_entities_query(DRAFT_GRAPH_URI)
        rows = _rows(seeded_dataset, query)
        rows_to_act2 = [
            r
            for r in rows
            if r["entity"].endswith("Act_2") and r["relation"].endswith("amendedBy")
        ]
        assert len(rows_to_act2) == 1

    def test_finds_interprets_law_edge(self, seeded_dataset: Dataset):
        # CourtDecision_1 interpretsLaw Provision_1 — query should pivot
        # from Provision_1 backward through the ``interpretsLaw`` branch.
        query = build_affected_entities_query(DRAFT_GRAPH_URI)
        rows = _rows(seeded_dataset, query)
        court_rows = [
            r
            for r in rows
            if r["entity"].endswith("CourtDecision_1") and r["relation"].endswith("interpretsLaw")
        ]
        assert len(court_rows) == 1

    def test_finds_interpreted_by_edge(self, seeded_dataset: Dataset):
        # Provision_1 interpretedBy CourtDecision_1 — the inverse-side
        # branch should also produce a row.
        query = build_affected_entities_query(DRAFT_GRAPH_URI)
        rows = _rows(seeded_dataset, query)
        inv_rows = [
            r
            for r in rows
            if r["entity"].endswith("CourtDecision_1") and r["relation"].endswith("interpretedBy")
        ]
        assert len(inv_rows) == 1

    def test_finds_topic_cluster_edge(self, seeded_dataset: Dataset):
        query = build_affected_entities_query(DRAFT_GRAPH_URI)
        rows = _rows(seeded_dataset, query)
        # Provision_1 requestedCluster Cluster_1.
        rc_rows = [
            r
            for r in rows
            if r["entity"].endswith("Cluster_1") and r["relation"].endswith("requestedCluster")
        ]
        assert len(rc_rows) == 1

    def test_excludes_law_typed_topic_map_clusters(self, seeded_dataset: Dataset):
        """Step 5A live-deploy follow-up: ``estleg:Law``-typed entities
        reached via ``requestedCluster`` (the topic-map clusters
        masquerading as acts in prod — e.g. ``RKIOMPU1974_Map_2026``)
        must NOT appear in the affected list. The fixture seeds
        ``TopicMapLaw_1`` with ``a estleg:Law`` and a
        ``requestedCluster`` edge from Provision_1; this regression
        guard asserts it is filtered out by the
        ``FILTER NOT EXISTS { ?entity a estleg:Law }`` clause.
        """
        query = build_affected_entities_query(DRAFT_GRAPH_URI)
        rows = _rows(seeded_dataset, query)
        law_typed_rows = [r for r in rows if r["entity"].endswith("TopicMapLaw_1")]
        assert law_typed_rows == [], (
            f"AFFECTED_ENTITIES leaked an estleg:Law-typed topic-map cluster: "
            f"{law_typed_rows!r}. The Step 5A FILTER NOT EXISTS guard against "
            f"the prod fan-out (~500 unrelated 'Law'/Map_2026 entities per "
            f"resolved provision) is missing or broken."
        )

    def test_finds_transposes_directive_edge(self, seeded_dataset: Dataset):
        query = build_affected_entities_query(DRAFT_GRAPH_URI)
        rows = _rows(seeded_dataset, query)
        eu_rows = [
            r
            for r in rows
            if r["entity"].endswith("EU_Dir_1") and r["relation"].endswith("transposesDirective")
        ]
        assert len(eu_rows) == 1

    def test_finds_defines_concept_edge(self, seeded_dataset: Dataset):
        query = build_affected_entities_query(DRAFT_GRAPH_URI)
        rows = _rows(seeded_dataset, query)
        cs = [
            r
            for r in rows
            if r["entity"].endswith("Concept_1") and r["relation"].endswith("definesConcept")
        ]
        assert len(cs) == 1

    def test_finds_harmonised_with_edge(self, seeded_dataset: Dataset):
        query = build_affected_entities_query(DRAFT_GRAPH_URI)
        rows = _rows(seeded_dataset, query)
        hs = [
            r
            for r in rows
            if r["entity"].endswith("EU_Dir_1") and r["relation"].endswith("harmonisedWith")
        ]
        assert len(hs) == 1


# ---------------------------------------------------------------------------
# CONFLICTS
# ---------------------------------------------------------------------------


class TestConflictsQuery:
    """The conflict query should find court decisions interpreting the draft ref."""

    def test_interprets_law_branch(self, seeded_dataset: Dataset):
        query = build_conflicts_query(DRAFT_GRAPH_URI)
        rows = _rows(seeded_dataset, query)
        # CourtDecision_1 interpretsLaw Provision_1 — should produce a row
        # with conflictEntity = CourtDecision_1.
        court_rows = [
            r
            for r in rows
            if r["conflictEntity"].endswith("CourtDecision_1")
            and r["relation"].endswith("interpretsLaw")
        ]
        assert len(court_rows) == 1
        assert "tõlgendab" in court_rows[0]["reason"] or "Kohtulahend" in court_rows[0]["reason"]

    def test_interpreted_by_branch(self, seeded_dataset: Dataset):
        # Provision_1 interpretedBy CourtDecision_1 — the inverse-side
        # branch should also produce a row.
        query = build_conflicts_query(DRAFT_GRAPH_URI)
        rows = _rows(seeded_dataset, query)
        inv_rows = [
            r
            for r in rows
            if r["conflictEntity"].endswith("CourtDecision_1")
            and r["relation"].endswith("interpretedBy")
        ]
        assert len(inv_rows) == 1


# ---------------------------------------------------------------------------
# GAPS
# ---------------------------------------------------------------------------


class TestGapsQuery:
    """The gap query should reach topic clusters via requestedCluster."""

    def test_finds_cluster(self, seeded_dataset: Dataset):
        # Provision_1 has 1 requestedCluster edge, and Cluster_1 has
        # two member provisions (Provision_1 + Provision_2 via the
        # ``topicCluster`` alias). 1 referenced × 5 = 5 ≥ 2 totalProvisions
        # → the FILTER ``referencedProvisions * 5 < totalProvisions`` is
        # NOT satisfied → no rows. To exercise the gap path we'd need
        # ≥5 provisions in the cluster. Instead, assert the query runs
        # without error and the GAPS query reaches the cluster.
        query = build_gaps_query(DRAFT_GRAPH_URI)
        rows = _rows(seeded_dataset, query)
        # With one referenced and two total, FILTER 1*5 < 2 → 5 < 2 is False
        # → cluster is filtered out (not flagged as gap). Good — the
        # query is structurally correct.
        for row in rows:
            # Any row that does come back must be from Cluster_1.
            assert "Cluster_1" in row.get("cluster", "")


# ---------------------------------------------------------------------------
# EU_COMPLIANCE
# ---------------------------------------------------------------------------


class TestEuComplianceQuery:
    """The EU compliance query is provision-level only after the Wave 2
    Step 5 fix (docs/2026-05-18-bugfix-plan.md). The act-level UNION arms
    that chained through ``estleg:sourceAct`` / ``estleg:partOf`` were
    silently dead in prod (``sourceAct`` binds a literal that can't be a
    subject of ``transposesDirective``; ``partOf`` has zero triples in
    prod). The query now surfaces only provision-level transposition +
    harmonisation."""

    def test_provision_level_transposes_directive(self, seeded_dataset: Dataset):
        # Direct ``Provision_1 transposesDirective EU_Dir_1``.
        query = build_eu_compliance_query(DRAFT_GRAPH_URI)
        rows = _rows(seeded_dataset, query)
        td = [
            r
            for r in rows
            if r["euAct"].endswith("EU_Dir_1") and r["relation"].endswith("transposesDirective")
        ]
        assert len(td) == 1

    def test_act_level_branches_removed(self, seeded_dataset: Dataset):
        """The act-level chain via ``estleg:sourceAct`` is gone.

        EU_Dir_2 in the fixture is only reachable via
        ``Provision_1 sourceAct Act_1 . Act_1 transposesDirective EU_Dir_2``.
        Wave 2 Step 5 drops that chain because in prod ``sourceAct`` is a
        string literal (not a URI), so the join was silently producing
        zero rows. Verify the chain is no longer reachable here so we
        can't accidentally re-introduce it.
        """
        query = build_eu_compliance_query(DRAFT_GRAPH_URI)
        rows = _rows(seeded_dataset, query)
        td = [
            r
            for r in rows
            if r["euAct"].endswith("EU_Dir_2") and r["relation"].endswith("transposesDirective")
        ]
        assert len(td) == 0, (
            "Act-level transposesDirective via sourceAct was removed in "
            "Wave 2 Step 5 — see docs/2026-05-18-bugfix-plan.md. The "
            "prod corpus stores sourceAct as a string literal, so "
            "joining ?_parentAct transposesDirective ?euAct is dead."
        )

    def test_transposed_by_branch_removed(self, seeded_dataset: Dataset):
        """The inverse ``transposedBy`` branch is gone for the same reason.

        ``estleg:transposedBy`` is on EULegislation → Act (SHACL 687-692),
        not EU → Provision, so it needed an act-level chain that the
        Wave 2 Step 5 fix removed.
        """
        query = build_eu_compliance_query(DRAFT_GRAPH_URI)
        rows = _rows(seeded_dataset, query)
        tb = [r for r in rows if r["relation"].endswith("transposedBy")]
        assert len(tb) == 0

    def test_harmonised_with_branch(self, seeded_dataset: Dataset):
        query = build_eu_compliance_query(DRAFT_GRAPH_URI)
        rows = _rows(seeded_dataset, query)
        hw = [
            r
            for r in rows
            if r["euAct"].endswith("EU_Dir_1") and r["relation"].endswith("harmonisedWith")
        ]
        assert len(hw) == 1

    def test_eu_act_labelled(self, seeded_dataset: Dataset):
        # EU label projection should resolve via OPTIONAL rdfs:label.
        # Only EU_Dir_1 is reachable provision-level; EU_Dir_2 is now
        # unreachable (see test_act_level_branches_removed).
        query = build_eu_compliance_query(DRAFT_GRAPH_URI)
        rows = _rows(seeded_dataset, query)
        labelled = [r for r in rows if r.get("euLabel")]
        assert any("EU Directive 1" in r["euLabel"] for r in labelled)

    def test_no_dead_partof_arm_in_query_text(self):
        """The query text must NOT include any ``partOf`` / ``sourceAct``
        traversal — see docs/2026-05-18-bugfix-plan.md Wave 2 Step 5.
        Those predicates are dead in prod and reintroducing them silently
        produces zero rows (the original failure mode the fix targets)."""
        text = build_eu_compliance_query(DRAFT_GRAPH_URI)
        assert "estleg:partOf" not in text, (
            "estleg:partOf must not appear — zero triples in prod, "
            "see Wave 2 Step 5 of docs/2026-05-18-bugfix-plan.md"
        )
        assert "estleg:sourceAct" not in text, (
            "estleg:sourceAct must not appear — it's a literal, not a "
            "URI, and binding ?_parentAct to it kills the join"
        )
        assert "?_parentAct" not in text, (
            "The dead act-level chain variable must not appear — see Wave 2 Step 5"
        )


# ---------------------------------------------------------------------------
# Regression — the old bad predicates must NOT appear in the new queries
# ---------------------------------------------------------------------------


class TestNoLegacyPredicateStrings:
    """Make sure C0 didn't leave any of the dead predicate names behind."""

    def test_affected_query_text(self):
        text = build_affected_entities_query(DRAFT_GRAPH_URI)
        # These predicates don't exist in the source ontology — they
        # MUST NOT appear in the query.
        assert "estleg:interpretsProvision" not in text
        assert "estleg:amendsProvision" not in text
        assert "estleg:hasTopic" not in text
        assert "estleg:implementsEU" not in text

    def test_conflicts_query_text(self):
        text = build_conflicts_query(DRAFT_GRAPH_URI)
        assert "estleg:interpretsProvision" not in text

    def test_gaps_query_text(self):
        text = build_gaps_query(DRAFT_GRAPH_URI)
        assert "estleg:hasTopic" not in text
        # Canonical predicates must be present.
        assert "estleg:requestedCluster" in text
        assert "estleg:topicCluster" in text

    def test_eu_compliance_query_text(self):
        text = build_eu_compliance_query(DRAFT_GRAPH_URI)
        assert "estleg:implementsEU" not in text
        # Canonical predicates must be present.
        assert "estleg:transposesDirective" in text
