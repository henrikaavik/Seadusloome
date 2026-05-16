"""Operational work queue for authenticated users — the ``/dashboard`` ("Töölaud") page.

Issue #717 (epic #714, design doc ``docs/2026-05-11-ministry-lawyer-ui-structure.md``):
the dashboard is no longer a welcome page. It is a daily work queue that answers
"what should I do next" by synthesising signals already present in the database:

    - active drafter sessions          → "jätka koostajas"
    - high/critical impact reports      → "vaata mõjuaruannet"
    - stale (post-re-analyze) annotations → "analüüsi uuesti"
    - recent ontology syncs             → "uued ontoloogia muudatused"
    - completed export jobs             → "hiljutised ekspordid"
    - drafts with unresolved annotations → "lahtised märkused"
    - personal bookmarks                → unchanged from the old dashboard

Every query is org-scoped (except ``sync_log``, which is global system-wide
state) and wrapped in try/except → log + return empty, mirroring the helper
pattern used across ``app/docs/draft_model.py`` and friends. No new migration
or schema change — everything is derivable from existing tables.
"""

from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FuturesTimeoutError
from typing import Any
from urllib.parse import urlencode

from fasthtml.common import *
from starlette.requests import Request
from starlette.responses import JSONResponse, RedirectResponse

from app.analyysikeskus.eu_transposition import (
    DEFAULT_TRANSPOSITION_HORIZON_DAYS,
    TranspositionDeadlineRow,
    list_overdue_or_upcoming_transpositions,
)
from app.auth.audit import log_action
from app.db import get_connection as _connect
from app.docs.impact.scoring import IMPACT_BAND_LABELS_ET, ImpactBand, impact_band
from app.drafter.state_machine import STEP_LABELS_ET, Step
from app.ui.data.data_table import Column, DataTable
from app.ui.forms.app_form import AppForm
from app.ui.forms.form_field import FormField
from app.ui.layout import PageShell
from app.ui.primitives.badge import Badge, BadgeVariant
from app.ui.primitives.button import Button
from app.ui.primitives.link_button import LinkButton
from app.ui.surfaces.card import Card, CardBody, CardHeader
from app.ui.theme import get_theme_from_request
from app.ui.time import format_tallinn

logger = logging.getLogger(__name__)


# How many synthesised "next action" rows to surface at the top of the page.
_MAX_NEXT_ACTIONS = 8

# Per-widget row caps for the supporting sections (kept small so the page
# stays dense-but-calm — see the design doc's "Visual Design Direction").
_MAX_HIGH_RISK = 8
_MAX_UNVIEWED = 8
_MAX_STALE = 8
_MAX_SYNCS = 5
_MAX_EXPORTS = 5
_MAX_UNRESOLVED = 8

# A6 (EU transposition deadlines widget): how many deadline rows the
# Töölaud renders inline; the rest go behind "Näita kõiki" → /analyysikeskus/el-ulevott.
_MAX_EU_DEADLINES = 5

# Soft timeout for the SPARQL deadline query — if Jena is slow the
# dashboard does not block. Empty list → widget hides; the page renders
# without delay (per A6 spec, the SPARQL call is gated behind a ~1s
# graceful-degradation timeout).
_EU_DEADLINES_SPARQL_TIMEOUT_S = 1.0


# ---------------------------------------------------------------------------
# DB helpers — work-queue widgets
# ---------------------------------------------------------------------------


def _get_active_drafter_sessions(user_id: str, org_id: str | None) -> list[dict]:  # type: ignore[type-arg]
    """Return the user's active drafter sessions, newest first.

    Org-scoped at the SQL layer so a stray cross-org session can never appear
    in the work queue. Returns ``[{id, current_step, updated_at}]`` (only the
    fields the "jätka koostajas" row needs).
    """
    if not user_id or not org_id:
        return []
    try:
        with _connect() as conn:
            rows = conn.execute(
                """
                SELECT id, current_step, updated_at
                FROM drafting_sessions
                WHERE user_id = %s AND org_id = %s AND status = 'active'
                ORDER BY updated_at DESC
                LIMIT %s
                """,
                (user_id, org_id, _MAX_NEXT_ACTIONS),
            ).fetchall()
        return [{"id": str(r[0]), "current_step": int(r[1]), "updated_at": r[2]} for r in rows]
    except Exception:
        logger.exception("Failed to fetch active drafter sessions for user %s", user_id)
        return []


def _get_high_risk_reports(org_id: str | None, *, limit: int = _MAX_HIGH_RISK) -> list[dict]:  # type: ignore[type-arg]
    """Return recent High/Critical impact reports for the org.

    "High/Critical" is decided by :func:`app.docs.impact.scoring.impact_band`
    (the 51-80 / 81-100 bands) — the SQL filter uses ``impact_score > 50`` as a
    cheap pre-filter and the band helper produces the user-facing label so the
    cut-offs are never duplicated.

    Returns one row per draft (the latest report for that draft) ordered by the
    most recent analysis first.
    """
    if not org_id:
        return []
    try:
        with _connect() as conn:
            # Pick the *latest* report per draft first, THEN filter by score —
            # so a draft re-analysed from high risk down to medium/low drops
            # off the list instead of clinging to its stale high-risk row.
            rows = conn.execute(
                """
                WITH latest_report AS (
                    SELECT DISTINCT ON (ir.draft_id)
                           ir.draft_id, ir.impact_score, ir.conflict_count,
                           ir.affected_count, ir.gap_count, ir.generated_at
                    FROM impact_reports ir
                    JOIN drafts d ON d.id = ir.draft_id
                    WHERE d.org_id = %s
                    ORDER BY ir.draft_id, ir.generated_at DESC
                )
                SELECT lr.draft_id, d.title, lr.impact_score,
                       lr.conflict_count, lr.affected_count, lr.gap_count,
                       lr.generated_at
                FROM latest_report lr
                JOIN drafts d ON d.id = lr.draft_id
                WHERE lr.impact_score > 50
                ORDER BY lr.generated_at DESC
                LIMIT %s
                """,
                (org_id, limit),
            ).fetchall()
    except Exception:
        logger.exception("Failed to fetch high-risk reports for org %s", org_id)
        return []
    return [
        {
            "draft_id": str(r[0]),
            "title": r[1] or "Pealkirjata eelnõu",
            "impact_score": int(r[2] or 0),
            "conflict_count": int(r[3] or 0),
            "affected_count": int(r[4] or 0),
            "gap_count": int(r[5] or 0),
            "generated_at": r[6],
        }
        for r in rows
    ]


def _get_unviewed_reports(  # type: ignore[type-arg]
    user_id: str, org_id: str | None, *, limit: int = _MAX_UNVIEWED
) -> list[dict]:
    """Return drafts whose latest impact report this user hasn't opened.

    Implements the "report ready but not viewed" next-action source from #717,
    derived from the ``draft.report.view`` audit_log entry that
    :func:`app.docs.report_routes.draft_report_page` already writes (no schema
    change). A draft surfaces when the current user has *never* opened its
    report, or last opened it before the latest (re-)analysis — so a fresh
    re-analysis re-appears in the queue.

    ``detail->>'draft_id'`` is compared as text against ``draft_id::text`` —
    no cast of the JSON field, so a stray non-UUID detail value can't take the
    whole query down. Returns ``[{draft_id, title, impact_score,
    conflict_count, generated_at, reanalyzed}]`` newest first.
    """
    if not user_id or not org_id:
        return []
    try:
        with _connect() as conn:
            rows = conn.execute(
                """
                WITH latest_report AS (
                    SELECT DISTINCT ON (ir.draft_id)
                           ir.draft_id, ir.impact_score, ir.conflict_count,
                           ir.generated_at
                    FROM impact_reports ir
                    JOIN drafts d ON d.id = ir.draft_id
                    WHERE d.org_id = %s
                    ORDER BY ir.draft_id, ir.generated_at DESC
                ),
                last_view AS (
                    SELECT a.detail->>'draft_id' AS draft_id, MAX(a.created_at) AS viewed_at
                    FROM audit_log a
                    WHERE a.user_id = %s AND a.action = 'draft.report.view'
                    GROUP BY a.detail->>'draft_id'
                )
                SELECT lr.draft_id, d.title, lr.impact_score, lr.conflict_count,
                       lr.generated_at, (lv.viewed_at IS NOT NULL) AS reanalyzed
                FROM latest_report lr
                JOIN drafts d ON d.id = lr.draft_id
                LEFT JOIN last_view lv ON lv.draft_id = lr.draft_id::text
                WHERE lv.viewed_at IS NULL OR lv.viewed_at < lr.generated_at
                ORDER BY lr.generated_at DESC
                LIMIT %s
                """,
                (org_id, user_id, limit),
            ).fetchall()
    except Exception:
        logger.exception("Failed to fetch unviewed reports for user %s", user_id)
        return []
    return [
        {
            "draft_id": str(r[0]),
            "title": r[1] or "Pealkirjata eelnõu",
            "impact_score": int(r[2] or 0),
            "conflict_count": int(r[3] or 0),
            "generated_at": r[4],
            "reanalyzed": bool(r[5]),
        }
        for r in rows
    ]


def _get_stale_analysis_drafts(org_id: str | None, *, limit: int = _MAX_STALE) -> list[dict]:  # type: ignore[type-arg]
    """Return drafts that have a ``stale=true`` unresolved annotation row.

    The ``stale`` flag is set by
    :func:`app.annotations.models.update_stale_flags_for_version` after a
    re-analyze removes a finding the user was discussing — it means "the
    ontology moved on; this analysis is out of date". Joins through
    ``draft_versions`` to recover the parent draft id + title.

    Org-scoped via ``annotations.org_id`` (and re-checked against
    ``drafts.org_id`` in the JOIN, which is harmless belt-and-braces).
    Returns ``[{draft_id, title, stale_count}]`` newest-draft first.
    """
    if not org_id:
        return []
    try:
        with _connect() as conn:
            rows = conn.execute(
                """
                SELECT d.id, d.title, COUNT(*) AS stale_count,
                       MAX(a.updated_at) AS last_change
                FROM annotations a
                JOIN draft_versions v ON v.id = a.draft_version_id
                JOIN drafts d ON d.id = v.draft_id
                WHERE a.org_id = %s
                  AND a.stale = TRUE
                  AND a.resolved = FALSE
                  AND a.draft_version_id IS NOT NULL
                GROUP BY d.id, d.title
                ORDER BY last_change DESC
                LIMIT %s
                """,
                (org_id, limit),
            ).fetchall()
        return [
            {
                "draft_id": str(r[0]),
                "title": r[1] or "Pealkirjata eelnõu",
                "stale_count": int(r[2] or 0),
            }
            for r in rows
        ]
    except Exception:
        logger.exception("Failed to fetch stale-analysis drafts for org %s", org_id)
        return []


def _get_recent_syncs(*, limit: int = _MAX_SYNCS) -> list[dict]:  # type: ignore[type-arg]
    """Return recent successful ontology syncs.

    ⚠ ``sync_log`` is global system-wide state — there is no ``org_id`` column
    and no org scoping. That is fine: a fresh ontology snapshot is information
    everyone should see. Returns ``[{id, finished_at, entity_count}]`` newest
    first.
    """
    try:
        with _connect() as conn:
            rows = conn.execute(
                """
                SELECT id, finished_at, started_at, entity_count
                FROM sync_log
                WHERE status = 'success'
                ORDER BY started_at DESC
                LIMIT %s
                """,
                (limit,),
            ).fetchall()
        return [
            {
                "id": int(r[0]),
                # finished_at can be NULL on a row that crashed between status
                # flip and timestamp write; fall back to started_at.
                "finished_at": r[1] or r[2],
                "entity_count": int(r[3]) if r[3] is not None else None,
            }
            for r in rows
        ]
    except Exception:
        logger.exception("Failed to fetch recent ontology syncs")
        return []


def _get_recent_exports(org_id: str | None, *, limit: int = _MAX_EXPORTS) -> list[dict]:  # type: ignore[type-arg]
    """Return recently completed report-export jobs for the org.

    The ``background_jobs`` table is not org-scoped, so we recover the org via
    the ``draft_id`` carried in the job payload (``payload->>'draft_id'``) and
    join to ``drafts``. The join compares ``drafts.id::text`` against the raw
    payload string — no cast of the JSON value to ``uuid``, so a single
    malformed payload can't fail the query and hide every valid export. Only
    ``export_report`` jobs in the ``success`` state are surfaced.

    Returns ``[{draft_id, title, finished_at}]`` newest first — the row links
    to ``/drafts/{id}/report`` (the report page hosts the download button), so
    we don't need the docx path here.
    """
    if not org_id:
        return []
    try:
        with _connect() as conn:
            rows = conn.execute(
                """
                SELECT j.payload->>'draft_id' AS draft_id, d.title, j.finished_at
                FROM background_jobs j
                JOIN drafts d ON d.id::text = (j.payload->>'draft_id')
                WHERE j.job_type = 'export_report'
                  AND j.status = 'success'
                  AND d.org_id = %s
                ORDER BY j.finished_at DESC NULLS LAST
                LIMIT %s
                """,
                (org_id, limit),
            ).fetchall()
        return [
            {
                "draft_id": str(r[0]),
                "title": r[1] or "Pealkirjata eelnõu",
                "finished_at": r[2],
            }
            for r in rows
        ]
    except Exception:
        # Payloads with a missing/garbled draft_id make the ``::uuid`` cast
        # throw — log it and degrade to an empty widget rather than 500.
        logger.exception("Failed to fetch recent exports for org %s", org_id)
        return []


def _get_unresolved_annotation_drafts(  # type: ignore[type-arg]
    org_id: str | None, *, limit: int = _MAX_UNRESOLVED
) -> list[dict]:
    """Return drafts that have one or more ``resolved=false`` annotations.

    Covers every annotation whose ``target_type`` resolves to a draft —
    impact-report row annotations carry a ``draft_version_id`` (joined through
    ``draft_versions``), while older ``target_type='draft'`` annotations carry
    the draft id directly in ``target_id``. That branch joins on
    ``drafts.id::text = a.target_id`` (text comparison — no ``::uuid`` cast),
    so a single malformed legacy ``target_id`` can't fail the query and hide
    every draft. Org-scoped via ``annotations.org_id``.

    Returns ``[{draft_id, title, unresolved_count}]`` ordered by the busiest
    draft first.
    """
    if not org_id:
        return []
    try:
        with _connect() as conn:
            rows = conn.execute(
                """
                SELECT d.id, d.title, COUNT(*) AS unresolved_count
                FROM annotations a
                JOIN draft_versions v ON v.id = a.draft_version_id
                JOIN drafts d ON d.id = v.draft_id
                WHERE a.org_id = %s
                  AND a.resolved = FALSE
                  AND a.draft_version_id IS NOT NULL
                GROUP BY d.id, d.title

                UNION ALL

                SELECT d.id, d.title, COUNT(*) AS unresolved_count
                FROM annotations a
                JOIN drafts d ON d.id::text = a.target_id
                WHERE a.org_id = %s
                  AND a.resolved = FALSE
                  AND a.target_type = 'draft'
                GROUP BY d.id, d.title
                """,
                (org_id, org_id),
            ).fetchall()
    except Exception:
        logger.exception("Failed to fetch drafts with unresolved annotations for org %s", org_id)
        return []
    # Merge the two UNION branches per draft (a draft could in theory have both
    # impact-report-row and draft-level annotations).
    merged: dict[str, dict[str, Any]] = {}
    for r in rows:
        draft_id = str(r[0])
        entry = merged.setdefault(
            draft_id,
            {"draft_id": draft_id, "title": r[1] or "Pealkirjata eelnõu", "unresolved_count": 0},
        )
        entry["unresolved_count"] += int(r[2] or 0)
    out = sorted(merged.values(), key=lambda d: d["unresolved_count"], reverse=True)
    return out[:limit]


# ---------------------------------------------------------------------------
# SPARQL helper — A6 EU transposition deadlines widget
# ---------------------------------------------------------------------------


def _get_eu_transposition_deadlines(
    org_id: str | None,
    *,
    horizon_days: int = DEFAULT_TRANSPOSITION_HORIZON_DAYS,
    timeout_s: float = _EU_DEADLINES_SPARQL_TIMEOUT_S,
) -> list[TranspositionDeadlineRow]:
    """Return EU directives whose transposition deadline is approaching or passed.

    Wraps :func:`app.analyysikeskus.eu_transposition.list_overdue_or_upcoming_transpositions`
    in a soft *wall-clock* timeout so a slow / stuck Jena cannot delay the
    dashboard render. On timeout (or any exception) returns ``[]`` — the
    widget then hides per the A6 empty-state rule.

    ``org_id`` is forwarded for forward-compatibility (the underlying helper
    accepts it today but does not yet scope by responsible ministry — the
    ontology doesn't expose that predicate yet).

    Implementation note (F1 + F8 fix, 2026-05-15 review): we deliberately
    do **not** use ``with ThreadPoolExecutor(...)``. Python's executor
    ``__exit__`` calls ``shutdown(wait=True)`` which blocks until
    in-flight tasks complete — that defeats the soft timeout because a
    stuck Jena query would still hold the dashboard render. Instead we
    manage the lifecycle manually and shut down with ``wait=False,
    cancel_futures=True`` in ``finally`` so the dashboard renders within
    ``timeout_s`` regardless of Jena state.

    Important resource caveat: ``concurrent.futures.ThreadPoolExecutor``
    worker threads are **not** daemonised by Python's default factory,
    and ``future.cancel()`` does not interrupt a thread that is already
    executing (it only stops a not-yet-started future). To prevent a
    stuck Jena from accumulating zombie worker threads across many
    dashboard renders, ``timeout_s`` is also pushed down into the SPARQL
    client via ``SparqlClient(timeout=timeout_s)`` inside
    :func:`list_overdue_or_upcoming_transpositions` — httpx then raises
    ``ReadTimeout`` at the network layer, the worker exits cleanly, and
    there is no orphaned thread.
    """
    pool = ThreadPoolExecutor(max_workers=1)
    try:
        future = pool.submit(
            list_overdue_or_upcoming_transpositions,
            horizon_days=horizon_days,
            org_id=org_id,
            timeout_s=timeout_s,
        )
        try:
            return future.result(timeout=timeout_s)
        except FuturesTimeoutError:
            logger.warning(
                "EU transposition deadlines query exceeded %.1fs — hiding widget", timeout_s
            )
            future.cancel()
            return []
    except Exception:
        logger.exception("Failed to fetch EU transposition deadlines")
        return []
    finally:
        pool.shutdown(wait=False, cancel_futures=True)


# ---------------------------------------------------------------------------
# DB helpers — kept verbatim from the welcome-page era
# ---------------------------------------------------------------------------


def _get_bookmarks(user_id: str) -> list[dict]:  # type: ignore[type-arg]
    """Return all bookmarks for the given user."""
    try:
        with _connect() as conn:
            rows = conn.execute(
                "SELECT id, entity_uri, label, created_at "
                "FROM bookmarks WHERE user_id = %s "
                "ORDER BY created_at DESC",
                (user_id,),
            ).fetchall()
        return [
            {
                "id": str(r[0]),
                "entity_uri": r[1],
                "label": r[2],
                "created_at": r[3],
            }
            for r in rows
        ]
    except Exception:
        logger.exception("Failed to fetch bookmarks for user %s", user_id)
        return []


def _get_user_org_info(user_id: str) -> dict | None:  # type: ignore[type-arg]
    """Return organization info for the given user (name, member count, role)."""
    try:
        with _connect() as conn:
            row = conn.execute(
                "SELECT o.name, u.role, "
                "(SELECT COUNT(*) FROM users u2 WHERE u2.org_id = o.id) AS member_count "
                "FROM users u "
                "JOIN organizations o ON o.id = u.org_id "
                "WHERE u.id = %s",
                (user_id,),
            ).fetchone()
        if row is None:
            return None
        return {
            "org_name": row[0],
            "role": row[1],
            "member_count": row[2],
        }
    except Exception:
        logger.exception("Failed to fetch org info for user %s", user_id)
        return None


def _add_bookmark(user_id: str, entity_uri: str, label: str | None) -> dict | None:  # type: ignore[type-arg]
    """Add a bookmark for the given user. Returns the created bookmark or None."""
    try:
        with _connect() as conn:
            row = conn.execute(
                "INSERT INTO bookmarks (user_id, entity_uri, label) "
                "VALUES (%s, %s, %s) "
                "ON CONFLICT (user_id, entity_uri) DO NOTHING "
                "RETURNING id, entity_uri, label, created_at",
                (user_id, entity_uri, label),
            ).fetchone()
            conn.commit()
        if row is None:
            return None
        return {
            "id": str(row[0]),
            "entity_uri": row[1],
            "label": row[2],
            "created_at": row[3],
        }
    except Exception:
        logger.exception("Failed to add bookmark for user %s", user_id)
        return None


def _remove_bookmark(bookmark_id: str, user_id: str) -> bool:
    """Remove a bookmark by ID, scoped to the given user. Returns True on success."""
    try:
        with _connect() as conn:
            result = conn.execute(
                "DELETE FROM bookmarks WHERE id = %s AND user_id = %s",
                (bookmark_id, user_id),
            )
            conn.commit()
        return (result.rowcount or 0) > 0
    except Exception:
        logger.exception("Failed to remove bookmark %s for user %s", bookmark_id, user_id)
        return False


# ---------------------------------------------------------------------------
# Rendering helpers
# ---------------------------------------------------------------------------

_ROLE_LABELS = {
    "admin": "Administraator",
    "org_admin": "Organisatsiooni admin",
    "reviewer": "Ülevaataja",
    "drafter": "Koostaja",
}

# Badge variant per impact band — restrained colour, matches the design doc's
# "clear status and risk coding" guidance.
_BAND_VARIANT: dict[ImpactBand, BadgeVariant] = {
    "low": "default",
    "medium": "warning",
    "high": "danger",
    "critical": "danger",
}


# A muted single-line "nothing here" row shared by every collapsible section.
def _empty_row(text: str):  # type: ignore[no-untyped-def]
    return P(text, cls="muted-text")


def _step_label(step_number: int) -> str:
    """Estonian label for a drafter step number, falling back to the bare number."""
    try:
        return STEP_LABELS_ET.get(Step(step_number), str(step_number))
    except ValueError:
        return str(step_number)


def _section_card(title: str, body):  # type: ignore[no-untyped-def]
    """A compact section card — ``Card(CardHeader(H3(...)), CardBody(...))``."""
    return Card(
        CardHeader(H3(title, cls="card-title")),
        CardBody(body),
    )


# ---------------------------------------------------------------------------
# Section 1: Minu järgmised tegevused
# ---------------------------------------------------------------------------


def _build_next_actions(
    sessions: list[dict],  # type: ignore[type-arg]
    high_risk: list[dict],  # type: ignore[type-arg]
    stale: list[dict],  # type: ignore[type-arg]
    unviewed: list[dict],  # type: ignore[type-arg]
) -> list[dict]:  # type: ignore[type-arg]
    """Synthesise the "what should I do next" list from existing signals.

    This is *not* a new data source — it folds four already-loaded widget
    result sets into one prioritised list of ``{text, href, link_label}``
    dicts, capped at :data:`_MAX_NEXT_ACTIONS`. Order, and one row per draft
    (a draft that qualifies for several sources gets only the most-urgent
    framing): drafter sessions first (unfinished work), then stale analyses
    (the ontology moved on — re-analyse, even if the now-outdated report
    flags high risk), then high-risk reports (conflicts to review), then
    reports you simply haven't opened yet.
    """
    actions: list[dict[str, str]] = []
    seen_drafts: set[str] = set()

    for s in sessions:
        step_num = s["current_step"]
        actions.append(
            {
                "text": f"Jätka koostajas — {step_num}. samm: {_step_label(step_num)}",
                "href": f"/drafter/{s['id']}",
                "link_label": "Ava koostaja",
            }
        )

    for st in stale:
        did = st["draft_id"]
        if did in seen_drafts:
            continue
        seen_drafts.add(did)
        actions.append(
            {
                "text": (
                    f"«{st['title']}»: ontoloogia uuenes pärast aruande koostamist. "
                    "Analüüsi uuesti."
                ),
                "href": f"/drafts/{did}/report",
                "link_label": "Ava aruanne",
            }
        )

    for r in high_risk:
        did = r["draft_id"]
        if did in seen_drafts:
            continue
        seen_drafts.add(did)
        conflicts = r["conflict_count"]
        title = r["title"]
        if conflicts > 0:
            text = f"Vaata mõjuaruannet «{title}» — {conflicts} konflikti vajavad ülevaatust"
        else:
            band_label = IMPACT_BAND_LABELS_ET[impact_band(r["impact_score"])].lower()
            text = f"Vaata mõjuaruannet «{title}» — {band_label} (skoor {r['impact_score']}/100)"
        actions.append(
            {"text": text, "href": f"/drafts/{did}/report", "link_label": "Ava aruanne"}
        )

    for u in unviewed:
        did = u["draft_id"]
        if did in seen_drafts:
            continue
        seen_drafts.add(did)
        title = u["title"]
        if u["reanalyzed"]:
            text = f"«{title}»: eelnõu analüüsiti uuesti — vaata uut mõjuaruannet."
        else:
            text = f"Mõjuaruanne valmis: «{title}». Vaata aruannet."
        actions.append(
            {"text": text, "href": f"/drafts/{did}/report", "link_label": "Ava aruanne"}
        )

    return actions[:_MAX_NEXT_ACTIONS]


def _next_actions_card(actions: list[dict]):  # type: ignore[type-arg, no-untyped-def]
    if not actions:
        return _section_card("Minu järgmised tegevused", _empty_row("Hetkel pole midagi ootel."))
    items = [
        Li(
            Span(a["text"], cls="next-action-text"),
            LinkButton(a["link_label"], href=a["href"], variant="secondary", size="sm"),
            cls="next-action-item",
        )
        for a in actions
    ]
    return _section_card("Minu järgmised tegevused", Ul(*items, cls="next-action-list"))


# ---------------------------------------------------------------------------
# Section 2: Kõrge riskiga leiud
# ---------------------------------------------------------------------------


def _high_risk_card(reports: list[dict]):  # type: ignore[type-arg, no-untyped-def]
    if not reports:
        return _section_card(
            "Kõrge riskiga leiud", _empty_row("Kõrge riskiga mõjuaruandeid hetkel pole.")
        )
    columns = [
        Column(key="title", label="Eelnõu", sortable=False),
        Column(
            key="band",
            label="Risk",
            sortable=False,
            render=lambda r: Badge(r["band_label"], variant=r["band_variant"]),
        ),
        Column(key="counts", label="Leiud", sortable=False),
        Column(key="generated_at", label="Analüüsitud", sortable=False),
        Column(
            key="actions",
            label="",
            sortable=False,
            render=lambda r: A("Vaata aruannet", href=r["href"], cls="table-link"),
        ),
    ]
    rows = []
    for r in reports:
        band = impact_band(r["impact_score"])
        rows.append(
            {
                "title": r["title"],
                "band_label": IMPACT_BAND_LABELS_ET[band],
                "band_variant": _BAND_VARIANT[band],
                "counts": (
                    f"{r['conflict_count']} konflikti · {r['affected_count']} mõjutatud · "
                    f"{r['gap_count']} lünka"
                ),
                "generated_at": format_tallinn(r["generated_at"]),
                "href": f"/drafts/{r['draft_id']}/report",
            }
        )
    return _section_card("Kõrge riskiga leiud", DataTable(columns=columns, rows=rows))


# ---------------------------------------------------------------------------
# Section 2b: EL ülevõtu tähtajad (A6)
# ---------------------------------------------------------------------------
#
# Proactive surveillance widget — surfaces EU directives whose
# transposition deadline is within the next 90 days **and** Estonia's
# transposition status is not yet ``"kaetud"``. The widget is operational,
# not decorative: every row click-throughs to the existing EL ülevõtt
# workflow pre-filled with the directive's CELEX.
#
# Empty-state policy: hide the entire card when there are no rows. The
# dashboard already runs long, and an empty "no upcoming transpositions"
# message would be noise. Caller (``dashboard_page``) only includes the
# card in the page tree when :func:`_eu_deadlines_card` returns a node.


# Map a row's days_remaining + status onto the Estonian status text +
# badge variant the table renders. Severity order (badge colour):
#
#   Tähtaeg möödunud  → danger  (red)
#   Tähtaeg läheneb   → warning (amber) — within 30 days
#   Ülevõtt puudub    → danger  (red)   — no transposing act at all
#   Ülevõtt osaline   → warning (amber)
#   Ebaselge          → default (neutral)
#
# Time-based status wins over status-bucket: an overdue row is always
# rendered as "Tähtaeg möödunud" even if its transposition status is
# only "osaline" — the message is "act now", not "this is partial".


def _deadline_badge_variant(days_remaining: int) -> BadgeVariant:
    """Pick the deadline-cell badge colour from days_remaining."""
    if days_remaining < 0:
        return "danger"
    if days_remaining < 30:
        return "warning"
    return "default"


def _deadline_badge_label(days_remaining: int) -> str:
    """Pick the deadline-cell Estonian label from days_remaining."""
    if days_remaining < 0:
        # ``abs()`` keeps the surface non-negative; the colour already
        # signals "overdue".
        return f"Tähtaeg möödunud ({abs(days_remaining)} p)"
    if days_remaining == 0:
        return "Tähtaeg täna"
    if days_remaining < 30:
        return f"Tähtaeg {days_remaining} päeva"
    return f"{days_remaining} päeva"


_STATUS_LABELS_ET: dict[str, str] = {
    "puudub": "Ülevõtt puudub",
    "osaline": "Ülevõtt osaline",
    "ebaselge": "Ebaselge",
    "kaetud": "Üle võetud",  # never rendered (filtered out) but kept for completeness
}

_STATUS_VARIANTS: dict[str, BadgeVariant] = {
    "puudub": "danger",
    "osaline": "warning",
    "ebaselge": "default",
    "kaetud": "success",
}


def _format_deadline_date(d: Any) -> str:
    """Format a deadline ``date`` as ``DD.MM.YYYY`` (matches ``format_tallinn``)."""
    try:
        return d.strftime("%d.%m.%Y")
    except Exception:
        return str(d)


def _el_ulevott_link(celex: str) -> str:
    """Build a ``/analyysikeskus/el-ulevott?sisend=<celex>`` URL."""
    return "/analyysikeskus/el-ulevott?" + urlencode({"sisend": celex})


def _eu_deadlines_card(rows: list[TranspositionDeadlineRow]):  # type: ignore[no-untyped-def]
    """Render the "EL ülevõtu tähtajad" Töölaud widget.

    Returns ``None`` when there are no rows so the caller can omit the
    section entirely (per the A6 empty-state rule). Renders the top
    :data:`_MAX_EU_DEADLINES` rows; when more exist, a "Näita kõiki (X)"
    link at the bottom points at the EL ülevõtt workflow.
    """
    if not rows:
        return None

    total = len(rows)
    top_rows = rows[:_MAX_EU_DEADLINES]

    columns = [
        Column(
            key="deadline",
            label="Tähtaeg",
            sortable=False,
            render=lambda r: Badge(
                r["deadline_label"],
                variant=r["deadline_variant"],
            ),
        ),
        Column(key="celex", label="CELEX", sortable=False),
        Column(key="directive_label", label="Direktiiv", sortable=False),
        Column(
            key="status",
            label="Staatus",
            sortable=False,
            render=lambda r: Badge(r["status_label"], variant=r["status_variant"]),
        ),
        Column(
            key="actions",
            label="",
            sortable=False,
            render=lambda r: A(
                "Vaata ülevõttu →",
                href=r["href"],
                cls="table-link el-deadlines-action",
                # Operational widget — keep the touch-target obvious.
                aria_label=f"Vaata EL ülevõttu — {r['celex']}",
            ),
        ),
    ]
    table_rows = [
        {
            "celex": row.celex,
            "directive_label": row.directive_label_et,
            "deadline_label": (
                f"{_format_deadline_date(row.deadline)} · "
                f"{_deadline_badge_label(row.days_remaining)}"
            ),
            "deadline_variant": _deadline_badge_variant(row.days_remaining),
            "status_label": _STATUS_LABELS_ET.get(row.status, row.status),
            "status_variant": _STATUS_VARIANTS.get(row.status, "default"),
            "href": _el_ulevott_link(row.celex),
        }
        for row in top_rows
    ]

    body_children: list[Any] = [DataTable(columns=columns, rows=table_rows)]
    if total > _MAX_EU_DEADLINES:
        body_children.append(
            P(
                A(
                    f"Näita kõiki ({total}) →",
                    href="/analyysikeskus/el-ulevott?vaade=tahtajad",
                    cls="el-deadlines-show-all",
                ),
                cls="el-deadlines-show-all-row",
            )
        )

    return Card(
        CardHeader(H3("EL ülevõtu tähtajad", cls="card-title")),
        CardBody(*body_children),
    )


# ---------------------------------------------------------------------------
# Section 3: Aegunud analüüsid
# ---------------------------------------------------------------------------


def _stale_card(drafts: list[dict]):  # type: ignore[type-arg, no-untyped-def]
    if not drafts:
        return _section_card("Aegunud analüüsid", _empty_row("Aegunud analüüse pole."))
    items = [
        Li(
            Span(
                f"«{d['title']}» — ontoloogia uuenes, analüüsi uuesti "
                f"({d['stale_count']} aegunud märkust).",
                cls="stale-text",
            ),
            LinkButton(
                "Ava aruanne",
                href=f"/drafts/{d['draft_id']}/report",
                variant="secondary",
                size="sm",
            ),
            cls="stale-item",
        )
        for d in drafts
    ]
    return _section_card("Aegunud analüüsid", Ul(*items, cls="stale-list"))


# ---------------------------------------------------------------------------
# Section 4: Uued ontoloogia muudatused
# ---------------------------------------------------------------------------


def _syncs_card(syncs: list[dict]):  # type: ignore[type-arg, no-untyped-def]
    if not syncs:
        return _section_card(
            "Uued ontoloogia muudatused", _empty_row("Hiljutisi ontoloogia uuendusi pole.")
        )
    columns = [
        Column(key="finished_at", label="Uuendatud", sortable=False),
        Column(key="entity_count", label="Olemeid ontoloogias", sortable=False),
    ]
    rows = [
        {
            "finished_at": format_tallinn(s["finished_at"]),
            "entity_count": (
                f"{s['entity_count']:,}".replace(",", " ")
                if s["entity_count"] is not None
                else "—"
            ),
        }
        for s in syncs
    ]
    return _section_card("Uued ontoloogia muudatused", DataTable(columns=columns, rows=rows))


# ---------------------------------------------------------------------------
# Section 5: Hiljutised ekspordid
# ---------------------------------------------------------------------------


def _exports_card(exports: list[dict]):  # type: ignore[type-arg, no-untyped-def]
    if not exports:
        return _section_card("Hiljutised ekspordid", _empty_row("Hiljutisi eksporte pole."))
    items = [
        Li(
            Span(
                f"«{e['title']}» mõjuaruanne eksporditud"
                + (f" — {format_tallinn(e['finished_at'])}" if e["finished_at"] else ""),
                cls="export-text",
            ),
            LinkButton(
                "Ava aruanne",
                href=f"/drafts/{e['draft_id']}/report",
                variant="secondary",
                size="sm",
            ),
            cls="export-item",
        )
        for e in exports
    ]
    return _section_card("Hiljutised ekspordid", Ul(*items, cls="export-list"))


# ---------------------------------------------------------------------------
# Section 6: Eelnõud lahtiste märkustega
# ---------------------------------------------------------------------------


def _unresolved_card(drafts: list[dict]):  # type: ignore[type-arg, no-untyped-def]
    if not drafts:
        return _section_card(
            "Eelnõud lahtiste märkustega", _empty_row("Lahtiste märkustega eelnõusid pole.")
        )
    columns = [
        Column(key="title", label="Eelnõu", sortable=False),
        Column(
            key="unresolved_count",
            label="Lahtisi märkusi",
            sortable=False,
            render=lambda r: Badge(str(r["unresolved_count"]), variant="warning"),
        ),
        Column(
            key="actions",
            label="",
            sortable=False,
            render=lambda r: A("Vaata aruannet", href=r["href"], cls="table-link"),
        ),
    ]
    rows = [
        {
            "title": d["title"],
            "unresolved_count": d["unresolved_count"],
            "href": f"/drafts/{d['draft_id']}/report",
        }
        for d in drafts
    ]
    return _section_card("Eelnõud lahtiste märkustega", DataTable(columns=columns, rows=rows))


# ---------------------------------------------------------------------------
# Section 7: Minu järjehoidjad — KEPT VERBATIM
# ---------------------------------------------------------------------------


def _bookmarks_card(bookmarks: list[dict]):  # type: ignore[type-arg]
    """Render the bookmarks card (list + add form)."""
    if not bookmarks:
        table: object = P("Järjehoidjaid ei leitud.", cls="muted-text")
    else:
        columns = [
            Column(key="label", label="Nimi", sortable=False),
            Column(
                key="entity_uri",
                label="URI",
                sortable=False,
                render=lambda r: A(r["entity_uri"], href=r["entity_uri"]),
            ),
            Column(
                key="actions",
                label="Tegevused",
                sortable=False,
                render=lambda r: AppForm(
                    Button(
                        "Eemalda",
                        type="submit",
                        variant="secondary",
                        size="sm",
                    ),
                    method="post",
                    action=f"/api/bookmarks/{r['id']}/delete",
                    cls="inline-form",
                ),
            ),
        ]
        rows = [
            {
                "id": bm["id"],
                "label": bm["label"] or bm["entity_uri"],
                "entity_uri": bm["entity_uri"],
            }
            for bm in bookmarks
        ]
        table = DataTable(columns=columns, rows=rows)

    add_form = AppForm(
        FormField(name="entity_uri", label="URI", type="text", required=True),
        FormField(name="label", label="Nimi", type="text"),
        Button("Lisa järjehoidja", type="submit", variant="primary"),
        method="post",
        action="/api/bookmarks",
        cls="bookmark-add-form",
    )

    return Card(
        CardHeader(H3("Järjehoidjad", cls="card-title")),
        CardBody(table, add_form),
    )


# ---------------------------------------------------------------------------
# Route handlers
# ---------------------------------------------------------------------------


def dashboard_page(req: Request):
    """GET /dashboard — operational work queue for authenticated users."""
    auth = req.scope.get("auth", {})
    theme = get_theme_from_request(req)
    user_id = auth.get("id")
    org_id = auth.get("org_id")
    full_name = auth.get("full_name", "Kasutaja")
    first_name = (full_name or "Kasutaja").split()[0] if full_name else "Kasutaja"

    if user_id:
        sessions = _get_active_drafter_sessions(user_id, org_id)
        high_risk = _get_high_risk_reports(org_id)
        unviewed = _get_unviewed_reports(user_id, org_id)
        stale = _get_stale_analysis_drafts(org_id)
        syncs = _get_recent_syncs()
        exports = _get_recent_exports(org_id)
        unresolved = _get_unresolved_annotation_drafts(org_id)
        bookmarks = _get_bookmarks(user_id)
        org_info = _get_user_org_info(user_id)
        eu_deadlines = _get_eu_transposition_deadlines(org_id)
    else:
        sessions = high_risk = unviewed = stale = syncs = exports = unresolved = bookmarks = []
        org_info = None
        eu_deadlines = []

    next_actions = _build_next_actions(sessions, high_risk, stale, unviewed)

    # Compact header — no marketing hero. H1 + a small org/role line.
    if org_info is not None:
        role_label = _ROLE_LABELS.get(org_info["role"], org_info["role"])
        subtitle = Small(f"{org_info['org_name']} · {role_label}", cls="page-subtitle")
    else:
        subtitle = Small("Te ei kuulu ühtegi organisatsiooni.", cls="page-subtitle")

    # A6: the EU deadlines widget hides entirely when there are no rows
    # (no decorative empty box), so it's spliced in only when present.
    eu_deadlines_card = _eu_deadlines_card(eu_deadlines)

    content_parts: list[Any] = [
        H1(f"Tere, {first_name}", cls="page-title"),
        subtitle,
        _next_actions_card(next_actions),
        _high_risk_card(high_risk),
    ]
    if eu_deadlines_card is not None:
        content_parts.append(eu_deadlines_card)
    content_parts.extend(
        [
            _stale_card(stale),
            _syncs_card(syncs),
            _exports_card(exports),
            _unresolved_card(unresolved),
            _bookmarks_card(bookmarks),
        ]
    )
    content = tuple(content_parts)

    return PageShell(
        *content,
        title="Töölaud",
        user=auth or None,
        theme=theme,
        active_nav="/dashboard",
    )


def _wants_json(req: Request) -> bool:
    """True when the caller is an XHR/fetch (``X-Requested-With: XMLHttpRequest``).

    The bookmark endpoints serve two callers: a plain HTML ``<form>`` on the
    dashboard (which wants a 303 redirect so the page re-renders) and the
    Õiguskaart bookmark button (a ``fetch()`` that needs a real JSON response
    — a 303 to ``/dashboard`` vs a 303 to ``/auth/login`` are indistinguishable
    to a ``redirect: "manual"`` fetch, so an expired session looked like a
    successful save — #743).
    """
    return req.headers.get("x-requested-with", "").lower() == "xmlhttprequest"


def add_bookmark(req: Request, entity_uri: str, label: str = ""):
    """POST /api/bookmarks — add a bookmark for the current user.

    XHR callers get JSON (``200 {"ok": true, ...}`` / ``401 {"ok": false,
    "error": "auth"}``); plain-form callers get the 303 redirects.
    """
    wants_json = _wants_json(req)
    auth = req.scope.get("auth", {})
    user_id = auth.get("id")
    if not user_id:
        if wants_json:
            return JSONResponse({"ok": False, "error": "auth"}, status_code=401)
        return RedirectResponse(url="/auth/login", status_code=303)

    actual_label = label.strip() if label else None
    bookmark = _add_bookmark(user_id, entity_uri.strip(), actual_label)
    if bookmark:
        log_action(user_id, "bookmark.add", {"entity_uri": entity_uri, "label": actual_label})
    if wants_json:
        # ``bookmark`` is None when the row already existed (ON CONFLICT DO
        # NOTHING) — still "ok" from the caller's point of view.
        return JSONResponse({"ok": True, "id": bookmark["id"] if bookmark else None})
    return RedirectResponse(url="/dashboard", status_code=303)


def remove_bookmark(req: Request, bookmark_id: str):
    """POST /api/bookmarks/{bookmark_id}/delete — remove a bookmark.

    XHR callers get JSON (``200 {"ok": <bool>}`` / ``401 {"ok": false,
    "error": "auth"}``); plain-form callers get the 303 redirects.
    """
    wants_json = _wants_json(req)
    auth = req.scope.get("auth", {})
    user_id = auth.get("id")
    if not user_id:
        if wants_json:
            return JSONResponse({"ok": False, "error": "auth"}, status_code=401)
        return RedirectResponse(url="/auth/login", status_code=303)

    success = _remove_bookmark(bookmark_id, user_id)
    if success:
        log_action(user_id, "bookmark.remove", {"bookmark_id": bookmark_id})
    if wants_json:
        return JSONResponse({"ok": bool(success)})
    return RedirectResponse(url="/dashboard", status_code=303)


def index_redirect(req: Request):
    """GET / — redirect authenticated users to the dashboard."""
    auth = req.scope.get("auth")
    if auth:
        return RedirectResponse(url="/dashboard", status_code=303)
    return RedirectResponse(url="/auth/login", status_code=303)


# ---------------------------------------------------------------------------
# Route registration
# ---------------------------------------------------------------------------


def register_dashboard_routes(rt) -> None:  # type: ignore[no-untyped-def]
    """Register personal dashboard routes on the FastHTML route decorator *rt*."""
    rt("/dashboard", methods=["GET"])(dashboard_page)
    rt("/api/bookmarks", methods=["POST"])(add_bookmark)
    rt("/api/bookmarks/{bookmark_id}/delete", methods=["POST"])(remove_bookmark)
