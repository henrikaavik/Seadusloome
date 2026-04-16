"""Unit tests for ``app.docs.graph_builder.build_draft_graph``.

The builder produces Turtle text from a :class:`Draft` + its resolved
references. The tests parse the output with ``rdflib.Graph`` and
assert on triples rather than string contents — that keeps the tests
robust against rdflib's ordering and whitespace choices.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from unittest.mock import patch

import pytest
from rdflib import Graph, Literal, Namespace, URIRef
from rdflib.namespace import RDF, RDFS

from app.docs.draft_model import Draft
from app.docs.entity_extractor import ExtractedRef
from app.docs.graph_builder import build_draft_graph, write_doc_lineage
from app.docs.reference_resolver import ResolvedRef

ESTLEG = Namespace("https://data.riik.ee/ontology/estleg#")


def _make_draft(
    *,
    title: str = "Tsiviilseadustiku muudatused 2026",
    filename: str = "eelnou.docx",
    draft_id: uuid.UUID | None = None,
    doc_type: str = "eelnou",
    parent_vtk_id: uuid.UUID | None = None,
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
        doc_type=doc_type,  # type: ignore[arg-type]
        parent_vtk_id=parent_vtk_id,
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


class TestBuildDraftGraphDocType:
    """``build_draft_graph`` must pick the RDF class from ``doc_type``.

    Without this, a VTK uploaded before #641 would land in Jena tagged
    as ``estleg:DraftLegislation`` and then gain a second
    ``estleg:DraftingIntent`` type from :func:`write_doc_lineage`,
    leaving the graph in an inconsistent dual-class state.
    """

    def test_vtk_emits_drafting_intent_class(self):
        draft = _make_draft(doc_type="vtk")
        turtle = build_draft_graph(draft, [])

        g = _parse(turtle)
        draft_iri = URIRef(f"{draft.graph_uri}#self")
        types = set(g.objects(draft_iri, RDF.type))
        assert ESTLEG.DraftingIntent in types
        assert ESTLEG.DraftLegislation not in types

    def test_eelnou_still_emits_draft_legislation(self):
        draft = _make_draft(doc_type="eelnou")
        turtle = build_draft_graph(draft, [])

        g = _parse(turtle)
        draft_iri = URIRef(f"{draft.graph_uri}#self")
        types = set(g.objects(draft_iri, RDF.type))
        assert ESTLEG.DraftLegislation in types
        assert ESTLEG.DraftingIntent not in types


# ---------------------------------------------------------------------------
# write_doc_lineage — #641
# ---------------------------------------------------------------------------
#
# The helper issues a SPARQL UPDATE against Fuseki.  We patch
# ``_sparql_update`` at the module seam so the tests never hit the
# network; the assertions inspect the exact update string that would
# have been sent, which is the only observable behaviour worth
# locking down.  Inspecting the update text (instead of mocking
# rdflib out + re-parsing it) keeps the test honest about the
# ``DELETE WHERE``/``INSERT DATA`` ordering that underpins the
# re-link semantics.


_EELNOU_ID = uuid.UUID("11111111-1111-1111-1111-111111111111")
_VTK_A_ID = uuid.UUID("22222222-2222-2222-2222-222222222222")
_VTK_B_ID = uuid.UUID("33333333-3333-3333-3333-333333333333")


def _make_eelnou(parent_vtk_id: uuid.UUID | None = None) -> Draft:
    return _make_draft(
        draft_id=_EELNOU_ID,
        doc_type="eelnou",
        parent_vtk_id=parent_vtk_id,
    )


def _make_vtk(draft_id: uuid.UUID = _VTK_A_ID) -> Draft:
    return _make_draft(draft_id=draft_id, doc_type="vtk")


class TestWriteDocLineageClassAssertion:
    def test_eelnou_without_parent_asserts_draft_legislation(self):
        draft = _make_eelnou(parent_vtk_id=None)
        with patch("app.docs.graph_builder._sparql_update", return_value=True) as mock_update:
            write_doc_lineage(draft, None)

        assert mock_update.call_count == 1
        update_text = mock_update.call_args.args[0]
        draft_iri = f"{draft.graph_uri}#self"

        # Type assertion lands.
        expected_class_triple = (
            f"<{draft_iri}> a <https://data.riik.ee/ontology/estleg#DraftLegislation> ."
        )
        assert expected_class_triple in update_text
        # No basedOn triple without a parent.
        assert "basedOn" in update_text  # present in the DELETE WHERE
        assert f"<{draft_iri}> <https://data.riik.ee/ontology/estleg#basedOn>" not in update_text
        # DELETE WHERE + INSERT DATA structure.
        assert "DELETE WHERE" in update_text
        assert "INSERT DATA" in update_text
        # Scoped to the draft's own graph.
        assert f"GRAPH <{draft.graph_uri}>" in update_text

    def test_vtk_asserts_drafting_intent_class(self):
        vtk = _make_vtk()
        with patch("app.docs.graph_builder._sparql_update", return_value=True) as mock_update:
            write_doc_lineage(vtk, None)

        update_text = mock_update.call_args.args[0]
        draft_iri = f"{vtk.graph_uri}#self"
        assert (
            f"<{draft_iri}> a <https://data.riik.ee/ontology/estleg#DraftingIntent> ."
            in update_text
        )
        # VTKs never get a DraftLegislation class from the lineage writer.
        assert (
            f"<{draft_iri}> a <https://data.riik.ee/ontology/estleg#DraftLegislation>"
            not in update_text
        )


class TestWriteDocLineageBasedOn:
    def test_eelnou_with_parent_writes_based_on_edge(self):
        vtk = _make_vtk()
        eelnou = _make_eelnou(parent_vtk_id=vtk.id)

        with patch("app.docs.graph_builder._sparql_update", return_value=True) as mock_update:
            write_doc_lineage(eelnou, vtk)

        update_text = mock_update.call_args.args[0]
        draft_iri = f"{eelnou.graph_uri}#self"
        parent_iri = f"{vtk.graph_uri}#self"
        # The lineage edge uses the VTK's #self IRI, not the graph URI itself.
        assert (
            f"<{draft_iri}> <https://data.riik.ee/ontology/estleg#basedOn> <{parent_iri}> ."
            in update_text
        )

    def test_unlink_omits_based_on_triple_but_still_deletes_old(self):
        """When parent_vtk is None, the helper must still clear any prior edge."""
        eelnou = _make_eelnou(parent_vtk_id=None)

        with patch("app.docs.graph_builder._sparql_update", return_value=True) as mock_update:
            write_doc_lineage(eelnou, None)

        update_text = mock_update.call_args.args[0]
        draft_iri = f"{eelnou.graph_uri}#self"
        # The DELETE WHERE block is unconditional so unlink works.
        assert f"<{draft_iri}> estleg:basedOn ?old" in update_text or "basedOn ?old" in update_text
        # No basedOn INSERT triple when unlinking.
        assert f"<{draft_iri}> <https://data.riik.ee/ontology/estleg#basedOn> <" not in update_text


class TestWriteDocLineageRelink:
    """Re-linking to a different VTK must flip the edge, not duplicate it.

    The spec requires ``DELETE WHERE { <draft> estleg:basedOn ?old }``
    *before* the INSERT DATA so exactly one basedOn edge exists after
    the update runs.
    """

    def test_delete_where_precedes_insert_data(self):
        vtk = _make_vtk(_VTK_B_ID)
        eelnou = _make_eelnou(parent_vtk_id=vtk.id)

        with patch("app.docs.graph_builder._sparql_update", return_value=True) as mock_update:
            write_doc_lineage(eelnou, vtk)

        update_text = mock_update.call_args.args[0]
        delete_pos = update_text.find("DELETE WHERE")
        insert_pos = update_text.find("INSERT DATA")
        assert delete_pos != -1 and insert_pos != -1
        assert delete_pos < insert_pos, (
            "DELETE WHERE must run before INSERT DATA so re-link produces a single edge"
        )

    def test_relink_from_vtk_a_to_vtk_b_sends_correct_new_edge(self):
        vtk_a = _make_vtk(_VTK_A_ID)
        vtk_b = _make_vtk(_VTK_B_ID)

        # First call: link to VTK A.
        eelnou_linked_to_a = _make_eelnou(parent_vtk_id=vtk_a.id)
        with patch("app.docs.graph_builder._sparql_update", return_value=True) as mock_update_a:
            write_doc_lineage(eelnou_linked_to_a, vtk_a)

        # Second call: re-link the same eelnõu to VTK B.
        eelnou_linked_to_b = _make_eelnou(parent_vtk_id=vtk_b.id)
        with patch("app.docs.graph_builder._sparql_update", return_value=True) as mock_update_b:
            write_doc_lineage(eelnou_linked_to_b, vtk_b)

        text_a = mock_update_a.call_args.args[0]
        text_b = mock_update_b.call_args.args[0]

        parent_a_iri = f"{vtk_a.graph_uri}#self"
        parent_b_iri = f"{vtk_b.graph_uri}#self"

        # First update carries VTK A as the new parent; second carries VTK B.
        assert f"<{parent_a_iri}>" in text_a
        assert f"<{parent_b_iri}>" in text_b
        # The second update does NOT re-assert VTK A (we rely on the
        # DELETE WHERE running server-side to remove it).
        assert f"<{parent_a_iri}> ." not in text_b


class TestWriteDocLineageIdempotency:
    """Calling ``write_doc_lineage`` twice must produce an identical update.

    SPARQL's ``INSERT DATA`` is already idempotent at the triple-store
    level (duplicate triples are not materialised), but we also want
    byte-level determinism so ops can diff Fuseki access logs without
    seeing spurious differences.
    """

    def test_repeat_calls_emit_byte_identical_update(self):
        vtk = _make_vtk()
        eelnou = _make_eelnou(parent_vtk_id=vtk.id)

        captured: list[str] = []

        def _capture(update: str, *, timeout: float = 0.0) -> bool:
            captured.append(update)
            return True

        with patch("app.docs.graph_builder._sparql_update", side_effect=_capture):
            write_doc_lineage(eelnou, vtk)
            write_doc_lineage(eelnou, vtk)

        assert len(captured) == 2
        assert captured[0] == captured[1]

    def test_repeat_calls_with_no_parent_also_stable(self):
        draft = _make_eelnou(parent_vtk_id=None)

        captured: list[str] = []
        with patch(
            "app.docs.graph_builder._sparql_update",
            side_effect=lambda upd, timeout=0.0: captured.append(upd) or True,
        ):
            write_doc_lineage(draft, None)
            write_doc_lineage(draft, None)

        assert len(captured) == 2
        assert captured[0] == captured[1]


class TestWriteDocLineageValidation:
    def test_rejects_unsafe_graph_uri(self):
        draft = _make_draft()
        # Point the draft at a graph URI outside the drafts/<uuid>
        # allowlist — the helper must refuse to dispatch the SPARQL
        # update rather than write to an arbitrary graph.
        draft.graph_uri = (
            "https://evil.example.com/ontology/estleg/drafts/00000000-0000-0000-0000-000000000000"
        )
        with (
            patch("app.docs.graph_builder._sparql_update", return_value=True) as mock_update,
            pytest.raises(ValueError, match="Unsafe graph URI"),
        ):
            write_doc_lineage(draft, None)
        mock_update.assert_not_called()

    def test_rejects_unsafe_parent_graph_uri(self):
        draft = _make_eelnou()
        bad_parent = _make_vtk()
        bad_parent.graph_uri = (
            "https://evil.example.com/ontology/estleg/drafts/00000000-0000-0000-0000-000000000000"
        )
        with (
            patch("app.docs.graph_builder._sparql_update", return_value=True) as mock_update,
            pytest.raises(ValueError, match="Unsafe graph URI"),
        ):
            write_doc_lineage(draft, bad_parent)
        mock_update.assert_not_called()

    def test_sparql_update_failure_raises_runtime_error(self):
        draft = _make_eelnou()
        with (
            patch("app.docs.graph_builder._sparql_update", return_value=False),
            pytest.raises(RuntimeError, match="SPARQL UPDATE rejected"),
        ):
            write_doc_lineage(draft, None)


class TestWriteDocLineageGraphScope:
    """The update must GRAPH-scope to ``draft.graph_uri`` — never the default
    graph — so lineage triples do not leak into the enacted-law graph.
    """

    def test_update_wraps_triples_in_graph_clause(self):
        draft = _make_eelnou()

        with patch("app.docs.graph_builder._sparql_update", return_value=True) as mock_update:
            write_doc_lineage(draft, None)

        text = mock_update.call_args.args[0]
        # Both the DELETE WHERE and INSERT DATA blocks must be GRAPH-scoped.
        assert text.count(f"GRAPH <{draft.graph_uri}>") == 2
