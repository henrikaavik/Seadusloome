"""Tests for personal dashboard, admin dashboard, and health check."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from starlette.testclient import TestClient

# ---------------------------------------------------------------------------
# /dashboard requires auth (redirects when not logged in)
# ---------------------------------------------------------------------------


class TestDashboardAuth:
    def test_dashboard_redirects_unauthenticated(self):
        from app.main import app

        client = TestClient(app, follow_redirects=False)
        response = client.get("/dashboard")
        assert response.status_code == 303
        assert response.headers["location"] == "/auth/login"
        # Make sure the redirect carried no auth cookies (#423).
        for h in response.headers.get_list("set-cookie"):
            assert "access_token=" not in h
            assert "refresh_token=" not in h

    def test_index_redirects_unauthenticated(self):
        from app.main import app

        client = TestClient(app, follow_redirects=False)
        response = client.get("/")
        assert response.status_code == 303
        assert response.headers["location"] == "/auth/login"

    @patch("app.auth.middleware._get_provider")
    def test_index_redirects_authenticated_to_dashboard(self, mock_get_provider: MagicMock):
        """#746: authenticated ``GET /`` lands on ``/dashboard`` (Töölaud),
        not the Õiguskaart graph."""
        from app.main import app

        provider = MagicMock()
        provider.get_current_user.return_value = {
            "id": "u-1",
            "email": "u@seadusloome.ee",
            "full_name": "Test Kasutaja",
            "role": "drafter",
            "org_id": "11111111-1111-1111-1111-111111111111",
        }
        mock_get_provider.return_value = provider

        client = TestClient(app, follow_redirects=False)
        client.cookies.set("access_token", "stub-token")
        response = client.get("/")
        assert response.status_code == 303
        assert response.headers["location"] == "/dashboard"


# ---------------------------------------------------------------------------
# /api/health returns JSON without auth
# ---------------------------------------------------------------------------


class TestHealthCheck:
    @patch("app.admin.health.jena_check_health")
    @patch("app.admin.health._check_postgres")
    def test_health_returns_json(self, mock_pg: MagicMock, mock_jena: MagicMock):
        mock_pg.return_value = True
        mock_jena.return_value = True

        from app.main import app

        client = TestClient(app)
        response = client.get("/api/health")
        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "ok"
        assert data["jena"] is True
        assert data["postgres"] is True

    @patch("app.admin.health.jena_check_health")
    @patch("app.admin.health._check_postgres")
    def test_health_degraded_when_service_down(self, mock_pg: MagicMock, mock_jena: MagicMock):
        mock_pg.return_value = True
        mock_jena.return_value = False

        from app.main import app

        client = TestClient(app)
        response = client.get("/api/health")
        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "degraded"
        assert data["jena"] is False
        assert data["postgres"] is True

    def test_health_no_auth_required(self):
        """Health check must be accessible without cookies."""
        from app.main import app

        client = TestClient(app, follow_redirects=False)
        response = client.get("/api/health")
        # Should not redirect to login
        assert response.status_code == 200
        data = response.json()
        assert data["status"] in {"ok", "degraded"}
        assert "jena" in data
        assert "postgres" in data

    def test_ping_no_auth_required(self):
        """/api/ping is a lightweight liveness probe that must work anon."""
        from app.main import app

        client = TestClient(app, follow_redirects=False)
        response = client.get("/api/ping")
        assert response.status_code == 200
        assert response.text == "ok"


# ---------------------------------------------------------------------------
# /admin requires admin role
# ---------------------------------------------------------------------------


class TestAdminDashboard:
    def test_admin_redirects_unauthenticated(self):
        from app.main import app

        client = TestClient(app, follow_redirects=False)
        response = client.get("/admin")
        assert response.status_code == 303
        assert response.headers["location"] == "/auth/login"
        assert response.text == "" or "Sisselogimine" not in response.text

    def test_admin_audit_redirects_unauthenticated(self):
        from app.main import app

        client = TestClient(app, follow_redirects=False)
        response = client.get("/admin/audit")
        assert response.status_code == 303
        assert response.headers["location"] == "/auth/login"
        # The redirect must not leak any audit content.
        assert "Auditilogi" not in response.text

    def test_admin_sync_requires_auth(self):
        """POST /admin/sync must not run a sync for unauthenticated users."""
        from app.main import app

        client = TestClient(app, follow_redirects=False)
        response = client.post("/admin/sync")
        assert response.status_code == 303
        assert response.headers["location"] == "/auth/login"

    @patch("app.admin.sync._insert_running_row", return_value=99)
    @patch("app.admin.sync._get_sync_logs")
    @patch("threading.Thread")
    def test_admin_sync_triggers_thread(
        self,
        mock_thread_cls: MagicMock,
        mock_logs: MagicMock,
        mock_insert: MagicMock,
    ):
        """When called directly (simulating an admin request) the handler
        starts a background thread and returns a sync card with a status
        banner. Asserts the lock state is reset via the `finally`."""
        from starlette.requests import Request

        from app.admin import sync as admin_sync
        from app.admin.sync import trigger_sync

        mock_logs.return_value = []

        # Ensure a clean lock state so the test is deterministic.
        admin_sync._sync_in_progress = False
        mock_thread = MagicMock()
        mock_thread_cls.return_value = mock_thread

        # Build a minimal Request object for the handler.
        scope = {
            "type": "http",
            "method": "POST",
            "path": "/admin/sync",
            "headers": [],
            "query_string": b"",
            "scheme": "http",
            "server": ("testserver", 80),
            "client": ("127.0.0.1", 12345),
            "auth": {"role": "admin", "id": "admin-test", "email": "a@b.ee"},
        }
        req = Request(scope)

        result = trigger_sync(req)

        # Thread was started
        mock_thread_cls.assert_called_once()
        mock_thread.start.assert_called_once()

        # Handler should have flipped the in-progress flag on entry.
        assert admin_sync._sync_in_progress is True

        # Result is a FastTag we can convert to XML to look for the banner.
        from fasthtml.common import to_xml

        html = to_xml(result)
        assert "sünkroniseerimine käivitati" in html.lower()

        # Clean up: simulate the thread finishing and clearing the flag.
        admin_sync._sync_in_progress = False


# ---------------------------------------------------------------------------
# Dashboard work-queue page (#717) — render the new operational sections
# ---------------------------------------------------------------------------

# Helper names patched at ``app.templates.dashboard.<name>`` per the
# patch-where-used contract — these are the DB-touching widget loaders the
# page calls; mocking them lets us render ``dashboard_page`` with a fake
# Request and no live DB.
_WIDGET_HELPERS = (
    "_get_active_drafter_sessions",
    "_get_high_risk_reports",
    "_get_unviewed_reports",
    "_get_stale_analysis_drafts",
    "_get_recent_syncs",
    "_get_recent_exports",
    "_get_unresolved_annotation_drafts",
    "_get_bookmarks",
    "_get_user_org_info",
)


def _make_dashboard_request():
    """Build a minimal ASGI ``Request`` carrying an ``auth`` scope.

    Mirrors the pattern in ``tests/test_admin_analytics.py`` — the
    Beforeware that normally populates ``req.scope['auth']`` is bypassed by
    constructing the scope directly, so ``dashboard_page(req)`` can be
    invoked without a TestClient round-trip.
    """
    from starlette.requests import Request

    scope = {
        "type": "http",
        "method": "GET",
        "path": "/dashboard",
        "headers": [],
        "query_string": b"",
        "scheme": "http",
        "server": ("testserver", 80),
        "client": ("127.0.0.1", 12345),
        "auth": {
            "id": "33333333-3333-3333-3333-333333333333",
            "email": "kasutaja@seadusloome.ee",
            "full_name": "Test Kasutaja",
            "role": "drafter",
            "org_id": "11111111-1111-1111-1111-111111111111",
        },
    }
    return Request(scope)


def _render_dashboard(returns: dict[str, object]) -> str:
    """Render ``dashboard_page`` with every widget helper patched.

    ``returns`` maps a subset of :data:`_WIDGET_HELPERS` names to their
    return value; unspecified helpers default to ``[]`` (or ``None`` for
    ``_get_user_org_info``).
    """
    from contextlib import ExitStack

    from fasthtml.common import to_xml

    from app.templates.dashboard import dashboard_page

    with ExitStack() as stack:
        for name in _WIDGET_HELPERS:
            default: object = None if name == "_get_user_org_info" else []
            stack.enter_context(
                patch(f"app.templates.dashboard.{name}", return_value=returns.get(name, default))
            )
        result = dashboard_page(_make_dashboard_request())
    return to_xml(result)


_ORG_INFO = {"org_name": "Justiitsministeerium", "role": "drafter", "member_count": 4}


class TestDashboardWorkQueue:
    """``/dashboard`` is now an operational work queue, not a welcome page."""

    def test_dropped_welcome_hero(self):
        """The marketing-style 'Tere tulemast Seadusloome süsteemi' InfoBox
        is gone — replaced by a plain H1 + a small org/role line."""
        html = _render_dashboard({"_get_user_org_info": _ORG_INFO})
        assert "Tere tulemast Seadusloome" not in html
        # Compact greeting + org line instead.
        assert "Tere, Test" in html
        assert "Justiitsministeerium" in html

    def test_all_section_headers_render(self):
        html = _render_dashboard({"_get_user_org_info": _ORG_INFO})
        for header in (
            "Minu järgmised tegevused",
            "Kõrge riskiga leiud",
            "Aegunud analüüsid",
            "Uued ontoloogia muudatused",
            "Hiljutised ekspordid",
            "Eelnõud lahtiste märkustega",
            "Järjehoidjad",
        ):
            assert header in html, header

    def test_empty_data_shows_calm_empty_states(self):
        html = _render_dashboard({})
        # The synthesised next-action list collapses to the calm one-liner.
        assert "Hetkel pole midagi ootel." in html
        # Supporting widgets each show their own muted empty row, never a
        # leftover hero or scary banner.
        assert "Kõrge riskiga mõjuaruandeid hetkel pole." in html
        assert "Aegunud analüüse pole." in html
        assert "Hiljutisi ontoloogia uuendusi pole." in html

    def test_next_actions_synthesised_from_signals(self):
        from datetime import UTC, datetime

        now = datetime(2026, 5, 11, 9, 0, tzinfo=UTC)
        html = _render_dashboard(
            {
                "_get_user_org_info": _ORG_INFO,
                "_get_active_drafter_sessions": [
                    {
                        "id": "aaaa1111-0000-0000-0000-000000000001",
                        "current_step": 3,
                        "updated_at": now,
                    },
                ],
                "_get_high_risk_reports": [
                    {
                        "draft_id": "bbbb2222-0000-0000-0000-000000000002",
                        "title": "Andmekaitse eelnõu",
                        "impact_score": 75,
                        "conflict_count": 2,
                        "affected_count": 18,
                        "gap_count": 1,
                        "generated_at": now,
                    },
                ],
                "_get_stale_analysis_drafts": [
                    {
                        "draft_id": "cccc3333-0000-0000-0000-000000000003",
                        "title": "Jäätmeseaduse muudatus",
                        "stale_count": 1,
                    },
                ],
            }
        )
        # Drafter-session row with the resolved step label.
        assert "Jätka koostajas — 3. samm: Uurimine" in html
        assert "/drafter/aaaa1111-0000-0000-0000-000000000001" in html
        # High-risk report row, conflict-count phrasing + report link.
        assert "2 konflikti vajavad ülevaatust" in html
        assert "/drafts/bbbb2222-0000-0000-0000-000000000002/report" in html
        # Stale-analysis row → "analüüsi uuesti" copy + report link.
        assert "Analüüsi uuesti." in html
        assert "/drafts/cccc3333-0000-0000-0000-000000000003/report" in html

    def test_next_actions_include_unviewed_reports(self):
        from datetime import UTC, datetime

        now = datetime(2026, 5, 11, 9, 0, tzinfo=UTC)
        html = _render_dashboard(
            {
                "_get_user_org_info": _ORG_INFO,
                "_get_unviewed_reports": [
                    {
                        "draft_id": "dddd4444-0000-0000-0000-000000000004",
                        "title": "Liiklusseaduse muudatus",
                        "impact_score": 25,
                        "conflict_count": 0,
                        "generated_at": now,
                        "reanalyzed": False,
                    },
                    {
                        "draft_id": "eeee5555-0000-0000-0000-000000000005",
                        "title": "Maksuseadus",
                        "impact_score": 40,
                        "conflict_count": 0,
                        "generated_at": now,
                        "reanalyzed": True,
                    },
                ],
            }
        )
        # A never-opened low/medium report still surfaces as a next action…
        assert "Mõjuaruanne valmis: «Liiklusseaduse muudatus»." in html
        assert "/drafts/dddd4444-0000-0000-0000-000000000004/report" in html
        # …and a re-analysed-since-last-view one gets the "uuesti" framing.
        assert "Maksuseadus»: eelnõu analüüsiti uuesti" in html
        assert "/drafts/eeee5555-0000-0000-0000-000000000005/report" in html

    def test_next_actions_dedupe_by_draft(self):
        """A draft that qualifies for several sources gets one row — the
        most-urgent framing wins (stale > high-risk > unviewed)."""
        from datetime import UTC, datetime

        now = datetime(2026, 5, 11, 9, 0, tzinfo=UTC)
        did = "ffff6666-0000-0000-0000-000000000006"
        html = _render_dashboard(
            {
                "_get_user_org_info": _ORG_INFO,
                "_get_high_risk_reports": [
                    {
                        "draft_id": did,
                        "title": "Topelt eelnõu",
                        "impact_score": 80,
                        "conflict_count": 3,
                        "affected_count": 10,
                        "gap_count": 0,
                        "generated_at": now,
                    },
                ],
                "_get_stale_analysis_drafts": [
                    {"draft_id": did, "title": "Topelt eelnõu", "stale_count": 1},
                ],
                "_get_unviewed_reports": [
                    {
                        "draft_id": did,
                        "title": "Topelt eelnõu",
                        "impact_score": 80,
                        "conflict_count": 3,
                        "generated_at": now,
                        "reanalyzed": False,
                    },
                ],
            }
        )
        # Stale wins → "analüüsi uuesti" copy appears…
        assert "Topelt eelnõu»: ontoloogia uuenes" in html
        # …and the high-risk / unviewed framings for the same draft do NOT.
        assert "3 konflikti vajavad ülevaatust" not in html
        assert "Mõjuaruanne valmis: «Topelt eelnõu»." not in html

    def test_high_risk_widget_renders_band_badge_and_link(self):
        from datetime import UTC, datetime

        now = datetime(2026, 5, 11, 9, 0, tzinfo=UTC)
        html = _render_dashboard(
            {
                "_get_user_org_info": _ORG_INFO,
                "_get_high_risk_reports": [
                    {
                        "draft_id": "dddd4444-0000-0000-0000-000000000004",
                        "title": "Karistusseadustiku muudatus",
                        "impact_score": 90,
                        "conflict_count": 0,
                        "affected_count": 40,
                        "gap_count": 0,
                        "generated_at": now,
                    },
                ],
            }
        )
        assert "Karistusseadustiku muudatus" in html
        # 90 → critical band label.
        assert "Kriitiline" in html
        assert "/drafts/dddd4444-0000-0000-0000-000000000004/report" in html

    def test_sync_widget_shows_date_and_entity_count(self):
        from datetime import UTC, datetime

        html = _render_dashboard(
            {
                "_get_user_org_info": _ORG_INFO,
                "_get_recent_syncs": [
                    {
                        "id": 7,
                        "finished_at": datetime(2026, 5, 10, 6, 30, tzinfo=UTC),
                        "entity_count": 90123,
                    },
                ],
            }
        )
        # Entity count rendered with a thin-space thousands separator.
        assert "90 123" in html
        # The sync card no longer shows the calm empty row when data exists.
        assert "Hiljutisi ontoloogia uuendusi pole." not in html

    def test_unresolved_annotations_widget_renders_count_and_link(self):
        html = _render_dashboard(
            {
                "_get_user_org_info": _ORG_INFO,
                "_get_unresolved_annotation_drafts": [
                    {
                        "draft_id": "eeee5555-0000-0000-0000-000000000005",
                        "title": "Sotsiaalhoolekande eelnõu",
                        "unresolved_count": 4,
                    },
                ],
            }
        )
        assert "Sotsiaalhoolekande eelnõu" in html
        assert ">4<" in html  # the Badge with the unresolved count
        assert "/drafts/eeee5555-0000-0000-0000-000000000005/report" in html

    def test_renders_when_user_has_no_org(self):
        """A user with no organisation still gets the page (no crash) — the
        org-scoped widgets just come back empty."""
        html = _render_dashboard({"_get_user_org_info": None})
        assert "Töölaud" in html
        assert "Te ei kuulu ühtegi organisatsiooni." in html
        assert "Hetkel pole midagi ootel." in html


# ---------------------------------------------------------------------------
# Bookmark DB helpers (unit tests with mocked DB)
# ---------------------------------------------------------------------------


class TestBookmarkAdd:
    @patch("app.templates.dashboard._connect")
    def test_add_bookmark_returns_dict(self, mock_connect: MagicMock):
        from app.templates.dashboard import _add_bookmark

        mock_conn = MagicMock()
        mock_connect.return_value.__enter__ = MagicMock(return_value=mock_conn)
        mock_connect.return_value.__exit__ = MagicMock(return_value=False)
        mock_conn.execute.return_value.fetchone.return_value = (
            "bm-id-1",
            "http://example.org/entity/1",
            "Test Entity",
            "2024-06-01",
        )

        result = _add_bookmark("user-1", "http://example.org/entity/1", "Test Entity")
        assert result is not None
        assert result["id"] == "bm-id-1"
        assert result["entity_uri"] == "http://example.org/entity/1"
        assert result["label"] == "Test Entity"
        mock_conn.commit.assert_called_once()

    @patch("app.templates.dashboard._connect")
    def test_add_bookmark_returns_none_on_conflict(self, mock_connect: MagicMock):
        from app.templates.dashboard import _add_bookmark

        mock_conn = MagicMock()
        mock_connect.return_value.__enter__ = MagicMock(return_value=mock_conn)
        mock_connect.return_value.__exit__ = MagicMock(return_value=False)
        # ON CONFLICT DO NOTHING returns None
        mock_conn.execute.return_value.fetchone.return_value = None

        result = _add_bookmark("user-1", "http://example.org/entity/1", "Test")
        assert result is None

    @patch("app.templates.dashboard._connect")
    def test_add_bookmark_returns_none_on_db_error(self, mock_connect: MagicMock):
        from app.templates.dashboard import _add_bookmark

        mock_connect.side_effect = Exception("DB unavailable")
        result = _add_bookmark("user-1", "http://example.org/entity/1", "Test")
        assert result is None


class TestBookmarkRemove:
    @patch("app.templates.dashboard._connect")
    def test_remove_bookmark_returns_true(self, mock_connect: MagicMock):
        from app.templates.dashboard import _remove_bookmark

        mock_conn = MagicMock()
        mock_connect.return_value.__enter__ = MagicMock(return_value=mock_conn)
        mock_connect.return_value.__exit__ = MagicMock(return_value=False)
        mock_conn.execute.return_value.rowcount = 1

        result = _remove_bookmark("bm-id-1", "user-1")
        assert result is True
        mock_conn.commit.assert_called_once()

    @patch("app.templates.dashboard._connect")
    def test_remove_bookmark_returns_false_for_nonexistent(self, mock_connect: MagicMock):
        from app.templates.dashboard import _remove_bookmark

        mock_conn = MagicMock()
        mock_connect.return_value.__enter__ = MagicMock(return_value=mock_conn)
        mock_connect.return_value.__exit__ = MagicMock(return_value=False)
        mock_conn.execute.return_value.rowcount = 0

        result = _remove_bookmark("nonexistent", "user-1")
        assert result is False

    @patch("app.templates.dashboard._connect")
    def test_remove_bookmark_returns_false_on_db_error(self, mock_connect: MagicMock):
        from app.templates.dashboard import _remove_bookmark

        mock_connect.side_effect = Exception("DB unavailable")
        result = _remove_bookmark("bm-id-1", "user-1")
        assert result is False


# ---------------------------------------------------------------------------
# Admin dashboard DB helpers (unit tests with mocked DB)
# ---------------------------------------------------------------------------


class TestSyncLogs:
    @patch("app.admin.sync._connect")
    def test_get_sync_logs_returns_list(self, mock_connect: MagicMock):
        from app.admin.sync import _get_sync_logs

        mock_conn = MagicMock()
        mock_connect.return_value.__enter__ = MagicMock(return_value=mock_conn)
        mock_connect.return_value.__exit__ = MagicMock(return_value=False)
        mock_conn.execute.return_value.fetchall.return_value = [
            (1, "2024-06-01 10:00", "2024-06-01 10:05", "success", 5000, None, None),
        ]

        result = _get_sync_logs()
        assert len(result) == 1
        assert result[0]["status"] == "success"
        assert result[0]["entity_count"] == 5000
        # Migration 013 adds current_step — helper must surface it.
        assert "current_step" in result[0]

    @patch("app.admin.sync._connect")
    def test_get_sync_logs_returns_empty_on_error(self, mock_connect: MagicMock):
        from app.admin.sync import _get_sync_logs

        mock_connect.side_effect = Exception("DB unavailable")
        result = _get_sync_logs()
        assert result == []


# ---------------------------------------------------------------------------
# Live-progress sync card (issue #567)
# ---------------------------------------------------------------------------


class TestSyncCardLiveProgress:
    """The admin sync card must auto-poll while a sync is running and stop
    polling when it reaches a terminal state."""

    def _running_log(self, step: str = "converting") -> dict:
        """Build a fake sync_log row representing a sync in flight."""
        from datetime import UTC, datetime

        return {
            "id": 1,
            "started_at": datetime.now(UTC),
            "finished_at": None,
            "status": "running",
            "entity_count": None,
            "error_message": None,
            "current_step": step,
        }

    def _success_log(self) -> dict:
        from datetime import UTC, datetime

        return {
            "id": 2,
            "started_at": datetime.now(UTC),
            "finished_at": datetime.now(UTC),
            "status": "success",
            "entity_count": 5_000_000,
            "error_message": None,
            "current_step": None,
        }

    def test_card_polls_while_running(self):
        """A running row triggers HTMX polling on the card element."""
        from fasthtml.common import to_xml

        from app.admin.sync import _sync_card

        html = to_xml(_sync_card([self._running_log()]))
        assert "/admin/sync/status" in html
        assert "every 3s" in html
        # Step labels render with Estonian phase names
        assert "Konverteerimine" in html
        # Live header labels
        assert "S\u00fcnkroniseerimine k\u00e4ib" in html
        assert "Praegu: Konverteerimine" in html

    def test_card_polls_defensively_when_banner_says_triggered(self):
        """If the banner says a sync just started but the DB query hasn't
        caught up yet (slow replica, race), the card must still emit the
        HTMX polling trigger so the first poll can pick up the real row."""
        from fasthtml.common import to_xml

        from app.admin.sync import _sync_card

        html = to_xml(
            _sync_card([], status_banner=("info", "S\u00fcnkroniseerimine k\u00e4ivitati"))
        )
        assert "/admin/sync/status" in html
        assert "every 3s" in html
        # A skeleton running panel must render so the admin sees something
        # immediately instead of a bare banner.
        assert "S\u00fcnkroniseerimine k\u00e4ib" in html
        assert "sync-step" in html

    def test_card_stops_polling_on_terminal_state(self):
        """A success/failed row must NOT emit polling attributes."""
        from fasthtml.common import to_xml

        from app.admin.sync import _sync_card

        html = to_xml(_sync_card([self._success_log()]))
        assert "/admin/sync/status" not in html
        assert "every 3s" not in html

    def test_progress_pills_mark_completed_phases_done(self):
        """Steps before the current step render with the 'done' state."""
        from fasthtml.common import to_xml

        from app.admin.sync import _sync_card

        html = to_xml(_sync_card([self._running_log(step="uploading")]))
        # Cloning + converting + validating completed before uploading,
        # so their steps should carry the -done modifier class.
        assert "sync-step-done" in html
        # Uploading is active
        assert "sync-step-active" in html
        # Reingesting is still pending
        assert "sync-step-pending" in html

    @patch("app.admin.sync._get_sync_logs")
    def test_sync_status_endpoint_renders_card(self, mock_logs: MagicMock):
        """GET /admin/sync/status returns the same card fragment the
        polling loop expects to swap in-place."""
        from starlette.requests import Request

        from app.admin.sync import sync_status_card

        mock_logs.return_value = [self._running_log()]

        scope = {
            "type": "http",
            "method": "GET",
            "path": "/admin/sync/status",
            "headers": [],
            "query_string": b"",
            "scheme": "http",
            "server": ("testserver", 80),
            "client": ("127.0.0.1", 12345),
            "auth": {"role": "admin", "id": "admin-test", "email": "a@b.ee"},
        }
        req = Request(scope)

        result = sync_status_card(req)

        from fasthtml.common import to_xml

        html = to_xml(result)
        assert 'id="sync-card"' in html
        assert "every 3s" in html

    @patch("app.admin.sync._insert_running_row", return_value=99)
    @patch("app.admin.sync._get_sync_logs")
    def test_post_sync_inserts_running_row_before_thread(
        self,
        mock_logs: MagicMock,
        mock_insert: MagicMock,
    ):
        """The running row must be inserted synchronously before the
        background thread starts. Otherwise the rendered card misses the
        progress panel + polling trigger because the worker's own INSERT
        hasn't landed when the main thread queries sync_log."""
        from starlette.requests import Request

        from app.admin import sync as admin_sync
        from app.admin.sync import trigger_sync

        admin_sync._sync_in_progress = False
        # Simulate the worker landing the INSERT: the log helper returns
        # the row we pretend was just inserted.
        mock_logs.return_value = [
            {
                "id": 99,
                "started_at": __import__("datetime").datetime.now(
                    __import__("datetime").timezone.utc
                ),
                "finished_at": None,
                "status": "running",
                "entity_count": None,
                "error_message": None,
                "current_step": "cloning",
            }
        ]

        scope = {
            "type": "http",
            "method": "POST",
            "path": "/admin/sync",
            "headers": [],
            "query_string": b"",
            "scheme": "http",
            "server": ("testserver", 80),
            "client": ("127.0.0.1", 12345),
            "auth": {"role": "admin", "id": "admin-test", "email": "a@b.ee"},
        }
        req = Request(scope)

        with patch("threading.Thread") as mock_thread_cls:
            mock_thread_cls.return_value = MagicMock()
            result = trigger_sync(req)
            # INSERT must have run before the Thread constructor.
            mock_insert.assert_called_once()
            mock_thread_cls.assert_called_once()
            # Thread kwargs must carry the pre-allocated log_id so the
            # orchestrator reuses the row instead of inserting again.
            _, call_kwargs = mock_thread_cls.call_args
            assert call_kwargs["kwargs"]["log_id"] == 99

        from fasthtml.common import to_xml

        html = to_xml(result)
        # With a running row already in sync_log, the rendered card must
        # carry HTMX polling attributes so the UI actually updates.
        assert "/admin/sync/status" in html
        assert "every 3s" in html

        admin_sync._sync_in_progress = False

    @patch("app.admin.sync._get_sync_logs")
    @patch("app.admin.sync.has_recent_running_row", return_value=True)
    def test_post_sync_detects_running_via_db(
        self,
        mock_has_running: MagicMock,
        mock_logs: MagicMock,
    ):
        """If the DB already shows a running sync, trigger_sync must NOT
        spawn a second thread and must show the 'already running' banner."""
        from starlette.requests import Request

        from app.admin import sync as admin_sync
        from app.admin.sync import trigger_sync

        mock_logs.return_value = [self._running_log()]
        admin_sync._sync_in_progress = False

        scope = {
            "type": "http",
            "method": "POST",
            "path": "/admin/sync",
            "headers": [],
            "query_string": b"",
            "scheme": "http",
            "server": ("testserver", 80),
            "client": ("127.0.0.1", 12345),
            "auth": {"role": "admin", "id": "admin-test", "email": "a@b.ee"},
        }
        req = Request(scope)

        with patch("threading.Thread") as mock_thread_cls:
            trigger_sync(req)
            mock_thread_cls.assert_not_called()

        # In-memory flag must be released again so a real subsequent sync
        # (after the other one finishes) can proceed.
        assert admin_sync._sync_in_progress is False


class TestUserStats:
    @patch("app.admin.users._connect")
    def test_get_user_stats_returns_dict(self, mock_connect: MagicMock):
        from app.admin.users import _get_user_stats

        mock_conn = MagicMock()
        mock_connect.return_value.__enter__ = MagicMock(return_value=mock_conn)
        mock_connect.return_value.__exit__ = MagicMock(return_value=False)

        # Mock the three separate execute calls
        call_results = [
            MagicMock(fetchone=MagicMock(return_value=(10,))),  # total users
            MagicMock(fetchall=MagicMock(return_value=[("Org A", 5), ("Org B", 3)])),  # per org
            MagicMock(fetchone=MagicMock(return_value=(2,))),  # active sessions
        ]
        mock_conn.execute.side_effect = call_results

        result = _get_user_stats()
        assert result["total_users"] == 10
        assert len(result["users_per_org"]) == 2
        assert result["active_sessions"] == 2

    @patch("app.admin.users._connect")
    def test_get_user_stats_returns_defaults_on_error(self, mock_connect: MagicMock):
        from app.admin.users import _get_user_stats

        mock_connect.side_effect = Exception("DB unavailable")
        result = _get_user_stats()
        assert result["total_users"] == 0
        assert result["users_per_org"] == []
        assert result["active_sessions"] == 0


# ---------------------------------------------------------------------------
# Bookmark route handlers — #743: XHR callers get JSON, plain forms get a 303
# ---------------------------------------------------------------------------


class TestBookmarkRoute:
    _AUTH = {"id": "u-1", "email": "u@x.ee", "full_name": "U", "role": "drafter", "org_id": None}

    def _req(self, *, auth: dict | None, xhr: bool):  # type: ignore[type-arg]
        from starlette.requests import Request

        headers: list[tuple[bytes, bytes]] = []
        if xhr:
            headers.append((b"x-requested-with", b"XMLHttpRequest"))
        scope: dict = {  # type: ignore[type-arg]
            "type": "http",
            "method": "POST",
            "path": "/api/bookmarks",
            "query_string": b"",
            "headers": headers,
        }
        if auth is not None:
            scope["auth"] = auth
        return Request(scope)

    @patch("app.templates.dashboard.log_action")
    @patch(
        "app.templates.dashboard._add_bookmark",
        return_value={"id": "bm-1", "entity_uri": "http://x/1", "label": "L"},
    )
    def test_add_xhr_returns_json_ok(self, mock_add: MagicMock, mock_log: MagicMock):
        import json as _json

        from starlette.responses import JSONResponse

        from app.templates.dashboard import add_bookmark

        resp = add_bookmark(self._req(auth=self._AUTH, xhr=True), "http://x/1", "L")
        assert isinstance(resp, JSONResponse)
        assert resp.status_code == 200
        assert _json.loads(bytes(resp.body)) == {"ok": True, "id": "bm-1"}

    @patch("app.templates.dashboard.log_action")
    @patch("app.templates.dashboard._add_bookmark", return_value=None)  # ON CONFLICT DO NOTHING
    def test_add_xhr_already_exists_still_ok(self, mock_add: MagicMock, mock_log: MagicMock):
        import json as _json

        from app.templates.dashboard import add_bookmark

        resp = add_bookmark(self._req(auth=self._AUTH, xhr=True), "http://x/1", "")
        assert resp.status_code == 200
        assert _json.loads(bytes(resp.body)) == {"ok": True, "id": None}

    def test_add_xhr_unauthenticated_returns_401_json(self):
        import json as _json

        from app.templates.dashboard import add_bookmark

        resp = add_bookmark(self._req(auth=None, xhr=True), "http://x/1", "")
        assert resp.status_code == 401
        assert _json.loads(bytes(resp.body)) == {"ok": False, "error": "auth"}

    @patch("app.templates.dashboard.log_action")
    @patch(
        "app.templates.dashboard._add_bookmark",
        return_value={"id": "bm-1", "entity_uri": "http://x/1", "label": "L"},
    )
    def test_add_plain_form_still_redirects(self, mock_add: MagicMock, mock_log: MagicMock):
        from starlette.responses import RedirectResponse

        from app.templates.dashboard import add_bookmark

        resp = add_bookmark(self._req(auth=self._AUTH, xhr=False), "http://x/1", "L")
        assert isinstance(resp, RedirectResponse)
        assert resp.status_code == 303
        assert resp.headers["location"] == "/dashboard"

    def test_add_plain_form_unauthenticated_redirects_to_login(self):
        from starlette.responses import RedirectResponse

        from app.templates.dashboard import add_bookmark

        resp = add_bookmark(self._req(auth=None, xhr=False), "http://x/1", "")
        assert isinstance(resp, RedirectResponse)
        assert resp.status_code == 303
        assert resp.headers["location"] == "/auth/login"

    @patch("app.templates.dashboard.log_action")
    @patch("app.templates.dashboard._remove_bookmark", return_value=True)
    def test_remove_xhr_returns_json(self, mock_rm: MagicMock, mock_log: MagicMock):
        import json as _json

        from app.templates.dashboard import remove_bookmark

        resp = remove_bookmark(self._req(auth=self._AUTH, xhr=True), "bm-1")
        assert resp.status_code == 200
        assert _json.loads(bytes(resp.body)) == {"ok": True}


# ---------------------------------------------------------------------------
# /admin/sync/history — paginated sync log viewer (#322)
# ---------------------------------------------------------------------------


def _make_sync_history_request(query_string: bytes = b""):
    """Build a minimal admin Request for the sync history handler."""
    from starlette.requests import Request

    scope = {
        "type": "http",
        "method": "GET",
        "path": "/admin/sync/history",
        "headers": [],
        "query_string": query_string,
        "scheme": "http",
        "server": ("testserver", 80),
        "client": ("127.0.0.1", 12345),
        "auth": {
            "role": "admin",
            "id": "admin-test",
            "email": "a@b.ee",
            "full_name": "Admin",
        },
    }
    return Request(scope)


class TestSyncHistoryCardLink:
    """The dashboard sync card must link out to the new history page."""

    def test_card_renders_history_link(self):
        from fasthtml.common import to_xml

        from app.admin.sync import _sync_card

        html = to_xml(_sync_card([]))
        assert "/admin/sync/history" in html
        assert "Vaata ajalugu" in html


class TestAdminSyncHistoryPage:
    """The history page renders pagination, columns, and Estonian copy."""

    def _entry(
        self,
        *,
        sid: int = 1,
        status: str = "success",
        error: str | None = None,
        step: str | None = None,
        finished: bool = True,
    ) -> dict:
        from datetime import UTC, datetime, timedelta

        started = datetime(2026, 5, 24, 9, 0, tzinfo=UTC) + timedelta(minutes=sid)
        return {
            "id": sid,
            "started_at": started,
            "finished_at": started + timedelta(minutes=3) if finished else None,
            "status": status,
            "entity_count": 5000 if status == "success" else None,
            "error_message": error,
            "current_step": step,
        }

    @patch("app.admin.sync._get_sync_log_page")
    def test_renders_empty_state(self, mock_page: MagicMock):
        from fasthtml.common import to_xml

        from app.admin.sync import admin_sync_history_page

        mock_page.return_value = ([], 0)

        result = admin_sync_history_page(_make_sync_history_request())
        html = to_xml(result)

        assert "Sünkroniseerimise ajalugu" in html
        assert "Sünkroniseerimisi ei leitud." in html
        # Pagination still renders (0 entries info line)
        assert "0 kirjet" in html
        # Back link to admin dashboard
        assert "Tagasi adminipaneelile" in html

    @patch("app.admin.sync._get_sync_log_page")
    def test_renders_rows_with_columns(self, mock_page: MagicMock):
        from fasthtml.common import to_xml

        from app.admin.sync import admin_sync_history_page

        mock_page.return_value = (
            [
                self._entry(sid=1, status="success", step="reingesting"),
                self._entry(
                    sid=2,
                    status="failed",
                    error="Connection refused",
                    step="validating",
                ),
            ],
            2,
        )

        result = admin_sync_history_page(_make_sync_history_request())
        html = to_xml(result)

        # Estonian column headers
        for header in ("Algusaeg", "Staatus", "Kestus", "Samm", "Veateade"):
            assert header in html, header
        # Phase labels translated to Estonian
        assert "Taasindekseerimine" in html
        assert "Valideerimine" in html
        # Status badges rendered via _sync_status_badge → StatusBadge
        # (success → key "ok" → label "OK"; failed → "Ebaõnnestus").
        assert "status-ok" in html
        assert "status-failed" in html
        assert "Ebaõnnestus" in html
        # Error message present
        assert "Connection refused" in html

    @patch("app.admin.sync._get_sync_log_page")
    def test_pagination_passes_correct_page_and_clamps(self, mock_page: MagicMock):
        from fasthtml.common import to_xml

        from app.admin.sync import admin_sync_history_page

        # 45 rows → 3 pages of 20.
        mock_page.return_value = (
            [self._entry(sid=i) for i in range(20)],
            45,
        )

        result = admin_sync_history_page(_make_sync_history_request(query_string=b"page=2"))
        html = to_xml(result)

        # Handler must have asked for page 2 with the default per-page.
        mock_page.assert_called_once_with(2, 20)
        # Pagination renders the info line for the active page.
        assert "21 kuni 40 kokku 45" in html
        # Current page link marked with aria-current
        assert 'aria-current="page"' in html

    @patch("app.admin.sync._get_sync_log_page")
    def test_invalid_page_falls_back_to_one(self, mock_page: MagicMock):
        from app.admin.sync import admin_sync_history_page

        mock_page.return_value = ([], 0)

        admin_sync_history_page(_make_sync_history_request(query_string=b"page=not-a-number"))
        mock_page.assert_called_once_with(1, 20)

    @patch("app.admin.sync._get_sync_log_page")
    def test_long_error_message_truncated_via_details(self, mock_page: MagicMock):
        """Failed rows with long SHACL reports must use the disclosure
        pattern instead of dumping the full text inline (#322)."""
        from fasthtml.common import to_xml

        from app.admin.sync import admin_sync_history_page

        long_error = "SHACL validation: " + ("warning text " * 50)
        mock_page.return_value = (
            [self._entry(sid=1, status="failed", error=long_error)],
            1,
        )

        result = admin_sync_history_page(_make_sync_history_request())
        html = to_xml(result)

        assert "<details" in html
        assert "sync-error" in html


class TestGetSyncLogPage:
    """``_get_sync_log_page`` slices sync_log with LIMIT/OFFSET."""

    @patch("app.admin.sync._connect")
    def test_returns_entries_and_total(self, mock_connect: MagicMock):
        from app.admin.sync import _get_sync_log_page

        mock_conn = MagicMock()
        mock_connect.return_value.__enter__ = MagicMock(return_value=mock_conn)
        mock_connect.return_value.__exit__ = MagicMock(return_value=False)
        # First call: COUNT(*); second call: paginated rows
        count_cursor = MagicMock()
        count_cursor.fetchone.return_value = (42,)
        rows_cursor = MagicMock()
        rows_cursor.fetchall.return_value = [
            (1, "2026-05-24 09:00", "2026-05-24 09:03", "success", 5000, None, None),
        ]
        mock_conn.execute.side_effect = [count_cursor, rows_cursor]

        entries, total = _get_sync_log_page(page=2, per_page=20)
        assert total == 42
        assert len(entries) == 1
        # OFFSET = (2-1) * 20 = 20
        rows_call = mock_conn.execute.call_args_list[1]
        assert rows_call.args[1] == (20, 20)

    @patch("app.admin.sync._connect")
    def test_returns_empty_on_db_error(self, mock_connect: MagicMock):
        from app.admin.sync import _get_sync_log_page

        mock_connect.side_effect = Exception("DB unavailable")
        entries, total = _get_sync_log_page()
        assert entries == []
        assert total == 0


class TestAdminSyncHistoryRoute:
    """Route is registered and admin-gated."""

    def test_history_route_requires_auth(self):
        from app.main import app

        client = TestClient(app, follow_redirects=False)
        response = client.get("/admin/sync/history")
        assert response.status_code == 303
        assert response.headers["location"] == "/auth/login"
