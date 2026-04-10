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

    @patch("app.drafter.routes._find_latest_job")
    @patch("app.drafter.routes.fetch_session")
    @patch("app.auth.middleware._get_provider")
    def test_step_2_shows_waiting_when_job_running(
        self,
        mock_get_provider: MagicMock,
        mock_fetch: MagicMock,
        mock_find_job: MagicMock,
    ):
        mock_get_provider.return_value = _stub_provider()
        session = _make_session(current_step=2)
        mock_fetch.return_value = session
        mock_find_job.return_value = {"status": "running", "result": None, "error_message": None}

        client = _authed_client()
        resp = client.get(f"/drafter/{_SESSION_ID}/step/2")

        assert resp.status_code == 200
        assert "Kusimuste genereerimine" in resp.text

    @patch("app.drafter.routes._find_latest_job")
    @patch("app.drafter.routes.fetch_session")
    @patch("app.auth.middleware._get_provider")
    def test_step_2_renders_qa_form_with_clarifications(
        self,
        mock_get_provider: MagicMock,
        mock_fetch: MagicMock,
        mock_find_job: MagicMock,
    ):
        mock_get_provider.return_value = _stub_provider()
        session = _make_session(current_step=2)
        session.clarifications = [
            {"question": "Milliseid asutusi see mojutab?", "answer": None, "rationale": "scope"},
            {"question": "Kas see on EL-iga seotud?", "answer": None, "rationale": "EU"},
            {"question": "Mis on ulemine periood?", "answer": None, "rationale": "timing"},
        ]
        mock_fetch.return_value = session
        mock_find_job.return_value = {"status": "success", "result": {}, "error_message": None}

        client = _authed_client()
        resp = client.get(f"/drafter/{_SESSION_ID}/step/2")

        assert resp.status_code == 200
        assert "Milliseid asutusi" in resp.text
        assert 'name="answer"' in resp.text

    @patch("app.drafter.routes._find_latest_job")
    @patch("app.drafter.routes.fetch_session")
    @patch("app.auth.middleware._get_provider")
    def test_step_2_shows_advance_when_all_answered(
        self,
        mock_get_provider: MagicMock,
        mock_fetch: MagicMock,
        mock_find_job: MagicMock,
    ):
        mock_get_provider.return_value = _stub_provider()
        session = _make_session(current_step=2)
        session.clarifications = [
            {"question": "Q1?", "answer": "A1"},
            {"question": "Q2?", "answer": "A2"},
            {"question": "Q3?", "answer": "A3"},
        ]
        mock_fetch.return_value = session
        mock_find_job.return_value = {"status": "success", "result": {}, "error_message": None}

        client = _authed_client()
        resp = client.get(f"/drafter/{_SESSION_ID}/step/2")

        assert resp.status_code == 200
        assert "Jatka uurimisega" in resp.text

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
    @patch("app.drafter.routes.JobQueue")
    @patch("app.drafter.routes.log_drafter_step_advance")
    @patch("app.drafter.routes._connect")
    @patch("app.drafter.routes.fetch_session")
    @patch("app.auth.middleware._get_provider")
    def test_valid_intent_redirects_to_step_2(
        self,
        mock_get_provider: MagicMock,
        mock_fetch: MagicMock,
        mock_connect: MagicMock,
        mock_log: MagicMock,
        mock_queue_cls: MagicMock,
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
        # Verify drafter_clarify job was enqueued
        mock_queue_cls.return_value.enqueue.assert_called_once()

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
    @patch("app.drafter.routes._find_latest_job")
    @patch("app.drafter.routes.fetch_session")
    @patch("app.auth.middleware._get_provider")
    def test_status_fragment_returns_polling_div_when_running(
        self,
        mock_get_provider: MagicMock,
        mock_fetch: MagicMock,
        mock_find_job: MagicMock,
    ):
        mock_get_provider.return_value = _stub_provider()
        session = _make_session(current_step=2)
        mock_fetch.return_value = session
        mock_find_job.return_value = {"status": "running", "result": None, "error_message": None}

        client = _authed_client()
        resp = client.get(
            f"/drafter/{_SESSION_ID}/step/2/status",
            headers={"HX-Request": "true"},
        )

        assert resp.status_code == 200
        assert "every 3s" in resp.text

    @patch("app.drafter.routes._find_latest_job")
    @patch("app.drafter.routes.fetch_session")
    @patch("app.auth.middleware._get_provider")
    def test_status_fragment_redirects_on_success(
        self,
        mock_get_provider: MagicMock,
        mock_fetch: MagicMock,
        mock_find_job: MagicMock,
    ):
        mock_get_provider.return_value = _stub_provider()
        session = _make_session(current_step=2)
        mock_fetch.return_value = session
        mock_find_job.return_value = {"status": "success", "result": {}, "error_message": None}

        client = _authed_client()
        resp = client.get(
            f"/drafter/{_SESSION_ID}/step/2/status",
            headers={"HX-Request": "true"},
        )

        assert resp.status_code == 200
        assert "HX-Redirect" in resp.headers


# ---------------------------------------------------------------------------
# Step 3: Research results
# ---------------------------------------------------------------------------


class TestStep3Page:
    @patch("app.drafter.routes._find_latest_job")
    @patch("app.drafter.routes.decrypt_text")
    @patch("app.drafter.routes.fetch_session")
    @patch("app.auth.middleware._get_provider")
    def test_step_3_renders_research_results(
        self,
        mock_get_provider: MagicMock,
        mock_fetch: MagicMock,
        mock_decrypt: MagicMock,
        mock_find_job: MagicMock,
    ):
        import json

        mock_get_provider.return_value = _stub_provider()
        session = _make_session(current_step=3)
        session.research_data_encrypted = b"encrypted"
        mock_fetch.return_value = session
        mock_decrypt.return_value = json.dumps(
            {
                "provisions": [
                    {"uri": "uri:1", "label": "TsiviilS par 1", "act_label": "TsiviilS"},
                ],
                "eu_directives": [],
                "court_decisions": [],
                "topic_clusters": [],
            }
        )

        client = _authed_client()
        resp = client.get(f"/drafter/{_SESSION_ID}/step/3")

        assert resp.status_code == 200
        assert "TsiviilS" in resp.text
        assert "Jatka struktuuriga" in resp.text


# ---------------------------------------------------------------------------
# Step 4: Structure editing
# ---------------------------------------------------------------------------


class TestStep4Page:
    @patch("app.drafter.routes._find_latest_job")
    @patch("app.drafter.routes.fetch_session")
    @patch("app.auth.middleware._get_provider")
    def test_step_4_renders_editable_tree(
        self,
        mock_get_provider: MagicMock,
        mock_fetch: MagicMock,
        mock_find_job: MagicMock,
    ):
        mock_get_provider.return_value = _stub_provider()
        session = _make_session(current_step=4)
        session.proposed_structure = {
            "title": "Test seadus",
            "chapters": [
                {
                    "number": "1. peatukk",
                    "title": "Uldsatted",
                    "sections": [
                        {"paragraph": "par 1", "title": "Reguleerimisala"},
                    ],
                }
            ],
        }
        mock_fetch.return_value = session

        client = _authed_client()
        resp = client.get(f"/drafter/{_SESSION_ID}/step/4")

        assert resp.status_code == 200
        assert 'name="law_title"' in resp.text
        assert "Uldsatted" in resp.text
        assert "Salvesta" in resp.text


# ---------------------------------------------------------------------------
# Step 5: Clauses
# ---------------------------------------------------------------------------


class TestStep5Page:
    @patch("app.drafter.routes._find_latest_job")
    @patch("app.drafter.routes.decrypt_text")
    @patch("app.drafter.routes.fetch_session")
    @patch("app.auth.middleware._get_provider")
    def test_step_5_renders_clauses_with_citations(
        self,
        mock_get_provider: MagicMock,
        mock_fetch: MagicMock,
        mock_decrypt: MagicMock,
        mock_find_job: MagicMock,
    ):
        import json

        mock_get_provider.return_value = _stub_provider()
        session = _make_session(current_step=5)
        session.draft_content_encrypted = b"encrypted"
        mock_fetch.return_value = session
        mock_decrypt.return_value = json.dumps(
            {
                "clauses": [
                    {
                        "chapter": "1",
                        "chapter_title": "Uldsatted",
                        "paragraph": "par 1",
                        "title": "Reguleerimisala",
                        "text": "Kaesolev seadus satestab...",
                        "citations": ["estleg:TsiviilS/par/1"],
                        "notes": "Test note",
                    }
                ]
            }
        )

        client = _authed_client()
        resp = client.get(f"/drafter/{_SESSION_ID}/step/5")

        assert resp.status_code == 200
        assert "Kaesolev seadus" in resp.text
        assert "estleg:TsiviilS" in resp.text
        assert "Muuda" in resp.text
        assert "Genereeri uuesti" in resp.text


# ---------------------------------------------------------------------------
# Step 6: Review
# ---------------------------------------------------------------------------


class TestStep6Page:
    @patch("app.drafter.routes.fetch_session")
    @patch("app.auth.middleware._get_provider")
    def test_step_6_shows_trigger_button_when_no_draft(
        self,
        mock_get_provider: MagicMock,
        mock_fetch: MagicMock,
    ):
        mock_get_provider.return_value = _stub_provider()
        session = _make_session(current_step=6)
        session.integrated_draft_id = None
        mock_fetch.return_value = session

        client = _authed_client()
        resp = client.get(f"/drafter/{_SESSION_ID}/step/6")

        assert resp.status_code == 200
        assert "Kaivita mojuanaluus" in resp.text

    @patch("app.drafter.routes.fetch_session")
    @patch("app.auth.middleware._get_provider")
    def test_step_6_shows_report_link_when_draft_linked(
        self,
        mock_get_provider: MagicMock,
        mock_fetch: MagicMock,
    ):
        mock_get_provider.return_value = _stub_provider()
        draft_id = uuid.UUID("55555555-5555-5555-5555-555555555555")
        session = _make_session(current_step=6)
        session.integrated_draft_id = draft_id
        mock_fetch.return_value = session

        client = _authed_client()
        resp = client.get(f"/drafter/{_SESSION_ID}/step/6")

        assert resp.status_code == 200
        assert "Vaata mojuanaluusi" in resp.text
        assert str(draft_id) in resp.text


# ---------------------------------------------------------------------------
# Step 7: Export
# ---------------------------------------------------------------------------


class TestStep7Page:
    @patch("app.drafter.routes.fetch_session")
    @patch("app.auth.middleware._get_provider")
    def test_step_7_shows_download_link(
        self,
        mock_get_provider: MagicMock,
        mock_fetch: MagicMock,
    ):
        mock_get_provider.return_value = _stub_provider()
        session = _make_session(current_step=7)
        mock_fetch.return_value = session

        client = _authed_client()
        resp = client.get(f"/drafter/{_SESSION_ID}/step/7")

        assert resp.status_code == 200
        assert "Laadi alla .docx" in resp.text
        assert "/export" in resp.text


# ---------------------------------------------------------------------------
# #505 — Step boundary guard
# ---------------------------------------------------------------------------


class TestStepBoundaryGuard:
    @patch("app.drafter.routes.fetch_session")
    @patch("app.auth.middleware._get_provider")
    def test_future_step_redirects_to_current(
        self,
        mock_get_provider: MagicMock,
        mock_fetch: MagicMock,
    ):
        """Requesting step 5 when session is at step 2 should redirect to step 2."""
        mock_get_provider.return_value = _stub_provider()
        session = _make_session(current_step=2)
        mock_fetch.return_value = session

        client = _authed_client()
        resp = client.get(f"/drafter/{_SESSION_ID}/step/5")

        assert resp.status_code == 303
        assert f"/drafter/{_SESSION_ID}/step/2" in resp.headers["location"]

    @patch("app.drafter.routes._find_latest_job")
    @patch("app.drafter.routes.fetch_session")
    @patch("app.auth.middleware._get_provider")
    def test_previous_step_allowed(
        self,
        mock_get_provider: MagicMock,
        mock_fetch: MagicMock,
        mock_find_job: MagicMock,
    ):
        """Viewing a previous step (e.g., step 1 when at step 3) is allowed."""
        mock_get_provider.return_value = _stub_provider()
        session = _make_session(current_step=3, intent="Test intent")
        mock_fetch.return_value = session

        client = _authed_client()
        resp = client.get(f"/drafter/{_SESSION_ID}/step/1")

        assert resp.status_code == 200
        assert "Kavatsus" in resp.text

    @patch("app.drafter.routes.fetch_session")
    @patch("app.auth.middleware._get_provider")
    def test_current_step_allowed(
        self,
        mock_get_provider: MagicMock,
        mock_fetch: MagicMock,
    ):
        """Viewing the current step is allowed."""
        mock_get_provider.return_value = _stub_provider()
        session = _make_session(current_step=1)
        mock_fetch.return_value = session

        client = _authed_client()
        resp = client.get(f"/drafter/{_SESSION_ID}/step/1")

        assert resp.status_code == 200


# ---------------------------------------------------------------------------
# #508 — POST handler tests
# ---------------------------------------------------------------------------


class TestPostStep2Answer:
    """POST step 2 answer submission -- saves answer, re-renders."""

    @patch("app.drafter.routes._find_latest_job")
    @patch("app.drafter.routes._connect")
    @patch("app.drafter.routes.fetch_session")
    @patch("app.auth.middleware._get_provider")
    def test_submit_answer_saves_and_rerenders(
        self,
        mock_get_provider: MagicMock,
        mock_fetch: MagicMock,
        mock_connect: MagicMock,
        mock_find_job: MagicMock,
    ):
        mock_get_provider.return_value = _stub_provider()
        session = _make_session(current_step=2)
        session.clarifications = [
            {"question": "Q1?", "answer": None, "rationale": "scope"},
            {"question": "Q2?", "answer": None, "rationale": "EU"},
            {"question": "Q3?", "answer": None, "rationale": "timing"},
        ]
        # First fetch for auth, second after save
        mock_fetch.side_effect = [session, session]
        mock_find_job.return_value = {"status": "success", "result": {}, "error_message": None}

        mock_conn = MagicMock()
        mock_connect.return_value.__enter__ = MagicMock(return_value=mock_conn)
        mock_connect.return_value.__exit__ = MagicMock(return_value=False)

        with patch("app.drafter.routes.update_session"):
            client = _authed_client()
            resp = client.post(
                f"/drafter/{_SESSION_ID}/step/2",
                data={"answer": "Vastus", "question_index": "0"},
            )

        assert resp.status_code == 200


class TestPostStep2Advance:
    """POST step 2 advance -- enqueues drafter_research job."""

    @patch("app.drafter.routes.JobQueue")
    @patch("app.drafter.routes.log_drafter_step_advance")
    @patch("app.drafter.routes._connect")
    @patch("app.drafter.routes.fetch_session")
    @patch("app.auth.middleware._get_provider")
    def test_advance_enqueues_research_job(
        self,
        mock_get_provider: MagicMock,
        mock_fetch: MagicMock,
        mock_connect: MagicMock,
        mock_log: MagicMock,
        mock_queue_cls: MagicMock,
    ):
        mock_get_provider.return_value = _stub_provider()
        session = _make_session(current_step=2)
        session.clarifications = [
            {"question": "Q1?", "answer": "A1"},
            {"question": "Q2?", "answer": "A2"},
            {"question": "Q3?", "answer": "A3"},
        ]
        mock_fetch.return_value = session

        mock_conn = MagicMock()
        mock_connect.return_value.__enter__ = MagicMock(return_value=mock_conn)
        mock_connect.return_value.__exit__ = MagicMock(return_value=False)

        with (
            patch("app.drafter.routes.get_session", return_value=session),
            patch("app.drafter.routes.advance_step", return_value=3),
        ):
            client = _authed_client()
            resp = client.post(
                f"/drafter/{_SESSION_ID}/step/2",
                data={"action": "advance"},
            )

        assert resp.status_code == 303
        assert f"/drafter/{_SESSION_ID}/step/3" in resp.headers["location"]
        mock_queue_cls.return_value.enqueue.assert_called_once()
        enqueue_args = mock_queue_cls.return_value.enqueue.call_args
        assert enqueue_args.args[0] == "drafter_research"
        assert enqueue_args.args[1]["session_id"] == str(_SESSION_ID)


class TestPostStep3Advance:
    """POST step 3 advance -- enqueues drafter_structure job."""

    @patch("app.drafter.routes.JobQueue")
    @patch("app.drafter.routes.log_drafter_step_advance")
    @patch("app.drafter.routes._connect")
    @patch("app.drafter.routes.fetch_session")
    @patch("app.auth.middleware._get_provider")
    def test_advance_enqueues_structure_job(
        self,
        mock_get_provider: MagicMock,
        mock_fetch: MagicMock,
        mock_connect: MagicMock,
        mock_log: MagicMock,
        mock_queue_cls: MagicMock,
    ):
        mock_get_provider.return_value = _stub_provider()
        session = _make_session(current_step=3)
        session.research_data_encrypted = b"encrypted"
        mock_fetch.return_value = session

        mock_conn = MagicMock()
        mock_connect.return_value.__enter__ = MagicMock(return_value=mock_conn)
        mock_connect.return_value.__exit__ = MagicMock(return_value=False)

        with (
            patch("app.drafter.routes.get_session", return_value=session),
            patch("app.drafter.routes.advance_step", return_value=4),
        ):
            client = _authed_client()
            resp = client.post(
                f"/drafter/{_SESSION_ID}/step/3",
                data={"action": "advance"},
            )

        assert resp.status_code == 303
        assert f"/drafter/{_SESSION_ID}/step/4" in resp.headers["location"]
        mock_queue_cls.return_value.enqueue.assert_called_once()
        enqueue_args = mock_queue_cls.return_value.enqueue.call_args
        assert enqueue_args.args[0] == "drafter_structure"
        assert enqueue_args.args[1]["session_id"] == str(_SESSION_ID)


class TestPostStep4Submit:
    """POST step 4 submit structure -- saves, enqueues drafter_draft job."""

    @patch("app.drafter.routes.JobQueue")
    @patch("app.drafter.routes.log_drafter_step_advance")
    @patch("app.drafter.routes._connect")
    @patch("app.drafter.routes.fetch_session")
    @patch("app.auth.middleware._get_provider")
    def test_submit_structure_saves_and_enqueues_draft_job(
        self,
        mock_get_provider: MagicMock,
        mock_fetch: MagicMock,
        mock_connect: MagicMock,
        mock_log: MagicMock,
        mock_queue_cls: MagicMock,
    ):
        mock_get_provider.return_value = _stub_provider()
        session = _make_session(current_step=4)
        session.proposed_structure = {
            "title": "Test",
            "chapters": [
                {
                    "number": "1",
                    "title": "Uldsatted",
                    "sections": [{"paragraph": "par 1", "title": "Test"}],
                }
            ],
        }
        mock_fetch.return_value = session

        mock_conn = MagicMock()
        mock_connect.return_value.__enter__ = MagicMock(return_value=mock_conn)
        mock_connect.return_value.__exit__ = MagicMock(return_value=False)

        updated_session = _make_session(current_step=4)
        updated_session.proposed_structure = session.proposed_structure

        with (
            patch("app.drafter.routes.update_session"),
            patch("app.drafter.routes.get_session", return_value=updated_session),
            patch("app.drafter.routes.advance_step", return_value=5),
        ):
            client = _authed_client()
            resp = client.post(
                f"/drafter/{_SESSION_ID}/step/4",
                data={
                    "law_title": "Test seadus",
                    "chapter_count": "1",
                    "chapter_0_number": "1",
                    "chapter_0_title": "Uldsatted",
                    "chapter_0_section_count": "1",
                    "chapter_0_section_0_paragraph": "par 1",
                    "chapter_0_section_0_title": "Test",
                },
            )

        assert resp.status_code == 303
        assert f"/drafter/{_SESSION_ID}/step/5" in resp.headers["location"]
        mock_queue_cls.return_value.enqueue.assert_called_once()
        enqueue_args = mock_queue_cls.return_value.enqueue.call_args
        assert enqueue_args.args[0] == "drafter_draft"


class TestPostStep5Advance:
    """POST step 5 advance -- triggers step advance to review."""

    @patch("app.drafter.routes.log_drafter_step_advance")
    @patch("app.drafter.routes._connect")
    @patch("app.drafter.routes.fetch_session")
    @patch("app.auth.middleware._get_provider")
    def test_advance_from_step_5_to_step_6(
        self,
        mock_get_provider: MagicMock,
        mock_fetch: MagicMock,
        mock_connect: MagicMock,
        mock_log: MagicMock,
    ):
        mock_get_provider.return_value = _stub_provider()
        session = _make_session(current_step=5)
        session.draft_content_encrypted = b"encrypted"
        mock_fetch.return_value = session

        mock_conn = MagicMock()
        mock_connect.return_value.__enter__ = MagicMock(return_value=mock_conn)
        mock_connect.return_value.__exit__ = MagicMock(return_value=False)

        with (
            patch("app.drafter.routes.get_session", return_value=session),
            patch("app.drafter.routes.advance_step", return_value=6),
        ):
            client = _authed_client()
            resp = client.post(
                f"/drafter/{_SESSION_ID}/step/5",
                data={"action": "advance"},
            )

        assert resp.status_code == 303
        assert f"/drafter/{_SESSION_ID}/step/6" in resp.headers["location"]
        mock_log.assert_called_once()


class TestPostStep5SaveClause:
    """POST step 5 save-clause -- updates clause text."""

    @patch("app.drafter.routes.log_drafter_clause_edit")
    @patch("app.drafter.routes.encrypt_text")
    @patch("app.drafter.routes._connect")
    @patch("app.drafter.routes.decrypt_text")
    @patch("app.drafter.routes.fetch_session")
    @patch("app.auth.middleware._get_provider")
    def test_save_clause_updates_text(
        self,
        mock_get_provider: MagicMock,
        mock_fetch: MagicMock,
        mock_decrypt: MagicMock,
        mock_connect: MagicMock,
        mock_encrypt: MagicMock,
        mock_log: MagicMock,
    ):
        import json

        mock_get_provider.return_value = _stub_provider()
        session = _make_session(current_step=5)
        session.draft_content_encrypted = b"encrypted"
        mock_fetch.return_value = session
        mock_decrypt.return_value = json.dumps(
            {
                "clauses": [
                    {
                        "chapter": "1",
                        "chapter_title": "Test",
                        "paragraph": "par 1",
                        "title": "Test",
                        "text": "Old text",
                        "citations": [],
                        "notes": "",
                    }
                ]
            }
        )
        mock_encrypt.return_value = b"new-encrypted"

        mock_conn = MagicMock()
        mock_connect.return_value.__enter__ = MagicMock(return_value=mock_conn)
        mock_connect.return_value.__exit__ = MagicMock(return_value=False)

        with patch("app.drafter.routes.update_session"):
            client = _authed_client()
            resp = client.post(
                f"/drafter/{_SESSION_ID}/step/5/save-clause",
                data={"text": "Updated text", "clause_index": "0"},
            )

        assert resp.status_code == 303
        mock_log.assert_called_once()


class TestPostStep5Regenerate:
    """POST step 5 regenerate -- enqueues regenerate job (after #506 fix)."""

    @patch("app.drafter.routes.log_drafter_regenerate")
    @patch("app.drafter.routes.JobQueue")
    @patch("app.drafter.routes.decrypt_text")
    @patch("app.drafter.routes.fetch_session")
    @patch("app.auth.middleware._get_provider")
    def test_regenerate_enqueues_background_job(
        self,
        mock_get_provider: MagicMock,
        mock_fetch: MagicMock,
        mock_decrypt: MagicMock,
        mock_queue_cls: MagicMock,
        mock_log: MagicMock,
    ):
        import json

        mock_get_provider.return_value = _stub_provider()
        session = _make_session(current_step=5)
        session.draft_content_encrypted = b"encrypted"
        mock_fetch.return_value = session
        mock_decrypt.return_value = json.dumps(
            {
                "clauses": [
                    {
                        "chapter": "1",
                        "paragraph": "par 1",
                        "title": "Test",
                        "text": "Old",
                        "citations": [],
                        "notes": "",
                    }
                ]
            }
        )

        client = _authed_client()
        resp = client.post(
            f"/drafter/{_SESSION_ID}/step/5/regenerate/0",
        )

        assert resp.status_code == 200
        # Should return a polling div
        assert "every 3s" in resp.text
        # Job should be enqueued
        mock_queue_cls.return_value.enqueue.assert_called_once()
        enqueue_args = mock_queue_cls.return_value.enqueue.call_args
        assert enqueue_args.args[0] == "drafter_regenerate_clause"
        assert enqueue_args.args[1]["session_id"] == str(_SESSION_ID)
        assert enqueue_args.args[1]["clause_index"] == 0


class TestPostStep6Review:
    """POST step 6 trigger review -- creates drafts row."""

    @patch("app.drafter.routes._trigger_integrated_review")
    @patch("app.drafter.routes._connect")
    @patch("app.drafter.routes.fetch_session")
    @patch("app.auth.middleware._get_provider")
    def test_trigger_review_links_draft(
        self,
        mock_get_provider: MagicMock,
        mock_fetch: MagicMock,
        mock_connect: MagicMock,
        mock_trigger: MagicMock,
    ):
        mock_get_provider.return_value = _stub_provider()
        session = _make_session(current_step=6)
        session.integrated_draft_id = None
        mock_fetch.side_effect = [session, session]

        draft_id = uuid.UUID("55555555-5555-5555-5555-555555555555")
        mock_trigger.return_value = draft_id

        mock_conn = MagicMock()
        mock_connect.return_value.__enter__ = MagicMock(return_value=mock_conn)
        mock_connect.return_value.__exit__ = MagicMock(return_value=False)

        with patch("app.drafter.routes.update_session"):
            client = _authed_client()
            resp = client.post(
                f"/drafter/{_SESSION_ID}/step/6",
                data={},
            )

        assert resp.status_code == 200
        mock_trigger.assert_called_once()


class TestGetExport:
    """GET step 7 export download -- FileResponse."""

    @patch("app.drafter.routes.log_drafter_export")
    @patch("app.drafter.routes._connect")
    @patch("app.drafter.routes.decrypt_text")
    @patch("app.drafter.routes.fetch_session")
    @patch("app.auth.middleware._get_provider")
    def test_export_returns_docx_file(
        self,
        mock_get_provider: MagicMock,
        mock_fetch: MagicMock,
        mock_decrypt: MagicMock,
        mock_connect: MagicMock,
        mock_log: MagicMock,
    ):
        import json

        mock_get_provider.return_value = _stub_provider()
        session = _make_session(current_step=7)
        session.draft_content_encrypted = b"encrypted"
        session.proposed_structure = {
            "title": "Test seadus",
            "chapters": [
                {
                    "number": "1",
                    "title": "Test",
                    "sections": [{"paragraph": "par 1", "title": "Test"}],
                }
            ],
        }
        mock_fetch.return_value = session
        mock_decrypt.return_value = json.dumps(
            {
                "clauses": [
                    {
                        "chapter": "1",
                        "chapter_title": "Test",
                        "paragraph": "par 1",
                        "title": "Test",
                        "text": "Seaduse tekst",
                        "citations": [],
                        "notes": "",
                    }
                ]
            }
        )

        mock_conn = MagicMock()
        mock_connect.return_value.__enter__ = MagicMock(return_value=mock_conn)
        mock_connect.return_value.__exit__ = MagicMock(return_value=False)

        with patch("app.drafter.routes.update_session"):
            client = _authed_client()
            resp = client.get(f"/drafter/{_SESSION_ID}/export")

        assert resp.status_code == 200
        assert "application/vnd.openxmlformats" in resp.headers.get("content-type", "")
        mock_log.assert_called_once()
