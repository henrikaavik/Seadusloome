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
* Lineage (doc_type class + ``estleg:basedOn``) is written by a
  **separate** helper (:func:`write_doc_lineage`) that issues a
  SPARQL UPDATE against the draft's existing named graph.  The
  builder above owns the *initial* Turtle upload via PUT-replace;
  the lineage helper mutates the graph in place so relink/unlink
  operations don't have to re-serialise every ``estleg:references``
  triple.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import TYPE_CHECKING

from rdflib import Graph, Literal, Namespace, URIRef
from rdflib.namespace import RDF, RDFS, XSD

from app.sync.jena_loader import _sparql_update, _validate_graph_uri

if TYPE_CHECKING:
    from app.docs.draft_model import Draft
    from app.docs.reference_resolver import ResolvedRef

logger = logging.getLogger(__name__)


ESTLEG = Namespace("https://data.riik.ee/ontology/estleg#")
USER_NS = Namespace("urn:user:")

# Single authoritative source for the draft subject IRI.  Both
# :func:`build_draft_graph` and :func:`write_doc_lineage` need it;
# centralising the shape keeps the two paths in lockstep.
_DRAFT_URI_TEMPLATE = "{graph_uri}#self"


def _draft_uri(draft: Draft) -> str:
    """Return the IRI used as the subject of triples inside *draft*'s graph.

    Mirrors the IRI constructed by :func:`build_draft_graph` so the
    lineage helper writes onto the same subject the analyzer already
    reads from.  Changes here must keep both call sites aligned.
    """
    return _DRAFT_URI_TEMPLATE.format(graph_uri=draft.graph_uri)


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

    draft_iri = URIRef(_draft_uri(draft))

    # Core draft triples.  The class assertion follows ``doc_type`` so
    # VTKs come out as ``estleg:DraftingIntent`` while eelnõud stay
    # ``estleg:DraftLegislation`` — the subsequent
    # :func:`write_doc_lineage` call (in analyze_handler) then becomes
    # idempotent for both shapes.  Falling back to DraftLegislation on
    # anything other than ``'vtk'`` matches the migration default and
    # keeps old rows predicting the same class as before #641.
    class_iri = ESTLEG.DraftingIntent if draft.doc_type == "vtk" else ESTLEG.DraftLegislation
    g.add((draft_iri, RDF.type, class_iri))
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


# ---------------------------------------------------------------------------
# Lineage writer — spec §5 / issue #641
# ---------------------------------------------------------------------------
#
# Why a dedicated helper instead of folding the triples into
# ``build_draft_graph``?
#
# * The analyze pipeline PUTs a fresh Turtle payload once per draft;
#   the class assertion (``a estleg:DraftLegislation`` / ``DraftingIntent``)
#   belongs in that payload and will be re-asserted every time the
#   analyzer runs.  Doing it here *as well* is load-bearing for the
#   two cases where the Turtle PUT has not happened yet or is already
#   stale:
#
#     1. A VTK that has been parsed + extracted but whose analyze job
#        is still queued — the A2 route wires a link-VTK handler that
#        must surface the lineage triple immediately.
#     2. An eelnõu that re-links to a different VTK after analysis has
#        already produced the named graph — the Turtle PUT is not
#        re-issued, but ``basedOn`` must flip in place.
#
# * SPARQL UPDATE against the named graph is the only primitive that
#   supports the re-link case (PUT-replace would drop the reference
#   edges and confidence blank nodes).  ``DELETE WHERE`` + ``INSERT
#   DATA`` wrapped in a single request is atomic from Fuseki's POV
#   and idempotent on repeat application.


def write_doc_lineage(draft: Draft, parent_vtk: Draft | None) -> None:
    """Assert doc_type class + optional ``estleg:basedOn`` into the draft graph.

    Writes into ``draft.graph_uri`` via SPARQL UPDATE so the triples
    land on the existing named graph without disturbing any
    ``estleg:references`` edges already there.  The update is split
    into two statements (separated by ``;`` per SPARQL 1.1 Update
    grammar):

        1. ``DELETE WHERE { GRAPH <g> { <draft> estleg:basedOn ?old } }``
           — unconditional, so relink *and* unlink both work; the
           clause is a no-op when no existing edge matches.
        2. ``INSERT DATA { GRAPH <g> { <draft> a <Class> . [<draft>
           estleg:basedOn <parent> .] } }`` — the class assertion is
           always written; the basedOn triple is only written when
           *parent_vtk* is provided.

    Idempotency:

        * ``INSERT DATA`` on already-present triples is a no-op per
          SPARQL semantics (duplicates are not materialised in an RDF
          graph).
        * ``DELETE WHERE`` on a non-matching pattern is a no-op.
        * Calling this helper twice in a row with the same arguments
          produces the same triple set.

    Args:
        draft: The draft row whose named graph receives the lineage
            edges.  ``draft.graph_uri`` must already match the
            ``drafts/<uuid>`` allowlist (same invariant as
            :func:`app.sync.jena_loader.put_named_graph`).
        parent_vtk: The VTK ``Draft`` this draft is based on, or
            ``None`` when there is no parent (including the case
            where the user has unlinked a previously-set VTK).  When
            provided, must itself have ``doc_type == 'vtk'`` — the
            helper does not re-validate that invariant because the
            route-handler already rejects mismatched types at the DB
            layer (see migration 019 CHECK + A2 validation).

    Raises:
        ValueError: If ``draft.graph_uri`` (or
            ``parent_vtk.graph_uri``) does not match the draft-graph
            allowlist.  Raised synchronously so the caller surfaces a
            handler-level failure rather than silently writing to an
            arbitrary graph.
        RuntimeError: If Fuseki rejects the SPARQL UPDATE.  Bubble it
            up so the analyze/link pipeline can retry or mark failed
            per its own retry policy.

    Returns:
        None.  Success is signalled by the absence of an exception.
    """
    # Validate URIs defensively — the GSP allowlist protects every
    # other write path into the drafts namespace; the lineage helper
    # must use the same guard because SPARQL UPDATE bypasses the
    # Graph Store Protocol entirely.
    _validate_graph_uri(draft.graph_uri)
    if parent_vtk is not None:
        _validate_graph_uri(parent_vtk.graph_uri)

    draft_iri = _draft_uri(draft)
    graph_uri = draft.graph_uri

    # doc_type → rdf:type.  Defaulting to DraftLegislation matches the
    # migration default and the existing ``build_draft_graph`` output,
    # so a row that somehow has a NULL doc_type (cannot happen post-019
    # but belt-and-braces) still gets a sensible class assertion.
    if draft.doc_type == "vtk":
        class_iri = f"{ESTLEG}DraftingIntent"
    else:
        class_iri = f"{ESTLEG}DraftLegislation"

    insert_lines = [f"        <{draft_iri}> a <{class_iri}> ."]
    if parent_vtk is not None:
        parent_iri = _draft_uri(parent_vtk)
        insert_lines.append(f"        <{draft_iri}> <{ESTLEG}basedOn> <{parent_iri}> .")
    insert_block = "\n".join(insert_lines)

    # SPARQL 1.1 Update: two statements separated by ``;``.
    # The DELETE WHERE is unconditional so this helper supports both
    # relink (old edge removed, new edge inserted) and unlink
    # (old edge removed, no new edge inserted).
    update = (
        f"PREFIX estleg: <{ESTLEG}>\n"
        f"DELETE WHERE {{\n"
        f"  GRAPH <{graph_uri}> {{\n"
        f"    <{draft_iri}> estleg:basedOn ?old .\n"
        f"  }}\n"
        f"}} ;\n"
        f"INSERT DATA {{\n"
        f"  GRAPH <{graph_uri}> {{\n"
        f"{insert_block}\n"
        f"  }}\n"
        f"}}"
    )

    logger.info(
        "write_doc_lineage: draft=%s doc_type=%s parent_vtk=%s",
        draft.id,
        draft.doc_type,
        parent_vtk.id if parent_vtk is not None else None,
    )

    ok = _sparql_update(update, timeout=30.0)
    if not ok:
        raise RuntimeError(
            f"write_doc_lineage: SPARQL UPDATE rejected by Fuseki for graph {graph_uri}"
        )
