"""Tests for C5 (#790) — "Seose liik" column in impact reports.

The four sections of the impact report (Affected / Conflicts / EU
compliance / Gaps) gained a new leftmost column that renders the
relation predicate URI through :func:`app.ontology.relations.legal_phrase`
so the lawyer sees a legal-language phrase ("muudab", "tõlgendab",
"viitab", "võtab üle direktiivi", "defineerib mõistet", "on
harmoneeritud aktiga") instead of a raw URI.

This file covers both render surfaces — the HTML DataTable used by
``/drafts/<id>/report`` and the .docx export — plus the graceful
fallback to ``"—"`` when:

* a row carries no ``relation`` (old impact reports persisted before
  the C0 query change), or
* the row is a gap row (no single predicate per topic cluster).
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from fasthtml.common import to_xml

from app.docs.docx_export import _REPORT_COLUMN_INDEX, build_impact_report_docx
from app.docs.draft_model import Draft
from app.docs.report_routes import (
    _affected_entities_section,
    _conflicts_section,
    _eu_compliance_section,
    _gaps_section,
    _relation_cell_text,
)
from app.impact.queries import (
    AFFECTED_ENTITIES,
    CONFLICTS,
    EU_COMPLIANCE,
    GAPS,
)
from app.ontology.relations import PREDICATES

_DRAFT_ID = uuid.UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
_REPORT_ID = uuid.UUID("bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb")


# ---------------------------------------------------------------------------
# Query layer — every relevant SELECT projects a ``?relation`` variable
# ---------------------------------------------------------------------------


class TestQueriesProjectRelation:
    """Static check: each impact-engine SELECT lists ``?relation`` in its
    projection so renderers downstream can read the predicate URI."""

    def test_affected_entities_selects_relation(self) -> None:
        assert "SELECT DISTINCT ?entity ?label ?type ?relation" in AFFECTED_ENTITIES

    def test_conflicts_selects_relation(self) -> None:
        assert (
            "SELECT DISTINCT ?draftRef ?conflictEntity ?conflictLabel ?reason ?relation"
            in CONFLICTS
        )

    def test_eu_compliance_selects_relation(self) -> None:
        assert (
            "SELECT DISTINCT ?euAct ?euLabel ?estonianProvision ?provisionLabel ?relation"
            in EU_COMPLIANCE
        )

    def test_gaps_does_not_select_relation(self) -> None:
        # Gap rows aggregate provisions across a topic cluster so there is
        # no single ``?relation`` to project — the renderer falls back to
        # "—" in the new column. Document the intentional asymmetry.
        assert "?relation" not in GAPS


# ---------------------------------------------------------------------------
# Helper — `_relation_cell_text` (shared with the docx export)
# ---------------------------------------------------------------------------


class TestRelationCellText:
    @pytest.mark.parametrize(
        "relation,expected",
        [
            (PREDICATES.AMENDS, "muudab"),
            (PREDICATES.INTERPRETS_LAW, "tõlgendab"),
            (PREDICATES.REFERENCES, "viitab"),
            (PREDICATES.TRANSPOSES_DIRECTIVE, "võtab üle direktiivi"),
            (PREDICATES.DEFINES_CONCEPT, "defineerib mõistet"),
            (PREDICATES.HARMONISED_WITH, "on harmoneeritud aktiga"),
        ],
    )
    def test_canonical_predicate_uris_map_to_legal_phrases(
        self, relation: str, expected: str
    ) -> None:
        assert _relation_cell_text({"relation": relation}) == expected

    def test_prefixed_form_resolves(self) -> None:
        # ``estleg:amends`` should also resolve via legal_phrase().
        assert _relation_cell_text({"relation": "estleg:amends"}) == "muudab"

    def test_missing_relation_renders_em_dash(self) -> None:
        # Old impact reports persisted before C5 had no ``relation`` field
        # in their JSONB. The renderer must not crash.
        assert _relation_cell_text({}) == "—"

    def test_none_relation_renders_em_dash(self) -> None:
        assert _relation_cell_text({"relation": None}) == "—"

    def test_empty_relation_renders_em_dash(self) -> None:
        assert _relation_cell_text({"relation": ""}) == "—"

    def test_blank_relation_renders_em_dash(self) -> None:
        assert _relation_cell_text({"relation": "   "}) == "—"


# ---------------------------------------------------------------------------
# HTML renderer — every section card includes a "Seose liik" header
# ---------------------------------------------------------------------------


def _findings_full() -> dict[str, Any]:
    return {
        "affected_entities": [
            {
                "uri": "https://data.riik.ee/ontology/estleg#Provision_1",
                "label": "Säte 1",
                "type": "https://data.riik.ee/ontology/estleg#LegalProvision",
                "relation": PREDICATES.AMENDS,
            },
            {
                "uri": "https://data.riik.ee/ontology/estleg#Concept_1",
                "label": "Mõiste 1",
                "type": "https://data.riik.ee/ontology/estleg#LegalConcept",
                "relation": PREDICATES.DEFINES_CONCEPT,
            },
            # Historical row with no relation — must still render.
            {
                "uri": "https://data.riik.ee/ontology/estleg#Old_1",
                "label": "Vana säte",
                "type": "https://data.riik.ee/ontology/estleg#LegalProvision",
            },
        ],
        "conflicts": [
            {
                "draft_ref": "Eelnõu § 1",
                "conflicting_entity": "https://data.riik.ee/ontology/estleg#CourtDecision_1",
                "conflicting_label": "Riigikohtu lahend",
                "reason": "Kohtulahend tõlgendab seda sätet",
                "relation": PREDICATES.INTERPRETS_LAW,
            },
        ],
        "eu_compliance": [
            {
                "eu_act": "https://data.riik.ee/ontology/estleg#EU_Dir_1",
                "eu_label": "EL direktiiv 1",
                "estonian_provision": "https://data.riik.ee/ontology/estleg#Provision_1",
                "provision_label": "Säte 1",
                "transposition_status": "linked",
                "relation": PREDICATES.TRANSPOSES_DIRECTIVE,
            },
        ],
        "gaps": [
            {
                "topic_cluster": "https://data.riik.ee/ontology/estleg#Cluster_1",
                "topic_cluster_label": "Andmekaitse",
                "total_provisions": "10",
                "referenced_provisions": "1",
                "description": "Vähene kaetus",
            },
        ],
    }


class TestHtmlRendererHasRelationColumn:
    def test_affected_section_renders_seose_liik_header(self) -> None:
        section = _affected_entities_section(_findings_full(), draft_id=str(_DRAFT_ID))
        xml = to_xml(section)
        assert "Seose liik" in xml

    def test_affected_section_renders_amends_phrase(self) -> None:
        section = _affected_entities_section(_findings_full(), draft_id=str(_DRAFT_ID))
        xml = to_xml(section)
        assert "muudab" in xml
        assert "defineerib mõistet" in xml

    def test_affected_section_falls_back_for_legacy_row(self) -> None:
        section = _affected_entities_section(_findings_full(), draft_id=str(_DRAFT_ID))
        xml = to_xml(section)
        # The "Vana säte" row has no ``relation`` — its cell must render
        # "—" so the column shape is preserved without a NameError.
        assert "—" in xml
        assert "Vana säte" in xml

    def test_conflicts_section_renders_interprets_phrase(self) -> None:
        section = _conflicts_section(_findings_full(), draft_id=str(_DRAFT_ID))
        xml = to_xml(section)
        assert "Seose liik" in xml
        assert "tõlgendab" in xml

    def test_eu_compliance_section_renders_transposes_phrase(self) -> None:
        section = _eu_compliance_section(_findings_full(), draft_id=str(_DRAFT_ID))
        xml = to_xml(section)
        assert "Seose liik" in xml
        assert "võtab üle direktiivi" in xml

    def test_gaps_section_renders_column_with_em_dash(self) -> None:
        # Gap rows have no ``relation`` so the column header is present
        # but every body cell is "—". Verify the header AND the fallback.
        section = _gaps_section(_findings_full(), draft_id=str(_DRAFT_ID))
        xml = to_xml(section)
        assert "Seose liik" in xml
        assert "—" in xml
        # The gap content itself still renders.
        assert "Andmekaitse" in xml


# ---------------------------------------------------------------------------
# .docx export — every table now has 4 columns and a "Seose liik" header
# ---------------------------------------------------------------------------


def _make_draft() -> Draft:
    now = datetime.now(UTC)
    return Draft(
        id=_DRAFT_ID,
        user_id=uuid.UUID("55555555-5555-5555-5555-555555555555"),
        org_id=uuid.UUID("66666666-6666-6666-6666-666666666666"),
        title="Test eelnõu",
        filename="eelnou.docx",
        content_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        file_size=2048,
        storage_path="/tmp/cipher.enc",
        graph_uri=f"https://data.riik.ee/ontology/estleg/drafts/{_DRAFT_ID}",
        status="ready",
        parsed_text_encrypted=None,
        entity_count=3,
        error_message=None,
        created_at=now,
        updated_at=now,
    )


def _report_row(findings: dict[str, Any]) -> tuple:
    row: list = [None] * len(_REPORT_COLUMN_INDEX)
    row[_REPORT_COLUMN_INDEX["id"]] = _REPORT_ID
    row[_REPORT_COLUMN_INDEX["draft_id"]] = _DRAFT_ID
    row[_REPORT_COLUMN_INDEX["affected_count"]] = len(findings.get("affected_entities") or [])
    row[_REPORT_COLUMN_INDEX["conflict_count"]] = len(findings.get("conflicts") or [])
    row[_REPORT_COLUMN_INDEX["gap_count"]] = len(findings.get("gaps") or [])
    row[_REPORT_COLUMN_INDEX["impact_score"]] = 42
    row[_REPORT_COLUMN_INDEX["report_data"]] = json.dumps(findings)
    row[_REPORT_COLUMN_INDEX["ontology_version"]] = "2026-05-16T00:00+00:00@1"
    row[_REPORT_COLUMN_INDEX["generated_at"]] = datetime(2026, 5, 16, 9, 0, tzinfo=UTC)
    return tuple(row)


@pytest.fixture
def tmp_export_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("EXPORT_DIR", str(tmp_path))
    return tmp_path


class TestDocxExportRelationColumn:
    def test_affected_table_has_seose_liik_header_and_phrase(self, tmp_export_dir: Path) -> None:
        findings = {
            "affected_entities": [
                {
                    "uri": "urn:x:1",
                    "label": "Säte 1",
                    "type": "LegalProvision",
                    "relation": PREDICATES.AMENDS,
                },
            ],
            "conflicts": [],
            "eu_compliance": [],
            "gaps": [],
        }
        draft = _make_draft()
        row = _report_row(findings)
        with patch("app.docs.docx_export.Document") as mock_doc_cls:
            doc = MagicMock()
            doc.sections = []
            # The header row is a real list of 4 cells; subsequent rows
            # appended via add_row().cells are also length-4 MagicMocks.
            mock_doc_cls.return_value = doc
            build_impact_report_docx(draft, row)

        # One table → 4 columns requested.
        calls = [c for c in doc.add_table.call_args_list]
        assert calls, "affected_entities table should be added"
        # First add_table call is the affected section.
        affected_call = calls[0]
        assert affected_call.kwargs.get("cols") == 4 or (
            len(affected_call.args) >= 2 and affected_call.args[1] == 4
        )

    def test_every_populated_table_has_four_columns(self, tmp_export_dir: Path) -> None:
        findings = {
            "affected_entities": [
                {
                    "uri": "urn:x:1",
                    "label": "Säte",
                    "type": "LegalProvision",
                    "relation": PREDICATES.AMENDS,
                },
            ],
            "conflicts": [
                {
                    "draft_ref": "Eelnõu § 1",
                    "conflicting_entity": "urn:cd:1",
                    "conflicting_label": "Kohtulahend",
                    "reason": "tõlgendab",
                    "relation": PREDICATES.INTERPRETS_LAW,
                },
            ],
            "eu_compliance": [
                {
                    "eu_act": "urn:eu:1",
                    "eu_label": "Direktiiv",
                    "estonian_provision": "urn:x:1",
                    "provision_label": "Säte",
                    "transposition_status": "linked",
                    "relation": PREDICATES.TRANSPOSES_DIRECTIVE,
                },
            ],
            "gaps": [
                {
                    "topic_cluster": "urn:cl:1",
                    "topic_cluster_label": "Andmekaitse",
                    "total_provisions": "10",
                    "referenced_provisions": "1",
                    "description": "Vähene kaetus",
                },
            ],
        }
        draft = _make_draft()
        row = _report_row(findings)
        with patch("app.docs.docx_export.Document") as mock_doc_cls:
            doc = MagicMock()
            doc.sections = []
            mock_doc_cls.return_value = doc
            build_impact_report_docx(draft, row)

        # 4 sections, each populated → 4 add_table calls.
        assert doc.add_table.call_count == 4
        # Every one of them asked for 4 columns.
        for call in doc.add_table.call_args_list:
            cols = call.kwargs.get("cols")
            if cols is None and len(call.args) >= 2:
                cols = call.args[1]
            assert cols == 4, f"expected 4 cols, got {cols}"

    def test_header_cell_text_writes_seose_liik(self, tmp_export_dir: Path) -> None:
        """The header row's first cell must read "Seose liik".

        We capture the header cells by inspecting the ``add_table`` mock's
        return value — each table starts with a single header row whose
        cells we mutate by ``.text = ...``.
        """
        findings = {
            "affected_entities": [
                {
                    "uri": "urn:x:1",
                    "label": "Säte",
                    "type": "LegalProvision",
                    "relation": PREDICATES.AMENDS,
                },
            ],
            "conflicts": [],
            "eu_compliance": [],
            "gaps": [],
        }
        draft = _make_draft()
        row = _report_row(findings)

        with patch("app.docs.docx_export.Document") as mock_doc_cls:
            doc = MagicMock()
            doc.sections = []
            # Build a real list of 4 header-cell mocks so attribute
            # assignment via ``header[0].text = "Seose liik"`` records.
            header_cells = [MagicMock() for _ in range(4)]
            body_cells = [MagicMock() for _ in range(4)]

            table = MagicMock()
            table.rows = [MagicMock(cells=header_cells)]
            body_row = MagicMock(cells=body_cells)
            table.add_row.return_value = body_row

            doc.add_table.return_value = table
            mock_doc_cls.return_value = doc

            build_impact_report_docx(draft, row)

        # The first header cell on the only populated table must be "Seose liik".
        assert header_cells[0].text == "Seose liik"
        # Body cell 0 must be the Estonian legal phrase for ``amends``.
        assert body_cells[0].text == "muudab"

    def test_gap_row_renders_em_dash_in_relation_cell(self, tmp_export_dir: Path) -> None:
        findings = {
            "affected_entities": [],
            "conflicts": [],
            "eu_compliance": [],
            "gaps": [
                {
                    "topic_cluster": "urn:cl:1",
                    "topic_cluster_label": "Andmekaitse",
                    "total_provisions": "10",
                    "referenced_provisions": "1",
                    "description": "Vähene kaetus",
                },
            ],
        }
        draft = _make_draft()
        row = _report_row(findings)

        with patch("app.docs.docx_export.Document") as mock_doc_cls:
            doc = MagicMock()
            doc.sections = []
            header_cells = [MagicMock() for _ in range(4)]
            body_cells = [MagicMock() for _ in range(4)]
            table = MagicMock()
            table.rows = [MagicMock(cells=header_cells)]
            table.add_row.return_value = MagicMock(cells=body_cells)
            doc.add_table.return_value = table
            mock_doc_cls.return_value = doc

            build_impact_report_docx(draft, row)

        # Gap rows have no ``relation`` field — must fall back to "—".
        assert body_cells[0].text == "—"
        # And the leftmost header is still "Seose liik".
        assert header_cells[0].text == "Seose liik"
