"""Wire-up functions that call ``notify()`` from domain events.

Each function in this module corresponds to a specific domain event
(annotation reply, analysis completion, etc.) and is responsible for
looking up the appropriate user(s) to notify and calling
:func:`app.notifications.notify.notify`.

All functions are fire-and-forget: they swallow exceptions so that a
notification failure never disrupts the primary workflow.
"""

from __future__ import annotations

import logging
from typing import Any
from uuid import UUID

from app.notifications.notify import notify

logger = logging.getLogger(__name__)


def notify_annotation_reply(annotation: Any, reply: Any) -> None:
    """Notify the annotation author when someone replies to their annotation.

    Args:
        annotation: An ``Annotation`` dataclass (from ``app.annotations.models``).
        reply: An ``AnnotationReply`` dataclass.
    """
    try:
        # Don't notify if the reply author is the annotation author
        if str(annotation.user_id) == str(reply.user_id):
            return

        notify(
            user_id=annotation.user_id,
            type="annotation_reply",
            title="Uus vastus teie margistusele",
            body=reply.content[:200] if reply.content else None,
            link=f"/annotations/{annotation.id}",
            metadata={
                "annotation_id": str(annotation.id),
                "reply_id": str(reply.id),
                "reply_user_id": str(reply.user_id),
            },
        )
    except Exception:
        logger.warning(
            "Failed to send annotation_reply notification for annotation=%s",
            getattr(annotation, "id", "?"),
            exc_info=True,
        )


def notify_analysis_done(draft: Any) -> None:
    """Notify the draft owner when impact analysis completes.

    Args:
        draft: A ``Draft`` dataclass (from ``app.docs.draft_model``).
    """
    try:
        notify(
            user_id=draft.user_id,
            type="analysis_done",
            title="Moju analuus valmis",
            body=f'Eelnou "{draft.title}" moju analuus on valmis.',
            link=f"/drafts/{draft.id}/report",
            metadata={
                "draft_id": str(draft.id),
            },
        )
    except Exception:
        logger.warning(
            "Failed to send analysis_done notification for draft=%s",
            getattr(draft, "id", "?"),
            exc_info=True,
        )


def notify_drafter_complete(session: Any) -> None:
    """Notify the session owner when a drafter session reaches step 7 (export).

    Args:
        session: A ``DraftingSession`` dataclass (from ``app.drafter.session_model``).
    """
    try:
        title_text = session.intent[:80] if session.intent else "Eelnou"
        notify(
            user_id=session.user_id,
            type="drafter_complete",
            title="Eelnou koostamine valmis",
            body=f'"{title_text}" on eksportimiseks valmis.',
            link=f"/drafter/{session.id}/step/7",
            metadata={
                "session_id": str(session.id),
            },
        )
    except Exception:
        logger.warning(
            "Failed to send drafter_complete notification for session=%s",
            getattr(session, "id", "?"),
            exc_info=True,
        )


def notify_sync_failed(error_message: str) -> None:
    """Notify all system admins when an ontology sync fails.

    Queries the ``users`` table for active admin-role users and sends
    each one a notification.

    Args:
        error_message: The error description from the sync pipeline.
    """
    try:
        from app.db import get_connection

        with get_connection() as conn:
            rows = conn.execute(
                "SELECT id FROM users WHERE role = 'admin' AND is_active = TRUE"
            ).fetchall()

        for row in rows:
            admin_id = row[0]
            notify(
                user_id=admin_id,
                type="sync_failed",
                title="Ontoloogia sunkroonimine ebaonnestus",
                body=error_message[:300] if error_message else None,
                link="/admin/sync",
                metadata={"error": error_message[:500] if error_message else None},
            )
    except Exception:
        logger.warning(
            "Failed to send sync_failed notifications",
            exc_info=True,
        )


def notify_draft_archive_warning(draft: Any) -> None:
    """Notify a draft owner when their draft has been stale for 90 days (#572).

    Pre-publication drafts must not persist indefinitely without an
    explicit "keep or delete" checkpoint. This factory is invoked by
    :func:`app.jobs.archive_warning.scan_stale_drafts` for every draft
    whose ``last_accessed_at`` is older than 90 days. The notification
    carries a deep-link back to the draft detail page, where the owner
    can either click "Hoia alles" to reset the clock or delete the
    draft outright.

    Args:
        draft: A ``Draft`` dataclass (from ``app.docs.draft_model``).
    """
    try:
        last_accessed_iso = (
            draft.last_accessed_at.isoformat()
            if getattr(draft, "last_accessed_at", None) is not None
            else None
        )
        notify(
            user_id=draft.user_id,
            type="draft_archive_warning",
            title="Eelnou vajab tahelepanu",
            body=(
                f'Eeln\u00f5u "{draft.title}" ei ole 90 paeva kasutatud. '
                "Palun kinnitage, et soovite seda alles hoida, v\u00f5i kustutage see."
            ),
            link=f"/drafts/{draft.id}",
            metadata={
                "draft_id": str(draft.id),
                "title": draft.title,
                "last_accessed_at": last_accessed_iso,
            },
        )
    except Exception:
        logger.warning(
            "Failed to send draft_archive_warning notification for draft=%s",
            getattr(draft, "id", "?"),
            exc_info=True,
        )


def notify_cost_alert(
    org_id: UUID | str,
    current_cost: float,
    budget: float,
) -> None:
    """Notify org admins when LLM cost hits 80% of the budget.

    Queries the ``users`` table for active org_admin/admin users in
    the given org and sends each one a notification.

    Args:
        org_id: The organisation UUID.
        current_cost: Current month's LLM cost in USD.
        budget: The monthly budget cap in USD.
    """
    try:
        from app.db import get_connection

        with get_connection() as conn:
            rows = conn.execute(
                "SELECT id FROM users "
                "WHERE org_id = %s AND role IN ('org_admin', 'admin') "
                "AND is_active = TRUE",
                (str(org_id),),
            ).fetchall()

        pct = int((current_cost / budget) * 100) if budget > 0 else 0
        for row in rows:
            admin_id = row[0]
            notify(
                user_id=admin_id,
                type="cost_alert",
                title=f"LLM kuluhoiatus: {pct}% eelarvest kasutatud",
                body=(
                    f"Organisatsiooni igakuine LLM-i kulu on "
                    f"{current_cost:.2f} USD / {budget:.2f} USD ({pct}%)."
                ),
                link="/admin/costs",
                metadata={
                    "org_id": str(org_id),
                    "current_cost": current_cost,
                    "budget": budget,
                    "pct": pct,
                },
            )
    except Exception:
        logger.warning(
            "Failed to send cost_alert notifications for org=%s",
            org_id,
            exc_info=True,
        )
