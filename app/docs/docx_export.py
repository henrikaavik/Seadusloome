"""Render an impact report as an Estonian-styled ``.docx`` file.

The builder is intentionally thin: it accepts a :class:`Draft` and the
raw ``impact_reports`` row tuple (as fetched in
:func:`app.docs.export_handler.export_report`) and writes a document
to ``EXPORT_DIR/<draft_id>-<report_id>.docx``.

Content structure (per spec §10.3):

    1. Cover heading — "Eelnõu mõjuanalüüsi aruanne" + draft title
    2. Metadata block (uploaded-at, generated-at, ontology version)
    3. "Kokkuvõte" — impact score + affected/conflict/gap counts
    4. "Mõjutatud üksused" — table of affected entities
    5. "Konfliktid" — table of detected conflicts
    6. "EL-i õigusaktide vastavus" — table of EU compliance links
    7. "Lüngad" — table of topic-cluster gaps
    8. Footer with page numbers

The docx layout is plain and uses ``python-docx``'s built-in styles so
the binary stays small and the document opens cleanly in LibreOffice,
Google Docs, and Microsoft Word. A themed template document is a
follow-up (§10.3 note in the spec) — once Riigi Tugiteenuste Keskus
provides the official .dotx we wire it via ``Document(template_path)``.

Environment:

    EXPORT_DIR   Root directory for generated exports. Defaults to
                 ``./storage/exports`` in development and
                 ``/var/seadusloome/exports`` otherwise. Unlike
                 ``STORAGE_DIR`` this value is not secret, so a
                 missing env var off-dev silently falls back to the
                 prod default (no ``RuntimeError``).
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any

from docx import Document
from docx.enum.section import WD_SECTION
from docx.shared import Pt

from app.docs.draft_model import Draft
from app.docs.labels import TYPE_LABELS_ET as _TYPE_LABELS_ET
from app.ui.time import format_tallinn

logger = logging.getLogger(__name__)


# Column order used by ``app.docs.export_handler.export_report`` when
# SELECTing from ``impact_reports``. Any schema change needs to be
# mirrored here so the tuple unpacking below stays in sync.
_REPORT_COLUMN_INDEX = {
    "id": 0,
    "draft_id": 1,
    "affected_count": 2,
    "conflict_count": 3,
    "gap_count": 4,
    "impact_score": 5,
    "report_data": 6,
    "ontology_version": 7,
    "generated_at": 8,
}


# ---------------------------------------------------------------------------
# EXPORT_DIR resolution (same pattern as STORAGE_DIR in app/storage/encrypted.py)
# ---------------------------------------------------------------------------


def _load_export_dir() -> Path:
    """Return the root export directory with a dev-friendly default.

    Unlike STORAGE_DIR this value is not secret, so we do not raise a
    RuntimeError when unset in prod — the default prod path is simply
    used as a fallback.
    """
    raw = os.environ.get("EXPORT_DIR")
    if raw:
        return Path(raw)
    if os.environ.get("APP_ENV", "development") == "development":
        return Path("./storage/exports").resolve()
    return Path("/var/seadusloome/exports")


def _get_export_dir() -> Path:
    """Re-read ``EXPORT_DIR`` on every call so tests can monkeypatch it."""
    return _load_export_dir()


# Convenience alias for callers that want to read the directory once.
EXPORT_DIR = _load_export_dir()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _format_timestamp(value: Any) -> str:
    """Render a ``datetime`` consistently across every docx section (Europe/Tallinn)."""
    return format_tallinn(value)


def _parse_report_data(raw: Any) -> dict[str, Any]:
    """Normalise the ``report_data`` JSONB column into a dict.

    psycopg 3 usually hands back a dict for JSONB columns but older
    drivers (or mocks) may return a JSON string. A missing/empty value
    yields an empty dict so the downstream iteration is a no-op rather
    than crashing the export.
    """
    if raw is None:
        return {}
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, (bytes, bytearray)):
        try:
            return json.loads(raw.decode())
        except (TypeError, ValueError, UnicodeDecodeError):
            logger.warning("docx_export: unparseable bytes in report_data")
            return {}
    if isinstance(raw, str):
        try:
            return json.loads(raw)
        except (TypeError, ValueError):
            logger.warning("docx_export: unparseable string in report_data")
            return {}
    logger.warning("docx_export: unexpected type in report_data: %s", type(raw).__name__)
    return {}


def _short_type(uri: str) -> str:
    """Translate a type URI into an Estonian label where possible."""
    if not uri:
        return "—"
    short = uri.rsplit("#", 1)[-1] if "#" in uri else uri.rsplit("/", 1)[-1]
    return _TYPE_LABELS_ET.get(short, short)


# ---------------------------------------------------------------------------
# Section renderers
# ---------------------------------------------------------------------------


def _add_cover(doc: Any, draft: Draft, report_row: tuple) -> None:
    """Write the cover block — title, draft metadata, generated-at."""
    doc.add_heading("Eelnõu mõjuanalüüsi aruanne", level=0)
    doc.add_heading(draft.title, level=1)

    generated_at = report_row[_REPORT_COLUMN_INDEX["generated_at"]]
    ontology_version = report_row[_REPORT_COLUMN_INDEX["ontology_version"]] or "unknown"

    meta_paragraph = doc.add_paragraph()
    meta_paragraph.add_run("Üles laaditud: ").bold = True
    meta_paragraph.add_run(_format_timestamp(draft.created_at))

    meta_paragraph2 = doc.add_paragraph()
    meta_paragraph2.add_run("Aruanne koostatud: ").bold = True
    meta_paragraph2.add_run(_format_timestamp(generated_at))

    meta_paragraph3 = doc.add_paragraph()
    meta_paragraph3.add_run("Ontoloogia versioon: ").bold = True
    meta_paragraph3.add_run(str(ontology_version))


def _add_summary(doc: Any, report_row: tuple) -> None:
    """Write the "Kokkuvõte" section with score + counts."""
    doc.add_heading("Kokkuvõte", level=1)

    score = report_row[_REPORT_COLUMN_INDEX["impact_score"]]
    affected = report_row[_REPORT_COLUMN_INDEX["affected_count"]]
    conflicts = report_row[_REPORT_COLUMN_INDEX["conflict_count"]]
    gaps = report_row[_REPORT_COLUMN_INDEX["gap_count"]]

    score_paragraph = doc.add_paragraph()
    run = score_paragraph.add_run(f"Mõjuskoor: {score}/100")
    run.bold = True
    run.font.size = Pt(14)

    doc.add_paragraph(f"Mõjutatud üksuste arv: {affected}")
    doc.add_paragraph(f"Tuvastatud konfliktide arv: {conflicts}")
    doc.add_paragraph(f"Tuvastatud lünkade arv: {gaps}")


def _add_affected_entities(doc: Any, findings: dict[str, Any]) -> None:
    """Write the "Mõjutatud üksused" table."""
    doc.add_heading("Mõjutatud üksused", level=1)

    rows: list[dict[str, Any]] = list(findings.get("affected_entities") or [])
    if not rows:
        doc.add_paragraph("Mõjutatud üksuseid ei tuvastatud.")
        return

    table = doc.add_table(rows=1, cols=3)
    table.style = "Light Grid Accent 1"
    header = table.rows[0].cells
    header[0].text = "Tüüp"
    header[1].text = "Nimetus"
    header[2].text = "URI"
    for row in rows:
        cells = table.add_row().cells
        cells[0].text = _short_type(str(row.get("type", "")))
        cells[1].text = str(row.get("label", "") or "—")
        cells[2].text = str(row.get("uri", "") or "—")


def _add_conflicts(doc: Any, findings: dict[str, Any]) -> None:
    """Write the "Konfliktid" table."""
    doc.add_heading("Konfliktid", level=1)

    rows: list[dict[str, Any]] = list(findings.get("conflicts") or [])
    if not rows:
        doc.add_paragraph("Konflikte ei tuvastatud.")
        return

    table = doc.add_table(rows=1, cols=3)
    table.style = "Light Grid Accent 1"
    header = table.rows[0].cells
    header[0].text = "Eelnõu viide"
    header[1].text = "Konflikti üksus"
    header[2].text = "Põhjus"
    for row in rows:
        cells = table.add_row().cells
        cells[0].text = str(row.get("draft_ref", "") or "—")
        cells[1].text = str(
            row.get("conflicting_label") or row.get("conflicting_entity", "") or "—"
        )
        cells[2].text = str(row.get("reason", "") or "—")


def _add_eu_compliance(doc: Any, findings: dict[str, Any]) -> None:
    """Write the "EL-i õigusaktide vastavus" table."""
    doc.add_heading("EL-i õigusaktide vastavus", level=1)

    rows: list[dict[str, Any]] = list(findings.get("eu_compliance") or [])
    if not rows:
        doc.add_paragraph("EL-i õigusaktide seoseid ei tuvastatud.")
        return

    table = doc.add_table(rows=1, cols=3)
    table.style = "Light Grid Accent 1"
    header = table.rows[0].cells
    header[0].text = "EL õigusakt"
    header[1].text = "Eesti säte"
    header[2].text = "Staatus"
    for row in rows:
        cells = table.add_row().cells
        cells[0].text = str(row.get("eu_label") or row.get("eu_act", "") or "—")
        cells[1].text = str(row.get("provision_label") or row.get("estonian_provision", "") or "—")
        cells[2].text = str(row.get("transposition_status", "") or "—")


def _add_gaps(doc: Any, findings: dict[str, Any]) -> None:
    """Write the "Lüngad" table."""
    doc.add_heading("Lüngad", level=1)

    rows: list[dict[str, Any]] = list(findings.get("gaps") or [])
    if not rows:
        doc.add_paragraph("Lünki ei tuvastatud.")
        return

    table = doc.add_table(rows=1, cols=3)
    table.style = "Light Grid Accent 1"
    header = table.rows[0].cells
    header[0].text = "Teemaklaster"
    header[1].text = "Sätete kaetus"
    header[2].text = "Kirjeldus"
    for row in rows:
        cells = table.add_row().cells
        cells[0].text = str(row.get("topic_cluster_label") or row.get("topic_cluster", "") or "—")
        cells[
            1
        ].text = f"{row.get('referenced_provisions', '0')} / {row.get('total_provisions', '0')}"
        cells[2].text = str(row.get("description", "") or "—")


def _add_footer_page_numbers(doc: Any) -> None:
    """Attach a centered ``Page X of Y`` footer to every section.

    python-docx does not expose a high-level "page number" field, so we
    inject the OOXML ``PAGE`` and ``NUMPAGES`` fields directly into the
    footer paragraph. The XML is safe because we control every token;
    no user-supplied text is placed inside the ``<w:instrText>`` field.
    """
    from docx.oxml import OxmlElement
    from docx.oxml.ns import qn

    for section in doc.sections:
        footer = section.footer
        paragraph = footer.paragraphs[0]
        paragraph.alignment = 1  # WD_ALIGN_PARAGRAPH.CENTER

        # Prefix
        paragraph.add_run("Lk ")

        # <w:fldChar w:fldCharType="begin"/>
        run1 = paragraph.add_run()
        fld_begin = OxmlElement("w:fldChar")
        fld_begin.set(qn("w:fldCharType"), "begin")
        run1._r.append(fld_begin)

        # <w:instrText xml:space="preserve"> PAGE </w:instrText>
        run2 = paragraph.add_run()
        instr = OxmlElement("w:instrText")
        instr.set(qn("xml:space"), "preserve")
        instr.text = " PAGE "
        run2._r.append(instr)

        # <w:fldChar w:fldCharType="end"/>
        run3 = paragraph.add_run()
        fld_end = OxmlElement("w:fldChar")
        fld_end.set(qn("w:fldCharType"), "end")
        run3._r.append(fld_end)

        paragraph.add_run(" / ")

        # NUMPAGES
        run4 = paragraph.add_run()
        fld_begin2 = OxmlElement("w:fldChar")
        fld_begin2.set(qn("w:fldCharType"), "begin")
        run4._r.append(fld_begin2)

        run5 = paragraph.add_run()
        instr2 = OxmlElement("w:instrText")
        instr2.set(qn("xml:space"), "preserve")
        instr2.text = " NUMPAGES "
        run5._r.append(instr2)

        run6 = paragraph.add_run()
        fld_end2 = OxmlElement("w:fldChar")
        fld_end2.set(qn("w:fldCharType"), "end")
        run6._r.append(fld_end2)


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def build_impact_report_docx(draft: Draft, report_row: tuple) -> Path:
    """Render the impact report as an Estonian-styled ``.docx`` file.

    Writes to ``<EXPORT_DIR>/<draft_id>-<report_id>.docx`` using
    ``python-docx``. The parent directory is created on first write so
    fresh deployments do not need a manual ``mkdir`` step.

    Args:
        draft: The owning draft dataclass (used for title + created_at).
        report_row: Raw tuple fetched in the order declared by
            :data:`_REPORT_COLUMN_INDEX`.

    Returns:
        Absolute :class:`pathlib.Path` to the generated file.
    """
    export_dir = _get_export_dir()
    export_dir.mkdir(parents=True, exist_ok=True)

    report_id = report_row[_REPORT_COLUMN_INDEX["id"]]
    out_path = export_dir / f"{draft.id}-{report_id}.docx"

    report_data = _parse_report_data(report_row[_REPORT_COLUMN_INDEX["report_data"]])

    doc = Document()

    _add_cover(doc, draft, report_row)
    _add_summary(doc, report_row)
    _add_affected_entities(doc, report_data)
    # Section break keeps the detail tables from bleeding into the cover
    # on single-page reports. WD_SECTION.NEW_PAGE is a page break; if the
    # tables are short the blank space is acceptable for a legal export.
    doc.add_section(WD_SECTION.CONTINUOUS)
    _add_conflicts(doc, report_data)
    _add_eu_compliance(doc, report_data)
    _add_gaps(doc, report_data)

    _add_footer_page_numbers(doc)

    doc.save(str(out_path))
    logger.info(
        "docx_export: wrote impact report docx draft=%s report=%s path=%s",
        draft.id,
        report_id,
        out_path,
    )
    return out_path
