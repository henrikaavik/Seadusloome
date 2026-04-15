"""FastHTML routes for the Phase 2 Impact Report UI + .docx export.

Route map:

    GET  /drafts/{draft_id}/report                       — full impact report page
    POST /drafts/{draft_id}/export                       — enqueue an export_report job
    GET  /drafts/{draft_id}/export-status/{job_id}       — HTMX polling fragment
    GET  /drafts/{draft_id}/export/{job_id}/download     — file download

All four routes require authentication and the org-scoping check from
:mod:`app.docs.routes`. Cross-org accesses return the 404 page rather
than 403 so we never leak the existence of another org's drafts.
"""

from __future__ import annotations

import json
import logging
import re
import unicodedata
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from fasthtml.common import *  # noqa: F403
from starlette.requests import Request
from starlette.responses import FileResponse, Response

from app.auth.audit import log_action
from app.auth.helpers import require_auth as _require_auth
from app.auth.policy import can_view_draft
from app.db import get_connection as _connect
from app.docs.draft_model import Draft, fetch_draft, touch_draft_access_conn
from app.docs.labels import TYPE_LABELS_ET as _TYPE_LABELS_ET
from app.jobs.queue import JobQueue
from app.ui.data.data_table import Column, DataTable
from app.ui.forms.app_form import AppForm
from app.ui.layout import PageShell
from app.ui.primitives.annotation_button import AnnotationButton
from app.ui.primitives.badge import Badge, BadgeVariant
from app.ui.primitives.button import Button
from app.ui.surfaces.alert import Alert
from app.ui.surfaces.card import Card, CardBody, CardHeader
from app.ui.surfaces.info_box import InfoBox
from app.ui.theme import get_theme_from_request
from app.ui.time import format_tallinn

logger = logging.getLogger(__name__)


_DOCX_MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"

# Cap how many rows we render inline. The full findings JSON is still
# embedded in the impact_reports row and the .docx export contains
# every row, so this is purely for page-weight control.
_MAX_INLINE_ROWS = 50

# #457: drop the polling attributes after this many seconds since the
# export job was created. Without an upper bound the export-status
# fragment hammers /export-status/<id> forever whenever a worker
# stalls, and the user has no actionable signal.
_EXPORT_POLLING_TIMEOUT_SECONDS = 300


# ---------------------------------------------------------------------------
# Auth + lookup helpers
# ---------------------------------------------------------------------------


def _parse_uuid(raw: str) -> uuid.UUID | None:
    """Return a ``UUID`` parsed from *raw*, or ``None`` if invalid."""
    try:
        return uuid.UUID(raw)
    except (ValueError, TypeError):
        return None


def _not_found_page(req: Request):
    """Render the 404 page used whenever a draft/report is missing or out of scope."""
    auth = req.scope.get("auth")
    theme = get_theme_from_request(req)
    return PageShell(
        H1("Eelnõu ei leitud", cls="page-title"),  # noqa: F405
        Alert(
            "Otsitud eelnõu või mõjuaruanne ei ole olemas või Te ei oma selle vaatamise õigust.",
            variant="warning",
        ),
        P(A("← Tagasi eelnõude nimekirja", href="/drafts"), cls="back-link"),  # noqa: F405
        title="Eelnõu ei leitud",
        user=auth,
        theme=theme,
        active_nav="/drafts",
    )


def _format_timestamp(value: Any) -> str:
    if value is None:
        return "—"
    try:
        return format_tallinn(value)
    except AttributeError:
        return str(value)


def _short_type(uri: str) -> str:
    """Translate a type URI to an Estonian label, falling back to the short name."""
    if not uri:
        return "—"
    short = uri.rsplit("#", 1)[-1] if "#" in uri else uri.rsplit("/", 1)[-1]
    return _TYPE_LABELS_ET.get(short, short)


# ---------------------------------------------------------------------------
# DB helpers
# ---------------------------------------------------------------------------


# Column order used by every SELECT in this module. Stays aligned with
# app.docs.docx_export._REPORT_COLUMN_INDEX so we can pass tuples
# directly into the .docx builder.
_REPORT_SELECT_COLUMNS = (
    "id, draft_id, affected_count, conflict_count, gap_count, "
    "impact_score, report_data, ontology_version, generated_at"
)


def _fetch_latest_report(draft_id: uuid.UUID) -> tuple | None:
    """Return the most recent ``impact_reports`` row for *draft_id*.

    A draft can in principle have several rows (one per analyse run);
    the report page always shows the latest. We open our own connection
    here so route handlers stay free of psycopg boilerplate.
    """
    try:
        with _connect() as conn:
            return conn.execute(
                f"""
                SELECT {_REPORT_SELECT_COLUMNS}
                FROM impact_reports
                WHERE draft_id = %s
                ORDER BY generated_at DESC
                LIMIT 1
                """,
                (str(draft_id),),
            ).fetchone()
    except Exception:
        logger.exception("_fetch_latest_report failed for draft=%s", draft_id)
        return None


def _parse_report_data(raw: Any) -> dict[str, Any]:
    """Normalise the JSONB ``report_data`` value into a dict."""
    if raw is None:
        return {}
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, (bytes, bytearray)):
        try:
            return json.loads(raw.decode())
        except (TypeError, ValueError, UnicodeDecodeError):
            return {}
    if isinstance(raw, str):
        try:
            return json.loads(raw)
        except (TypeError, ValueError):
            return {}
    return {}


# ---------------------------------------------------------------------------
# Report page sections
# ---------------------------------------------------------------------------


def _score_variant(score: int) -> BadgeVariant:
    """Map a 0-100 impact score to a Badge variant."""
    if score < 30:
        return "success"
    if score < 60:
        return "warning"
    return "danger"


def _summary_card(report_row: tuple) -> Any:
    """Build the score + counts summary card."""
    impact_score = int(report_row[5] or 0)
    affected = int(report_row[2] or 0)
    conflicts = int(report_row[3] or 0)
    gaps = int(report_row[4] or 0)
    ontology_version = str(report_row[7] or "unknown")
    generated_at = _format_timestamp(report_row[8])

    return Card(
        CardHeader(H3("Kokkuvõte", cls="card-title")),  # noqa: F405
        CardBody(
            Div(
                Div(
                    Span("Mõjuskoor", cls="report-summary-label"),  # noqa: F405
                    Badge(
                        f"{impact_score}/100",
                        variant=_score_variant(impact_score),
                        cls="report-summary-score",
                    ),
                    cls="report-summary-cell",
                ),
                Div(
                    Span("Mõjutatud üksused", cls="report-summary-label"),  # noqa: F405
                    Span(str(affected), cls="report-summary-value"),  # noqa: F405
                    cls="report-summary-cell",
                ),
                Div(
                    Span("Konfliktid", cls="report-summary-label"),  # noqa: F405
                    Span(str(conflicts), cls="report-summary-value"),  # noqa: F405
                    cls="report-summary-cell",
                ),
                Div(
                    Span("Lüngad", cls="report-summary-label"),  # noqa: F405
                    Span(str(gaps), cls="report-summary-value"),  # noqa: F405
                    cls="report-summary-cell",
                ),
                cls="report-summary-grid",
            ),
            P(  # noqa: F405
                f"Aruanne koostatud: {generated_at} · Ontoloogia versioon: {ontology_version}",
                cls="muted-text",
            ),
        ),
    )


def _affected_entities_section(findings: dict[str, Any]) -> Any:
    """Build the "Mõjutatud üksused" data table section."""
    rows = list(findings.get("affected_entities") or [])
    total = len(rows)

    def _type_cell(row: dict[str, Any]):
        return _short_type(str(row.get("type", "")))

    def _label_cell(row: dict[str, Any]):
        return str(row.get("label") or "—")

    def _uri_cell(row: dict[str, Any]):
        uri = str(row.get("uri") or "")
        if not uri:
            return "—"
        return A(  # noqa: F405
            uri,
            href=f"/explorer?focus={uri}",
            cls="data-table-link",
        )

    columns = [
        Column(key="type", label="Tüüp", sortable=False, render=_type_cell),
        Column(key="label", label="Nimetus", sortable=False, render=_label_cell),
        Column(key="uri", label="URI", sortable=False, render=_uri_cell),
    ]

    visible = rows[:_MAX_INLINE_ROWS]
    table_rows = [{"_": True, **row} for row in visible]

    body_children: list = [
        DataTable(
            columns=columns,
            rows=table_rows,
            empty_message="Mõjutatud üksuseid ei tuvastatud.",
        )
    ]
    if total > _MAX_INLINE_ROWS:
        body_children.append(
            P(  # noqa: F405
                f"Kuvatud {len(visible)} esimest reast {total}-st.",
                cls="muted-text",
            )
        )

    return Card(
        CardHeader(H3("Mõjutatud üksused", cls="card-title")),  # noqa: F405
        CardBody(*body_children),
    )


def _conflicts_section(findings: dict[str, Any], draft_id: str = "") -> Any:
    """Build the "Konfliktid" section."""
    rows = list(findings.get("conflicts") or [])

    if not rows:
        body = Alert(
            "Konflikte ei tuvastatud.",
            variant="success",
        )
    else:

        def _draft_ref(row: dict[str, Any]):
            return str(row.get("draft_ref") or "—")

        def _conflict_entity(row: dict[str, Any]):
            uri = str(row.get("conflicting_entity") or "")
            label = str(row.get("conflicting_label") or uri or "—")
            if not uri:
                return label
            return A(  # noqa: F405
                label,
                href=f"/explorer?focus={uri}",
                cls="data-table-link",
            )

        def _reason(row: dict[str, Any]):
            return str(row.get("reason") or "—")

        columns = [
            Column(key="draft_ref", label="Eelnõu viide", sortable=False, render=_draft_ref),
            Column(
                key="conflicting_entity",
                label="Konflikti üksus",
                sortable=False,
                render=_conflict_entity,
            ),
            Column(key="reason", label="Põhjus", sortable=False, render=_reason),
        ]
        body = DataTable(
            columns=columns,
            rows=rows,
            empty_message="Konflikte ei tuvastatud.",
        )

    body_children: list = [body]
    if draft_id:
        # #615: one-line helper so first-time users understand what the
        # speech-balloon annotation button beside the header does.
        body_children.insert(
            0,
            P(  # noqa: F405
                "Saate märkida olulised read meeskonna jaoks.",
                cls="muted-text annotation-hint",
            ),
        )
    return Card(
        CardHeader(
            H3("Konfliktid", cls="card-title"),  # noqa: F405
            AnnotationButton("draft", f"{draft_id}-conflicts") if draft_id else "",
        ),
        CardBody(*body_children),
    )


def _eu_compliance_section(findings: dict[str, Any], draft_id: str = "") -> Any:
    """Build the "EL-i õigusaktide vastavus" section."""
    rows = list(findings.get("eu_compliance") or [])

    def _eu_act(row: dict[str, Any]):
        uri = str(row.get("eu_act") or "")
        label = str(row.get("eu_label") or uri or "—")
        if not uri:
            return label
        return A(  # noqa: F405
            label,
            href=f"/explorer?focus={uri}",
            cls="data-table-link",
        )

    def _ee_provision(row: dict[str, Any]):
        return str(row.get("provision_label") or row.get("estonian_provision") or "—")

    def _status(row: dict[str, Any]):
        return str(row.get("transposition_status") or "—")

    columns = [
        Column(key="eu_act", label="EL õigusakt", sortable=False, render=_eu_act),
        Column(key="provision", label="Eesti säte", sortable=False, render=_ee_provision),
        Column(key="status", label="Staatus", sortable=False, render=_status),
    ]

    body_children: list = [
        DataTable(
            columns=columns,
            rows=rows,
            empty_message="EL-i õigusaktide seoseid ei tuvastatud.",
        )
    ]
    if draft_id:
        # #615: onboarding hint for the annotation button.
        body_children.insert(
            0,
            P(  # noqa: F405
                "Saate märkida olulised read meeskonna jaoks.",
                cls="muted-text annotation-hint",
            ),
        )
    return Card(
        CardHeader(
            H3("EL-i õigusaktide vastavus", cls="card-title"),  # noqa: F405
            AnnotationButton("draft", f"{draft_id}-eu-compliance") if draft_id else "",
        ),
        CardBody(*body_children),
    )


def _gaps_section(findings: dict[str, Any], draft_id: str = "") -> Any:
    """Build the "Lüngad" section."""
    rows = list(findings.get("gaps") or [])

    def _cluster(row: dict[str, Any]):
        return str(row.get("topic_cluster_label") or row.get("topic_cluster") or "—")

    def _coverage(row: dict[str, Any]):
        return f"{row.get('referenced_provisions', '0')} / {row.get('total_provisions', '0')}"

    def _description(row: dict[str, Any]):
        return str(row.get("description") or "—")

    columns = [
        Column(key="cluster", label="Teemaklaster", sortable=False, render=_cluster),
        Column(key="coverage", label="Sätete kaetus", sortable=False, render=_coverage),
        Column(key="description", label="Kirjeldus", sortable=False, render=_description),
    ]

    body_children: list = [
        DataTable(
            columns=columns,
            rows=rows,
            empty_message="Lünki ei tuvastatud.",
        )
    ]
    if draft_id:
        # #615: onboarding hint for the annotation button.
        body_children.insert(
            0,
            P(  # noqa: F405
                "Saate märkida olulised read meeskonna jaoks.",
                cls="muted-text annotation-hint",
            ),
        )
    return Card(
        CardHeader(
            H3("Lüngad", cls="card-title"),  # noqa: F405
            AnnotationButton("draft", f"{draft_id}-gaps") if draft_id else "",
        ),
        CardBody(*body_children),
    )


def _export_section(draft: Draft) -> Any:
    """Build the export action card with HTMX-driven status placeholder."""
    return Card(
        CardHeader(H3("Eksport", cls="card-title")),  # noqa: F405
        CardBody(
            P(  # noqa: F405
                "Laadi alla terviklik mõjuaruanne .docx vormingus.",
                cls="muted-text",
            ),
            AppForm(
                Button(
                    "Laadi alla .docx",
                    type="submit",
                    variant="primary",
                ),
                # #599: spinner beside the submit so the form does not
                # appear frozen while the export is generated. HTMX
                # toggles ``.htmx-request`` on the indicator element.
                Span(  # noqa: F405
                    "",
                    cls="btn-spinner export-spinner",
                    aria_hidden="true",
                ),
                method="post",
                action=f"/drafts/{draft.id}/export",
                hx_post=f"/drafts/{draft.id}/export",
                hx_swap="innerHTML",
                hx_target="#export-status",
                hx_indicator=".export-spinner",
                cls="export-form",
            ),
            Div(id="export-status", cls="export-status"),
        ),
    )


# ---------------------------------------------------------------------------
# GET /drafts/{draft_id}/report
# ---------------------------------------------------------------------------


def draft_report_page(req: Request, draft_id: str):
    """GET /drafts/{draft_id}/report — render the impact report page."""
    auth_or_redirect = _require_auth(req)
    if isinstance(auth_or_redirect, Response):
        return auth_or_redirect
    auth = auth_or_redirect
    theme = get_theme_from_request(req)

    parsed = _parse_uuid(draft_id)
    if parsed is None:
        return _not_found_page(req)

    draft = fetch_draft(parsed)
    if draft is None or not can_view_draft(auth, draft):
        return _not_found_page(req)

    report_row = _fetch_latest_report(parsed)
    if report_row is None:
        return _not_found_page(req)

    log_action(
        auth.get("id"),
        "draft.report.view",
        {"draft_id": str(parsed), "report_id": str(report_row[0])},
    )
    # #572: surface-to-user counts as access; reset the archive clock.
    touch_draft_access_conn(parsed)

    findings = _parse_report_data(report_row[6])

    header = Div(
        H1(draft.title, cls="page-title"),  # noqa: F405
        Div(
            A(  # noqa: F405
                "← Tagasi eelnõu juurde",
                href=f"/drafts/{draft.id}",
                cls="back-link",
            ),
            Div(
                A(  # noqa: F405
                    "Ava uurijas →",
                    href=f"/explorer?draft={draft.id}",
                    cls="btn btn-secondary btn-md",
                    title="Visualiseeri eelnõu ja mõjutatud sätted graafil.",
                ),
                # #614: one-line helper below the button so reviewers
                # know what "Ava uurijas" actually does before clicking.
                Small(  # noqa: F405
                    "Visualiseeri eelnõu ja mõjutatud sätted graafil.",
                    cls="muted-text explorer-cta-hint",
                ),
                cls="explorer-cta",
            ),
            cls="page-actions",
        ),
        cls="report-page-header",
    )

    return PageShell(
        header,
        InfoBox(
            P(
                "See aruanne n\u00e4itab, kuidas teie eeln\u00f5u m\u00f5jutab "
                "olemasolevat \u00f5igusraamistikku. Mida k\u00f5rgem on "
                "m\u00f5juskoor, seda rohkem muudatusi eeln\u00f5u p\u00f5hjustab."
            ),
            variant="info",
            dismissible=True,
        ),
        _summary_card(report_row),
        _affected_entities_section(findings),
        _conflicts_section(findings, draft_id=str(draft.id)),
        _eu_compliance_section(findings, draft_id=str(draft.id)),
        _gaps_section(findings, draft_id=str(draft.id)),
        _export_section(draft),
        title=f"Mõjuaruanne — {draft.title}",
        user=auth,
        theme=theme,
        active_nav="/drafts",
    )


# ---------------------------------------------------------------------------
# POST /drafts/{draft_id}/export
# ---------------------------------------------------------------------------


def _export_poll_interval_seconds(job_created: datetime | None) -> int:
    """Return the export-status poll interval with exponential-ish backoff.

    0-30s since enqueue → 2s, 30-120s → 5s, 120s+ → 10s. See #607.
    """
    if job_created is None:
        return 2
    try:
        elapsed = (datetime.now(UTC) - job_created).total_seconds()
    except (TypeError, ValueError):
        return 2
    if elapsed < 30:
        return 2
    if elapsed < 120:
        return 5
    return 10


def _export_status_spinner(
    draft_id: uuid.UUID,
    job_id: int,
    job_created: datetime | None = None,
) -> Any:
    """Return the spinner-with-poll fragment used while a job is in flight."""
    interval = _export_poll_interval_seconds(job_created)
    return Div(
        Span(cls="btn-spinner", aria_hidden="true"),  # noqa: F405
        Span(" Eksport käimas... (jälgi allpool olevast logist)"),  # noqa: F405
        id="export-status",
        cls="export-status export-status-pending",
        hx_get=f"/drafts/{draft_id}/export-status/{job_id}",
        hx_trigger=f"every {interval}s",
        hx_swap="outerHTML",
    )


def export_draft_report_handler(req: Request, draft_id: str):
    """POST /drafts/{draft_id}/export — enqueue an export_report job."""
    auth_or_redirect = _require_auth(req)
    if isinstance(auth_or_redirect, Response):
        return auth_or_redirect
    auth = auth_or_redirect

    parsed = _parse_uuid(draft_id)
    if parsed is None:
        return _not_found_page(req)

    draft = fetch_draft(parsed)
    if draft is None or not can_view_draft(auth, draft):
        return _not_found_page(req)

    report_row = _fetch_latest_report(parsed)
    if report_row is None:
        return _not_found_page(req)

    report_id = str(report_row[0])
    try:
        job_id = JobQueue().enqueue(
            "export_report",
            {"draft_id": str(parsed), "report_id": report_id},
            priority=10,
        )
    except Exception:
        logger.exception("Failed to enqueue export_report for draft=%s", parsed)
        return Div(
            Alert(
                "Ekspordi käivitamine ebaõnnestus. Palun proovige uuesti.",
                variant="danger",
            ),
            id="export-status",
            cls="export-status export-status-failed",
        )

    log_action(
        auth.get("id"),
        "draft.report.export",
        {
            "draft_id": str(parsed),
            "report_id": report_id,
            "job_id": job_id,
        },
    )
    # #572: export counts as access; reset the archive clock.
    touch_draft_access_conn(parsed)

    return _export_status_spinner(parsed, job_id)


# ---------------------------------------------------------------------------
# GET /drafts/{draft_id}/export-status/{job_id}
# ---------------------------------------------------------------------------


def export_status_fragment(req: Request, draft_id: str, job_id: str):
    """GET /drafts/{draft_id}/export-status/{job_id} — poll fragment."""
    auth_or_redirect = _require_auth(req)
    if isinstance(auth_or_redirect, Response):
        return auth_or_redirect
    auth = auth_or_redirect

    parsed_draft = _parse_uuid(draft_id)
    if parsed_draft is None:
        return _not_found_page(req)

    draft = fetch_draft(parsed_draft)
    if draft is None or not can_view_draft(auth, draft):
        return _not_found_page(req)

    try:
        parsed_job_id = int(job_id)
    except (TypeError, ValueError):
        return Div(
            Alert("Tundmatu töö id.", variant="warning"),
            id="export-status",
        )

    job = JobQueue().get(parsed_job_id)
    if job is None:
        return Div(
            Alert("Eksporditööd ei leitud.", variant="warning"),
            id="export-status",
        )

    # Cross-check the job belongs to this draft. Drafts and reports
    # cascade to delete with their org, but the explicit guard means a
    # leaked job_id from another org cannot reveal status info.
    payload = job.payload or {}
    if str(payload.get("draft_id")) != str(parsed_draft):
        return _not_found_page(req)

    if job.status == "success":
        # #572: a successful export that the user observes counts as
        # access; reset the archive clock.
        touch_draft_access_conn(parsed_draft)
        result = job.result or {}
        docx_path = str(result.get("docx_path") or "")
        if not docx_path:
            return Div(
                Alert(
                    "Eksport valmis, kuid faili ei leitud.",
                    variant="danger",
                ),
                id="export-status",
                cls="export-status export-status-failed",
            )
        return Div(
            Span("Eksport valmis. ", cls="export-status-text"),  # noqa: F405
            A(  # noqa: F405
                "Laadi alla .docx",
                href=f"/drafts/{parsed_draft}/export/{parsed_job_id}/download",
                cls="btn btn-primary btn-sm",
            ),
            id="export-status",
            cls="export-status export-status-success",
        )

    if job.status == "failed":
        return Div(
            Alert(
                job.error_message or "Eksport ebaõnnestus.",
                variant="danger",
                title="Eksport ebaõnnestus",
            ),
            id="export-status",
            cls="export-status export-status-failed",
        )

    # #457/#471: cap polling at _EXPORT_POLLING_TIMEOUT_SECONDS so the
    # browser doesn't hammer this endpoint forever when a worker
    # hangs. After the cap we drop the polling attributes and surface
    # a yellow alert directing the user to the admin dashboard.
    # #471: if ``job.created_at`` is missing (older rows, DB race)
    # treat the current wall-clock as "just started" instead of
    # falling through to "keep polling" — that prevented the stale
    # alert from ever firing for jobs whose timestamp was NULL and
    # left the browser hammering the endpoint indefinitely.
    job_created = job.created_at
    if job_created is None:
        logger.warning(
            "Job %s has no created_at timestamp — treating as just-started for polling budget",
            job.id,
        )
        job_created = datetime.now(UTC)
    try:
        elapsed = (datetime.now(UTC) - job_created).total_seconds()
    except (TypeError, ValueError):
        elapsed = 0.0
    if elapsed > _EXPORT_POLLING_TIMEOUT_SECONDS:
        return Div(
            Alert(
                "Vajab tähelepanu — töötlemine võtab oodatust kauem aega. "
                "Kontrollige administreerimispaneelilt, kas taustajob on kinni jäänud.",
                variant="warning",
                title="Eksport venib",
            ),
            id="export-status",
            cls="export-status export-status-stale",
        )

    # pending / claimed / running / retrying — keep polling.
    # #607: hand the job's created_at to the spinner helper so it can
    # back off the poll interval (2s → 5s → 10s) as the job ages.
    return _export_status_spinner(parsed_draft, parsed_job_id, job.created_at)


# ---------------------------------------------------------------------------
# GET /drafts/{draft_id}/export/{job_id}/download
# ---------------------------------------------------------------------------


def _slugify(value: str) -> str:
    """Return a filename-safe slug for the export filename.

    Strips Estonian diacritics, replaces non-alphanumeric runs with a
    single underscore, and lower-cases the result. Empty input falls
    back to ``"impact_report"``.
    """
    if not value:
        return "impact_report"
    normalised = unicodedata.normalize("NFKD", value)
    ascii_only = normalised.encode("ascii", "ignore").decode("ascii")
    slug = re.sub(r"[^A-Za-z0-9]+", "_", ascii_only).strip("_").lower()
    return slug or "impact_report"


def download_export_handler(req: Request, draft_id: str, job_id: str):
    """GET /drafts/{draft_id}/export/{job_id}/download — file download."""
    auth_or_redirect = _require_auth(req)
    if isinstance(auth_or_redirect, Response):
        return auth_or_redirect
    auth = auth_or_redirect

    parsed_draft = _parse_uuid(draft_id)
    if parsed_draft is None:
        return _not_found_page(req)

    draft = fetch_draft(parsed_draft)
    if draft is None or not can_view_draft(auth, draft):
        return _not_found_page(req)

    try:
        parsed_job_id = int(job_id)
    except (TypeError, ValueError):
        return _not_found_page(req)

    job = JobQueue().get(parsed_job_id)
    if job is None or job.status != "success":
        return _not_found_page(req)

    payload = job.payload or {}
    if str(payload.get("draft_id")) != str(parsed_draft):
        return _not_found_page(req)

    result = job.result or {}
    docx_path = result.get("docx_path")
    if not docx_path or not Path(str(docx_path)).exists():
        return _not_found_page(req)

    filename = f"impact_report_{_slugify(draft.title)}.docx"
    log_action(
        auth.get("id"),
        "draft.report.export.download",
        {
            "draft_id": str(parsed_draft),
            "job_id": parsed_job_id,
            "filename": filename,
        },
    )
    # #572: download counts as access; reset the archive clock.
    touch_draft_access_conn(parsed_draft)

    return FileResponse(
        path=str(docx_path),
        media_type=_DOCX_MIME,
        filename=filename,
    )


# ---------------------------------------------------------------------------
# Route registration
# ---------------------------------------------------------------------------


def register_report_routes(rt) -> None:  # type: ignore[no-untyped-def]
    """Mount the impact-report + export routes on FastHTML's *rt* decorator."""
    rt("/drafts/{draft_id}/report", methods=["GET"])(draft_report_page)
    rt("/drafts/{draft_id}/export", methods=["POST"])(export_draft_report_handler)
    rt("/drafts/{draft_id}/export-status/{job_id}", methods=["GET"])(export_status_fragment)
    rt("/drafts/{draft_id}/export/{job_id}/download", methods=["GET"])(download_export_handler)
