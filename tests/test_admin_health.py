"""Tests for the system-health aggregator (#183).

Covers:
- ``_get_system_health`` returns the expected dict for the green path.
- Missing pgvector downgrades ``overall_status`` to ``"degraded"``.
- Postgres unreachable forces ``overall_status`` to ``"down"`` and short-
  circuits the remaining DB-backed probes.
- Empty sync_log renders "Sünki pole veel tehtud" in the aggregator card.
- Worker activity of zero still renders without crashing.
- The ``/admin/health/aggregator`` detail page renders and includes a
  Värskenda refresh button that does a plain GET back to itself.
"""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import MagicMock, patch

from starlette.requests import Request
from starlette.testclient import TestClient


def _make_request(path: str = "/admin/health/aggregator") -> Request:
    """Build a minimal ASGI Request carrying an admin ``auth`` scope.

    Mirrors the ``tests/test_admin_audit_enhanced.py`` pattern — the
    Beforeware that would normally populate ``scope['auth']`` is
    bypassed so the handler can be invoked without a TestClient.
    """
    scope = {
        "type": "http",
        "method": "GET",
        "path": path,
        "headers": [],
        "query_string": b"",
        "scheme": "http",
        "server": ("testserver", 80),
        "client": ("127.0.0.1", 12345),
        "auth": {
            "role": "admin",
            "id": "admin-health-test",
            "email": "admin@seadusloome.ee",
            "full_name": "Admin Test",
        },
    }
    return Request(scope)


# ---------------------------------------------------------------------------
# _get_system_health — happy path and degraded variants
# ---------------------------------------------------------------------------


class TestGetSystemHealth:
    @patch("app.admin.health._get_metrics_recent_count", return_value=42)
    @patch("app.admin.health._get_worker_recent_activity", return_value=3)
    @patch("app.admin.health._get_last_sync")
    @patch("app.admin.health._check_pgvector", return_value=True)
    @patch("app.admin.health.jena_check_health", return_value=True)
    @patch("app.admin.health._check_postgres", return_value=True)
    def test_all_green(
        self,
        mock_pg: MagicMock,
        mock_jena: MagicMock,
        mock_pgvector: MagicMock,
        mock_sync: MagicMock,
        mock_worker: MagicMock,
        mock_metrics: MagicMock,
    ):
        from app.admin.health import _get_system_health

        mock_sync.return_value = {
            "status": "success",
            "started_at": datetime(2026, 5, 24, 9, 0, tzinfo=UTC),
            "finished_at": datetime(2026, 5, 24, 9, 4, tzinfo=UTC),
            "duration_s": 240.0,
            "error_message": None,
        }

        result = _get_system_health()
        assert result["postgres_ok"] is True
        assert result["jena_ok"] is True
        assert result["pgvector_ok"] is True
        assert result["last_sync"] is not None
        assert result["last_sync"]["status"] == "success"
        assert result["worker_recent_activity"] == 3
        assert result["metrics_recent_count"] == 42
        assert result["overall_status"] == "ok"

    @patch("app.admin.health._get_metrics_recent_count", return_value=10)
    @patch("app.admin.health._get_worker_recent_activity", return_value=0)
    @patch("app.admin.health._get_last_sync", return_value=None)
    @patch("app.admin.health._check_pgvector", return_value=False)
    @patch("app.admin.health.jena_check_health", return_value=True)
    @patch("app.admin.health._check_postgres", return_value=True)
    def test_pgvector_missing_marks_degraded(
        self,
        mock_pg: MagicMock,
        mock_jena: MagicMock,
        mock_pgvector: MagicMock,
        mock_sync: MagicMock,
        mock_worker: MagicMock,
        mock_metrics: MagicMock,
    ):
        from app.admin.health import _get_system_health

        result = _get_system_health()
        assert result["pgvector_ok"] is False
        assert result["overall_status"] == "degraded"

    @patch("app.admin.health._get_metrics_recent_count", return_value=10)
    @patch("app.admin.health._get_worker_recent_activity", return_value=0)
    @patch("app.admin.health._get_last_sync", return_value=None)
    @patch("app.admin.health._check_pgvector", return_value=True)
    @patch("app.admin.health.jena_check_health", return_value=False)
    @patch("app.admin.health._check_postgres", return_value=True)
    def test_jena_failing_marks_degraded(
        self,
        mock_pg: MagicMock,
        mock_jena: MagicMock,
        mock_pgvector: MagicMock,
        mock_sync: MagicMock,
        mock_worker: MagicMock,
        mock_metrics: MagicMock,
    ):
        from app.admin.health import _get_system_health

        result = _get_system_health()
        assert result["jena_ok"] is False
        assert result["overall_status"] == "degraded"

    @patch("app.admin.health._check_pgvector")
    @patch("app.admin.health.jena_check_health", return_value=True)
    @patch("app.admin.health._check_postgres", return_value=False)
    def test_postgres_down_short_circuits(
        self,
        mock_pg: MagicMock,
        mock_jena: MagicMock,
        mock_pgvector: MagicMock,
    ):
        """When Postgres is unreachable the DB-backed probes must not run
        (no point — they'd just fail and log noise) and overall_status
        must be ``"down"``."""
        from app.admin.health import _get_system_health

        result = _get_system_health()
        assert result["postgres_ok"] is False
        assert result["overall_status"] == "down"
        # pgvector / last_sync / worker activity / metrics counts must
        # all collapse to their safe defaults without re-querying.
        assert result["pgvector_ok"] is False
        assert result["last_sync"] is None
        assert result["worker_recent_activity"] == 0
        assert result["metrics_recent_count"] == 0
        mock_pgvector.assert_not_called()


# ---------------------------------------------------------------------------
# _health_aggregator_card — empty sync_log + zero-activity rendering
# ---------------------------------------------------------------------------


class TestHealthAggregatorCard:
    @patch("app.admin.health._get_metrics_recent_count", return_value=12)
    @patch("app.admin.health._get_worker_recent_activity", return_value=0)
    @patch("app.admin.health._get_last_sync", return_value=None)
    @patch("app.admin.health._check_pgvector", return_value=True)
    @patch("app.admin.health.jena_check_health", return_value=True)
    @patch("app.admin.health._check_postgres", return_value=True)
    def test_empty_sync_log_renders_estonian_empty_state(
        self,
        mock_pg: MagicMock,
        mock_jena: MagicMock,
        mock_pgvector: MagicMock,
        mock_sync: MagicMock,
        mock_worker: MagicMock,
        mock_metrics: MagicMock,
    ):
        from fasthtml.common import to_xml

        from app.admin.health import _health_aggregator_card

        html = to_xml(_health_aggregator_card())
        assert "Sünki pole veel tehtud" in html
        # The Estonian section labels should all be present.
        assert "Postgres" in html
        assert "Jena" in html
        assert "pgvector" in html
        assert "Viimane sünk" in html
        assert "Töötaja aktiivsus (5m)" in html
        assert "Metrics (5m)" in html
        # Link to the detail page.
        assert "/admin/health/aggregator" in html
        assert "Vaata üksikasju" in html

    @patch("app.admin.health._get_metrics_recent_count", return_value=0)
    @patch("app.admin.health._get_worker_recent_activity", return_value=0)
    @patch("app.admin.health._get_last_sync", return_value=None)
    @patch("app.admin.health._check_pgvector", return_value=True)
    @patch("app.admin.health.jena_check_health", return_value=True)
    @patch("app.admin.health._check_postgres", return_value=True)
    def test_zero_worker_activity_renders_without_crash(
        self,
        mock_pg: MagicMock,
        mock_jena: MagicMock,
        mock_pgvector: MagicMock,
        mock_sync: MagicMock,
        mock_worker: MagicMock,
        mock_metrics: MagicMock,
    ):
        """PR #835 may not have landed yet — the aggregator must still
        cope with a ``job_execution_ms`` metric stream that's empty."""
        from fasthtml.common import to_xml

        from app.admin.health import _health_aggregator_card

        html = to_xml(_health_aggregator_card())
        # The card must render the row label even when count is 0.
        assert "Töötaja aktiivsus (5m)" in html
        # Zero counts render as the muted "Ootel" status text (StatusBadge "pending").
        assert "Ootel" in html

    @patch("app.admin.health._get_metrics_recent_count", return_value=0)
    @patch("app.admin.health._get_worker_recent_activity", return_value=0)
    @patch("app.admin.health._get_last_sync", return_value=None)
    @patch("app.admin.health._check_pgvector", return_value=False)
    @patch("app.admin.health.jena_check_health", return_value=True)
    @patch("app.admin.health._check_postgres", return_value=True)
    def test_degraded_overall_badge_renders_warning(
        self,
        mock_pg: MagicMock,
        mock_jena: MagicMock,
        mock_pgvector: MagicMock,
        mock_sync: MagicMock,
        mock_worker: MagicMock,
        mock_metrics: MagicMock,
    ):
        from fasthtml.common import to_xml

        from app.admin.health import _health_aggregator_card

        html = to_xml(_health_aggregator_card())
        # The overall_status="degraded" → StatusBadge("warning") → "Hoiatus"
        assert "Hoiatus" in html


# ---------------------------------------------------------------------------
# admin_health_page — detail page renders + refresh button
# ---------------------------------------------------------------------------


class TestAdminHealthPage:
    @patch("app.admin.health._get_metrics_recent_count", return_value=99)
    @patch("app.admin.health._get_worker_recent_activity", return_value=5)
    @patch("app.admin.health._get_last_sync")
    @patch("app.admin.health._check_pgvector", return_value=True)
    @patch("app.admin.health.jena_check_health", return_value=True)
    @patch("app.admin.health._check_postgres", return_value=True)
    def test_detail_page_renders_full_data(
        self,
        mock_pg: MagicMock,
        mock_jena: MagicMock,
        mock_pgvector: MagicMock,
        mock_sync: MagicMock,
        mock_worker: MagicMock,
        mock_metrics: MagicMock,
    ):
        from fasthtml.common import to_xml

        from app.admin.health import admin_health_page

        mock_sync.return_value = {
            "status": "success",
            "started_at": datetime(2026, 5, 24, 9, 0, tzinfo=UTC),
            "finished_at": datetime(2026, 5, 24, 9, 4, tzinfo=UTC),
            "duration_s": 240.0,
            "error_message": None,
        }

        result = admin_health_page(_make_request())
        html = to_xml(result)
        # Page chrome
        assert "Süsteemi tervis" in html
        # Refresh button → plain GET back to the same URL
        assert "Värskenda" in html
        # The refresh link must point to the same detail URL (plain GET reload).
        assert 'href="/admin/health/aggregator"' in html
        # Back-link to /admin
        assert "Tagasi adminipaneelile" in html
        # Activity counts surface as their numeric strings.
        assert ">5<" in html
        assert ">99<" in html

    @patch("app.admin.health._get_metrics_recent_count", return_value=0)
    @patch("app.admin.health._get_worker_recent_activity", return_value=0)
    @patch("app.admin.health._get_last_sync")
    @patch("app.admin.health._check_pgvector", return_value=True)
    @patch("app.admin.health.jena_check_health", return_value=True)
    @patch("app.admin.health._check_postgres", return_value=True)
    def test_detail_page_renders_sync_error_message(
        self,
        mock_pg: MagicMock,
        mock_jena: MagicMock,
        mock_pgvector: MagicMock,
        mock_sync: MagicMock,
        mock_worker: MagicMock,
        mock_metrics: MagicMock,
    ):
        from fasthtml.common import to_xml

        from app.admin.health import admin_health_page

        mock_sync.return_value = {
            "status": "failed",
            "started_at": datetime(2026, 5, 24, 9, 0, tzinfo=UTC),
            "finished_at": datetime(2026, 5, 24, 9, 4, tzinfo=UTC),
            "duration_s": 240.0,
            "error_message": "SHACL validation failed: malformed Provision",
        }

        result = admin_health_page(_make_request())
        html = to_xml(result)
        # Error message must be surfaced inside the Details disclosure.
        assert "Veateade" in html
        assert "SHACL validation failed" in html

    def test_detail_route_requires_auth(self):
        from app.main import app

        client = TestClient(app, follow_redirects=False)
        response = client.get("/admin/health/aggregator")
        assert response.status_code == 303
        assert response.headers["location"] == "/auth/login"

    @patch("app.admin.health._get_system_health")
    def test_detail_page_renders_error_fallback_on_failure(self, mock_health: MagicMock):
        """If the aggregator itself crashes, the handler must return a
        styled PageShell error banner rather than a raw 500."""
        from fasthtml.common import to_xml

        from app.admin.health import admin_health_page

        mock_health.side_effect = RuntimeError("unexpected boom")
        result = admin_health_page(_make_request())
        html = to_xml(result)
        assert "Süsteemi tervis" in html
        # The shared error banner copy.
        assert "Andmete laadimine ebaõnnestus" in html


# ---------------------------------------------------------------------------
# /api/health public-payload reduction (#861-D)
# ---------------------------------------------------------------------------


def _health_request(*, auth: dict | None = None, cookies: dict | None = None) -> Request:  # type: ignore[type-arg]
    """Build an ``/api/health`` Request, optionally with an auth scope/cookie."""
    headers: list[tuple[bytes, bytes]] = []
    if cookies:
        cookie_str = "; ".join(f"{k}={v}" for k, v in cookies.items())
        headers.append((b"cookie", cookie_str.encode()))
    scope = {
        "type": "http",
        "method": "GET",
        "path": "/api/health",
        "headers": headers,
        "query_string": b"",
        "scheme": "http",
        "server": ("testserver", 80),
        "client": ("127.0.0.1", 12345),
    }
    if auth is not None:
        scope["auth"] = auth
    return Request(scope)


class TestHealthCheckPayload:
    @patch("app.admin.health._check_postgres", return_value=True)
    @patch("app.admin.health.jena_check_health", return_value=True)
    def test_unauthenticated_payload_is_status_only(
        self, mock_jena: MagicMock, mock_pg: MagicMock
    ):
        """#861-D: anonymous callers must not see version/SHA/subsystems."""
        import json

        from app.admin.health import health_check

        resp = health_check(_health_request())
        body = json.loads(bytes(resp.body).decode())
        assert body == {"status": "ok"}
        assert "version" not in body
        assert "jena" not in body
        assert "postgres" not in body

    @patch("app.admin.health._check_postgres", return_value=False)
    @patch("app.admin.health.jena_check_health", return_value=True)
    def test_unauthenticated_degraded_status_still_minimal(
        self, mock_jena: MagicMock, mock_pg: MagicMock
    ):
        import json

        from app.admin.health import health_check

        resp = health_check(_health_request())
        body = json.loads(bytes(resp.body).decode())
        assert body == {"status": "degraded"}

    @patch("app.admin.health._check_postgres", return_value=True)
    @patch("app.admin.health.jena_check_health", return_value=True)
    def test_admin_scope_gets_detailed_payload(self, mock_jena: MagicMock, mock_pg: MagicMock):
        """An admin (resolved via scope) gets the rich payload (#861-D)."""
        import json

        from app.admin.health import health_check

        resp = health_check(_health_request(auth={"role": "admin", "id": "a-1"}))
        body = json.loads(bytes(resp.body).decode())
        assert body["status"] == "ok"
        assert body["jena"] is True
        assert body["postgres"] is True
        assert "version" in body
        assert set(body["version"]) >= {"app", "sha", "built_at"}

    @patch("app.admin.health._check_postgres", return_value=True)
    @patch("app.admin.health.jena_check_health", return_value=True)
    def test_non_admin_scope_gets_minimal_payload(self, mock_jena: MagicMock, mock_pg: MagicMock):
        import json

        from app.admin.health import health_check

        resp = health_check(_health_request(auth={"role": "drafter", "id": "d-1"}))
        body = json.loads(bytes(resp.body).decode())
        assert body == {"status": "ok"}

    @patch("app.admin.health._check_postgres", return_value=True)
    @patch("app.admin.health.jena_check_health", return_value=True)
    def test_admin_cookie_gets_detailed_payload(self, mock_jena: MagicMock, mock_pg: MagicMock):
        """When scope['auth'] is absent (the live SKIP_PATHS case), the
        handler resolves an admin from the access_token cookie (#861-D)."""
        import json

        from app.admin import health as health_mod

        provider = MagicMock()
        provider.get_current_user.return_value = {"role": "admin", "id": "a-1"}
        with patch("app.auth.middleware._get_provider", return_value=provider):
            resp = health_mod.health_check(_health_request(cookies={"access_token": "tok"}))
        body = json.loads(bytes(resp.body).decode())
        assert "version" in body
        provider.get_current_user.assert_called_once_with("tok")

    @patch("app.admin.health._check_postgres", return_value=True)
    @patch("app.admin.health.jena_check_health", return_value=True)
    def test_bad_cookie_does_not_500(self, mock_jena: MagicMock, mock_pg: MagicMock):
        """A malformed/expired cookie must degrade to the public payload,
        never raise (#861-D)."""
        import json

        from app.admin import health as health_mod

        with patch("app.auth.middleware._get_provider", side_effect=Exception("boom")):
            resp = health_mod.health_check(_health_request(cookies={"access_token": "bad"}))
        body = json.loads(bytes(resp.body).decode())
        assert body == {"status": "ok"}
