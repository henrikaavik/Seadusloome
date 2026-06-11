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


def _notification_exists(
    conn: Any,
    *,
    user_id: UUID | str,
    notif_type: str,
    metadata_key: str,
    metadata_value: str,
) -> bool:
    """Return ``True`` if a matching notification already exists for *user_id*.

    Matches on ``(user_id, type, metadata->>key == value)``. Used to
    dedupe one-shot per-resource notifications (e.g. a drafter session
    reaching "complete") so a repeated trigger — such as re-downloading
    an export — does not re-notify the owner. Querying existing rows
    avoids any new table or migration.

    Returns ``False`` on any DB error so a transient failure degrades to
    "send the notification" rather than silently swallowing it.
    """
    try:
        row = conn.execute(
            "SELECT 1 FROM notifications "
            "WHERE user_id = %s "
            "AND type = %s "
            "AND metadata->>%s = %s "
            "LIMIT 1",
            (str(user_id), notif_type, metadata_key, metadata_value),
        ).fetchone()
    except Exception:
        logger.debug(
            "notification-exists check failed for user=%s type=%s (treating as not-sent)",
            user_id,
            notif_type,
            exc_info=True,
        )
        return False
    return row is not None


def _sync_failure_within_window(conn: Any, minutes: int) -> bool:
    """Return ``True`` if a ``sync_failed`` notification fired in the last *minutes*.

    Used to throttle repeated ontology-sync-failure fan-outs (one alert
    per window across all admins). ``make_interval(mins => %s)`` is used
    instead of ``interval %s`` because the Postgres parser rejects a
    bound parameter immediately after the ``interval`` keyword.

    Returns ``False`` on any DB error so a transient failure degrades to
    "send the alert".
    """
    try:
        row = conn.execute(
            "SELECT 1 FROM notifications "
            "WHERE type = 'sync_failed' "
            "AND created_at >= now() - make_interval(mins => %s) "
            "LIMIT 1",
            (minutes,),
        ).fetchone()
    except Exception:
        logger.debug(
            "sync-failure throttle check failed (treating as not-sent)",
            exc_info=True,
        )
        return False
    return row is not None


def _annotation_target_link(annotation: Any) -> str:
    """Return a real GET page for an annotation target.

    Notifications used to link to ``/annotations/{id}`` but that route
    does not exist — the only routes under ``/api/annotations`` are
    POST/DELETE endpoints. Each annotation carries a ``target_type``
    and ``target_id`` pointing at the resource it's attached to, so the
    notification link should navigate to the target's detail page
    (where the user can see the annotation in context).

    Falls back to the notification inbox (``/notifications``) when the
    target type is unknown or fields are missing, which always renders
    a 200 GET page.
    """
    target_type = getattr(annotation, "target_type", None)
    target_id = getattr(annotation, "target_id", None)
    if not target_type or not target_id:
        return "/notifications"

    # Known target types map to existing GET pages. If a future target
    # type is added without a matching GET route, we still return a
    # safe fallback rather than a dead link.
    if target_type == "draft":
        return f"/drafts/{target_id}"
    if target_type == "conversation":
        return f"/chat/{target_id}"
    # Provisions and entities don't yet have a dedicated standalone
    # GET detail page; point the user at the inbox as the safest
    # existing 200-GET destination. (Follow-up: route for those.)
    return "/notifications"


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
            title="Uus vastus teie märgistusele",
            body=reply.content[:200] if reply.content else None,
            link=_annotation_target_link(annotation),
            metadata={
                "annotation_id": str(annotation.id),
                "reply_id": str(reply.id),
                "reply_user_id": str(reply.user_id),
                "target_type": getattr(annotation, "target_type", None),
                "target_id": getattr(annotation, "target_id", None),
            },
        )
    except Exception:
        logger.warning(
            "Failed to send annotation_reply notification for annotation=%s",
            getattr(annotation, "id", "?"),
            exc_info=True,
        )


def notify_annotation_mention(
    annotation: Any,
    mentioned_user_ids: list[UUID] | list[str],
    mentioner_user_id: UUID | str,
) -> None:
    """Notify every user mentioned in an annotation body (#176).

    The annotation must already have its ``mentions`` array resolved
    (see :func:`app.annotations.models.parse_mentions`). Self-mentions
    are silently skipped so an author writing ``@iseennast`` doesn't
    ping their own inbox.

    Args:
        annotation: The freshly-created ``Annotation`` row.
        mentioned_user_ids: The resolved in-org user UUIDs to notify.
        mentioner_user_id: The author of the message (excluded from fan-out).
    """
    try:
        mentioner_str = str(mentioner_user_id)
        body_preview = annotation.content[:200] if getattr(annotation, "content", None) else None
        link = _annotation_target_link(annotation)
        for target_id in mentioned_user_ids:
            if str(target_id) == mentioner_str:
                # Self-mention: nothing to notify.
                continue
            notify(
                user_id=target_id,
                type="annotation_mention",
                title="Sind mainiti märkuses",
                body=body_preview,
                link=link,
                metadata={
                    "annotation_id": str(getattr(annotation, "id", "")),
                    "mentioner_user_id": mentioner_str,
                    "target_type": getattr(annotation, "target_type", None),
                    "target_id": getattr(annotation, "target_id", None),
                },
            )
    except Exception:
        logger.warning(
            "Failed to send annotation_mention notifications for annotation=%s",
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
            title="Mõjuanalüüs valmis",
            body=f'Eelnõu "{draft.title}" mõjuanalüüs on valmis.',
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

    Deduped per session: step 7 is the export step, and every export
    *download* re-runs the completion path, so without a guard the owner
    gets a fresh "valmis" notification each time they re-download their
    own draft. We skip when an existing ``drafter_complete`` notification
    for the same session id already exists for this user (read or unread —
    a session reaches "complete" exactly once, so any prior alert means
    we already told them). Querying existing rows keeps this schema-free.

    Args:
        session: A ``DraftingSession`` dataclass (from ``app.drafter.session_model``).
    """
    try:
        session_id = getattr(session, "id", None)
        user_id = getattr(session, "user_id", None)
        if session_id is None or user_id is None:
            return

        from app.db import get_connection

        with get_connection() as conn:
            if _notification_exists(
                conn,
                user_id=user_id,
                notif_type="drafter_complete",
                metadata_key="session_id",
                metadata_value=str(session_id),
            ):
                return

        title_text = session.intent[:80] if session.intent else "Eelnõu"
        notify(
            user_id=user_id,
            type="drafter_complete",
            title="Eelnõu koostamine valmis",
            body=f'"{title_text}" on eksportimiseks valmis.',
            link=f"/drafter/{session_id}/step/7",
            metadata={
                "session_id": str(session_id),
            },
        )
    except Exception:
        logger.warning(
            "Failed to send drafter_complete notification for session=%s",
            getattr(session, "id", "?"),
            exc_info=True,
        )


# Throttle window for repeated ontology-sync-failure alerts. A flapping
# sync (e.g. an upstream outage retried every few minutes) must not bury
# every admin under a fresh fan-out per retry; one alert per window is
# enough to tell them "sync is broken, go look".
_SYNC_FAILED_THROTTLE_MINUTES = 30


def notify_sync_failed(error_message: str) -> None:
    """Notify all system admins when an ontology sync fails.

    Queries the ``users`` table for active admin-role users and sends
    each one a notification.

    Throttled to one ``sync_failed`` notification per
    ``_SYNC_FAILED_THROTTLE_MINUTES`` window (across all admins): a
    failing sync that retries on a short interval would otherwise fan out
    to every admin on every retry. If a ``sync_failed`` notification was
    created within the window we skip this one entirely. Querying
    existing rows keeps this schema-free.

    Args:
        error_message: The error description from the sync pipeline.
    """
    try:
        from app.db import get_connection

        with get_connection() as conn:
            if _sync_failure_within_window(conn, _SYNC_FAILED_THROTTLE_MINUTES):
                logger.info(
                    "Suppressing sync_failed notification — another fired within %d min",
                    _SYNC_FAILED_THROTTLE_MINUTES,
                )
                return

            rows = conn.execute(
                "SELECT id FROM users WHERE role = 'admin' AND is_active = TRUE"
            ).fetchall()

        for row in rows:
            admin_id = row[0]
            notify(
                user_id=admin_id,
                type="sync_failed",
                title="Ontoloogia sünkroonimine ebaõnnestus",
                body=error_message[:300] if error_message else None,
                # /admin/sync is POST-only; anchor on the sync card on
                # the admin dashboard, which is a real GET page.
                link="/admin#sync-card",
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
            title="Eeln\u00f5u vajab t\u00e4helepanu",
            body=(
                f'Eeln\u00f5u "{draft.title}" ei ole 90 p\u00e4eva kasutatud. '
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


def notify_draft_shared(draft: Any, uploader_id: UUID | str | None) -> None:
    """Notify same-org drafters and reviewers when a new draft is uploaded (#299).

    Pre-publication drafts are visible to every same-org ``drafter`` and
    ``reviewer`` (see ``app/auth/policy.py``), so when one team member
    uploads a draft the rest of the team should see it in their inbox.
    The acting uploader themselves is excluded — they obviously know
    about the draft they just uploaded.

    System admins (``role = 'admin'``) and org admins (``role =
    'org_admin'``) are intentionally NOT notified: this is a
    collaboration-team event, not an audit event. Inactive users
    (``is_active = FALSE``) are skipped — they have no inbox to read.

    Args:
        draft: A ``Draft`` dataclass (from ``app.docs.draft_model``)
            with ``id``, ``org_id``, ``user_id``, ``title`` and
            ``filename`` attributes.
        uploader_id: The id of the user who actually performed the
            upload. MUST be the acting caller's id, NOT
            ``draft.user_id`` — for a v2+ upload ``handle_upload``
            returns the PARENT draft's owner in ``draft.user_id``
            (the audit-trail owner), so falling back to that would
            both let the real uploader notify themselves and exclude
            the original drafter from the fan-out.
    """
    try:
        from app.db import get_connection

        org_id = getattr(draft, "org_id", None)
        if org_id is None or uploader_id is None:
            return

        with get_connection() as conn:
            rows = conn.execute(
                "SELECT id FROM users "
                "WHERE org_id = %s "
                "AND role IN ('drafter', 'reviewer') "
                "AND id != %s "
                "AND is_active = TRUE",
                (str(org_id), str(uploader_id)),
            ).fetchall()

        draft_id = getattr(draft, "id", None)
        title = getattr(draft, "title", None) or getattr(draft, "filename", "") or ""
        body = (
            f'Kolleeg laadis üles uue eelnõu: "{title}".'
            if title
            else "Kolleeg laadis üles uue eelnõu."
        )
        for row in rows:
            recipient_id = row[0]
            notify(
                user_id=recipient_id,
                type="draft_shared",
                title="Uus eelnõu jagatud",
                body=body,
                link=f"/drafts/{draft_id}",
                metadata={
                    "draft_id": str(draft_id),
                    "uploaded_by": str(uploader_id),
                    "org_id": str(org_id),
                },
            )
    except Exception:
        logger.warning(
            "Failed to send draft_shared notifications for draft=%s",
            getattr(draft, "id", "?"),
            exc_info=True,
        )


def _cost_alert_already_sent_today(conn: Any, org_id: UUID | str, notif_type: str) -> bool:
    """Return ``True`` if a *notif_type* alert for *org_id* exists from today.

    Cost alerts are noisy: ``check_org_cost_budget`` runs on every LLM
    entry point, so without a guard an org over 80% would get a fresh
    fan-out to every admin on every single chat/drafter turn. We dedupe
    to **one alert per org per calendar day per type** by checking for an
    existing notification of the same ``type`` whose ``metadata->>'org_id'``
    matches and whose ``created_at`` falls in the current day (server
    time, matching ``date_trunc('day', now())`` used by the budget sum).

    Querying existing rows keeps this schema-free — no new table or
    migration. The ``cost_alert`` (80%) and ``cost_exhausted`` (100%)
    types are deduped independently so crossing 100% still fires once
    even after an 80% alert went out earlier the same day.

    Returns ``False`` on any DB error so a transient failure degrades to
    "send the alert" rather than silently suppressing it.
    """
    try:
        row = conn.execute(
            "SELECT 1 FROM notifications "
            "WHERE type = %s "
            "AND metadata->>'org_id' = %s "
            "AND created_at >= date_trunc('day', now()) "
            "LIMIT 1",
            (notif_type, str(org_id)),
        ).fetchone()
    except Exception:
        logger.debug(
            "cost-alert dedupe check failed for org=%s type=%s (treating as not-sent)",
            org_id,
            notif_type,
            exc_info=True,
        )
        return False
    return row is not None


def notify_cost_alert(
    org_id: UUID | str,
    current_cost: float,
    budget: float,
) -> None:
    """Notify org admins when LLM cost hits 80% of the budget.

    Queries the ``users`` table for active org_admin/admin users in
    the given org and sends each one a notification.

    Deduped to one ``cost_alert`` per org per calendar day (see
    :func:`_cost_alert_already_sent_today`): the budget check runs on
    every LLM turn, so without this guard every admin's inbox would fill
    with one identical alert per message once the org crossed 80%.

    Args:
        org_id: The organisation UUID.
        current_cost: Current month's LLM cost in USD.
        budget: The monthly budget cap in USD.
    """
    try:
        from app.db import get_connection

        with get_connection() as conn:
            if _cost_alert_already_sent_today(conn, org_id, "cost_alert"):
                return

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


def notify_cost_exhausted(
    org_id: UUID | str,
    current_cost: float,
    budget: float,
) -> None:
    """Notify org admins once when the monthly LLM budget is fully spent.

    Fired by :func:`app.chat.rate_limiter.check_org_cost_budget` the
    moment the running monthly cost reaches or exceeds the cap (100%),
    distinct from the 80% advisory :func:`notify_cost_alert`. At 100% the
    org's LLM features stop working (the budget check raises), so admins
    need a louder, one-time heads-up to raise the cap or wait for the
    monthly reset.

    Deduped to one ``cost_exhausted`` per org per calendar day (see
    :func:`_cost_alert_already_sent_today`), independent of the 80%
    alert, so the budget check firing on every blocked turn does not spam
    admins.

    Args:
        org_id: The organisation UUID.
        current_cost: Current month's LLM cost in USD.
        budget: The monthly budget cap in USD.
    """
    try:
        from app.db import get_connection

        with get_connection() as conn:
            if _cost_alert_already_sent_today(conn, org_id, "cost_exhausted"):
                return

            rows = conn.execute(
                "SELECT id FROM users "
                "WHERE org_id = %s AND role IN ('org_admin', 'admin') "
                "AND is_active = TRUE",
                (str(org_id),),
            ).fetchall()

        pct = int((current_cost / budget) * 100) if budget > 0 else 100
        for row in rows:
            admin_id = row[0]
            notify(
                user_id=admin_id,
                type="cost_exhausted",
                title="LLM kuluhoiatus: kuueelarve on täis",
                body=(
                    f"Organisatsiooni igakuine LLM-i kulueelarve on täielikult "
                    f"kasutatud ({current_cost:.2f} USD / {budget:.2f} USD). "
                    "AI-funktsioonid on peatatud kuni eelarve suurendamiseni "
                    "või kuu vahetumiseni."
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
            "Failed to send cost_exhausted notifications for org=%s",
            org_id,
            exc_info=True,
        )
