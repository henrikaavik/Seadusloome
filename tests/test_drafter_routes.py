"""Integration tests for the Phase 3A AI Law Drafter routes.

These tests exercise the full ``app.main.app`` via ``TestClient`` so
they validate the FastHTML wiring, the auth Beforeware, and the HTMX
partial swap behaviour. External dependencies -- Postgres, LLM -- are
mocked out.

Patterns follow ``tests/test_docs_routes.py``:
    - ``patch('app.auth.middleware._get_provider')`` to stub auth
    - ``patch('app.drafter.routes.<helper>')`` to replace DB calls
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any
from unittest.mock import MagicMock, patch

from app.drafter.session_model import DraftingSession

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_ORG_ID = "11111111-1111-1111-1111-111111111111"
_OTHER_ORG_ID = "22222222-2222-2222-2222-222222222222"
_USER_ID = "33333333-3333-3333-3333-333333333333"
_SESSION_ID = uuid.UUID("44444444-4444-4444-4444-444444444444")


def _authed_user() -> dict[str, Any]:
    return {
        "id": _USER_ID,
        "email": "koostaja@seadusloome.ee",
        "full_name": "Test Koostaja",
        "role": "drafter",
        "org_id": _ORG_ID,
    }


def _make_session(
    *,
    session_id: uuid.UUID = _SESSION_ID,
    org_id: str = _ORG_ID,
    workflow_type: str = "full_law",
    current_step: int = 1,
    intent: str | None = None,
    status: str = "active",
) -> DraftingSession:
    now = datetime.now(UTC)
    return DraftingSession(
        id=session_id,
        user_id=uuid.UUID(_USER_ID),
        org_id=uuid.UUID(org_id),
        workflow_type=workflow_type,
        current_step=current_step,
        intent=intent,
        clarifications=[],
        research_data_encrypted=None,
        proposed_structure=None,
        draft_content_encrypted=None,
        integrated_draft_id=None,
        status=status,
        created_at=now,
        updated_at=now,
    )


def _stub_provider() -> MagicMock:
    """Build a provider whose ``get_current_user`` returns ``_authed_user``."""
    provider = MagicMock()
    provider.get_current_user.return_value = _authed_user()
    return provider


def _authed_client():
    """Return a TestClient with a valid ``access_token`` cookie."""
    from starlette.testclient import TestClient

    client = TestClient(
        __import__("app.main", fromlist=["app"]).app,
        follow_redirects=False,
    )
    client.cookies.set("access_token", "stub-token")
    return client


# ---------------------------------------------------------------------------
# Unauthenticated requests redirect to login
# ---------------------------------------------------------------------------


class TestAuthRequired:
    def test_drafter_list_redirects_unauthenticated(self):
        from starlette.testclient import TestClient

        from app.main import app

        client = TestClient(app, follow_redirects=False)
        resp = client.get("/drafter")
        assert resp.status_code == 303
        assert resp.headers["location"] == "/auth/login"

    def test_drafter_new_redirects_unauthenticated(self):
        from starlette.testclient import TestClient

        from app.main import app

        client = TestClient(app, follow_redirects=False)
        resp = client.get("/drafter/new")
        assert resp.status_code == 303
        assert resp.headers["location"] == "/auth/login"

    def test_drafter_session_redirects_unauthenticated(self):
        from starlette.testclient import TestClient

        from app.main import app

        client = TestClient(app, follow_redirects=False)
        resp = client.get(f"/drafter/{_SESSION_ID}")
        assert resp.status_code == 303
        assert resp.headers["location"] == "/auth/login"


# ---------------------------------------------------------------------------
# GET /drafter -- session list
# ---------------------------------------------------------------------------


class TestDrafterList:
    @patch("app.drafter.routes.count_sessions_for_user_conn")
    @patch("app.drafter.routes.fetch_sessions_for_user")
    @patch("app.auth.middleware._get_provider")
    def test_session_list_empty_state(
        self,
        mock_get_provider: MagicMock,
        mock_fetch: MagicMock,
        mock_count: MagicMock,
    ):
        mock_get_provider.return_value = _stub_provider()
        mock_fetch.return_value = []
        mock_count.return_value = 0

        client = _authed_client()
        resp = client.get("/drafter")

        assert resp.status_code == 200
        assert "AI koostaja" in resp.text
        assert "Alusta uut koostamist" in resp.text

    @patch("app.drafter.routes.count_sessions_for_user_conn")
    @patch("app.drafter.routes.fetch_sessions_for_user")
    @patch("app.auth.middleware._get_provider")
    def test_session_list_with_sessions(
        self,
        mock_get_provider: MagicMock,
        mock_fetch: MagicMock,
        mock_count: MagicMock,
    ):
        mock_get_provider.return_value = _stub_provider()
        session = _make_session()
        mock_fetch.return_value = [session]
        mock_count.return_value = 1

        client = _authed_client()
        resp = client.get("/drafter")

        assert resp.status_code == 200
        assert "Kavatsus" in resp.text  # step label
        assert str(_SESSION_ID) in resp.text


# ---------------------------------------------------------------------------
# GET /drafter/new -- workflow selection
# ---------------------------------------------------------------------------


class TestNewSessionPage:
    @patch("app.auth.middleware._get_provider")
    def test_workflow_selection_renders_radio_buttons(
        self,
        mock_get_provider: MagicMock,
    ):
        mock_get_provider.return_value = _stub_provider()

        client = _authed_client()
        resp = client.get("/drafter/new")

        assert resp.status_code == 200
        assert 'type="radio"' in resp.text
        assert 'value="full_law"' in resp.text
        assert 'value="vtk"' in resp.text


# ---------------------------------------------------------------------------
# POST /drafter/new -- create session
# ---------------------------------------------------------------------------


class TestCreateSessionHandler:
    @patch("app.drafter.routes.log_action")
    @patch("app.drafter.routes._connect")
    @patch("app.drafter.routes.require_real_llm")
    @patch("app.auth.middleware._get_provider")
    def test_create_session_with_stub_claude_shows_alert(
        self,
        mock_get_provider: MagicMock,
        mock_guard: MagicMock,
        mock_connect: MagicMock,
        mock_log: MagicMock,
    ):
        """When Claude is stubbed, the guard fires and an Alert is shown."""
        from app.drafter.errors import DrafterNotAvailableError

        mock_get_provider.return_value = _stub_provider()
        mock_guard.side_effect = DrafterNotAvailableError("ANTHROPIC_API_KEY puudub")

        client = _authed_client()
        resp = client.post(
            "/drafter/new",
            data={"workflow_type": "full_law"},
        )

        assert resp.status_code == 200
        assert "ANTHROPIC_API_KEY" in resp.text
        mock_connect.assert_not_called()
        mock_log.assert_not_called()

    @patch("app.drafter.routes.log_action")
    @patch("app.drafter.routes._connect")
    @patch("app.drafter.routes.require_real_llm")
    @patch("app.auth.middleware._get_provider")
    def test_create_session_success_redirects_to_step_1(
        self,
        mock_get_provider: MagicMock,
        mock_guard: MagicMock,
        mock_connect: MagicMock,
        mock_log: MagicMock,
    ):
        """With a real Claude, session creation redirects to step 1."""
        mock_get_provider.return_value = _stub_provider()
        mock_guard.return_value = None  # Guard passes

        session = _make_session()
        mock_conn = MagicMock()
        mock_connect.return_value.__enter__ = MagicMock(return_value=mock_conn)
        mock_connect.return_value.__exit__ = MagicMock(return_value=False)

        # Patch create_session to return our fake session
        with patch("app.drafter.routes.create_session", return_value=session):
            client = _authed_client()
            resp = client.post(
                "/drafter/new",
                data={"workflow_type": "full_law"},
            )

        assert resp.status_code == 303
        assert f"/drafter/{session.id}/step/1" in resp.headers["location"]
        mock_log.assert_called_once()
        log_call = mock_log.call_args
        assert log_call.args[1] == "drafter.session.create"


# ---------------------------------------------------------------------------
# GET /drafter/{session_id} -- redirect to current step
# ---------------------------------------------------------------------------


class TestSessionRedirect:
    @patch("app.drafter.routes.fetch_session")
    @patch("app.auth.middleware._get_provider")
    def test_redirect_to_current_step(
        self,
        mock_get_provider: MagicMock,
        mock_fetch: MagicMock,
    ):
        mock_get_provider.return_value = _stub_provider()
        session = _make_session(current_step=3)
        mock_fetch.return_value = session

        client = _authed_client()
        resp = client.get(f"/drafter/{_SESSION_ID}")

        assert resp.status_code == 303
        assert f"/drafter/{_SESSION_ID}/step/3" in resp.headers["location"]


# ---------------------------------------------------------------------------
# GET /drafter/{session_id}/step/{n} -- step pages
# ---------------------------------------------------------------------------


class TestStepPage:
    @patch("app.drafter.routes.fetch_session")
    @patch("app.auth.middleware._get_provider")
    def test_step_1_renders_intent_form(
        self,
        mock_get_provider: MagicMock,
        mock_fetch: MagicMock,
    ):
        mock_get_provider.return_value = _stub_provider()
        session = _make_session(current_step=1)
        mock_fetch.return_value = session

        client = _authed_client()
        resp = client.get(f"/drafter/{_SESSION_ID}/step/1")

        assert resp.status_code == 200
        assert 'name="intent"' in resp.text
        assert "Kavatsus" in resp.text

    @patch("app.drafter.routes.fetch_session")
    @patch("app.auth.middleware._get_provider")
    def test_step_2_renders_placeholder(
        self,
        mock_get_provider: MagicMock,
        mock_fetch: MagicMock,
    ):
        mock_get_provider.return_value = _stub_provider()
        session = _make_session(current_step=2)
        mock_fetch.return_value = session

        client = _authed_client()
        resp = client.get(f"/drafter/{_SESSION_ID}/step/2")

        assert resp.status_code == 200
        assert "Tapsustamine" in resp.text
        assert "jargmises arendusetapis" in resp.text

    @patch("app.drafter.routes.fetch_session")
    @patch("app.auth.middleware._get_provider")
    def test_nonexistent_session_returns_404(
        self,
        mock_get_provider: MagicMock,
        mock_fetch: MagicMock,
    ):
        mock_get_provider.return_value = _stub_provider()
        mock_fetch.return_value = None

        client = _authed_client()
        resp = client.get(f"/drafter/{_SESSION_ID}/step/1")

        assert resp.status_code == 200
        assert "ei leitud" in resp.text

    @patch("app.drafter.routes.fetch_session")
    @patch("app.auth.middleware._get_provider")
    def test_cross_org_session_returns_404(
        self,
        mock_get_provider: MagicMock,
        mock_fetch: MagicMock,
    ):
        mock_get_provider.return_value = _stub_provider()
        foreign = _make_session(org_id=_OTHER_ORG_ID)
        mock_fetch.return_value = foreign

        client = _authed_client()
        resp = client.get(f"/drafter/{_SESSION_ID}/step/1")

        assert resp.status_code == 200
        assert "ei leitud" in resp.text


# ---------------------------------------------------------------------------
# POST /drafter/{session_id}/step/1 -- submit intent
# ---------------------------------------------------------------------------


class TestSubmitIntent:
    @patch("app.drafter.routes.log_action")
    @patch("app.drafter.routes._connect")
    @patch("app.drafter.routes.fetch_session")
    @patch("app.auth.middleware._get_provider")
    def test_valid_intent_redirects_to_step_2(
        self,
        mock_get_provider: MagicMock,
        mock_fetch: MagicMock,
        mock_connect: MagicMock,
        mock_log: MagicMock,
    ):
        mock_get_provider.return_value = _stub_provider()
        session = _make_session(current_step=1)
        mock_fetch.return_value = session

        mock_conn = MagicMock()
        mock_connect.return_value.__enter__ = MagicMock(return_value=mock_conn)
        mock_connect.return_value.__exit__ = MagicMock(return_value=False)

        # After update_session, get_session returns the updated session
        updated = _make_session(current_step=1, intent="Test kavatsus")
        with (
            patch("app.drafter.routes.update_session"),
            patch("app.drafter.routes.get_session", return_value=updated),
            patch("app.drafter.routes.advance_step", return_value=2),
        ):
            client = _authed_client()
            resp = client.post(
                f"/drafter/{_SESSION_ID}/step/1",
                data={"intent": "Test kavatsus"},
            )

        assert resp.status_code == 303
        assert f"/drafter/{_SESSION_ID}/step/2" in resp.headers["location"]
        mock_log.assert_called_once()
        assert mock_log.call_args.args[1] == "drafter.step.advance"

    @patch("app.drafter.routes.fetch_session")
    @patch("app.auth.middleware._get_provider")
    def test_empty_intent_rerenders_with_error(
        self,
        mock_get_provider: MagicMock,
        mock_fetch: MagicMock,
    ):
        mock_get_provider.return_value = _stub_provider()
        session = _make_session(current_step=1)
        mock_fetch.return_value = session

        client = _authed_client()
        resp = client.post(
            f"/drafter/{_SESSION_ID}/step/1",
            data={"intent": ""},
        )

        assert resp.status_code == 200
        assert "kohustuslik" in resp.text

    @patch("app.drafter.routes.fetch_session")
    @patch("app.auth.middleware._get_provider")
    def test_too_long_intent_rerenders_with_error(
        self,
        mock_get_provider: MagicMock,
        mock_fetch: MagicMock,
    ):
        mock_get_provider.return_value = _stub_provider()
        session = _make_session(current_step=1)
        mock_fetch.return_value = session

        client = _authed_client()
        resp = client.post(
            f"/drafter/{_SESSION_ID}/step/1",
            data={"intent": "x" * 2001},
        )

        assert resp.status_code == 200
        assert "liiga pikk" in resp.text


# ---------------------------------------------------------------------------
# GET /drafter/{session_id}/step/{n}/status -- HTMX polling
# ---------------------------------------------------------------------------


class TestStepStatusFragment:
    @patch("app.drafter.routes.fetch_session")
    @patch("app.auth.middleware._get_provider")
    def test_status_fragment_returns_polling_div(
        self,
        mock_get_provider: MagicMock,
        mock_fetch: MagicMock,
    ):
        mock_get_provider.return_value = _stub_provider()
        session = _make_session(current_step=2)
        mock_fetch.return_value = session

        client = _authed_client()
        resp = client.get(
            f"/drafter/{_SESSION_ID}/step/2/status",
            headers={"HX-Request": "true"},
        )

        assert resp.status_code == 200
        assert "Ootamine" in resp.text
        assert "every 3s" in resp.text
