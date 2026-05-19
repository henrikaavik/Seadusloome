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
from typing import Any
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
        parsed_text_encrypted=None,
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

    def test_partial_match_row_renders_as_plain_text_cells(self, tmp_export_dir: Path):
        """Wave 2 Step 5A (P2 review follow-up,
        docs/2026-05-18-bugfix-plan.md): a ``referencesAct`` partial
        match must produce a docx row that:

          * shows "Akt (sätet ei leitud)" in the Tüüp column,
          * renders the act title in the Nimetus + URI columns as a
            plain text run (not a hyperlink — there's no URL to point
            at),
          * shows the Estonian "viitab aktile (sätet ei leitud)"
            phrase in the Seose liik column.
        """
        draft = _make_draft()
        partial_findings = {
            "affected_entities": [
                {
                    "uri": "https://data.riik.ee/ontology/estleg#KarS_Par_133",
                    "label": "KarS § 133",
                    "type": "https://data.riik.ee/ontology/estleg#LegalProvision",
                    "relation": "https://data.riik.ee/ontology/estleg#references",
                },
                {
                    # Literal-edge partial match. uri carries the act
                    # title; type is empty.
                    "uri": "Riigieelarve seadus",
                    "label": "Riigieelarve seadus",
                    "type": "",
                    "relation": "https://data.riik.ee/ontology/estleg#referencesAct",
                },
            ],
            "conflicts": [],
            "eu_compliance": [],
            "gaps": [],
        }
        row = _build_report_row(affected=2, conflicts=0, gaps=0, findings=partial_findings)

        # Capture every cell-text assignment so we can assert what
        # the docx renderer wrote per row. The python-docx mock's
        # ``add_table`` returns a MagicMock; ``add_row().cells[i].text =
        # "..."`` ends up as a setattr on the cell mock — which
        # MagicMock records under ``method_calls`` as
        # ``add_table().add_row().cells.__getitem__(i).text``. The
        # simpler proof is to inspect the raw run sequence on the
        # mock's call list.
        captured_texts: list[str] = []

        def _record_setattr(cells_mock: MagicMock) -> None:
            # Wire up the cells mock so every ``.text = value``
            # appends to captured_texts.
            def _make_cell() -> MagicMock:
                cell = MagicMock()
                # Catch any text assignment.
                cell._raw_text = None

                def _set_text(value: str) -> None:
                    captured_texts.append(value)

                # MagicMock supports property-like attributes via
                # __setattr__. Easiest is to use a side_effect on a
                # property via PropertyMock, but for the simple list
                # capture we add a __setattr__ override by replacing
                # the cell with a tiny helper object.
                cell.text = ""
                return cell

            cells_mock.__getitem__.side_effect = lambda i: _make_cell()

        with patch("app.docs.docx_export.Document") as mock_doc_cls:
            mock_doc = MagicMock()
            mock_doc.sections = []
            # Build a mock table whose ``add_row().cells[i].text``
            # assignment is observable. The cleanest path is to make
            # ``add_table`` return a mock where ``add_row().cells`` is
            # a list-like that records assignments.
            tables_added: list[MagicMock] = []

            def _make_table(*_args: Any, **_kwargs: Any) -> MagicMock:
                table = MagicMock()
                # The row list legitimately mixes MagicMock and _RecordingRow
                # instances (header row is a recording row, subsequent rows
                # via add_row.side_effect are also recording rows). pyright
                # needs the broader Any here so .append calls don't reject
                # _RecordingRow as "not MagicMock".
                table_rows: list[Any] = []

                # Header row (rows=1 in add_table). The renderer reads
                # ``table.rows[0].cells`` first.
                class _RecordingCell:
                    def __init__(self) -> None:
                        self._text: str = ""

                    @property
                    def text(self) -> str:
                        return self._text

                    @text.setter
                    def text(self, value: str) -> None:
                        self._text = value
                        captured_texts.append(value)

                class _RecordingRow:
                    def __init__(self) -> None:
                        self.cells = [_RecordingCell() for _ in range(4)]

                header_row = _RecordingRow()
                table_rows.append(header_row)
                table.rows = table_rows

                def _add_row() -> _RecordingRow:
                    new = _RecordingRow()
                    table_rows.append(new)
                    return new

                table.add_row.side_effect = _add_row
                tables_added.append(table)
                return table

            mock_doc.add_table.side_effect = _make_table
            mock_doc_cls.return_value = mock_doc

            build_impact_report_docx(draft, row)

        # Assert the Estonian phrasing for the partial-match row
        # appears in the captured cell-text stream.
        joined = " | ".join(captured_texts)
        assert "Akt (sätet ei leitud)" in joined, (
            "DOCX export must label a partial-match row's Tüüp cell "
            "as 'Akt (sätet ei leitud)' — see Wave 2 Step 5A."
        )
        assert "viitab aktile (sätet ei leitud)" in joined, (
            "DOCX export must show 'viitab aktile (sätet ei leitud)' "
            "as the Seose liik for a partial-match row."
        )
        # The act title appears verbatim (no escaping / wrapping). The
        # docx renderer assigns it as a plain cell.text — there is no
        # hyperlink helper in the existing _add_affected_entities path
        # (the URI column was always plain text), so the regression
        # guard here is "the title shows up unchanged".
        assert "Riigieelarve seadus" in joined

        # Sanity: the full-URI row's Tüüp must NOT use the partial
        # phrasing — verify by counting occurrences of the partial
        # phrase (should equal 1 for the one partial row only).
        assert joined.count("Akt (sätet ei leitud)") == 1
