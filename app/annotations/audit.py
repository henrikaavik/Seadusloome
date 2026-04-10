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
