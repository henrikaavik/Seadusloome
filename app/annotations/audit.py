"""Annotation-specific audit log helpers.

Thin wrappers around :func:`app.auth.audit.log_action` with structured
detail payloads. Every annotation mutation -- creation, reply, resolve,
and deletion -- is recorded in ``audit_log`` for compliance and debugging.

All functions are fire-and-forget: failures are logged but never raised.
Same pattern as :mod:`app.chat.audit`.
"""

from __future__ import annotations

import uuid

from app.auth.audit import log_action


def log_annotation_create(
    user_id: str | uuid.UUID | None,
    annotation_id: str | uuid.UUID,
    target_type: str,
    target_id: str,
) -> None:
    """Record creation of a new annotation."""
    log_action(
        str(user_id) if user_id else None,
        "annotation.create",
        {
            "annotation_id": str(annotation_id),
            "target_type": target_type,
            "target_id": target_id,
        },
    )


def log_annotation_reply(
    user_id: str | uuid.UUID | None,
    annotation_id: str | uuid.UUID,
    reply_id: str | uuid.UUID,
) -> None:
    """Record a reply added to an annotation."""
    log_action(
        str(user_id) if user_id else None,
        "annotation.reply",
        {
            "annotation_id": str(annotation_id),
            "reply_id": str(reply_id),
        },
    )


def log_annotation_resolve(
    user_id: str | uuid.UUID | None,
    annotation_id: str | uuid.UUID,
) -> None:
    """Record resolution of an annotation."""
    log_action(
        str(user_id) if user_id else None,
        "annotation.resolve",
        {
            "annotation_id": str(annotation_id),
        },
    )


def log_annotation_delete(
    user_id: str | uuid.UUID | None,
    annotation_id: str | uuid.UUID,
) -> None:
    """Record deletion of an annotation."""
    log_action(
        str(user_id) if user_id else None,
        "annotation.delete",
        {
            "annotation_id": str(annotation_id),
        },
    )


# ---------------------------------------------------------------------------
# §9.4 row-annotation audit helpers (PR-B)
# ---------------------------------------------------------------------------
#
# Distinct action labels for row-scoped operations so the auditor can tell at
# a glance whether an event came from the legacy Phase 4 routes or the new
# version-scoped surface. The action label is the only structured field we
# use for log queries; the detail payload is for human inspection.


def log_row_annotation_create(
    user_id: str | uuid.UUID | None,
    annotation_id: str | uuid.UUID,
    draft_version_id: str | uuid.UUID,
    row_kind: str,
    row_key: str,
) -> None:
    """Record creation of the first message in a row-annotation thread."""
    log_action(
        str(user_id) if user_id else None,
        "annotation.row.create",
        {
            "annotation_id": str(annotation_id),
            "draft_version_id": str(draft_version_id),
            "row_kind": row_kind,
            "row_key": row_key,
        },
    )


def log_row_annotation_message(
    user_id: str | uuid.UUID | None,
    annotation_id: str | uuid.UUID,
    draft_version_id: str | uuid.UUID,
    row_kind: str,
    row_key: str,
) -> None:
    """Record a follow-up message appended to a row-annotation thread."""
    log_action(
        str(user_id) if user_id else None,
        "annotation.row.message.create",
        {
            "annotation_id": str(annotation_id),
            "draft_version_id": str(draft_version_id),
            "row_kind": row_kind,
            "row_key": row_key,
        },
    )


def log_row_annotation_resolve(
    user_id: str | uuid.UUID | None,
    draft_version_id: str | uuid.UUID,
    row_kind: str,
    row_key: str,
) -> None:
    """Record resolution of a row-annotation thread."""
    log_action(
        str(user_id) if user_id else None,
        "annotation.row.resolve",
        {
            "draft_version_id": str(draft_version_id),
            "row_kind": row_kind,
            "row_key": row_key,
        },
    )


def log_row_annotation_reopen(
    user_id: str | uuid.UUID | None,
    draft_version_id: str | uuid.UUID,
    row_kind: str,
    row_key: str,
) -> None:
    """Record reopening of a previously resolved row-annotation thread."""
    log_action(
        str(user_id) if user_id else None,
        "annotation.row.reopen",
        {
            "draft_version_id": str(draft_version_id),
            "row_kind": row_kind,
            "row_key": row_key,
        },
    )
