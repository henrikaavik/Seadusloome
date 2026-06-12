"""Framework-free data layer for the ``/dashboard`` ("Töölaud") work queue.

This module owns every database / SPARQL query the Töölaud page renders. It
is deliberately **framework-free** — no ``fasthtml`` / ``starlette`` imports —
so each widget loader is a clean ``inputs → rows`` function that the Phase-5
public API + MCP server can wrap as a tool without dragging in the web layer
(CLAUDE.md "Internal service functions"). The rendering + route layer lives in
:mod:`app.dashboard.pages` and calls these functions (the
``ThreadPoolExecutor`` widget-timeout pattern stays in the page layer).

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

from app.analyysikeskus.eu_transposition import (
    DEFAULT_TRANSPOSITION_HORIZON_DAYS,
    TranspositionDeadlineRow,
    list_overdue_or_upcoming_transpositions,
)
from app.db import get_connection as _connect

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

# #817: reviewer "awaiting my review" widget — number of drafts surfaced
# on the reviewer's Töölaud where this reviewer has not yet submitted a
# review outcome. Same shape as the other operational widgets so the
# dashboard stays dense-but-calm.
_MAX_AWAITING_REVIEW = 8

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

    "High/Critical" is decided by :func:`app.impact.scoring.impact_band`
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


def _get_awaiting_review_drafts(  # type: ignore[type-arg]
    user_id: str,
    org_id: str | None,
    *,
    limit: int = _MAX_AWAITING_REVIEW,
) -> list[dict]:
    """Return drafts in the user's org that this reviewer has not yet reviewed (#817).

    Powers the "Ülevaatuse järgi ootavad" widget on the reviewer Töölaud.
    A draft surfaces when:

    * It belongs to the caller's org (existing org-scoping rule).
    * Its status is ``ready`` (the analysis pipeline has finished — there
      is something for a reviewer to look at).
    * The caller has NOT submitted a ``draft_reviews`` row for it. A
      reviewer who has already posted any outcome (even
      "needs_discussion") is treated as having taken action; if they
      want to revise their conclusion they can re-open the draft and
      add a new review — the draft will stay off the queue meanwhile.

    Returns ``[{draft_id, title, created_at}]`` newest first. Empty
    list on any DB error so a dead DB never bricks the dashboard.
    """
    if not user_id or not org_id:
        return []
    try:
        with _connect() as conn:
            rows = conn.execute(
                """
                SELECT d.id, d.title, d.created_at
                FROM drafts d
                WHERE d.org_id = %s
                  AND d.status = 'ready'
                  AND NOT EXISTS (
                      SELECT 1 FROM draft_reviews r
                      WHERE r.draft_id = d.id
                        AND r.reviewer_id = %s
                  )
                ORDER BY d.created_at DESC
                LIMIT %s
                """,
                (org_id, user_id, limit),
            ).fetchall()
        return [
            {
                "draft_id": str(r[0]),
                "title": r[1] or "Pealkirjata eelnõu",
                "created_at": r[2],
            }
            for r in rows
        ]
    except Exception:
        logger.exception("Failed to fetch awaiting-review drafts for user %s", user_id)
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
