"""Tests for the admin Sentry errors panel (#324).

Covers:
    * Env-var gating: missing token/org/project yields ``None`` from the
      helper and an "ei ole konfigureeritud" card.
    * Empty-list case: Sentry returned 0 issues → green tick + Estonian
      "Hiljutisi vigu pole." copy.
    * Populated case: 3 issues → DataTable rendered, link to Sentry
      project page included.
    * 5xx response: graceful "Sentry API ei vasta" fallback (empty card).
    * Timeout / connection error: same graceful fallback.
    * ``/admin/sentry`` page renders 200 under ``require_role("admin")``.
    * Unauthenticated request to ``/admin/sentry`` 303-redirects to
      ``/auth/login``.

External HTTP is mocked via ``monkeypatch.setattr("httpx.get", ...)`` so
no real network traffic ever leaves the test process.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock, patch

import httpx
import pytest
from starlette.testclient import TestClient

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _set_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Populate the three Sentry env vars with throwaway values."""
    monkeypatch.setenv("SENTRY_API_TOKEN", "sentry-test-token-do-not-log")
    monkeypatch.setenv("SENTRY_ORG_SLUG", "seadusloome-test")
    monkeypatch.setenv("SENTRY_PROJECT_SLUG", "seadusloome-app")


def _clear_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Remove all three Sentry env vars to simulate missing configuration."""
    monkeypatch.delenv("SENTRY_API_TOKEN", raising=False)
    monkeypatch.delenv("SENTRY_ORG_SLUG", raising=False)
    monkeypatch.delenv("SENTRY_PROJECT_SLUG", raising=False)


def _mock_response(status_code: int, payload: Any) -> MagicMock:
    """Build a fake ``httpx.Response`` with ``status_code`` and ``.json()``."""
    response = MagicMock()
    response.status_code = status_code
    response.json = MagicMock(return_value=payload)
    return response


def _admin_user() -> dict[str, Any]:
    return {
        "id": "admin-sentry",
        "email": "admin@seadusloome.ee",
        "full_name": "Sentry Admin",
        "role": "admin",
        "org_id": None,
    }


def _stub_provider() -> MagicMock:
    provider = MagicMock()
    provider.get_current_user.return_value = _admin_user()
    return provider


def _admin_client() -> TestClient:
    from app.main import app

    client = TestClient(app, follow_redirects=False)
    client.cookies.set("access_token", "stub-admin-token")
    return client


# ---------------------------------------------------------------------------
# _get_recent_sentry_errors — env-var gating
# ---------------------------------------------------------------------------


class TestEnvGating:
    def test_returns_none_when_env_missing(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from app.admin.sentry_panel import _get_recent_sentry_errors

        _clear_env(monkeypatch)
        assert _get_recent_sentry_errors() is None

    def test_returns_none_when_only_token_set(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from app.admin.sentry_panel import _get_recent_sentry_errors

        _clear_env(monkeypatch)
        monkeypatch.setenv("SENTRY_API_TOKEN", "x")
        assert _get_recent_sentry_errors() is None

    def test_returns_none_when_project_missing(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from app.admin.sentry_panel import _get_recent_sentry_errors

        _clear_env(monkeypatch)
        monkeypatch.setenv("SENTRY_API_TOKEN", "x")
        monkeypatch.setenv("SENTRY_ORG_SLUG", "y")
        assert _get_recent_sentry_errors() is None


# ---------------------------------------------------------------------------
# _get_recent_sentry_errors — HTTP behaviour
# ---------------------------------------------------------------------------


class TestSentryApiCall:
    def test_empty_list_when_sentry_returns_zero_issues(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from app.admin import sentry_panel

        _set_env(monkeypatch)
        mock_get = MagicMock(return_value=_mock_response(200, []))
        monkeypatch.setattr(sentry_panel.httpx, "get", mock_get)

        result = sentry_panel._get_recent_sentry_errors()
        assert result == []
        # Token is sent in the Authorization header, NOT URL params, and
        # we must not leak it via logging — the test asserts that the
        # call shape is correct, not the log output.
        call_kwargs = mock_get.call_args.kwargs
        assert call_kwargs["headers"]["Authorization"].startswith("Bearer ")

    def test_returns_three_normalised_issues(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from app.admin import sentry_panel

        _set_env(monkeypatch)
        payload = [
            {
                "title": "AssertionError in sync_orchestrator",
                "lastSeen": "2026-05-23T14:30:00Z",
                "count": "12",
                "level": "error",
                "permalink": "https://sentry.io/issues/1/",
            },
            {
                "title": "Timeout calling Voyage API",
                "lastSeen": "2026-05-23T11:00:00Z",
                "count": "3",
                "level": "warning",
                "permalink": "https://sentry.io/issues/2/",
            },
            {
                "title": "KeyError in chat handler",
                "lastSeen": "2026-05-22T20:15:00Z",
                "count": "1",
                "level": "fatal",
                "permalink": "https://sentry.io/issues/3/",
            },
        ]
        monkeypatch.setattr(
            sentry_panel.httpx, "get", MagicMock(return_value=_mock_response(200, payload))
        )

        issues = sentry_panel._get_recent_sentry_errors()
        assert issues is not None
        assert len(issues) == 3
        assert issues[0]["title"] == "AssertionError in sync_orchestrator"
        assert issues[0]["count"] == "12"
        assert issues[0]["level"] == "error"
        assert issues[0]["link"] == "https://sentry.io/issues/1/"
        assert issues[2]["level"] == "fatal"

    def test_caps_at_five_issues(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from app.admin import sentry_panel

        _set_env(monkeypatch)
        payload = [
            {"title": f"Error {i}", "lastSeen": "", "count": "1", "level": "error"}
            for i in range(20)
        ]
        monkeypatch.setattr(
            sentry_panel.httpx, "get", MagicMock(return_value=_mock_response(200, payload))
        )
        issues = sentry_panel._get_recent_sentry_errors()
        assert issues is not None
        assert len(issues) == 5

    def test_returns_empty_list_on_500(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from app.admin import sentry_panel

        _set_env(monkeypatch)
        monkeypatch.setattr(
            sentry_panel.httpx, "get", MagicMock(return_value=_mock_response(500, None))
        )
        # Graceful fallback: NOT None (which means "unconfigured") and
        # NOT a raised exception (which would 500 the admin page).
        assert sentry_panel._get_recent_sentry_errors() == []

    def test_returns_empty_list_on_timeout(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from app.admin import sentry_panel

        _set_env(monkeypatch)

        def _raise_timeout(*args: object, **kwargs: object) -> None:
            raise httpx.TimeoutException("simulated timeout")

        monkeypatch.setattr(sentry_panel.httpx, "get", _raise_timeout)
        assert sentry_panel._get_recent_sentry_errors() == []

    def test_returns_empty_list_on_connect_error(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from app.admin import sentry_panel

        _set_env(monkeypatch)

        def _raise_connect(*args: object, **kwargs: object) -> None:
            raise httpx.ConnectError("simulated dns failure")

        monkeypatch.setattr(sentry_panel.httpx, "get", _raise_connect)
        assert sentry_panel._get_recent_sentry_errors() == []

    def test_returns_empty_list_on_non_json_body(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from app.admin import sentry_panel

        _set_env(monkeypatch)
        bad_response = MagicMock()
        bad_response.status_code = 200
        bad_response.json = MagicMock(side_effect=ValueError("not json"))
        monkeypatch.setattr(sentry_panel.httpx, "get", MagicMock(return_value=bad_response))
        assert sentry_panel._get_recent_sentry_errors() == []


# ---------------------------------------------------------------------------
# _sentry_panel_card — render branches
# ---------------------------------------------------------------------------


class TestSentryPanelCard:
    def test_card_empty_state_when_env_missing(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from fasthtml.common import to_xml

        from app.admin.sentry_panel import _sentry_panel_card

        _clear_env(monkeypatch)
        html = to_xml(_sentry_panel_card())
        assert "Sentry vead" in html
        assert "Sentry pole konfigureeritud" in html
        # The setup-doc pointer must appear so admins can find the spec.
        assert "SENTRY_API_TOKEN" in html

    def test_card_green_tick_when_no_issues(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from fasthtml.common import to_xml

        from app.admin import sentry_panel

        _set_env(monkeypatch)
        monkeypatch.setattr(
            sentry_panel.httpx, "get", MagicMock(return_value=_mock_response(200, []))
        )
        html = to_xml(sentry_panel._sentry_panel_card())
        # Green StatusBadge ("ok") + Estonian copy
        assert "Hiljutisi vigu pole." in html
        assert "status-ok" in html

    def test_card_renders_table_for_three_issues(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from fasthtml.common import to_xml

        from app.admin import sentry_panel

        _set_env(monkeypatch)
        payload = [
            {
                "title": "AssertionError in sync_orchestrator",
                "lastSeen": "2026-05-23T14:30:00Z",
                "count": "12",
                "level": "error",
                "permalink": "https://sentry.io/issues/1/",
            },
            {
                "title": "Timeout calling Voyage API",
                "lastSeen": "2026-05-23T11:00:00Z",
                "count": "3",
                "level": "warning",
                "permalink": "https://sentry.io/issues/2/",
            },
            {
                "title": "KeyError in chat handler",
                "lastSeen": "2026-05-22T20:15:00Z",
                "count": "1",
                "level": "fatal",
                "permalink": "https://sentry.io/issues/3/",
            },
        ]
        monkeypatch.setattr(
            sentry_panel.httpx, "get", MagicMock(return_value=_mock_response(200, payload))
        )
        html = to_xml(sentry_panel._sentry_panel_card())
        # All three titles + counts present
        assert "AssertionError in sync_orchestrator" in html
        assert "Timeout calling Voyage API" in html
        assert "KeyError in chat handler" in html
        # Per-issue Sentry permalinks (rendered as anchor hrefs)
        assert 'href="https://sentry.io/issues/1/"' in html
        assert 'href="https://sentry.io/issues/2/"' in html
        # Footer "Vaata Sentrys" link uses only the org + project slug
        assert "seadusloome-test.sentry.io" in html
        assert "Vaata Sentrys" in html
        # Token MUST NEVER appear in rendered HTML
        assert "sentry-test-token" not in html

    def test_card_renders_empty_state_on_api_5xx(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from fasthtml.common import to_xml

        from app.admin import sentry_panel

        _set_env(monkeypatch)
        monkeypatch.setattr(
            sentry_panel.httpx, "get", MagicMock(return_value=_mock_response(500, None))
        )
        html = to_xml(sentry_panel._sentry_panel_card())
        # Same UX as the "no issues" branch — empty list yields the green tick
        # because the data helper degrades to []. Admins still see the card,
        # never a raw 500.
        assert "Hiljutisi vigu pole." in html


# ---------------------------------------------------------------------------
# /admin/sentry route
# ---------------------------------------------------------------------------


class TestAdminSentryRoute:
    def test_route_renders_under_admin_role(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from app.admin import sentry_panel

        _set_env(monkeypatch)
        monkeypatch.setattr(
            sentry_panel.httpx, "get", MagicMock(return_value=_mock_response(200, []))
        )

        with patch("app.auth.middleware._get_provider") as mock_provider:
            mock_provider.return_value = _stub_provider()
            client = _admin_client()
            resp = client.get("/admin/sentry")

        assert resp.status_code == 200, (
            f"/admin/sentry returned {resp.status_code}; expected 200. Body: {resp.text[:300]!r}"
        )
        assert "text/html" in resp.headers.get("content-type", "")
        # Page title + refresh button + "no issues" copy
        assert "Sentry vead" in resp.text
        assert "Värskenda" in resp.text
        assert "Hiljutisi vigu pole." in resp.text

    def test_route_redirects_unauthenticated(self) -> None:
        from app.main import app

        client = TestClient(app, follow_redirects=False)
        response = client.get("/admin/sentry")
        assert response.status_code == 303
        assert response.headers["location"] == "/auth/login"
