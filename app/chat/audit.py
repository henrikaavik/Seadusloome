"""Chat-specific audit log helpers.

Thin wrappers around :func:`app.auth.audit.log_action` with structured
detail payloads. Every chat mutation -- conversation creation, message
sends, and conversation deletion -- is recorded in ``audit_log`` for
compliance and debugging.

All functions are fire-and-forget: failures are logged but never raised.
Same pattern as :mod:`app.drafter.audit`.
"""

from __future__ import annotations

import uuid

from app.auth.audit import log_action


def log_chat_conversation_create(
    user_id: str | uuid.UUID | None,
    conv_id: str | uuid.UUID,
    context_draft_id: str | uuid.UUID | None = None,
) -> None:
    """Record creation of a new chat conversation."""
    detail: dict[str, str | None] = {
        "conversation_id": str(conv_id),
    }
    if context_draft_id is not None:
        detail["context_draft_id"] = str(context_draft_id)
    log_action(
        str(user_id) if user_id else None,
        "chat.conversation.create",
        detail,
    )


def log_chat_message_send(
    user_id: str | uuid.UUID | None,
    conv_id: str | uuid.UUID,
    message_id: str | uuid.UUID,
) -> None:
    """Record a message send in a chat conversation."""
    log_action(
        str(user_id) if user_id else None,
        "chat.message.send",
        {
            "conversation_id": str(conv_id),
            "message_id": str(message_id),
        },
    )


def log_chat_conversation_delete(
    user_id: str | uuid.UUID | None,
    conv_id: str | uuid.UUID,
) -> None:
    """Record deletion of a chat conversation."""
    log_action(
        str(user_id) if user_id else None,
        "chat.conversation.delete",
        {
            "conversation_id": str(conv_id),
        },
    )
