"""Convert a :class:`Draft` + resolved references into Turtle.

The impact analyzer needs the draft represented as an RDF graph so
SPARQL property paths can traverse from the draft to every affected
ontology entity. This module owns that conversion step.

Design decisions:

* The draft itself becomes an ``estleg:DraftLegislation`` instance at
  ``<graph_uri>#self``. Using a fragment IRI keeps the draft's
  subject distinct from the graph URI (SPARQL queries GRAPH-select
  against the graph URI, but the triples inside use ``#self`` as the
  subject) without forcing the caller to pass a second IRI.
* Only resolved references (``entity_uri is not None``) become
  ``estleg:references`` triples. Unresolved refs are dropped from
  the graph — they still live in ``draft_entities`` for the UI but
  they cannot participate in SPARQL traversal so there is no point
  materialising them.
* Confidence scores are attached as blank-node annotations using a
  minimal ``estleg:refConfidence`` structure so the analyzer can
  later rank affected entities by extraction confidence without a
  schema migration.
* Estonian characters (õ, ä, ö, ü, š, ž) are preserved by relying on
  rdflib's Turtle serialiser; we never hand-roll the output.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import TYPE_CHECKING

from rdflib import Graph, Literal, Namespace, URIRef
from rdflib.namespace import RDF, RDFS, XSD

if TYPE_CHECKING:
    from app.docs.draft_model import Draft
    from app.docs.reference_resolver import ResolvedRef

logger = logging.getLogger(__name__)


ESTLEG = Namespace("https://data.riik.ee/ontology/estleg#")
USER_NS = Namespace("urn:user:")


def build_draft_graph(draft: Draft, refs: list[ResolvedRef]) -> str:
    """Return the Turtle serialisation of *draft* + resolved *refs*.

    The emitted graph contains one primary subject (the draft) plus
    one ``estleg:references`` edge per resolved reference. Only refs
    with ``entity_uri is not None`` are serialised; unresolved refs
    are skipped (see module docstring for why).

    Args:
        draft: The :class:`Draft` row to serialise. ``draft.id``,
            ``draft.graph_uri``, ``draft.title``, ``draft.filename``,
            ``draft.user_id`` and ``draft.created_at`` are all read.
        refs: Every :class:`ResolvedRef` the extractor produced for
            this draft. Callers typically pass the full output of
            :func:`app.docs.reference_resolver.resolve_refs`, not a
            pre-filtered list — we drop unresolved refs internally.

    Returns:
        Turtle text (UTF-8) ready to POST to Jena's Graph Store
        Protocol endpoint via :func:`app.sync.jena_loader.put_named_graph`.
    """
    g = Graph()
    g.bind("estleg", ESTLEG)
    g.bind("rdfs", RDFS)
    g.bind("xsd", XSD)

    draft_iri = URIRef(f"{draft.graph_uri}#self")

    # Core draft triples.
    g.add((draft_iri, RDF.type, ESTLEG.DraftLegislation))
    g.add((draft_iri, RDFS.label, Literal(draft.title)))
    g.add((draft_iri, ESTLEG.filename, Literal(draft.filename)))
    g.add((draft_iri, ESTLEG.uploadedBy, URIRef(f"{USER_NS}{draft.user_id}")))
    g.add(
        (
            draft_iri,
            ESTLEG.uploadedAt,
            Literal(_isoformat(draft.created_at), datatype=XSD.dateTime),
        )
    )

    # References — one edge per resolved ref.
    written = 0
    for ref in refs:
        entity_uri = ref.entity_uri
        if not entity_uri:
            continue
        try:
            entity_iri = URIRef(entity_uri)
        except Exception:  # noqa: BLE001 — malformed URIs should never kill the graph build
            logger.warning(
                "build_draft_graph: skipping ref with malformed URI: %r",
                entity_uri,
            )
            continue
        g.add((draft_iri, ESTLEG.references, entity_iri))

        # Confidence annotation as a blank node. Keeping it as a BNode
        # means the analyzer can OPTIONAL-match it without bloating the
        # primary ``estleg:references`` edges and without needing a new
        # class in the ontology.
        confidence = getattr(ref.extracted, "confidence", None)
        if confidence is not None:
            try:
                conf_val = float(confidence)
            except (TypeError, ValueError):
                conf_val = None
            if conf_val is not None:
                from rdflib import BNode

                bnode = BNode()
                g.add((draft_iri, ESTLEG.refConfidence, bnode))
                g.add((bnode, ESTLEG.aboutEntity, entity_iri))
                g.add(
                    (
                        bnode,
                        ESTLEG.confidenceScore,
                        Literal(conf_val, datatype=XSD.decimal),
                    )
                )
        written += 1

    logger.info(
        "build_draft_graph: serialised draft %s with %d references (%d total refs)",
        draft.id,
        written,
        len(refs),
    )

    # rdflib returns bytes when given `format="turtle"` in some
    # versions and str in others; normalise to str so the caller
    # can hand it straight to the Graph Store Protocol helper.
    serialised = g.serialize(format="turtle")
    if isinstance(serialised, bytes):
        return serialised.decode("utf-8")
    return serialised


def _isoformat(value: datetime | None) -> str:
    """Return an ISO-8601 timestamp suitable for ``xsd:dateTime``.

    ``datetime.isoformat()`` emits the right format for rdflib when
    the timezone is set; callers always pass ``created_at`` from
    Postgres which comes back as a timezone-aware ``datetime``.
    Returns an empty string on ``None`` so the Literal still round-trips.
    """
    if value is None:
        return ""
    return value.isoformat()
