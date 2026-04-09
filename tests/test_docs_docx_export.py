"""Unit tests for ``app.docs.docx_export.build_impact_report_docx``.

We mock ``python-docx``'s ``Document`` class so the tests neither
require a working python-docx install at runtime *nor* write real
binaries to disk during pytest. The mock captures every
``add_heading`` / ``add_paragraph`` / ``add_table`` call and exposes
them via ``MagicMock.method_calls`` so assertions read naturally.
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from app.docs.docx_export import _REPORT_COLUMN_INDEX, build_impact_report_docx
from app.docs.draft_model import Draft

_DRAFT_ID = uuid.UUID("88888888-8888-8888-8888-888888888888")
_REPORT_ID = uuid.UUID("99999999-9999-9999-9999-999999999999")


def _make_draft(title: str = "Test eelnõu") -> Draft:
    now = datetime.now(UTC)
    return Draft(
        id=_DRAFT_ID,
        user_id=uuid.UUID("55555555-5555-5555-5555-555555555555"),
        org_id=uuid.UUID("66666666-6666-6666-6666-666666666666"),
        title=title,
        filename="eelnou.docx",
        content_type=("application/vnd.openxmlformats-officedocument.wordprocessingml.document"),
        file_size=2048,
        storage_path="/tmp/cipher.enc",
        graph_uri=f"https://data.riik.ee/ontology/estleg/drafts/{_DRAFT_ID}",
        status="ready",
        parsed_text="§ 1. Test.",
        entity_count=2,
        error_message=None,
        created_at=now,
        updated_at=now,
    )


def _build_report_row(
    *,
    affected: int = 2,
    conflicts: int = 1,
    gaps: int = 0,
    score: int = 42,
    findings: dict | None = None,
) -> tuple:
    """Construct a tuple matching ``_REPORT_COLUMN_INDEX`` ordering."""
    findings_json = findings or {
        "affected_entities": [
            {
                "uri": "urn:x:1",
                "label": "Märkimisväärne säte",
                "type": "https://data.riik.ee/ontology/estleg#EnactedLaw",
            }
        ]
        * affected,
        "conflicts": [
            {
                "draft_ref": "Eelnõu § 1",
                "conflicting_entity": "urn:x:c1",
                "conflicting_label": "Vana säte õ",
                "reason": "Vastuolu paragrahvis 2",
            }
        ]
        * conflicts,
        "eu_compliance": [],
        "gaps": [
            {
                "topic_cluster": "urn:cluster:1",
                "topic_cluster_label": "Andmekaitse",
                "total_provisions": "10",
                "referenced_provisions": "2",
                "description": "Vähene kaetus",
            }
        ]
        * gaps,
    }
    row: list = [None] * len(_REPORT_COLUMN_INDEX)
    row[_REPORT_COLUMN_INDEX["id"]] = _REPORT_ID
    row[_REPORT_COLUMN_INDEX["draft_id"]] = _DRAFT_ID
    row[_REPORT_COLUMN_INDEX["affected_count"]] = affected
    row[_REPORT_COLUMN_INDEX["conflict_count"]] = conflicts
    row[_REPORT_COLUMN_INDEX["gap_count"]] = gaps
    row[_REPORT_COLUMN_INDEX["impact_score"]] = score
    row[_REPORT_COLUMN_INDEX["report_data"]] = findings_json
    row[_REPORT_COLUMN_INDEX["ontology_version"]] = "2026-04-09T12:00+00:00@1061123"
    row[_REPORT_COLUMN_INDEX["generated_at"]] = datetime(2026, 4, 9, 12, 0, tzinfo=UTC)
    return tuple(row)


def _heading_calls(doc_mock: MagicMock) -> list[str]:
    """Return the text passed to every ``doc.add_heading`` call."""
    return [call.args[0] for call in doc_mock.add_heading.call_args_list if call.args]


@pytest.fixture
def tmp_export_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point ``EXPORT_DIR`` at a temp directory for the duration of a test."""
    monkeypatch.setenv("EXPORT_DIR", str(tmp_path))
    return tmp_path


class TestBuildImpactReportDocx:
    def test_writes_file_to_export_dir(self, tmp_export_dir: Path):
        draft = _make_draft()
        row = _build_report_row()

        with patch("app.docs.docx_export.Document") as mock_doc_cls:
            mock_doc = MagicMock()
            mock_doc.sections = []  # footer loop becomes a no-op
            mock_doc_cls.return_value = mock_doc

            result = build_impact_report_docx(draft, row)

        # File path matches the spec convention <draft>-<report>.docx
        assert result == tmp_export_dir / f"{_DRAFT_ID}-{_REPORT_ID}.docx"
        mock_doc.save.assert_called_once_with(str(result))

    def test_emits_all_expected_section_headings(self, tmp_export_dir: Path):
        draft = _make_draft()
        row = _build_report_row()

        with patch("app.docs.docx_export.Document") as mock_doc_cls:
            mock_doc = MagicMock()
            mock_doc.sections = []
            mock_doc_cls.return_value = mock_doc

            build_impact_report_docx(draft, row)

        headings = _heading_calls(mock_doc)
        # Cover title + draft title
        assert "Eelnõu mõjuanalüüsi aruanne" in headings
        assert draft.title in headings
        # Section headings (Estonian)
        for expected in (
            "Kokkuvõte",
            "Mõjutatud üksused",
            "Konfliktid",
            "EL-i õigusaktide vastavus",
            "Lüngad",
        ):
            assert expected in headings, f"Missing heading: {expected}"

    def test_estonian_characters_preserved(self, tmp_export_dir: Path):
        draft = _make_draft(title="Tööõiguse põhjalik täiendus")
        row = _build_report_row()

        with patch("app.docs.docx_export.Document") as mock_doc_cls:
            mock_doc = MagicMock()
            mock_doc.sections = []
            mock_doc_cls.return_value = mock_doc

            build_impact_report_docx(draft, row)

        headings = _heading_calls(mock_doc)
        assert "Tööõiguse põhjalik täiendus" in headings
        # Estonian section names with diacritics survive intact.
        assert "Mõjutatud üksused" in headings
        assert "Lüngad" in headings

    def test_empty_report_renders_placeholder_paragraphs(self, tmp_export_dir: Path):
        """Empty findings must produce paragraphs, not crash on tables."""
        draft = _make_draft()
        row = _build_report_row(
            affected=0,
            conflicts=0,
            gaps=0,
            findings={
                "affected_entities": [],
                "conflicts": [],
                "eu_compliance": [],
                "gaps": [],
            },
        )

        with patch("app.docs.docx_export.Document") as mock_doc_cls:
            mock_doc = MagicMock()
            mock_doc.sections = []
            mock_doc_cls.return_value = mock_doc

            build_impact_report_docx(draft, row)

        # No tables added because every section was empty.
        mock_doc.add_table.assert_not_called()
        # The empty placeholders are written as paragraphs.
        paragraph_texts: list[str] = []
        for call in mock_doc.add_paragraph.call_args_list:
            if call.args:
                paragraph_texts.append(call.args[0])
        joined = " ".join(paragraph_texts)
        assert "Mõjutatud üksuseid ei tuvastatud." in joined
        assert "Konflikte ei tuvastatud." in joined
        assert "Lünki ei tuvastatud." in joined

    def test_populated_report_adds_tables(self, tmp_export_dir: Path):
        draft = _make_draft()
        row = _build_report_row(affected=3, conflicts=2, gaps=1)

        with patch("app.docs.docx_export.Document") as mock_doc_cls:
            mock_doc = MagicMock()
            mock_doc.sections = []
            mock_doc_cls.return_value = mock_doc

            build_impact_report_docx(draft, row)

        # 3 tables: affected entities, conflicts, gaps. EU compliance is empty.
        assert mock_doc.add_table.call_count == 3

    def test_report_data_string_jsonb_is_parsed(self, tmp_export_dir: Path):
        """JSON-encoded ``report_data`` strings must be tolerated."""
        draft = _make_draft()
        row = list(_build_report_row(affected=1))
        row[_REPORT_COLUMN_INDEX["report_data"]] = json.dumps(
            {
                "affected_entities": [
                    {
                        "uri": "urn:x:from-string",
                        "label": "Stringist tulnud",
                        "type": "EnactedLaw",
                    }
                ],
                "conflicts": [],
                "eu_compliance": [],
                "gaps": [],
            }
        )

        with patch("app.docs.docx_export.Document") as mock_doc_cls:
            mock_doc = MagicMock()
            mock_doc.sections = []
            mock_doc_cls.return_value = mock_doc

            build_impact_report_docx(draft, tuple(row))

        # Affected entities table was added — proves the JSON string was parsed.
        assert mock_doc.add_table.call_count >= 1

    def test_creates_export_dir_if_missing(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        """A first-time prod export must lazily create EXPORT_DIR."""
        target = tmp_path / "fresh-export-dir"
        assert not target.exists()
        monkeypatch.setenv("EXPORT_DIR", str(target))

        draft = _make_draft()
        row = _build_report_row()
        with patch("app.docs.docx_export.Document") as mock_doc_cls:
            mock_doc = MagicMock()
            mock_doc.sections = []
            mock_doc_cls.return_value = mock_doc

            build_impact_report_docx(draft, row)

        assert target.exists()
        assert target.is_dir()

    def test_filename_uses_draft_and_report_ids(self, tmp_export_dir: Path):
        draft = _make_draft()
        row = _build_report_row()

        with patch("app.docs.docx_export.Document") as mock_doc_cls:
            mock_doc = MagicMock()
            mock_doc.sections = []
            mock_doc_cls.return_value = mock_doc

            result = build_impact_report_docx(draft, row)

        assert result.name == f"{_DRAFT_ID}-{_REPORT_ID}.docx"
