"""Integration tests for the Phase 2 Document Upload routes.

These tests exercise the full ``app.main.app`` via ``TestClient`` so
they validate the FastHTML wiring, the auth Beforeware, and the HTMX
partial swap behaviour. External dependencies — Postgres, Fernet,
JobQueue — are mocked out.

Patterns:
    - ``patch('app.auth.middleware._get_provider')`` lets us hand the
      Beforeware a stubbed ``JWTAuthProvider`` so the request reaches
      the handler with a valid ``req.scope['auth']``.
    - ``patch('app.docs.routes.fetch_drafts_for_org')`` et al. replace
      the DB helpers with plain Python stubs.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any
from unittest.mock import MagicMock, patch

from starlette.testclient import TestClient

from app.docs.draft_model import Draft
from app.docs.upload import DraftUploadError

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


_ORG_ID = "11111111-1111-1111-1111-111111111111"
_OTHER_ORG_ID = "22222222-2222-2222-2222-222222222222"
_USER_ID = "33333333-3333-3333-3333-333333333333"


def _authed_user() -> dict[str, Any]:
    return {
        "id": _USER_ID,
        "email": "koostaja@seadusloome.ee",
        "full_name": "Test Koostaja",
        "role": "drafter",
        "org_id": _ORG_ID,
    }


def _make_draft(
    *,
    draft_id: uuid.UUID | None = None,
    org_id: str = _ORG_ID,
    status: str = "uploaded",
    title: str = "Test eelnõu",
    error_message: str | None = None,
) -> Draft:
    now = datetime.now(UTC)
    resolved_id = draft_id or uuid.UUID("44444444-4444-4444-4444-444444444444")
    return Draft(
        id=resolved_id,
        user_id=uuid.UUID(_USER_ID),
        org_id=uuid.UUID(org_id),
        title=title,
        filename="eelnou.docx",
        content_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        file_size=2048,
        storage_path="/tmp/ciphertext.enc",
        graph_uri=f"https://data.riik.ee/ontology/estleg/drafts/{resolved_id}",
        status=status,
        parsed_text=None,
        entity_count=None,
        error_message=error_message,
        created_at=now,
        updated_at=now,
    )


def _stub_provider() -> MagicMock:
    """Build a provider whose ``get_current_user`` returns ``_authed_user``."""
    provider = MagicMock()
    provider.get_current_user.return_value = _authed_user()
    return provider


def _authed_client() -> TestClient:
    """Return a TestClient preloaded with a valid ``access_token`` cookie.

    The actual token value doesn't matter because the provider is
    mocked — any non-empty string keeps the middleware happy.
    """
    client = TestClient(__import__("app.main", fromlist=["app"]).app, follow_redirects=False)
    client.cookies.set("access_token", "stub-token")
    return client


# ---------------------------------------------------------------------------
# Unauthenticated requests redirect to login
# ---------------------------------------------------------------------------


class TestAuthRequired:
    def test_drafts_list_redirects_unauthenticated(self):
        from app.main import app

        client = TestClient(app, follow_redirects=False)
        resp = client.get("/drafts")
        assert resp.status_code == 303
        assert resp.headers["location"] == "/auth/login"

    def test_new_draft_redirects_unauthenticated(self):
        from app.main import app

        client = TestClient(app, follow_redirects=False)
        resp = client.get("/drafts/new")
        assert resp.status_code == 303
        assert resp.headers["location"] == "/auth/login"

    def test_draft_detail_redirects_unauthenticated(self):
        from app.main import app

        client = TestClient(app, follow_redirects=False)
        resp = client.get("/drafts/44444444-4444-4444-4444-444444444444")
        assert resp.status_code == 303
        assert resp.headers["location"] == "/auth/login"


# ---------------------------------------------------------------------------
# GET /drafts — listing
# ---------------------------------------------------------------------------


class TestDraftsList:
    @patch("app.docs.routes.count_drafts_for_org_conn")
    @patch("app.docs.routes.fetch_drafts_for_org")
    @patch("app.auth.middleware._get_provider")
    def test_empty_list_renders_empty_state(
        self,
        mock_get_provider: MagicMock,
        mock_fetch: MagicMock,
        mock_count: MagicMock,
    ):
        mock_get_provider.return_value = _stub_provider()
        mock_fetch.return_value = []
        mock_count.return_value = 0

        client = _authed_client()
        resp = client.get("/drafts")

        assert resp.status_code == 200
        assert "Eelnõud" in resp.text
        # Empty state CTA must be present.
        assert "Laadi üles uus eelnõu" in resp.text
        assert "ei ole veel ühtegi eelnõu üles laadinud" in resp.text

    @patch("app.docs.routes.count_drafts_for_org_conn")
    @patch("app.docs.routes.fetch_drafts_for_org")
    @patch("app.auth.middleware._get_provider")
    def test_populated_list_shows_rows(
        self,
        mock_get_provider: MagicMock,
        mock_fetch: MagicMock,
        mock_count: MagicMock,
    ):
        mock_get_provider.return_value = _stub_provider()
        draft_a = _make_draft(
            draft_id=uuid.UUID("55555555-5555-5555-5555-555555555555"),
            status="uploaded",
            title="Eelnõu A",
        )
        draft_b = _make_draft(
            draft_id=uuid.UUID("66666666-6666-6666-6666-666666666666"),
            status="ready",
            title="Eelnõu B",
        )
        mock_fetch.return_value = [draft_a, draft_b]
        mock_count.return_value = 2

        client = _authed_client()
        resp = client.get("/drafts")

        assert resp.status_code == 200
        assert "Eelnõu A" in resp.text
        assert "Eelnõu B" in resp.text
        # Fetch was org-scoped.
        mock_fetch.assert_called_once()
        fetch_kwargs = mock_fetch.call_args
        # First positional arg is the org_id.
        assert fetch_kwargs.args[0] == _ORG_ID


# ---------------------------------------------------------------------------
# GET /drafts/new
# ---------------------------------------------------------------------------


class TestNewDraftPage:
    @patch("app.auth.middleware._get_provider")
    def test_new_draft_renders_multipart_form(self, mock_get_provider: MagicMock):
        mock_get_provider.return_value = _stub_provider()

        client = _authed_client()
        resp = client.get("/drafts/new")

        assert resp.status_code == 200
        # The form must be multipart — verifies the raw Form primitive
        # was used instead of AppForm (which defaults to urlencoded).
        assert 'enctype="multipart/form-data"' in resp.text
        assert 'name="title"' in resp.text
        assert 'name="file"' in resp.text
        assert 'type="file"' in resp.text
        assert ".docx" in resp.text


# ---------------------------------------------------------------------------
# POST /drafts — upload
# ---------------------------------------------------------------------------


class TestCreateDraftHandler:
    @patch("app.docs.routes.log_draft_upload")
    @patch("app.docs.routes.handle_upload")
    @patch("app.auth.middleware._get_provider")
    def test_successful_upload_redirects_to_detail(
        self,
        mock_get_provider: MagicMock,
        mock_handle: MagicMock,
        mock_log: MagicMock,
    ):
        mock_get_provider.return_value = _stub_provider()
        draft = _make_draft(
            draft_id=uuid.UUID("77777777-7777-7777-7777-777777777777"),
        )

        async def _fake_handle(*_a: Any, **_kw: Any) -> Draft:
            return draft

        mock_handle.side_effect = _fake_handle

        client = _authed_client()
        resp = client.post(
            "/drafts",
            data={"title": "Test eelnõu"},
            files={
                "file": (
                    "eelnou.docx",
                    b"Test sisu",
                    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                )
            },
        )

        assert resp.status_code == 303
        assert resp.headers["location"] == f"/drafts/{draft.id}"
        mock_handle.assert_called_once()
        mock_log.assert_called_once()

    @patch("app.docs.routes.handle_upload")
    @patch("app.auth.middleware._get_provider")
    def test_validation_error_rerenders_form_with_title(
        self,
        mock_get_provider: MagicMock,
        mock_handle: MagicMock,
    ):
        mock_get_provider.return_value = _stub_provider()

        async def _raise(*_a: Any, **_kw: Any) -> Draft:
            raise DraftUploadError("Toetamata failitüüp.")

        mock_handle.side_effect = _raise

        client = _authed_client()
        resp = client.post(
            "/drafts",
            data={"title": "Minu eelnõu"},
            files={
                "file": (
                    "bad.txt",
                    b"content",
                    "text/plain",
                )
            },
        )

        assert resp.status_code == 200
        # Error banner is present.
        assert "Toetamata failitüüp." in resp.text
        # Title was preserved so the user can fix the file and retry.
        assert "Minu eelnõu" in resp.text


# ---------------------------------------------------------------------------
# GET /drafts/{draft_id} — detail page
# ---------------------------------------------------------------------------


class TestDraftDetailPage:
    @patch("app.docs.routes.log_draft_view")
    @patch("app.docs.routes.fetch_draft")
    @patch("app.auth.middleware._get_provider")
    def test_draft_in_own_org_renders(
        self,
        mock_get_provider: MagicMock,
        mock_fetch: MagicMock,
        mock_log: MagicMock,
    ):
        mock_get_provider.return_value = _stub_provider()
        draft = _make_draft(status="parsing", title="Minu eelnõu")
        mock_fetch.return_value = draft

        client = _authed_client()
        resp = client.get(f"/drafts/{draft.id}")

        assert resp.status_code == 200
        assert "Minu eelnõu" in resp.text
        # Status tracker visible.
        assert "Üles laaditud" in resp.text
        assert "Töötlemine" in resp.text
        # Polling attrs are present because status is not terminal.
        assert f"/drafts/{draft.id}/status" in resp.text
        assert "every 3s" in resp.text
        # Audit log was written.
        mock_log.assert_called_once()

    @patch("app.docs.routes.fetch_draft")
    @patch("app.auth.middleware._get_provider")
    def test_draft_in_other_org_returns_404_page(
        self,
        mock_get_provider: MagicMock,
        mock_fetch: MagicMock,
    ):
        mock_get_provider.return_value = _stub_provider()
        # Draft belongs to a different org.
        foreign = _make_draft(org_id=_OTHER_ORG_ID)
        mock_fetch.return_value = foreign

        client = _authed_client()
        resp = client.get(f"/drafts/{foreign.id}")

        # We return the not-found page (HTTP 200 with 404-style content)
        # rather than a raw 403 so we never leak the existence of drafts
        # belonging to other organisations.
        assert resp.status_code == 200
        assert "Eelnõu ei leitud" in resp.text
        assert "Minu eelnõu" not in resp.text

    @patch("app.docs.routes.fetch_draft")
    @patch("app.auth.middleware._get_provider")
    def test_missing_draft_returns_404_page(
        self,
        mock_get_provider: MagicMock,
        mock_fetch: MagicMock,
    ):
        mock_get_provider.return_value = _stub_provider()
        mock_fetch.return_value = None

        client = _authed_client()
        resp = client.get("/drafts/99999999-9999-9999-9999-999999999999")

        assert resp.status_code == 200
        assert "Eelnõu ei leitud" in resp.text

    @patch("app.docs.routes.fetch_draft")
    @patch("app.auth.middleware._get_provider")
    def test_ready_draft_shows_report_link(
        self,
        mock_get_provider: MagicMock,
        mock_fetch: MagicMock,
    ):
        mock_get_provider.return_value = _stub_provider()
        draft = _make_draft(status="ready")
        mock_fetch.return_value = draft

        client = _authed_client()
        resp = client.get(f"/drafts/{draft.id}")

        assert resp.status_code == 200
        assert "Vaata mõjuaruannet" in resp.text
        assert f"/drafts/{draft.id}/report" in resp.text
        # No polling for terminal statuses.
        assert "every 3s" not in resp.text

    @patch("app.docs.routes.fetch_draft")
    @patch("app.auth.middleware._get_provider")
    def test_failed_draft_shows_error_message(
        self,
        mock_get_provider: MagicMock,
        mock_fetch: MagicMock,
    ):
        mock_get_provider.return_value = _stub_provider()
        draft = _make_draft(
            status="failed",
            error_message="Tika teenus on kättesaamatu.",
        )
        mock_fetch.return_value = draft

        client = _authed_client()
        resp = client.get(f"/drafts/{draft.id}")

        assert resp.status_code == 200
        assert "Tika teenus on kättesaamatu." in resp.text
        assert "every 3s" not in resp.text


# ---------------------------------------------------------------------------
# GET /drafts/{draft_id}/status — HTMX polling fragment
# ---------------------------------------------------------------------------


class TestDraftStatusFragment:
    @patch("app.docs.routes.fetch_draft")
    @patch("app.auth.middleware._get_provider")
    def test_status_fragment_returns_partial_without_shell(
        self,
        mock_get_provider: MagicMock,
        mock_fetch: MagicMock,
    ):
        mock_get_provider.return_value = _stub_provider()
        draft = _make_draft(status="extracting")
        mock_fetch.return_value = draft

        client = _authed_client()
        # Send the HX-Request header so FastHTML returns a partial instead
        # of wrapping the Div in a full HTML document.
        resp = client.get(
            f"/drafts/{draft.id}/status",
            headers={"HX-Request": "true"},
        )

        assert resp.status_code == 200
        # No PageShell wrapper — no sidebar, no topbar.
        assert "app-shell" not in resp.text
        assert "sidebar" not in resp.text
        # The status tracker is in the body.
        assert "Olemite eraldamine" in resp.text
        assert f"draft-status-{draft.id}" in resp.text

    @patch("app.docs.routes.fetch_draft")
    @patch("app.auth.middleware._get_provider")
    def test_status_fragment_other_org_returns_placeholder(
        self,
        mock_get_provider: MagicMock,
        mock_fetch: MagicMock,
    ):
        mock_get_provider.return_value = _stub_provider()
        mock_fetch.return_value = _make_draft(org_id=_OTHER_ORG_ID)

        client = _authed_client()
        resp = client.get("/drafts/44444444-4444-4444-4444-444444444444/status")

        assert resp.status_code == 200
        assert "Eelnõu ei leitud" in resp.text


# ---------------------------------------------------------------------------
# POST /drafts/{draft_id}/delete
# ---------------------------------------------------------------------------


class TestDeleteDraftHandler:
    @patch("app.docs.routes.delete_named_graph")
    @patch("app.docs.routes.log_draft_delete")
    @patch("app.docs.routes.delete_encrypted_file")
    @patch("app.docs.routes.delete_draft")
    @patch("app.docs.routes._connect")
    @patch("app.docs.routes.fetch_draft")
    @patch("app.auth.middleware._get_provider")
    def test_delete_removes_draft_and_file(
        self,
        mock_get_provider: MagicMock,
        mock_fetch: MagicMock,
        mock_connect: MagicMock,
        mock_delete: MagicMock,
        mock_delete_file: MagicMock,
        mock_log: MagicMock,
        mock_delete_graph: MagicMock,
    ):
        mock_get_provider.return_value = _stub_provider()
        draft = _make_draft()
        mock_fetch.return_value = draft

        # _connect() is a context manager.
        mock_conn = MagicMock()
        mock_connect.return_value.__enter__ = MagicMock(return_value=mock_conn)
        mock_connect.return_value.__exit__ = MagicMock(return_value=False)
        mock_delete.return_value = "/tmp/ciphertext.enc"

        client = _authed_client()
        resp = client.post(f"/drafts/{draft.id}/delete")

        assert resp.status_code == 303
        assert resp.headers["location"] == "/drafts"
        mock_delete.assert_called_once()
        mock_delete_file.assert_called_once_with("/tmp/ciphertext.enc")
        mock_log.assert_called_once()
        # Named graph cleanup must have been triggered with the draft's URI.
        mock_delete_graph.assert_called_once_with(draft.graph_uri)

    @patch("app.docs.routes.fetch_draft")
    @patch("app.auth.middleware._get_provider")
    def test_delete_other_org_draft_returns_not_found(
        self,
        mock_get_provider: MagicMock,
        mock_fetch: MagicMock,
    ):
        mock_get_provider.return_value = _stub_provider()
        mock_fetch.return_value = _make_draft(org_id=_OTHER_ORG_ID)

        client = _authed_client()
        resp = client.post("/drafts/44444444-4444-4444-4444-444444444444/delete")

        assert resp.status_code == 200
        assert "Eelnõu ei leitud" in resp.text
