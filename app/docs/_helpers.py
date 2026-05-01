"""Small shared helpers for the drafts module (#671).

Extracted out of :mod:`app.docs.routes` so that sibling modules
(notably :mod:`app.docs.retry_handler`) can import them without
creating an import cycle at module-load time.

Keep this file **dependency-light**: anything imported here must not
depend on :mod:`app.docs.routes` itself, or the cycle returns. The
heavy UI imports used by :func:`_not_found_page` are deferred inside
the function body so this charter is preserved (post-review fix).
"""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING, Any, Literal, NamedTuple

from starlette.requests import Request
from starlette.responses import Response

from app.auth.helpers import require_auth
from app.auth.policy import can_delete_draft, can_edit_draft, can_view_draft
from app.auth.provider import UserDict
from app.docs import draft_model as _dm
from app.docs.draft_model import Draft

if TYPE_CHECKING:
    from fastcore.xml import FT


# Set of valid actions for :func:`resolve_draft`. Using ``Literal`` so a
# typo at the call site is a pyright error rather than a silent fallback
# to the more permissive ``view`` gate.
DraftAction = Literal["view", "edit", "delete"]


class ResolvedDraft(NamedTuple):
    """Result of a successful :func:`resolve_draft` call.

    A typed ``NamedTuple`` (rather than a bare ``tuple[Draft, dict]``) so
    call sites can discriminate the success branch from the failure
    branch via ``isinstance(result, ResolvedDraft)``. The failure branch
    returns an opaque response object (Starlette ``Response`` for auth
    redirects, FastHTML FT element for the 404 page) and the typed
    discriminator stays unambiguous regardless of what the failure side
    returns. Future-proofs against fastcore versions where FT may
    inherit from ``tuple``, and against ad-hoc ``(error, code)`` tuples
    returned by any upstream helper.

    Supports both attribute access (``resolved.draft``) and tuple
    unpacking (``draft, auth = resolved``) — see the test in
    ``tests/test_docs_helpers.py`` that locks in this dual API.
    """

    draft: Draft
    # ``UserDict`` is a TypedDict; widen so call sites can also pass a
    # plain ``dict`` (test fixtures, audit-log reconstructions). The
    # extra ``Any`` is dropped (post-review fix) because it collapsed
    # the union to ``Any`` and erased downstream type info.
    auth: UserDict | dict[str, Any]


def _parse_uuid(raw: str) -> uuid.UUID | None:
    """Return a ``UUID`` parsed from *raw*, or ``None`` if invalid."""
    try:
        return uuid.UUID(raw)
    except (ValueError, TypeError):
        return None


def _not_found_page(req: Request) -> Any:
    """Render the 404 page used whenever a draft is missing or out of scope.

    UI-layer imports are deferred to the function body so the module
    stays dependency-light at import time (preserves the charter that
    lets :mod:`app.docs.retry_handler` and other lean importers load
    without dragging the entire ``app.ui`` subtree).

    Return is typed ``Any`` because :func:`PageShell` returns an FT
    element whose concrete fastcore type is exposed as a 3-tuple,
    which conflicts with the narrower ``FT`` annotation on
    :func:`resolve_draft`'s return. Pragmatically the value is opaque
    to callers — they just return it from their handler.
    """
    from fasthtml.common import H1, A, P

    from app.ui.layout import PageShell
    from app.ui.surfaces.alert import Alert
    from app.ui.theme import get_theme_from_request

    auth = req.scope.get("auth")
    theme = get_theme_from_request(req)
    return PageShell(
        H1("Eelnõu ei leitud", cls="page-title"),
        Alert(
            "Otsitud eelnõu ei ole olemas või Te ei oma selle vaatamise õigust.",
            variant="warning",
        ),
        P(A("← Tagasi eelnõude nimekirja", href="/drafts"), cls="back-link"),
        title="Eelnõu ei leitud",
        user=auth,
        theme=theme,
        active_nav="/drafts",
        request=req,
    )


def resolve_draft(
    req: Request,
    draft_id: str,
    *,
    action: DraftAction = "view",
) -> ResolvedDraft | Response | FT:
    """Run the standard auth + parse + load + authorize preamble for a draft route.

    The drafts module has ~15 handlers that all open with the same five
    steps:

    1. Auth check (redirect to /auth/login on miss).
    2. Parse the URL ``draft_id`` into a UUID.
    3. Load the draft from the DB.
    4. Authorize the caller against the draft (view / edit / delete) —
       return 404 (not 403) so we never leak existence of out-of-scope
       drafts.

    This helper consolidates that preamble (#624). On success it returns
    a :class:`ResolvedDraft` named tuple. On any failure it returns the
    appropriate response object — either a Starlette ``Response`` (auth
    redirect) or a FastHTML FT element from :func:`_not_found_page`.

    Discriminate via ``isinstance(result, ResolvedDraft)`` rather than
    ``isinstance(result, tuple)`` — see :class:`ResolvedDraft` for the
    rationale (typed discriminator that does not depend on whether the
    failure-branch return type happens to subclass ``tuple``)::

        resolved = resolve_draft(req, draft_id)
        if not isinstance(resolved, ResolvedDraft):
            return resolved
        draft, auth = resolved.draft, resolved.auth

    Args:
        req: The Starlette request.
        draft_id: The URL-path UUID string for the draft.
        action: Authorization gate. ``"view"`` (default) gates on
            :func:`can_view_draft`; ``"edit"`` gates on
            :func:`can_edit_draft` for handlers that mutate state;
            ``"delete"`` gates on :func:`can_delete_draft`. Using
            :data:`DraftAction` (``Literal``) so a typo at the call site
            is a pyright error rather than a silent fallback to the more
            permissive view gate.

    Returns:
        Either a :class:`ResolvedDraft` if authorised, or an opaque
        response object to return verbatim.
    """
    auth_or_redirect = require_auth(req)
    if isinstance(auth_or_redirect, Response):
        return auth_or_redirect
    auth = auth_or_redirect

    parsed = _parse_uuid(draft_id)
    if parsed is None:
        return _not_found_page(req)

    # Look up via the module rather than a direct import so test patches
    # on ``app.docs.draft_model.fetch_draft`` actually intercept the call.
    draft = _dm.fetch_draft(parsed)
    if draft is None:
        return _not_found_page(req)

    if action == "edit":
        gate = can_edit_draft
    elif action == "delete":
        gate = can_delete_draft
    else:  # "view" — Literal narrowing means this is the only remaining option
        gate = can_view_draft

    if not gate(auth, draft):
        # 404 (not 403) so cross-org probing can't enumerate draft ids.
        return _not_found_page(req)

    return ResolvedDraft(draft=draft, auth=auth)


def audit_draft_access(
    auth: UserDict | dict[str, Any],
    draft: Draft,
    action: str,
) -> None:
    """Log + touch-access for a draft access event.

    Companion to :func:`resolve_draft` (#624 follow-up). Callers used to
    pair these two operations by hand:

        log_action(auth.get("id"), f"draft.{action}", {...})
        touch_draft_access_conn(draft.id)

    This helper consolidates them so the migration to ``resolve_draft``
    doesn't leave 6+ copy-pasted audit blocks behind. The ``action``
    string becomes the audit-log discriminator (``"draft.view"``,
    ``"draft.edit"``, ``"draft.delete"``, etc.).

    Failures are non-fatal: a failed audit must not block the request,
    so every step is wrapped in its own try/except and silenced.
    """
    # Lazy imports to keep the module dependency-light at import time.
    from app.auth.audit import log_action

    user_id = auth.get("id") if isinstance(auth, dict) else None
    try:
        log_action(
            user_id,
            f"draft.{action}",
            {"draft_id": str(draft.id)},
        )
    except Exception:
        # Audit-log infrastructure is best-effort; never block on it.
        pass

    # #572: surface-to-user counts as access; reset the archive clock.
    try:
        _dm.touch_draft_access_conn(draft.id)
    except Exception:
        pass
