"""Unit tests for ``app.docs.graph_builder.build_draft_graph``.

The builder produces Turtle text from a :class:`Draft` + its resolved
references. The tests parse the output with ``rdflib.Graph`` and
assert on triples rather than string contents — that keeps the tests
robust against rdflib's ordering and whitespace choices.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from rdflib import Graph, Literal, Namespace, URIRef
from rdflib.namespace import RDF, RDFS

from app.docs.draft_model import Draft
from app.docs.entity_extractor import ExtractedRef
from app.docs.graph_builder import build_draft_graph
from app.docs.reference_resolver import ResolvedRef

ESTLEG = Namespace("https://data.riik.ee/ontology/estleg#")


def _make_draft(
    *,
    title: str = "Tsiviilseadustiku muudatused 2026",
    filename: str = "eelnou.docx",
    draft_id: uuid.UUID | None = None,
) -> Draft:
    now = datetime.now(UTC)
    resolved_id = draft_id or uuid.UUID("44444444-4444-4444-4444-444444444444")
    return Draft(
        id=resolved_id,
        user_id=uuid.UUID("55555555-5555-5555-5555-555555555555"),
        org_id=uuid.UUID("66666666-6666-6666-6666-666666666666"),
        title=title,
        filename=filename,
        content_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        file_size=2048,
        storage_path="/tmp/cipher.enc",
        graph_uri=f"https://data.riik.ee/ontology/estleg/drafts/{resolved_id}",
        status="analyzing",
        parsed_text_encrypted=None,
        entity_count=None,
        error_message=None,
        created_at=now,
        updated_at=now,
    )


def _ref(
    *,
    text: str = "KarS § 133",
    rtype: str = "provision",
    entity_uri: str | None = "https://data.riik.ee/ontology/estleg#KarS_Par_133",
    confidence: float = 0.9,
) -> ResolvedRef:
    return ResolvedRef(
        extracted=ExtractedRef(
            ref_text=text,
            ref_type=rtype,
            confidence=confidence,
            location={"chunk": 0, "offset": 0},
        ),
        entity_uri=entity_uri,
        matched_label=text,
        match_score=1.0 if entity_uri else 0.0,
    )


def _parse(turtle: str) -> Graph:
    g = Graph()
    g.parse(data=turtle, format="turtle")
    return g


class TestBuildDraftGraphHappyPath:
    def test_builds_draft_subject_with_core_triples(self):
        draft = _make_draft()
        refs = [_ref()]
        turtle = build_draft_graph(draft, refs)

        g = _parse(turtle)
        draft_iri = URIRef(f"{draft.graph_uri}#self")

        # Type, label, filename, uploadedBy must all be present.
        assert (draft_iri, RDF.type, ESTLEG.DraftLegislation) in g
        assert (draft_iri, RDFS.label, Literal(draft.title)) in g
        assert (draft_iri, ESTLEG.filename, Literal(draft.filename)) in g

        user_iri = URIRef(f"urn:user:{draft.user_id}")
        assert (draft_iri, ESTLEG.uploadedBy, user_iri) in g

    def test_references_are_serialised(self):
        draft = _make_draft()
        ref_uri = "https://data.riik.ee/ontology/estleg#KarS_Par_133"
        turtle = build_draft_graph(draft, [_ref(entity_uri=ref_uri)])

        g = _parse(turtle)
        draft_iri = URIRef(f"{draft.graph_uri}#self")
        assert (draft_iri, ESTLEG.references, URIRef(ref_uri)) in g

    def test_confidence_is_attached_as_blank_node(self):
        draft = _make_draft()
        refs = [_ref(confidence=0.75)]
        turtle = build_draft_graph(draft, refs)

        g = _parse(turtle)
        draft_iri = URIRef(f"{draft.graph_uri}#self")
        # There must be at least one refConfidence annotation.
        conf_nodes = list(g.objects(draft_iri, ESTLEG.refConfidence))
        assert len(conf_nodes) == 1
        bnode = conf_nodes[0]
        # The blank node must point at the entity and carry a score.
        entity_iri = URIRef("https://data.riik.ee/ontology/estleg#KarS_Par_133")
        assert (bnode, ESTLEG.aboutEntity, entity_iri) in g
        scores = list(g.objects(bnode, ESTLEG.confidenceScore))
        assert len(scores) == 1
        assert float(str(scores[0])) == 0.75


class TestBuildDraftGraphEdgeCases:
    def test_no_refs_still_emits_draft_subject(self):
        draft = _make_draft()
        turtle = build_draft_graph(draft, [])

        g = _parse(turtle)
        draft_iri = URIRef(f"{draft.graph_uri}#self")
        assert (draft_iri, RDF.type, ESTLEG.DraftLegislation) in g
        # No references emitted.
        assert list(g.objects(draft_iri, ESTLEG.references)) == []

    def test_unresolved_refs_are_skipped(self):
        draft = _make_draft()
        refs = [
            _ref(entity_uri=None, text="made-up ref"),
            _ref(entity_uri="https://data.riik.ee/ontology/estleg#KarS_Par_133"),
        ]
        turtle = build_draft_graph(draft, refs)

        g = _parse(turtle)
        draft_iri = URIRef(f"{draft.graph_uri}#self")
        ref_objects = list(g.objects(draft_iri, ESTLEG.references))
        # Only the resolved ref should appear.
        assert len(ref_objects) == 1
        assert str(ref_objects[0]) == "https://data.riik.ee/ontology/estleg#KarS_Par_133"

    def test_estonian_characters_preserved_in_title(self):
        draft = _make_draft(title="Tsiviilseadustiku üldosa § 12 täpsustus")
        turtle = build_draft_graph(draft, [])

        g = _parse(turtle)
        draft_iri = URIRef(f"{draft.graph_uri}#self")
        labels = list(g.objects(draft_iri, RDFS.label))
        assert len(labels) == 1
        assert str(labels[0]) == "Tsiviilseadustiku üldosa § 12 täpsustus"

    def test_special_chars_in_filename_escape_cleanly(self):
        draft = _make_draft(filename='draft "with" quotes.docx')
        turtle = build_draft_graph(draft, [])

        g = _parse(turtle)
        draft_iri = URIRef(f"{draft.graph_uri}#self")
        filenames = list(g.objects(draft_iri, ESTLEG.filename))
        assert len(filenames) == 1
        assert str(filenames[0]) == 'draft "with" quotes.docx'

    def test_returns_str_not_bytes(self):
        draft = _make_draft()
        turtle = build_draft_graph(draft, [])
        assert isinstance(turtle, str)
