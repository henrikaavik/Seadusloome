"""Drafter-specific audit log helpers.

Thin wrappers around :func:`app.auth.audit.log_action` with structured
detail payloads. Every drafter mutation — session creation, step
advances, clause edits, regenerations, and exports — is recorded in
``audit_log`` for compliance and debugging.

All functions are fire-and-forget: failures are logged but never raised.
"""

from __future__ import annotations

import uuid

from app.auth.audit import log_action


def log_drafter_session_create(
    user_id: str | uuid.UUID | None,
    session_id: str | uuid.UUID,
    workflow_type: str,
) -> None:
    """Record creation of a new drafting session."""
    log_action(
        str(user_id) if user_id else None,
        "drafter.session.create",
        {
            "session_id": str(session_id),
            "workflow_type": workflow_type,
        },
    )


def log_drafter_step_advance(
    user_id: str | uuid.UUID | None,
    session_id: str | uuid.UUID,
    from_step: int,
    to_step: int,
) -> None:
    """Record a step transition in the drafter wizard."""
    log_action(
        str(user_id) if user_id else None,
        "drafter.step.advance",
        {
            "session_id": str(session_id),
            "from_step": from_step,
            "to_step": to_step,
        },
    )


def log_drafter_clause_edit(
    user_id: str | uuid.UUID | None,
    session_id: str | uuid.UUID,
    section_ref: str,
) -> None:
    """Record a manual clause edit by the user."""
    log_action(
        str(user_id) if user_id else None,
        "drafter.clause.edit",
        {
            "session_id": str(session_id),
            "section_ref": section_ref,
        },
    )


def log_drafter_regenerate(
    user_id: str | uuid.UUID | None,
    session_id: str | uuid.UUID,
    section_ref: str,
) -> None:
    """Record a clause regeneration request."""
    log_action(
        str(user_id) if user_id else None,
        "drafter.clause.regenerate",
        {
            "session_id": str(session_id),
            "section_ref": section_ref,
        },
    )


def log_drafter_export(
    user_id: str | uuid.UUID | None,
    session_id: str | uuid.UUID,
) -> None:
    """Record a .docx export action."""
    log_action(
        str(user_id) if user_id else None,
        "drafter.export",
        {
            "session_id": str(session_id),
        },
    )
