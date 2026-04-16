"""Authorization policy helpers (issue #575).

Before this module existed, every route in ``app/docs``, ``app/chat``,
``app/drafter``, and ``app/admin`` open-coded its own authz check —
typically ``if str(resource.org_id) != str(auth['org_id']): return 404``.
That pattern caused at least three shipped permission bugs (#568, #569):

* Any same-org drafter/reviewer could **delete** another user's draft,
  even though the NFR permission matrix specifies owner-only deletion.
* Any same-org colleague could read or mutate another user's private
  chat conversations and drafter sessions, despite the NFR flagging
  those resources as owner-only.
* A system admin acting on a resource in a different org was rejected,
  which is the inverse of the documented rule.

This module centralizes the decisions so new features can't re-introduce
the same bugs. Every helper returns a ``bool``; callers are expected to
translate ``False`` to the appropriate HTTP response (usually 404 to
avoid leaking the resource's existence).

See ``docs/nfr-baseline.md`` §5 (Authorization Matrix) for the
authoritative policy.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Protocol

# Sentinel role names — match the values written to users.role in the
# database (migration 001) and the JWT claims produced by
# app/auth/jwt_provider.py.
ROLE_SYSTEM_ADMIN = "admin"
ROLE_ORG_ADMIN = "org_admin"
ROLE_REVIEWER = "reviewer"
ROLE_DRAFTER = "drafter"


class _HasOwnerOrg(Protocol):
    """Duck-type for resources carrying both an owner (user_id / owner_id)
    and an org (org_id). Both chat Conversation and DrafterSession and
    DraftRow satisfy this by virtue of the schema."""

    # Python protocols don't enforce attribute names at runtime, but this
    # hint documents what we read so mypy / pyright can flag drift.


def _auth_id(auth: Mapping[str, Any] | None) -> str | None:
    if not auth:
        return None
    value = auth.get("id") or auth.get("user_id")
    return str(value) if value is not None else None


def _auth_org_id(auth: Mapping[str, Any] | None) -> str | None:
    if not auth:
        return None
    value = auth.get("org_id")
    return str(value) if value is not None else None


def _auth_role(auth: Mapping[str, Any] | None) -> str:
    if not auth:
        return ""
    return str(auth.get("role") or "")


def _resource_owner(resource: Any) -> str | None:
    """Return the owner id for a resource in a schema-tolerant way.

    Different models name the column differently: drafts use ``user_id``
    (the uploader), chat conversations use ``user_id``, drafter sessions
    use ``user_id``. Some older models exposed it as ``owner_id``. Try
    both so migrating a single field name doesn't require touching every
    call site.
    """
    for attr in ("user_id", "owner_id"):
        value = getattr(resource, attr, None)
        if value is not None:
            return str(value)
    return None


def _resource_org(resource: Any) -> str | None:
    value = getattr(resource, "org_id", None)
    return str(value) if value is not None else None


# ---------------------------------------------------------------------------
# Draft policy
# ---------------------------------------------------------------------------


def can_view_draft(auth: Mapping[str, Any] | None, draft: Any) -> bool:
    """Org-visible: any member of the draft's org, or a system admin."""
    if auth is None or draft is None:
        return False
    if _auth_role(auth) == ROLE_SYSTEM_ADMIN:
        return True
    return _auth_org_id(auth) == _resource_org(draft)


def can_delete_draft(auth: Mapping[str, Any] | None, draft: Any) -> bool:
    """Owner-only per NFR matrix, with a system-admin override.

    Composition: ``can_delete_draft`` implies ``can_view_draft``. This
    means a drafter in a different org who happens to know the draft id
    still can't delete it, because they can't even view it. System admin
    remains cross-org capable through the view helper's admin override.

    Org admin and reviewer **cannot** delete other people's drafts — the
    matrix is explicit about this.
    """
    if not can_view_draft(auth, draft):
        return False
    if _auth_role(auth) == ROLE_SYSTEM_ADMIN:
        return True
    return _auth_id(auth) == _resource_owner(draft)


def can_edit_draft(auth: Mapping[str, Any] | None, draft: Any) -> bool:
    """Owner-only mutation policy for draft-scoped edits.

    Used by endpoints that mutate a draft's own metadata (e.g. linking
    an eelnõu to its preceding VTK — #640) without removing the draft
    itself. The rule is identical to :func:`can_delete_draft`: the row
    owner or a system admin, and nobody else. Same-org reviewers and
    org admins can read a draft but must not re-link it on the owner's
    behalf — that's a governance action tied to the document's author.
    """
    return can_delete_draft(auth, draft)


# ---------------------------------------------------------------------------
# Chat conversation policy
# ---------------------------------------------------------------------------


def can_access_conversation(auth: Mapping[str, Any] | None, conversation: Any) -> bool:
    """Owner-only for read and write (NFR §5).

    System admin does NOT get a backdoor here — chat transcripts are
    explicitly flagged as owner-private in the NFR, matching the product
    decision that pre-publication drafting reasoning should not be
    visible to anyone except the author. If audit access is ever
    required, add it through an explicit, logged admin-audit route, not
    by softening this check.
    """
    if auth is None or conversation is None:
        return False
    return _auth_id(auth) == _resource_owner(conversation)


# ---------------------------------------------------------------------------
# Drafter session policy
# ---------------------------------------------------------------------------


def can_access_drafter_session(auth: Mapping[str, Any] | None, session: Any) -> bool:
    """Owner-only (NFR §5 chat/drafter row).

    Same reasoning as can_access_conversation — drafter sessions carry
    in-progress legislative drafting with political sensitivity, so the
    matrix locks this to the owner.
    """
    if auth is None or session is None:
        return False
    return _auth_id(auth) == _resource_owner(session)


# ---------------------------------------------------------------------------
# Admin convenience
# ---------------------------------------------------------------------------


def is_system_admin(auth: Mapping[str, Any] | None) -> bool:
    """Shorthand for the system-wide admin role."""
    return _auth_role(auth) == ROLE_SYSTEM_ADMIN


def is_org_admin(auth: Mapping[str, Any] | None) -> bool:
    """Shorthand for the per-org admin role."""
    return _auth_role(auth) == ROLE_ORG_ADMIN
