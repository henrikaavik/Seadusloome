"""Tenant-scoping regression tests for impact conflict detection (#844).

DoD verification:
- Impact conflict detection only returns org-owned draft graph rows or
  public ontology rows; foreign draft graph URIs/labels are never
  persisted or rendered (A3b).
- Self-conflict rows from the same draft's prior version graphs are
  excluded from impact conflict scoring (A5).
- Ephemeral adhoc probe graphs never surface as phantom conflicts (A3c).

The cross-org masking mirrors ``similarity.list_similar_drafts_for_view``:
a foreign row keeps its shape (so the conflict *count* stays honest) but
its identity is blanked and ``masked=True`` flagged.
"""

from __future__ import annotations

from unittest.mock import MagicMock

from rdflib import Dataset, Literal, Namespace, URIRef
from rdflib.namespace import RDF, RDFS

from app.docs.impact.analyzer import ImpactAnalyzer
from app.docs.impact.masking import (
    _MASKED_CONFLICT_LABEL,
    drop_adhoc_conflict_rows,
    mask_conflict_rows,
    mask_stored_conflict_rows,
)
from app.docs.impact.queries import CONFLICTS, build_conflicts_query
from app.ontology.scoping import ADHOC_GRAPH_PREFIX, draft_graph_prefix_for

EST = Namespace("https://data.riik.ee/ontology/estleg#")

_ORG_A_DRAFT = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
_ORG_B_DRAFT = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"
_DRAFTS = "https://data.riik.ee/ontology/estleg/drafts/"
_CUR_GRAPH = _DRAFTS + _ORG_A_DRAFT
_FOREIGN_GRAPH = _DRAFTS + _ORG_B_DRAFT
_FOREIGN_SELF = _FOREIGN_GRAPH + "#self"
_ADHOC_GRAPH = ADHOC_GRAPH_PREFIX + "cccccccc-cccc-cccc-cccc-cccccccccccc"


def _rows(ds: Dataset, query: str) -> list[dict[str, str]]:
    out: list[dict[str, str]] = []
    for row in ds.query(query):
        d: dict[str, str] = {}
        for var in row.labels:  # type: ignore[attr-defined,union-attr]
            value = row[var]  # type: ignore[index]
            d[str(var)] = str(value) if value is not None else ""
        out.append(d)
    return out


# ---------------------------------------------------------------------------
# A5 — self-version exclusion in the SPARQL query
# ---------------------------------------------------------------------------


class TestSelfVersionExclusion:
    def test_query_text_has_prefix_strstarts(self):
        q = build_conflicts_query(_CUR_GRAPH)
        assert f'!STRSTARTS(str(?otherGraph), "{_CUR_GRAPH}")' in q

    def test_own_prior_version_graph_excluded(self):
        """A v3 analyze run must NOT report this draft's own v2 graph.

        The strict GSP allowlist rejects ``/v<n>`` URIs today (#849 owns
        widening it), so we format the template directly to prove the
        forward-compatible behaviour: keyed on the version-agnostic
        ``…/drafts/<uuid>`` prefix, both v2 and v3 are excluded.
        """
        current = _CUR_GRAPH + "/v3"
        sibling = _CUR_GRAPH + "/v2"
        q = CONFLICTS.format(
            graph_uri=current,
            draft_prefix=draft_graph_prefix_for(current),
            adhoc_prefix=ADHOC_GRAPH_PREFIX,
        )
        ds = Dataset()
        prov = EST.Provision_Y
        cur = ds.graph(URIRef(current))
        cur.add((URIRef(current + "#self"), RDF.type, EST.DraftLegislation))
        cur.add((URIRef(current + "#self"), EST.references, prov))
        sib = ds.graph(URIRef(sibling))
        sib.add((URIRef(sibling + "#self"), RDF.type, EST.DraftLegislation))
        sib.add((URIRef(sibling + "#self"), EST.references, prov))
        sib.add((URIRef(sibling + "#self"), RDFS.label, Literal("MY OWN V2")))

        rows = _rows(ds, q)
        leaked = [r for r in rows if r.get("conflictEntity")]
        assert leaked == [], f"A5: own prior-version graph leaked: {leaked!r}"


# ---------------------------------------------------------------------------
# A3c — adhoc probe exclusion
# ---------------------------------------------------------------------------


class TestAdhocExclusion:
    def test_query_text_excludes_adhoc(self):
        q = build_conflicts_query(_CUR_GRAPH)
        assert f'!STRSTARTS(str(?otherGraph), "{ADHOC_GRAPH_PREFIX}")' in q

    def test_adhoc_graph_not_a_conflict(self):
        """A concurrent Normi-mõjuahel probe (typed DraftLegislation) must
        not surface as a phantom conflict in a real draft's report."""
        q = build_conflicts_query(_CUR_GRAPH)
        ds = Dataset()
        prov = EST.Provision_Z
        cur = ds.graph(URIRef(_CUR_GRAPH))
        cur.add((URIRef(_CUR_GRAPH + "#self"), RDF.type, EST.DraftLegislation))
        cur.add((URIRef(_CUR_GRAPH + "#self"), EST.references, prov))
        ah = ds.graph(URIRef(_ADHOC_GRAPH))
        ah.add((URIRef(_ADHOC_GRAPH + "#self"), RDF.type, EST.DraftLegislation))
        ah.add((URIRef(_ADHOC_GRAPH + "#self"), EST.references, prov))

        rows = _rows(ds, q)
        leaked = [r for r in rows if r.get("conflictEntity")]
        assert leaked == [], f"A3c: adhoc probe leaked as conflict: {leaked!r}"

    def test_drop_adhoc_conflict_rows_defence_in_depth(self):
        rows = [
            {"conflicting_entity": _ADHOC_GRAPH + "#self", "reason": "x"},
            {"conflicting_entity": _FOREIGN_SELF, "reason": "y"},
            {"conflicting_entity": "urn:case:3-1-1-1-20", "reason": "court"},
        ]
        out = drop_adhoc_conflict_rows(rows)
        entities = [r["conflicting_entity"] for r in out]
        assert _ADHOC_GRAPH + "#self" not in entities
        assert _FOREIGN_SELF in entities
        assert "urn:case:3-1-1-1-20" in entities


# ---------------------------------------------------------------------------
# A3b — cross-org masking (the masking module)
# ---------------------------------------------------------------------------


class TestMaskConflictRows:
    def test_foreign_draft_row_masked(self):
        rows = [
            {
                "draft_ref": "https://data.riik.ee/ontology/estleg#KarS_Par_1",
                "conflicting_entity": _FOREIGN_SELF,
                "conflicting_label": "ORG B SECRET DRAFT",
                "reason": "Teine eelnõu viitab juba sellele sättele",
            }
        ]
        # Viewer owns ORG A only — the foreign (ORG B) draft is masked.
        out = mask_conflict_rows(rows, owned_draft_ids={_ORG_A_DRAFT})
        assert len(out) == 1, "count preserved so the impact score stays honest"
        assert out[0]["conflicting_entity"] == ""
        assert out[0]["conflicting_label"] == _MASKED_CONFLICT_LABEL
        assert "ORG B SECRET DRAFT" not in out[0]["conflicting_label"]
        assert _ORG_B_DRAFT not in out[0].get("conflicting_entity", "")
        assert out[0]["masked"] is True
        # The reason (non-identifying) is preserved.
        assert out[0]["reason"]

    def test_owned_draft_row_kept_verbatim(self):
        owned_self = _CUR_GRAPH + "#self"
        rows = [
            {
                "draft_ref": "https://data.riik.ee/ontology/estleg#KarS_Par_1",
                "conflicting_entity": owned_self,
                "conflicting_label": "My own other draft",
                "reason": "Teine eelnõu viitab juba sellele sättele",
            }
        ]
        out = mask_conflict_rows(rows, owned_draft_ids={_ORG_A_DRAFT})
        assert out[0]["conflicting_entity"] == owned_self
        assert out[0]["conflicting_label"] == "My own other draft"
        assert not out[0].get("masked")

    def test_court_decision_row_never_masked(self):
        rows = [
            {
                "draft_ref": "https://data.riik.ee/ontology/estleg#KarS_Par_1",
                "conflicting_entity": "https://data.riik.ee/ontology/estleg#Decision_3_1_1_1_20",
                "conflicting_label": "Riigikohus 3-1-1-1-20",
                "reason": "Kohtulahend tõlgendab seda sätet",
            }
        ]
        # Even with no owned drafts, a public court decision is shown.
        out = mask_conflict_rows(rows, owned_draft_ids=set())
        assert out[0]["conflicting_label"] == "Riigikohus 3-1-1-1-20"
        assert not out[0].get("masked")

    def test_empty_owned_set_masks_all_foreign(self):
        rows = [
            {
                "draft_ref": "x",
                "conflicting_entity": _FOREIGN_SELF,
                "conflicting_label": "SECRET",
                "reason": "r",
            }
        ]
        out = mask_conflict_rows(rows, owned_draft_ids=set())
        assert out[0]["masked"] is True
        assert out[0]["conflicting_label"] == _MASKED_CONFLICT_LABEL


# ---------------------------------------------------------------------------
# A3b — analyzer integration (detection-time masking)
# ---------------------------------------------------------------------------


def _client_returning(rows: list[dict[str, str]]) -> MagicMock:
    mock = MagicMock()

    def side_effect(sparql: str, *args, **kwargs) -> list[dict[str, str]]:
        if "?conflictEntity" in sparql:
            return rows
        return []

    mock.query.side_effect = side_effect
    return mock


class TestAnalyzerConflictMasking:
    def test_detect_conflicts_masks_foreign_draft(self):
        rows = [
            {
                "draftRef": "https://data.riik.ee/ontology/estleg#KarS_Par_1",
                "conflictEntity": _FOREIGN_SELF,
                "conflictLabel": "ORG B SECRET",
                "reason": "Teine eelnõu viitab juba sellele sättele",
                "relation": "https://data.riik.ee/ontology/estleg#references",
                "otherGraph": _FOREIGN_GRAPH,
            }
        ]
        analyzer = ImpactAnalyzer(sparql_client=_client_returning(rows))
        # Org owns ORG A only; ORG B is foreign → masked.
        out = analyzer._detect_conflicts(_CUR_GRAPH, owned_draft_ids={_ORG_A_DRAFT})
        assert len(out) == 1
        assert out[0]["conflicting_label"] == _MASKED_CONFLICT_LABEL
        assert out[0]["conflicting_entity"] == ""
        # The internal helper key must be stripped from the persisted row.
        assert "other_graph" not in out[0]
        # No part of the foreign UUID survives anywhere in the row.
        assert _ORG_B_DRAFT not in repr(out[0])

    def test_detect_conflicts_keeps_owned_draft(self):
        owned_self = _CUR_GRAPH + "#self"
        rows = [
            {
                "draftRef": "https://data.riik.ee/ontology/estleg#KarS_Par_1",
                "conflictEntity": owned_self,
                "conflictLabel": "My other draft",
                "reason": "Teine eelnõu viitab juba sellele sättele",
                "relation": "https://data.riik.ee/ontology/estleg#references",
                "otherGraph": _CUR_GRAPH,
            }
        ]
        analyzer = ImpactAnalyzer(sparql_client=_client_returning(rows))
        out = analyzer._detect_conflicts(_CUR_GRAPH, owned_draft_ids={_ORG_A_DRAFT})
        assert out[0]["conflicting_label"] == "My other draft"
        assert not out[0].get("masked")

    def test_detect_conflicts_no_owned_masks_all(self):
        """An ad-hoc analysis (owned_draft_ids=None) masks every foreign
        draft row — it has no owning org."""
        rows = [
            {
                "draftRef": "https://data.riik.ee/ontology/estleg#KarS_Par_1",
                "conflictEntity": _FOREIGN_SELF,
                "conflictLabel": "ORG B SECRET",
                "reason": "Teine eelnõu viitab juba sellele sättele",
                "relation": "https://data.riik.ee/ontology/estleg#references",
                "otherGraph": _FOREIGN_GRAPH,
            }
        ]
        analyzer = ImpactAnalyzer(sparql_client=_client_returning(rows))
        out = analyzer._detect_conflicts(_CUR_GRAPH, owned_draft_ids=None)
        assert out[0]["masked"] is True
        assert out[0]["conflicting_label"] == _MASKED_CONFLICT_LABEL


# ---------------------------------------------------------------------------
# Render-time masking of stored reports (data remediation)
# ---------------------------------------------------------------------------


def _make_conn(owned_ids: list[str]) -> MagicMock:
    conn = MagicMock()
    cursor = MagicMock()
    cursor.fetchall.return_value = [(i,) for i in owned_ids]
    conn.execute.return_value = cursor
    return conn


class TestMaskStoredConflictRows:
    def test_legacy_report_foreign_uri_masked_at_render(self):
        """A report persisted before #844 carries the foreign draft URI;
        render-time masking scrubs it."""
        stored = [
            {
                "draft_ref": "https://data.riik.ee/ontology/estleg#KarS_Par_1",
                "conflicting_entity": _FOREIGN_SELF,
                "conflicting_label": "ORG B SECRET DRAFT TITLE",
                "reason": "Teine eelnõu viitab juba sellele sättele",
            }
        ]
        conn = _make_conn(owned_ids=[_ORG_A_DRAFT])  # viewer owns ORG A only
        out = mask_stored_conflict_rows(stored, viewer_org_id="org-a", conn=conn)
        assert out[0]["conflicting_label"] == _MASKED_CONFLICT_LABEL
        assert out[0]["conflicting_entity"] == ""
        assert _ORG_B_DRAFT not in repr(out)

    def test_stored_adhoc_row_dropped(self):
        stored = [
            {
                "conflicting_entity": _ADHOC_GRAPH + "#self",
                "conflicting_label": "stale probe",
                "reason": "x",
            }
        ]
        conn = _make_conn(owned_ids=[])
        out = mask_stored_conflict_rows(stored, viewer_org_id="org-a", conn=conn)
        assert out == []

    def test_no_viewer_org_masks_all(self):
        stored = [
            {
                "conflicting_entity": _FOREIGN_SELF,
                "conflicting_label": "SECRET",
                "reason": "r",
            }
        ]
        out = mask_stored_conflict_rows(stored, viewer_org_id=None)
        assert out[0]["masked"] is True
        assert out[0]["conflicting_label"] == _MASKED_CONFLICT_LABEL
