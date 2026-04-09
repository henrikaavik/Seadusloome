"""Thin audit-log wrapper for draft-related actions.

Every draft mutation (upload, delete, view) should be recorded so a
later reviewer can trace who touched which pre-publication draft. This
module is a trivial adapter around :func:`app.auth.audit.log_action` so
handlers can call a domain-named function (``log_draft_upload``) instead
of re-specifying the action string every time.
"""

from __future__ import annotations

from typing import Any

from app.auth.audit import log_action


def log_draft_upload(user_id: str | None, draft_id: Any, **extra: Any) -> None:
    """Record a ``draft.upload`` event.

    Extra keyword arguments are folded into the audit ``detail`` blob
    so callers can attach filename / content-type without editing the
    wrapper.
    """
    detail: dict[str, Any] = {"draft_id": str(draft_id), **extra}
    log_action(user_id, "draft.upload", detail)


def log_draft_delete(user_id: str | None, draft_id: Any, **extra: Any) -> None:
    """Record a ``draft.delete`` event."""
    detail: dict[str, Any] = {"draft_id": str(draft_id), **extra}
    log_action(user_id, "draft.delete", detail)


def log_draft_view(user_id: str | None, draft_id: Any, **extra: Any) -> None:
    """Record a ``draft.view`` event.

    Intentionally invoked from :func:`draft_detail_page` rather than from
    any listing endpoint — the listing view is a *list* of metadata and
    does not count as viewing a single draft's contents.
    """
    detail: dict[str, Any] = {"draft_id": str(draft_id), **extra}
    log_action(user_id, "draft.view", detail)
