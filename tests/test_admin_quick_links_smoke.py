"""Live-smoke regression coverage for admin dashboard quick-link routes.

The 2026-04-29 UI review (`docs/2026-04-29-ui-review-seadusloome-live.md`,
finding "P1 - Admin quick links lead to raw 500 pages") found that all
five admin sub-pages returned ``Internal Server Error`` on the live
deployment even though the routes were correctly registered. The cause
was the now-removed ``app.templates.admin_dashboard`` shim's ``_rebind``
helper, which swapped each rebound page handler's ``__globals__`` to the
shim's module dict (so test patches on the shim took effect) but only
re-imported a subset of the helpers each handler used; every helper not
imported into the shim raised ``NameError`` at call-time and bubbled up
as a 500.

The shim and its ``_rebind`` machinery are gone — handlers now import
their module-private helpers as locals — but this module remains the
regression guard: each admin quick-link route is hit through the real
FastHTML ``app`` with an authenticated admin client and must return
200. A failure here means a new admin sub-page was added without
inlining the necessary local imports in its handler, and a 500 is
reaching real admins again.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from starlette.testclient import TestClient


def _admin_user() -> dict[str, Any]:
    """Return a stub admin user dict matching the JWTAuthProvider contract."""
    return {
        "id": "admin-smoke",
        "email": "admin@seadusloome.ee",
        "full_name": "Smoke Admin",
        "role": "admin",
        "org_id": None,
    }


def _stub_provider() -> MagicMock:
    """Build a JWTAuthProvider stub that authenticates any token as admin."""
    provider = MagicMock()
    provider.get_current_user.return_value = _admin_user()
    return provider


def _admin_client() -> TestClient:
    """Return a TestClient pre-loaded with a stub admin ``access_token``."""
    from app.main import app

    client = TestClient(app, follow_redirects=False)
    client.cookies.set("access_token", "stub-admin-token")
    return client


_QUICK_LINK_ROUTES: tuple[str, ...] = (
    "/admin/audit",
    "/admin/jobs",
    "/admin/analytics",
    "/admin/costs",
    "/admin/performance",
)


@pytest.mark.parametrize("path", _QUICK_LINK_ROUTES)
def test_admin_quick_link_returns_200(path: str) -> None:
    """Each admin dashboard quick link must render (not 500) for an admin.

    Backend dependencies (``llm_usage``, ``metrics``, ``usage_daily``,
    ``audit_log``, ``background_jobs``) may be empty or unreachable in
    the smoke environment — the data-fetch helpers swallow errors and
    return defaults, and the page handlers wrap the renderer in a
    try/except that falls back to a styled ``PageShell`` error banner.
    Either path must yield status 200; only an unhandled exception in
    the rebound handler chain produces a 500.
    """
    with patch("app.auth.middleware._get_provider") as mock_provider:
        mock_provider.return_value = _stub_provider()
        client = _admin_client()
        resp = client.get(path)

    assert resp.status_code == 200, (
        f"{path} returned {resp.status_code}; expected 200. Body excerpt: {resp.text[:300]!r}"
    )
    # A 200 with the styled error fallback is still acceptable — the
    # contract is "no raw 500" — but the response must always be HTML.
    assert "text/html" in resp.headers.get("content-type", "")
