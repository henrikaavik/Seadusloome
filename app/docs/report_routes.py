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


def _current_ontology_version() -> str:
    """Return the live Jena sync snapshot tag for version-drift checks (#612).

    Matches the ``<iso-timestamp>@<entity_count>`` format written by the
    analyze handler into ``impact_reports.ontology_version`` so a
    direct string comparison tells us whether the snapshot the report
    ran against is still current. Returns ``"unknown"`` on any failure
    so the banner gracefully degrades to "no drift detected".
    """
    try:
        with _connect() as conn:
            row = conn.execute(
                """
                select started_at, entity_count
                from sync_log
                where status = 'success'
                order by started_at desc
                limit 1
                """
            ).fetchone()
    except Exception:
        logger.warning("current ontology version lookup failed", exc_info=True)
        return "unknown"
    if row is None:
        return "unknown"
    started_at, entity_count = row
    if started_at is None:
        return "unknown"
    if isinstance(started_at, datetime):
        ts = started_at.astimezone(UTC).isoformat()
    else:
        ts = str(started_at)
    return f"{ts}@{entity_count or 0}"


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


# #611: identifier for the paginated report sections. Kept in sync
# with :func:`_resolve_section` which maps these keys to the column
# builder + source of rows. Any new section must be added both here
# and in ``_SECTION_CONFIG`` so the "Näita rohkem" route recognises it.
_SECTION_AFFECTED = "affected"
_SECTION_CONFLICTS = "conflicts"
_SECTION_EU = "eu"
_SECTION_GAPS = "gaps"


def _affected_columns() -> list[Column]:
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

    return [
        Column(key="type", label="Tüüp", sortable=False, render=_type_cell),
        Column(key="label", label="Nimetus", sortable=False, render=_label_cell),
        Column(key="uri", label="URI", sortable=False, render=_uri_cell),
    ]


def _conflicts_columns() -> list[Column]:
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

    return [
        Column(key="draft_ref", label="Eelnõu viide", sortable=False, render=_draft_ref),
        Column(
            key="conflicting_entity",
            label="Konflikti üksus",
            sortable=False,
            render=_conflict_entity,
        ),
        Column(key="reason", label="Põhjus", sortable=False, render=_reason),
    ]


def _eu_columns() -> list[Column]:
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

    return [
        Column(key="eu_act", label="EL õigusakt", sortable=False, render=_eu_act),
        Column(key="provision", label="Eesti säte", sortable=False, render=_ee_provision),
        Column(key="status", label="Staatus", sortable=False, render=_status),
    ]


def _gaps_columns() -> list[Column]:
    def _cluster(row: dict[str, Any]):
        return str(row.get("topic_cluster_label") or row.get("topic_cluster") or "—")

    def _coverage(row: dict[str, Any]):
        return f"{row.get('referenced_provisions', '0')} / {row.get('total_provisions', '0')}"

    def _description(row: dict[str, Any]):
        return str(row.get("description") or "—")

    return [
        Column(key="cluster", label="Teemaklaster", sortable=False, render=_cluster),
        Column(key="coverage", label="Sätete kaetus", sortable=False, render=_coverage),
        Column(key="description", label="Kirjeldus", sortable=False, render=_description),
    ]


# Section → (findings key, columns builder, empty message). Used by
# the "Näita rohkem" route to look up the source rows + column shape
# from just the URL slug.
_SECTION_CONFIG: dict[str, tuple[str, Any, str]] = {
    _SECTION_AFFECTED: (
        "affected_entities",
        _affected_columns,
        "Mõjutatud üksuseid ei tuvastatud.",
    ),
    _SECTION_CONFLICTS: (
        "conflicts",
        _conflicts_columns,
        "Konflikte ei tuvastatud.",
    ),
    _SECTION_EU: (
        "eu_compliance",
        _eu_columns,
        "EL-i õigusaktide seoseid ei tuvastatud.",
    ),
    _SECTION_GAPS: (
        "gaps",
        _gaps_columns,
        "Lünki ei tuvastatud.",
    ),
}


def _section_pager(
    draft_id: str,
    section: str,
    shown: int,
    total: int,
) -> Any:
    """Render the "Kuvatud X / Y" footer + optional "Näita rohkem" button.

    The whole pager is wrapped in a Div with id
    ``section-<section>-pager`` so the HTMX fragment can swap it with
    ``outerHTML`` when the user clicks to load more.
    """
    pager_id = f"section-{section}-pager"
    children: list = []
    if total > _MAX_INLINE_ROWS:
        children.append(
            P(  # noqa: F405
                f"Kuvatud {shown} / {total}",
                cls="muted-text section-pager-count",
            )
        )
    if shown < total and draft_id:
        children.append(
            Button(
                "Näita rohkem",
                type="button",
                variant="secondary",
                size="sm",
                hx_get=(
                    f"/drafts/{draft_id}/report/section/{section}"
                    f"?offset={shown}&limit={_MAX_INLINE_ROWS}"
                ),
                hx_target=f"#{pager_id}",
                hx_swap="outerHTML",
            )
        )
    return Div(*children, id=pager_id, cls="section-pager")  # noqa: F405


def _affected_entities_section(findings: dict[str, Any], draft_id: str = "") -> Any:
    """Build the "Mõjutatud üksused" data table section."""
    rows = list(findings.get("affected_entities") or [])
    total = len(rows)
    visible = rows[:_MAX_INLINE_ROWS]
    table_rows = [{"_": True, **row} for row in visible]

    body_children: list = [
        DataTable(
            columns=_affected_columns(),
            rows=table_rows,
            empty_message="Mõjutatud üksuseid ei tuvastatud.",
        ),
        # #611: paginated footer + "Näita rohkem" button
        _section_pager(draft_id, _SECTION_AFFECTED, len(visible), total),
    ]

    return Card(
        CardHeader(H3("Mõjutatud üksused", cls="card-title")),  # noqa: F405
        CardBody(*body_children),
    )


def _conflicts_section(findings: dict[str, Any], draft_id: str = "") -> Any:
    """Build the "Konfliktid" section."""
    rows = list(findings.get("conflicts") or [])
    total = len(rows)

    if not rows:
        body: Any = Alert(
            "Konflikte ei tuvastatud.",
            variant="success",
        )
        pager: Any = _section_pager(draft_id, _SECTION_CONFLICTS, 0, 0)
    else:
        visible = rows[:_MAX_INLINE_ROWS]
        body = DataTable(
            columns=_conflicts_columns(),
            rows=visible,
            empty_message="Konflikte ei tuvastatud.",
        )
        pager = _section_pager(draft_id, _SECTION_CONFLICTS, len(visible), total)

    body_children: list = [body, pager]
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
    total = len(rows)
    visible = rows[:_MAX_INLINE_ROWS]

    body_children: list = [
        DataTable(
            columns=_eu_columns(),
            rows=visible,
            empty_message="EL-i õigusaktide seoseid ei tuvastatud.",
        ),
        _section_pager(draft_id, _SECTION_EU, len(visible), total),
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
    total = len(rows)
    visible = rows[:_MAX_INLINE_ROWS]

    body_children: list = [
        DataTable(
            columns=_gaps_columns(),
            rows=visible,
            empty_message="Lünki ei tuvastatud.",
        ),
        _section_pager(draft_id, _SECTION_GAPS, len(visible), total),
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

    # #612: detect ontology-version drift. The report's snapshot tag
    # lives in report_row[7]; if it no longer matches the live Jena
    # sync_log snapshot, surface a banner offering a one-click re-run.
    report_version = str(report_row[7] or "unknown")
    current_version = _current_ontology_version()
    version_banner: Any = None
    if (
        report_version
        and current_version
        and report_version != "unknown"
        and current_version != "unknown"
        and report_version != current_version
    ):
        version_banner = Alert(
            Div(
                P(  # noqa: F405
                    f"Ontoloogia on uuenenud versioonile {current_version} "
                    f"(aruanne kasutas {report_version}).",
                ),
                Form(  # noqa: F405
                    Button(
                        "Analüüsi uuesti",
                        type="submit",
                        variant="primary",
                        size="sm",
                    ),
                    method="post",
                    action=f"/drafts/{draft.id}/report/reanalyze",
                    hx_post=f"/drafts/{draft.id}/report/reanalyze",
                    hx_swap="outerHTML",
                    hx_target="closest .alert",
                    cls="inline-form",
                ),
                cls="ontology-drift-banner",
            ),
            variant="warning",
            title="Ontoloogia on uuenenud",
        )

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

    shell_children: list = [header]
    if version_banner is not None:
        shell_children.append(version_banner)
    shell_children.extend(
        [
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
        ]
    )
    return PageShell(
        *shell_children,
        _affected_entities_section(findings, draft_id=str(draft.id)),
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
# POST /drafts/{draft_id}/report/reanalyze — ontology-drift re-run (#612)
# ---------------------------------------------------------------------------


def reanalyze_report_handler(req: Request, draft_id: str):
    """POST /drafts/{draft_id}/report/reanalyze — enqueue a fresh analyze job.

    Wired to the "Analüüsi uuesti" button in the ontology-drift banner
    (#612). Authentication + org scoping mirror :func:`draft_report_page`:
    drafts from other orgs resolve to the 404 page so we don't leak
    their existence.
    """
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

    try:
        job_id = JobQueue().enqueue(
            "analyze_impact",
            {"draft_id": str(parsed)},
            priority=5,
        )
    except Exception:
        logger.exception("Failed to enqueue re-analyze for draft=%s", parsed)
        return Div(
            Alert(
                "Uuesti analüüsi käivitamine ebaõnnestus. Palun proovige uuesti.",
                variant="danger",
            ),
            cls="alert",
        )

    log_action(
        auth.get("id"),
        "draft.report.reanalyze",
        {"draft_id": str(parsed), "job_id": job_id},
    )
    logger.info(
        "Ontology-drift re-analyze enqueued draft=%s job_id=%s user=%s",
        parsed,
        job_id,
        auth.get("id"),
    )

    # HTMX: replace the banner with a success message in place.
    if req.headers.get("HX-Request") == "true":
        return Alert(
            "Uuesti analüüs käivitati. Tulemused uuenevad mõne minuti jooksul.",
            variant="success",
            title="Analüüs alustati",
        )
    # Non-HTMX: redirect back to the draft detail where the status
    # tracker will pick up the new pipeline run.
    from starlette.responses import RedirectResponse

    return RedirectResponse(url=f"/drafts/{parsed}", status_code=303)


# ---------------------------------------------------------------------------
# GET /drafts/{draft_id}/report/section/{section} — pagination fragment (#611)
# ---------------------------------------------------------------------------


def report_section_fragment(req: Request, draft_id: str, section: str):
    """Return the next page of rows for a report section.

    Emits a batch of rows rendered as a DataTable (same columns as the
    initial page) followed by a refreshed ``_section_pager`` that
    drives the next click. The caller swaps this fragment in with
    ``hx-target=#section-{section}-pager, hx-swap=outerHTML``.
    """
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

    config = _SECTION_CONFIG.get(section)
    if config is None:
        return _not_found_page(req)
    findings_key, columns_builder, empty_message = config

    try:
        offset = max(0, int(req.query_params.get("offset", "0")))
    except ValueError:
        offset = 0
    try:
        limit = max(1, int(req.query_params.get("limit", str(_MAX_INLINE_ROWS))))
    except ValueError:
        limit = _MAX_INLINE_ROWS

    report_row = _fetch_latest_report(parsed_draft)
    if report_row is None:
        return _not_found_page(req)

    findings = _parse_report_data(report_row[6])
    rows = list(findings.get(findings_key) or [])
    total = len(rows)
    batch = rows[offset : offset + limit]

    # Affected-entities rows are rendered through a tuple-dict shim —
    # keep the same key on the follow-up batch for consistency.
    if section == _SECTION_AFFECTED:
        batch = [{"_": True, **row} for row in batch]

    children: list = []
    if batch:
        children.append(
            DataTable(
                columns=columns_builder(),
                rows=batch,
                empty_message=empty_message,
            )
        )
    children.append(
        _section_pager(
            str(draft.id),
            section,
            shown=min(offset + len(batch), total),
            total=total,
        )
    )
    # Because the trigger pager is swapped outerHTML, we wrap the
    # overflow batch + the new pager in a Fragment-like container so
    # both land in the DOM in place of the old pager.
    return Div(*children, cls="section-pager-fragment")  # noqa: F405


# ---------------------------------------------------------------------------
# POST /drafts/{draft_id}/export
# ---------------------------------------------------------------------------


def _find_active_export_job(draft_id: uuid.UUID, report_id: str) -> int | None:
    """Return an in-flight export_report job id for *draft_id* if one exists.

    "In-flight" = status IN (pending, claimed, running, retrying). The
    caller uses this to dedupe #627: a repeated click should reuse the
    job already running rather than queueing a second .docx render.
    Returns ``None`` if nothing is in flight, or on any DB error (the
    fallback is to just enqueue a fresh job so we never block the user
    on a broken lookup).
    """
    try:
        with _connect() as conn:
            row = conn.execute(
                """
                SELECT id FROM background_jobs
                WHERE job_type = 'export_report'
                  AND status IN ('pending', 'claimed', 'running', 'retrying')
                  AND payload->>'draft_id' = %s
                  AND payload->>'report_id' = %s
                ORDER BY id DESC
                LIMIT 1
                """,
                (str(draft_id), str(report_id)),
            ).fetchone()
    except Exception:
        logger.exception(
            "_find_active_export_job failed for draft=%s report=%s",
            draft_id,
            report_id,
        )
        return None
    return int(row[0]) if row else None


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
    # #627: dedupe. If an export job for this draft+report is already
    # pending/claimed/running, reuse its id instead of enqueueing a new
    # one. Without this guard a reviewer could spam-click the button
    # (or the HTMX request could double-fire on flaky networks) and
    # queue up a pile of redundant .docx renders.
    existing_job_id = _find_active_export_job(parsed, report_id)
    if existing_job_id is not None:
        logger.info(
            "Reusing existing export_report job %s for draft=%s report=%s",
            existing_job_id,
            parsed,
            report_id,
        )
        # #572: still counts as access.
        touch_draft_access_conn(parsed)
        return _export_status_spinner(parsed, existing_job_id)

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
    # #611: paginated "Näita rohkem" fragment for each report section.
    rt("/drafts/{draft_id}/report/section/{section}", methods=["GET"])(report_section_fragment)
    # #612: "Analüüsi uuesti" when the ontology has drifted past the
    # snapshot the report ran against.
    rt("/drafts/{draft_id}/report/reanalyze", methods=["POST"])(reanalyze_report_handler)
    rt("/drafts/{draft_id}/export", methods=["POST"])(export_draft_report_handler)
    rt("/drafts/{draft_id}/export-status/{job_id}", methods=["GET"])(export_status_fragment)
    rt("/drafts/{draft_id}/export/{job_id}/download", methods=["GET"])(download_export_handler)
