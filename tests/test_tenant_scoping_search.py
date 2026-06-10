"""Tenant-scoping regression tests for global entity search (#844 A4).

DoD verification:
- Global search explicitly scopes to public/default ontology data and
  does not depend on Fuseki ``unionDefaultGraph`` behaviour.
- A test with a *mocked configuration that simulates union-default*
  behaviour proves draft labels can never appear in search results.

The simulated-union scenario flattens a dataset's named graphs into the
default graph (exactly what ``unionDefaultGraph=true`` does on Fuseki) so
we can assert the scoped query still excludes draft labels.
"""

from __future__ import annotations

from rdflib import Dataset, Graph, Literal, Namespace, URIRef
from rdflib.namespace import RDF, RDFS

from app.ontology.queries import SEARCH_ENTITIES

EST = Namespace("https://data.riik.ee/ontology/estleg#")

_DRAFT_GRAPH = "https://data.riik.ee/ontology/estleg/drafts/aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
_DRAFT_SELF = _DRAFT_GRAPH + "#self"
_ADHOC_GRAPH = "https://data.riik.ee/ontology/estleg/adhoc/cccccccc-cccc-cccc-cccc-cccccccccccc"
_ADHOC_SELF = _ADHOC_GRAPH + "#self"


def _build_dataset() -> Dataset:
    """Public entity in the default graph + a private draft + an adhoc probe.

    All three carry a ``rdfs:label`` containing the same search token so a
    naive search would match every one of them.
    """
    ds = Dataset()
    # Public ontology entity (default graph).
    ds.add((EST.KalapyygiProv, RDF.type, EST.LegalProvision))
    ds.add((EST.KalapyygiProv, RDFS.label, Literal("Kalapüügi avalik säte")))
    # Org-private draft (named graph).
    dg = ds.graph(URIRef(_DRAFT_GRAPH))
    dg.add((URIRef(_DRAFT_SELF), RDF.type, EST.DraftLegislation))
    dg.add((URIRef(_DRAFT_SELF), RDFS.label, Literal("Kalapüügi SALAJANE eelnõu")))
    # Ephemeral adhoc probe (named graph).
    ag = ds.graph(URIRef(_ADHOC_GRAPH))
    ag.add((URIRef(_ADHOC_SELF), RDF.type, EST.DraftLegislation))
    ag.add((URIRef(_ADHOC_SELF), RDFS.label, Literal("Kalapüügi ajutine sond")))
    return ds


def _flatten_to_union(ds: Dataset) -> Graph:
    """Collapse every quad into a single Graph — simulates unionDefaultGraph.

    On Fuseki with ``unionDefaultGraph=true`` the default graph the query
    engine sees is the union of all named graphs; this mirrors that worst
    case so the scoping FILTER is exercised against draft triples that
    have "bled into" the default graph.
    """
    g = Graph()
    for s, p, o, _c in ds.quads((None, None, None, None)):
        g.add((s, p, o))
    return g


def _labels(graph, query: str) -> list[str]:
    """Return the ``?label`` projection of *query* as a sorted str list."""
    out: list[str] = []
    for row in graph.query(query):
        # SEARCH_ENTITIES projects ``?entity ?label ?type``; pick the
        # label binding by name (index 0 is the entity URI).
        try:
            value = row["label"]  # type: ignore[index]
        except (KeyError, TypeError):
            value = row[0]
        if value is not None:
            out.append(str(value))
    return sorted(out)


class TestSearchScoping:
    def test_query_text_carries_public_filter(self):
        q = SEARCH_ENTITIES.format(search_pattern="kala", limit=20)
        assert "STRSTARTS" in q
        assert "estleg/drafts/" in q
        assert "estleg/adhoc/" in q

    def test_default_graph_only_excludes_drafts(self):
        """Normal config: draft triples live in named graphs. A
        default-graph query already excludes them, and the scoped query
        still returns the public row."""
        ds = _build_dataset()
        # rdflib Dataset.query targets the default graph only.
        q = SEARCH_ENTITIES.format(search_pattern="Kalapüügi", limit=20)
        out = _labels(ds, q)
        assert "Kalapüügi avalik säte" in out
        assert all("SALAJANE" not in label for label in out)
        assert all("sond" not in label for label in out)

    def test_simulated_union_default_still_excludes_drafts(self):
        """The critical test: even with ``unionDefaultGraph`` simulated
        (draft triples flattened into the default graph), the scoped
        search must NOT return the private draft / adhoc labels."""
        union = _flatten_to_union(_build_dataset())

        # Sanity: a NAIVE (unscoped) query DOES leak under union-default.
        naive = """
        PREFIX rdfs: <http://www.w3.org/2000/01/rdf-schema#>
        SELECT DISTINCT ?label WHERE {
            ?entity rdfs:label ?label .
            ?entity rdf:type ?type .
            FILTER(REGEX(?label, "Kalapüügi", "i"))
        }
        """
        naive_labels = _labels(union, naive)
        assert "Kalapüügi SALAJANE eelnõu" in naive_labels, (
            "union-default simulation is not exercising the leak — fix the fixture"
        )

        # The scoped query must drop the draft + adhoc labels.
        scoped = SEARCH_ENTITIES.format(search_pattern="Kalapüügi", limit=20)
        scoped_labels = _labels(union, scoped)
        assert "Kalapüügi avalik säte" in scoped_labels
        assert "Kalapüügi SALAJANE eelnõu" not in scoped_labels, (
            "A4 FAILED: a private draft label surfaced in global search under "
            "simulated unionDefaultGraph"
        )
        assert "Kalapüügi ajutine sond" not in scoped_labels, (
            "A4 FAILED: an adhoc probe label surfaced in global search"
        )
