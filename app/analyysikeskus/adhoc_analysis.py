"""Run the impact analyser against an ephemeral synthetic named graph (#722).

Epic #714 design note — *mechanism: synthetic ephemeral named graph,
reusing the existing impact queries*. The "Normi mõjuahel" workflow
resolves the user's input to one ontology entity URI, then:

    1. Mints a fresh graph URI ``…/estleg/adhoc/<uuid4>`` (the
       ``adhoc/`` arm of the :data:`app.sync.jena_loader._SAFE_GRAPH_URI`
       allowlist — see #722 widening).
    2. Writes **one triple** into Jena via Graph Store Protocol PUT —
       ``<that-graph> { <adhoc-subject> estleg:references <entityUri> }``
       — mirroring the single ``estleg:references`` edge a draft's named
       graph carries (the analyzer's SPARQL queries pivot off exactly
       that predicate; see :mod:`app.docs.graph_builder` +
       :mod:`app.docs.impact.queries`).
    3. Runs :meth:`app.docs.impact.analyzer.ImpactAnalyzer.analyze` and
       :func:`app.docs.impact.scoring.calculate_impact_score` against
       the graph — exactly as the draft analyze pipeline does.
    4. **Always** ``delete_named_graph``-s the graph in a ``finally`` —
       including on a PUT failure or an analyzer exception — so no
       ephemeral graph ever lingers in Fuseki.

Nothing is persisted: ad-hoc analyses are recomputed on each GET of
``/analyysikeskus/normi-mojuahel`` (C-lite — no ``analysis_runs`` table,
no migration).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from uuid import uuid4

from rdflib import Graph, Namespace, URIRef
from rdflib.namespace import RDF

from app.docs.impact import ImpactAnalyzer, ImpactFindings, calculate_impact_score
from app.sync.jena_loader import delete_named_graph, put_named_graph

logger = logging.getLogger(__name__)

# Same namespace the draft graph builder uses, so the synthetic graph's
# ``estleg:references`` predicate is byte-identical to a real draft's.
_ESTLEG = Namespace("https://data.riik.ee/ontology/estleg#")

# Host + path prefix for the ephemeral graphs. The trailing ``<uuid4>``
# segment is appended per request; the result must match the ``adhoc/``
# arm of ``app.sync.jena_loader._SAFE_GRAPH_URI``.
_ADHOC_GRAPH_PREFIX = "https://data.riik.ee/ontology/estleg/adhoc/"


@dataclass(frozen=True)
class AdhocAnalysisResult:
    """Bundle of an ad-hoc impact analysis run.

    Attributes:
        findings: The :class:`ImpactFindings` the analyzer produced
            (empty lists / zero counts if Jena was unreachable or the
            PUT failed — the route still renders a graceful "nothing
            found" page in that case).
        score: The 0-100 impact score from
            :func:`calculate_impact_score`.
        graph_uri: The ephemeral graph URI that was minted (and, by the
            time this result is returned, already deleted). Surfaced
            only for logging/debugging — the UI never shows it.
    """

    findings: ImpactFindings
    score: int
    graph_uri: str


def _mint_adhoc_graph_uri() -> str:
    """Return a fresh ``…/estleg/adhoc/<uuid4>`` graph URI.

    ``uuid4()`` renders as a 36-char lowercase hyphenated string, which
    is exactly what the allowlist regex's ``[0-9a-f-]{36}`` arm
    expects.
    """
    return f"{_ADHOC_GRAPH_PREFIX}{uuid4()}"


def _build_adhoc_turtle(graph_uri: str, entity_uri: str) -> str:
    """Serialise the one-triple synthetic graph as Turtle.

    The subject is ``<graph_uri>#self`` — the same fragment-IRI trick
    :func:`app.docs.graph_builder.build_draft_graph` uses to keep the
    graph's subject distinct from the graph name. We also assert the
    subject's ``rdf:type`` as ``estleg:DraftLegislation`` so the
    conflict pass's ``?otherDraft a estleg:DraftLegislation`` filter
    treats neighbouring real drafts (not this synthetic node) as the
    "other draft" side — exactly as it does for a real draft graph.
    """
    g = Graph()
    g.bind("estleg", _ESTLEG)
    subject = URIRef(f"{graph_uri}#self")
    g.add((subject, RDF.type, _ESTLEG.DraftLegislation))
    g.add((subject, _ESTLEG.references, URIRef(entity_uri)))
    serialised = g.serialize(format="turtle")
    if isinstance(serialised, bytes):
        return serialised.decode("utf-8")
    return serialised


def run_adhoc_impact_analysis(
    entity_uri: str,
    *,
    analyzer: ImpactAnalyzer | None = None,
) -> AdhocAnalysisResult:
    """Run the impact analyser against a fresh synthetic graph for *entity_uri*.

    Args:
        entity_uri: The resolved ontology entity URI to analyse the
            impact of (a provision, EU act, court decision …). Must be
            a non-empty string; an empty value short-circuits to an
            empty result without touching Jena.
        analyzer: Optional :class:`ImpactAnalyzer` override (tests inject
            one whose ``analyze`` is patched). Defaults to a fresh
            :class:`ImpactAnalyzer`.

    Returns:
        An :class:`AdhocAnalysisResult`. On a Jena PUT failure or an
        analyzer exception the result carries empty findings + score 0
        (the route degrades to "nothing found" rather than 500). The
        ephemeral graph is **always** deleted before this returns,
        including on the failure paths.
    """
    if not entity_uri or not entity_uri.strip():
        return AdhocAnalysisResult(findings=ImpactFindings(), score=0, graph_uri="")

    graph_uri = _mint_adhoc_graph_uri()
    runner = analyzer if analyzer is not None else ImpactAnalyzer()

    try:
        turtle = _build_adhoc_turtle(graph_uri, entity_uri)
        loaded = put_named_graph(graph_uri, turtle)
        if not loaded:
            logger.warning(
                "run_adhoc_impact_analysis: Jena PUT failed for graph=%s entity=%s",
                graph_uri,
                entity_uri,
            )
            return AdhocAnalysisResult(findings=ImpactFindings(), score=0, graph_uri=graph_uri)

        findings = runner.analyze(graph_uri)
        score = calculate_impact_score(findings)
        logger.info(
            "run_adhoc_impact_analysis: entity=%s affected=%d conflicts=%d score=%d",
            entity_uri,
            findings.affected_count,
            findings.conflict_count,
            score,
        )
        return AdhocAnalysisResult(findings=findings, score=score, graph_uri=graph_uri)
    except Exception:
        logger.exception(
            "run_adhoc_impact_analysis: analysis failed for entity=%s graph=%s",
            entity_uri,
            graph_uri,
        )
        return AdhocAnalysisResult(findings=ImpactFindings(), score=0, graph_uri=graph_uri)
    finally:
        # Always tear down the ephemeral graph — even on a PUT failure
        # (delete is idempotent: a 404 counts as success) or a render
        # error upstream. Best-effort: a delete failure is logged inside
        # ``delete_named_graph`` and must not mask the analysis result.
        try:
            delete_named_graph(graph_uri)
        except Exception:
            logger.exception(
                "run_adhoc_impact_analysis: failed to delete ephemeral graph %s",
                graph_uri,
            )
