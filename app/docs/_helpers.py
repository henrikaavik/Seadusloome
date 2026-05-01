"""Small shared helpers for the drafts module (#671).

Extracted out of :mod:`app.docs.routes` so that sibling modules
(notably :mod:`app.docs.retry_handler`) can import them without
creating an import cycle at module-load time.

Keep this file **dependency-light**: anything imported here must not
depend on :mod:`app.docs.routes` itself, or the cycle returns.
"""

from __future__ import annotations

import uuid
from typing import Any, NamedTuple

from fasthtml.common import H1, A, P  # noqa: F401
from starlette.requests import Request
from starlette.responses import Response

from app.auth.helpers import require_auth
from app.auth.policy import can_edit_draft, can_view_draft
from app.auth.provider import UserDict
from app.docs import draft_model as _dm
from app.docs.draft_model import Draft
from app.ui.layout import PageShell
from app.ui.surfaces.alert import Alert
from app.ui.theme import get_theme_from_request


class ResolvedDraft(NamedTuple):
    """Result of a successful :func:`resolve_draft` call.

    A real subclass (not a bare ``tuple[Draft, dict]``) so call sites can
    discriminate via ``isinstance(result, ResolvedDraft)`` — important
    because FastHTML FT elements (e.g. the 404 page returned on
    auth/load/authz failure) are themselves plain tuples and would
    otherwise match a tuple-based discriminator.
    """

    draft: Draft
    # ``UserDict`` is a TypedDict; widen to ``Any`` so call sites can
    # also pass a plain ``dict`` (test fixtures, audit-log reconstructions).
    auth: UserDict | dict[str, Any] | Any


def _parse_uuid(raw: str) -> uuid.UUID | None:
    """Return a ``UUID`` parsed from *raw*, or ``None`` if invalid."""
    try:
        return uuid.UUID(raw)
    except (ValueError, TypeError):
        return None


def _not_found_page(req: Request):
    """Render the 404 page used whenever a draft is missing or out of scope."""
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
    action: str = "view",
) -> ResolvedDraft | Any:
    """Run the standard auth + parse + load + authorize preamble for a draft route.

    The drafts module has ~15 handlers that all open with the same five
    steps:

    1. Auth check (redirect to /auth/login on miss).
    2. Parse the URL ``draft_id`` into a UUID.
    3. Load the draft from the DB.
    4. Authorize the caller against the draft (view / edit) — return 404
       (not 403) so we never leak existence of out-of-scope drafts.

    This helper consolidates that preamble (#624). On success it returns
    a :class:`ResolvedDraft` named tuple. On any failure it returns the
    appropriate response object — either a Starlette ``Response`` (auth
    redirect) or a FastHTML FT element from :func:`_not_found_page`.

    Discriminate via ``isinstance(result, ResolvedDraft)`` (NOT plain
    ``tuple`` — FT elements are tuples too)::

        resolved = resolve_draft(req, draft_id)
        if not isinstance(resolved, ResolvedDraft):
            return resolved
        draft, auth = resolved.draft, resolved.auth

    Args:
        req: The Starlette request.
        draft_id: The URL-path UUID string for the draft.
        action: ``"view"`` (default) gates on :func:`can_view_draft`;
            ``"edit"`` gates on :func:`can_edit_draft` for handlers that
            mutate state.

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

    gate = can_edit_draft if action == "edit" else can_view_draft
    if not gate(auth, draft):
        # 404 (not 403) so cross-org probing can't enumerate draft ids.
        return _not_found_page(req)

    return ResolvedDraft(draft=draft, auth=auth)
