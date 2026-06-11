"""90-day draft + drafting-session auto-archive warning scan (#572, #845).

Runs daily from the ASGI lifespan hook in :mod:`app.main` (a background
thread that sleeps 24h between scans). For every draft whose
``last_accessed_at`` column is older than ``threshold_days`` days AND
status is not ``'archived'``, emit a ``draft_archive_warning``
notification to the owner. A per-draft dedup window suppresses
duplicate warnings: if a warning was already written for the draft
within the last ``dedupe_window_days`` days, the scan skips it.

#845 (B4b): ``drafting_sessions`` rows carry the same class of
politically sensitive content (encrypted draft clauses + legislative
intent) but were excluded from the retention lifecycle. The same daily
tick therefore also scans **active** drafting sessions whose
``updated_at`` is older than the threshold and emits a
``drafting_session_archive_warning`` to the owner, with the identical
NOT-EXISTS dedup window over ``notifications`` (migration 038 extends
the ``notifications.type`` CHECK and indexes the scan). Completed and
abandoned sessions are terminal states and are not nagged about.

Scheduler approach: lifespan thread. Coolify has no native cron, and a
pg_cron dependency would add operational weight for a task that runs
once a day. The thread is cancelled cleanly on shutdown through the
same ``threading.Event`` the job worker uses.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Any

from app.db import get_connection
from app.docs.draft_model import Draft, _row_to_draft
from app.drafter.session_model import (
    _SESSION_COLUMNS,
    DraftingSession,
    _row_to_session,
)
from app.notifications.notify import notify
from app.notifications.wire import notify_draft_archive_warning

logger = logging.getLogger(__name__)


_SCAN_SQL = """
SELECT
    id, user_id, org_id, title, filename, content_type, file_size,
    storage_path, graph_uri, status, parsed_text_encrypted, entity_count,
    error_message, created_at, updated_at, last_accessed_at
FROM drafts
WHERE last_accessed_at < now() - make_interval(days => %s)
  AND status != 'archived'
  AND NOT EXISTS (
      SELECT 1
      FROM notifications n
      WHERE n.type = 'draft_archive_warning'
        AND n.metadata->>'draft_id' = drafts.id::text
        AND n.created_at > now() - make_interval(days => %s)
  )
ORDER BY last_accessed_at ASC
"""


def scan_stale_drafts(
    threshold_days: int = 90,
    dedupe_window_days: int = 7,
) -> list[dict[str, Any]]:
    """Find stale drafts and emit ``draft_archive_warning`` notifications.

    Args:
        threshold_days: A draft is stale when ``last_accessed_at`` is
            older than this many days. Default 90 matches the NFR.
        dedupe_window_days: Suppress a warning if one was already written
            for the same draft within this many days. Default 7 gives
            owners a full week to react before we nag again.

    Returns:
        A list of ``{"draft_id": str, "user_id": str, "org_id": str,
        "title": str, "last_accessed_at": datetime}`` dicts describing
        every draft that received a warning. Callers (tests, admin
        dashboards) can use the return value without re-querying.
    """
    notified: list[dict[str, Any]] = []
    try:
        with get_connection() as conn:
            rows = conn.execute(
                _SCAN_SQL,
                (int(threshold_days), int(dedupe_window_days)),
            ).fetchall()
    except Exception:
        logger.exception("scan_stale_drafts: failed to query stale drafts")
        return notified

    for row in rows:
        try:
            draft: Draft = _row_to_draft(row)
        except Exception:
            logger.exception("scan_stale_drafts: failed to parse draft row")
            continue

        notify_draft_archive_warning(draft)
        notified.append(
            {
                "draft_id": str(draft.id),
                "user_id": str(draft.user_id),
                "org_id": str(draft.org_id),
                "title": draft.title,
                "last_accessed_at": draft.last_accessed_at,
            }
        )

    if notified:
        logger.info(
            "scan_stale_drafts: emitted %d draft_archive_warning notifications",
            len(notified),
        )
    return notified


# ---------------------------------------------------------------------------
# Stale drafting-session scan (#845 B4b)
# ---------------------------------------------------------------------------


_SESSION_SCAN_SQL = f"""
SELECT {_SESSION_COLUMNS}
FROM drafting_sessions
WHERE updated_at < now() - make_interval(days => %s)
  AND status = 'active'
  AND NOT EXISTS (
      SELECT 1
      FROM notifications n
      WHERE n.type = 'drafting_session_archive_warning'
        AND n.metadata->>'session_id' = drafting_sessions.id::text
        AND n.created_at > now() - make_interval(days => %s)
  )
ORDER BY updated_at ASC
"""


def _notify_drafting_session_archive_warning(session: DraftingSession) -> None:
    """Emit the 90-day retention warning for a stale drafting session.

    Lives here (not in :mod:`app.notifications.wire`) next to the scan
    that is its only producer. ``notify`` itself swallows DB errors;
    the extra try/except keeps an unexpected attribute problem on one
    row from aborting the rest of the scan loop.
    """
    try:
        label = (session.intent or "").strip()[:80] or "Eelnõu"
        notify(
            user_id=session.user_id,
            type="drafting_session_archive_warning",
            title="Koostamise seanss vajab tähelepanu",
            body=(
                f'Eelnõu koostamise seanssi "{label}" ei ole 90 päeva kasutatud. '
                "Palun jätkake koostamist või kustutage seanss, kui seda enam ei vajata."
            ),
            link=f"/drafter/{session.id}",
            metadata={
                "session_id": str(session.id),
                "workflow_type": session.workflow_type,
                "updated_at": (
                    session.updated_at.isoformat() if session.updated_at is not None else None
                ),
            },
        )
    except Exception:
        logger.warning(
            "Failed to send drafting_session_archive_warning for session=%s",
            getattr(session, "id", "?"),
            exc_info=True,
        )


def scan_stale_drafting_sessions(
    threshold_days: int = 90,
    dedupe_window_days: int = 7,
) -> list[dict[str, Any]]:
    """Find stale *active* drafting sessions and warn their owners (#845 B4b).

    Mirrors :func:`scan_stale_drafts` for the ``drafting_sessions``
    table: sessions hold encrypted draft content + legislative intent,
    so they need the same 90-day "keep working or clean up" checkpoint
    as uploaded drafts. Staleness is measured on ``updated_at`` (bumped
    by every wizard step / edit); only ``status = 'active'`` rows are
    scanned because ``completed`` / ``abandoned`` are terminal.

    Args:
        threshold_days: A session is stale when ``updated_at`` is older
            than this many days. Default 90 matches the drafts NFR.
        dedupe_window_days: Suppress a warning if one was already
            written for the same session within this many days.

    Returns:
        A list of ``{"session_id": str, "user_id": str, "org_id": str,
        "workflow_type": str, "updated_at": datetime}`` dicts, one per
        warned session.
    """
    notified: list[dict[str, Any]] = []
    try:
        with get_connection() as conn:
            rows = conn.execute(
                _SESSION_SCAN_SQL,
                (int(threshold_days), int(dedupe_window_days)),
            ).fetchall()
    except Exception:
        logger.exception("scan_stale_drafting_sessions: failed to query stale sessions")
        return notified

    for row in rows:
        try:
            session: DraftingSession = _row_to_session(row)
        except Exception:
            logger.exception("scan_stale_drafting_sessions: failed to parse session row")
            continue

        _notify_drafting_session_archive_warning(session)
        notified.append(
            {
                "session_id": str(session.id),
                "user_id": str(session.user_id),
                "org_id": str(session.org_id),
                "workflow_type": session.workflow_type,
                "updated_at": session.updated_at,
            }
        )

    if notified:
        logger.info(
            "scan_stale_drafting_sessions: emitted %d drafting_session_archive_warning "
            "notifications",
            len(notified),
        )
    return notified


# ---------------------------------------------------------------------------
# Lifespan scheduler
# ---------------------------------------------------------------------------

_DEFAULT_INTERVAL_SECONDS = 24 * 60 * 60  # 24 hours

# Startup grace before the first scan — avoids racing the rest of the
# lifespan hooks (DB pools, worker thread) during container startup.
# Module-level so tests can patch it to 0 instead of sleeping 5s.
_INITIAL_DELAY_SECONDS = 5.0


def _scheduler_loop(stop_event: threading.Event, interval_seconds: int) -> None:
    """Run both stale scans forever until ``stop_event`` is set.

    Runs one scan immediately on startup, then sleeps ``interval_seconds``
    between iterations. ``stop_event.wait(timeout)`` is used instead of
    ``time.sleep`` so the shutdown path cancels the wait promptly
    rather than blocking the ASGI lifespan for up to 24h on deploy.

    Each scan is wrapped in its own ``try`` so a failure in the drafts
    sweep cannot starve the drafting-sessions sweep (#845 B4b) and
    vice versa.
    """
    logger.info(
        "Draft archive-warning scheduler started (interval=%ds)",
        interval_seconds,
    )
    # A tiny initial delay avoids racing the rest of the lifespan
    # hooks (DB pools, worker thread) during container startup.
    if stop_event.wait(timeout=_INITIAL_DELAY_SECONDS):
        return
    while not stop_event.is_set():
        start = time.monotonic()
        try:
            scan_stale_drafts()
        except Exception:
            logger.exception("Draft archive-warning scan raised; continuing")
        try:
            scan_stale_drafting_sessions()
        except Exception:
            logger.exception("Drafting-session archive-warning scan raised; continuing")
        elapsed = time.monotonic() - start
        remaining = max(1.0, interval_seconds - elapsed)
        if stop_event.wait(timeout=remaining):
            break
    logger.info("Draft archive-warning scheduler stopped")


def start_archive_warning_scheduler(
    stop_event: threading.Event,
    interval_seconds: int = _DEFAULT_INTERVAL_SECONDS,
) -> threading.Thread:
    """Start the archive-warning scheduler as a daemon thread.

    The thread is marked daemon so it never blocks interpreter exit if
    the ASGI lifespan hook fails to signal shutdown cleanly. Callers
    should still set ``stop_event`` on shutdown so the final
    ``scan_stale_drafts`` completes cleanly.
    """
    thread = threading.Thread(
        target=_scheduler_loop,
        args=(stop_event, interval_seconds),
        name="archive-warning-scheduler",
        daemon=True,
    )
    thread.start()
    return thread
