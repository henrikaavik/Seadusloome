"""Integration-style tests for #849 (versioned draft graph URIs) + #855
(silently-zero C6 burden section, conflict-label binding, sanctions
warning spam).

These exercise the real code paths end to end, mocking only the Jena
transport (``httpx``) / the SPARQL client, so they prove the fixes hold
across module seams rather than at a single function:

#849
    * A v2 upload's analyze path (Turtle PUT → impact query builders →
      lineage writer) passes graph-URI validation instead of raising
      ``ValueError("Unsafe graph URI")``.
    * The version-graph cleanup DELETE passes validation too.
    * #868's A5 self-version exclusion still holds for a v2 graph (the
      draft does not report its own v1 graph as a conflict).

#855
    * The C6 burden lookup is GRAPH-scoped to the draft's named graph and
      addresses the ``#self`` subject, so a synthetic draft graph yields
      non-zero burden rows (the section was silently always zero before).
    * Other-draft conflict labels bind (the OPTIONAL now lives inside the
      ``GRAPH ?otherGraph`` block).
    * Act-title literal rows (``estleg:referencesAct``) are skipped before
      sanctions URI validation, so no ``invalid URI`` warnings are logged.
"""

from __future__ import annotations

import logging
import uuid
from unittest.mock import MagicMock, patch

import pytest
from rdflib import Dataset, Literal, Namespace, URIRef
from rdflib.namespace import RDF, RDFS

from app.analyysikeskus.burden import (
    _build_draft_affected_provisions_graph_query,
    burden_delta_for_draft,
)
from app.docs.impact.analyzer import analyze_burden_delta, analyze_sanctions_delta
from app.docs.impact.queries import (
    build_affected_entities_query,
    build_conflicts_query,
    build_eu_compliance_query,
    build_gaps_query,
)
from app.sync import jena_loader

EST = Namespace("https://data.riik.ee/ontology/estleg#")

_PREFIX = "https://data.riik.ee/ontology/estleg/"
_UUID = "44444444-4444-4444-4444-444444444444"
_V1_GRAPH = f"{_PREFIX}drafts/{_UUID}"
_V2_GRAPH = f"{_PREFIX}drafts/{_UUID}/v2"
_V2_SELF = f"{_V2_GRAPH}#self"


def _rows(ds: Dataset, query: str) -> list[dict[str, str]]:
    """Run *query* against *ds* and flatten each binding to a str dict.

    Mirrors the helper in ``tests/test_tenant_scoping_conflicts.py`` — the
    ``# type: ignore`` annotations paper over rdflib's wide
    ``Result``/``ResultRow`` union types (``query`` may return a bool for
    ASK; SELECT rows expose ``.labels`` + ``__getitem__``).
    """
    out: list[dict[str, str]] = []
    for row in ds.query(query):
        d: dict[str, str] = {}
        for var in row.labels:  # type: ignore[attr-defined,union-attr]
            value = row[var]  # type: ignore[index]
            d[str(var)] = str(value) if value is not None else ""
        out.append(d)
    return out


# ---------------------------------------------------------------------------
# #849 — versioned graph URIs flow through every gate
# ---------------------------------------------------------------------------


class TestVersionedUploadAnalyzeAndCleanup:
    """Every gate a v2 upload crosses must accept ``…/drafts/<uuid>/v<n>``."""

    @patch("app.sync.jena_loader.httpx.put")
    @patch("app.sync.jena_loader.httpx.delete")
    def test_v2_put_then_cleanup_delete_pass_validation(
        self, mock_delete: MagicMock, mock_put: MagicMock
    ):
        """A v2 graph survives the PUT (analyze) → DELETE (cleanup) round-trip
        without raising the allowlist ``ValueError``."""
        put_resp = MagicMock()
        put_resp.status_code = 204
        mock_put.return_value = put_resp
        del_resp = MagicMock()
        del_resp.status_code = 204
        mock_delete.return_value = del_resp

        # Analyze-side write.
        assert jena_loader.put_named_graph(_V2_GRAPH, "# turtle") is True
        # Cleanup-side delete (the version-graph cleanup path #849 calls).
        assert jena_loader.delete_named_graph(_V2_GRAPH) is True
        mock_put.assert_called_once()
        mock_delete.assert_called_once()

    def test_all_impact_builders_accept_v2_graph(self):
        """The four impact query builders must not raise on a v2 graph and
        must GRAPH-scope to it."""
        for builder in (
            build_affected_entities_query,
            build_gaps_query,
            build_eu_compliance_query,
            build_conflicts_query,
        ):
            q = builder(_V2_GRAPH)
            assert f"GRAPH <{_V2_GRAPH}>" in q

    def test_lineage_writer_accepts_v2_graph(self):
        """``write_doc_lineage`` (analyze pipeline) must accept a v2 graph."""
        from datetime import UTC, datetime

        from app.docs.draft_model import Draft
        from app.docs.graph_builder import write_doc_lineage

        now = datetime.now(UTC)
        draft = Draft(
            id=uuid.UUID(_UUID),
            user_id=uuid.UUID("55555555-5555-5555-5555-555555555555"),
            org_id=uuid.UUID("66666666-6666-6666-6666-666666666666"),
            title="v2 eelnõu",
            filename="eelnou_v2.docx",
            content_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            file_size=1024,
            storage_path="/tmp/c.enc",
            graph_uri=_V2_GRAPH,
            status="analyzing",
            parsed_text_encrypted=None,
            entity_count=None,
            error_message=None,
            created_at=now,
            updated_at=now,
            doc_type="eelnou",  # type: ignore[arg-type]
            parent_vtk_id=None,
        )
        with patch("app.docs.graph_builder._sparql_update", return_value=True) as mock_update:
            write_doc_lineage(draft, None)
        text = mock_update.call_args.args[0]
        assert f"GRAPH <{_V2_GRAPH}>" in text
        assert f"<{_V2_SELF}> a " in text

    def test_a5_self_version_exclusion_holds_for_v2(self):
        """#868 sequencing guard: a v2 analyze must NOT report this draft's
        own v1 graph as a conflict (the whole ``…/drafts/<uuid>`` namespace
        is excluded)."""
        from app.docs.impact.queries import CONFLICTS
        from app.ontology.scoping import ADHOC_GRAPH_PREFIX, draft_graph_prefix_for

        q = CONFLICTS.format(
            graph_uri=_V2_GRAPH,
            draft_prefix=draft_graph_prefix_for(_V2_GRAPH),
            adhoc_prefix=ADHOC_GRAPH_PREFIX,
        )
        ds = Dataset()
        prov = EST.Provision_Shared
        cur = ds.graph(URIRef(_V2_GRAPH))
        cur.add((URIRef(_V2_SELF), RDF.type, EST.DraftLegislation))
        cur.add((URIRef(_V2_SELF), EST.references, prov))
        # The same draft's own v1 graph references the same provision.
        v1 = ds.graph(URIRef(_V1_GRAPH))
        v1.add((URIRef(f"{_V1_GRAPH}#self"), RDF.type, EST.DraftLegislation))
        v1.add((URIRef(f"{_V1_GRAPH}#self"), EST.references, prov))
        v1.add((URIRef(f"{_V1_GRAPH}#self"), RDFS.label, Literal("MY OWN V1")))

        leaked = [x for x in _rows(ds, q) if x.get("conflictEntity")]
        assert leaked == [], f"A5: v2 reported its own v1 graph as a conflict: {leaked!r}"


# ---------------------------------------------------------------------------
# #855 — C6 burden section is no longer silently zero
# ---------------------------------------------------------------------------


class TestBurdenGraphScoping:
    def test_graph_scoped_query_finds_provisions_in_named_graph(self):
        """The GRAPH-scoped affected-provisions query returns the draft's
        referenced provisions when its triples live in a named graph."""
        ds = Dataset()
        g = ds.graph(URIRef(_V2_GRAPH))
        g.add((URIRef(_V2_SELF), RDF.type, EST.DraftLegislation))
        g.add((URIRef(_V2_SELF), EST.references, EST.TLS_p12))
        g.add((URIRef(_V2_SELF), EST.references, EST.TLS_p20))

        q = _build_draft_affected_provisions_graph_query(_V2_GRAPH)
        # Mirror SparqlClient's VALUES injection for the ``#self`` subject.
        cut = q.rstrip().rfind("}")
        q_bound = (
            q.rstrip()[:cut] + f"\n  VALUES ?draftSelf {{ <{_V2_SELF}> }}\n" + q.rstrip()[cut:]
        )
        rows = sorted(r["provision"] for r in _rows(ds, q_bound))
        assert rows == [str(EST.TLS_p12), str(EST.TLS_p20)]

    def test_default_graph_query_misses_named_graph_triples(self):
        """Regression guard: the OLD default-graph variant returns nothing
        against named-graph triples — the exact #855 root cause."""
        from app.analyysikeskus.burden import _build_draft_affected_provisions_query

        ds = Dataset()
        g = ds.graph(URIRef(_V2_GRAPH))
        g.add((URIRef(_V2_SELF), EST.references, EST.TLS_p12))

        q = _build_draft_affected_provisions_query()
        cut = q.rstrip().rfind("}")
        q_bound = (
            q.rstrip()[:cut] + f"\n  VALUES ?draftUri {{ <{_V2_GRAPH}> }}\n" + q.rstrip()[cut:]
        )
        assert _rows(ds, q_bound) == []

    def test_burden_delta_for_draft_graph_scoped_yields_rows(self):
        """``burden_delta_for_draft(graph_uri=…)`` GRAPH-scopes the lookup and
        addresses ``#self``; a stubbed client returns non-zero rows."""
        stub = MagicMock()
        stub.query.side_effect = [
            # affected-provisions list
            [{"provision": f"{EST}P1"}, {"provision": f"{EST}P2"}],
            # ONE batched VALUES burden query for the whole set (#858)
            [
                {
                    "provision": f"{EST}P1",
                    "provisionLabel": "P1",
                    "normType": f"{EST}NormType_Obligation",
                    "dutyHolder": "Tööandja",
                },
                {
                    "provision": f"{EST}P2",
                    "provisionLabel": "P2",
                    "normType": f"{EST}NormType_Prohibition",
                    "dutyHolder": "Riik",
                },
            ],
        ]
        delta = burden_delta_for_draft(_V2_SELF, graph_uri=_V2_GRAPH, sparql_client=stub)
        assert delta.affected_count == 2
        assert delta.before.counts["obligation"] == 1
        assert delta.before.counts["prohibition"] == 1
        # #858 G5: exactly TWO round-trips total — affected lookup + one
        # batched VALUES query (never one query per provision).
        assert stub.query.call_count == 2
        assert "VALUES ?provision" in stub.query.call_args_list[1].args[0]
        # The first query must be GRAPH-scoped + bind the ``#self`` subject.
        first = stub.query.call_args_list[0]
        assert f"GRAPH <{_V2_GRAPH}>" in first.args[0]
        assert first.kwargs["uri_bindings"]["draftSelf"] == _V2_SELF

    def test_analyze_burden_delta_threads_graph_uri(self):
        """``analyze_burden_delta(draft_graph_uri)`` must GRAPH-scope the
        burden lookup to the named graph (non-zero C6 section)."""
        stub = MagicMock()
        stub.query.side_effect = [
            [{"provision": f"{EST}P1"}],
            [
                {
                    "provision": f"{EST}P1",
                    "provisionLabel": "P1",
                    "normType": f"{EST}NormType_Obligation",
                    "dutyHolder": "Tööandja",
                }
            ],
        ]
        report = analyze_burden_delta(_V2_GRAPH, sparql_client=stub)
        assert report.affected_count == 1
        assert report.counts["obligation"] == 1
        assert report.before_score == 1
        first = stub.query.call_args_list[0]
        assert f"GRAPH <{_V2_GRAPH}>" in first.args[0]
        assert first.kwargs["uri_bindings"]["draftSelf"] == _V2_SELF


class TestConflictLabelBinding:
    def test_other_draft_label_binds_from_inside_named_graph(self):
        """#855: the other draft's ``rdfs:label`` lives inside its own named
        graph; the conflict query must bind it (no raw-URI fallback)."""
        cur_uuid = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
        other_uuid = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"
        cur_graph = f"{_PREFIX}drafts/{cur_uuid}"
        other_graph = f"{_PREFIX}drafts/{other_uuid}"

        q = build_conflicts_query(cur_graph)
        ds = Dataset()
        prov = EST.Provision_X
        cur = ds.graph(URIRef(cur_graph))
        cur.add((URIRef(f"{cur_graph}#self"), RDF.type, EST.DraftLegislation))
        cur.add((URIRef(f"{cur_graph}#self"), EST.references, prov))
        oth = ds.graph(URIRef(other_graph))
        oth.add((URIRef(f"{other_graph}#self"), RDF.type, EST.DraftLegislation))
        oth.add((URIRef(f"{other_graph}#self"), EST.references, prov))
        oth.add((URIRef(f"{other_graph}#self"), RDFS.label, Literal("OTHER ORG DRAFT")))

        hits = [x for x in _rows(ds, q) if x.get("conflictEntity")]
        assert hits, "expected a cross-draft conflict row"
        assert any(h.get("conflictLabel") == "OTHER ORG DRAFT" for h in hits), (
            f"conflict label did not bind from the named graph: {hits!r}"
        )


class TestSanctionsActTitleNoWarningSpam:
    def test_act_title_literals_skipped_no_invalid_uri_warning(
        self, caplog: pytest.LogCaptureFixture
    ):
        """#855: act-title literal rows must be skipped before sanctions URI
        validation, so no ``invalid URI`` / ``Unsafe URI`` warning is logged
        and the SPARQL client is never called for them."""
        stub = MagicMock()
        # A mix: one real provision URI + two act-title literals (the shape
        # ``estleg:referencesAct`` rows leave in ``affected_uris``).
        affected = [
            f"{EST}KarS_p211",
            "Riigieelarve seadus",
            "Töölepingu seadus",
        ]
        # The single valid provision returns no sanctions (empty list).
        stub.query.return_value = []

        with caplog.at_level(logging.WARNING):
            delta = analyze_sanctions_delta("urn:draft:1", affected, sparql_client=stub)

        # No sanctions found (empty), and crucially no warning spam.
        assert delta.rows == []
        # The act-title literals never reached list_sanctions_for_provision,
        # so the client was called at most once (for the one real URI).
        assert stub.query.call_count <= 1
        spammy = [
            rec
            for rec in caplog.records
            if "invalid uri" in rec.getMessage().lower()
            or "unsafe uri" in rec.getMessage().lower()
            or "list_sanctions_for_provision" in rec.getMessage().lower()
        ]
        assert spammy == [], (
            f"unexpected URI-validation warning spam: {[r.getMessage() for r in spammy]}"
        )

    def test_valid_provision_uri_still_queried(self):
        """A real provision URI must still be passed through to the lookup."""
        stub = MagicMock()
        stub.query.return_value = []
        analyze_sanctions_delta("urn:draft:1", [f"{EST}KarS_p211"], sparql_client=stub)
        stub.query.assert_called_once()
