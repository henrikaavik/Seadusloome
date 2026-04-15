"""Session-backed flash messages rendered as toasts on the next page view.

Usage:

    # In a POST handler, before redirecting:
    from app.ui.feedback.flash import push_flash
    push_flash(request, "Eelnõu kustutatud.", kind="success")
    return RedirectResponse(url="/drafts", status_code=303)

    # In PageShell (the flash is consumed automatically when ``req`` is
    # passed to :func:`PageShell`). Each call to :func:`pop_flashes`
    # drains and clears the session key so toasts never repeat.

The session key is ``flash`` and stores a list of ``{"kind", "msg"}``
dicts. Kinds map to Toast variants (info, success, warning, danger).
"""

from __future__ import annotations

from typing import Literal

from starlette.requests import Request

from app.ui.feedback.toast import Toast, ToastVariant

FlashKind = Literal["info", "success", "warning", "danger"]

_SESSION_KEY = "flash"


def _session(request: Request) -> dict | None:
    """Return the Starlette session mapping, or None if middleware absent."""
    try:
        return request.session  # type: ignore[attr-defined]
    except (AssertionError, AttributeError, KeyError):
        return None


def push_flash(request: Request, message: str, *, kind: FlashKind = "info") -> None:
    """Queue a flash message to render on the next page load.

    Safe to call multiple times — flashes accumulate in order. When the
    session middleware is not installed (shouldn't happen in production
    but can in unit tests), the call becomes a no-op.
    """
    sess = _session(request)
    if sess is None:
        return
    existing = sess.get(_SESSION_KEY)
    if not isinstance(existing, list):
        existing = []
    existing.append({"kind": kind, "msg": message})
    sess[_SESSION_KEY] = existing


def pop_flashes(request: Request) -> list[dict[str, str]]:
    """Return and clear any pending flash messages.

    Returns an empty list if none are pending or the session is absent.
    """
    sess = _session(request)
    if sess is None:
        return []
    raw = sess.pop(_SESSION_KEY, None)
    if not isinstance(raw, list):
        return []
    out: list[dict[str, str]] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        msg = item.get("msg")
        kind = item.get("kind", "info")
        if not isinstance(msg, str) or not msg:
            continue
        if kind not in ("info", "success", "warning", "danger"):
            kind = "info"
        out.append({"kind": kind, "msg": msg})
    return out


def render_flash_toasts(request: Request) -> list:
    """Return a list of Toast FT components from any pending flashes.

    The flashes are drained from the session — calling this twice for
    the same request will return an empty list the second time.
    """
    toasts: list = []
    for entry in pop_flashes(request):
        kind = entry["kind"]
        variant: ToastVariant = kind  # type: ignore[assignment]
        toasts.append(Toast(entry["msg"], variant=variant))
    return toasts
