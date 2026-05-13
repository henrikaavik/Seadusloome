"""Regression tests for #774 — clause controls survive regeneration.

The drafter step-5 ``regenerate_clause_status`` HTMX polling endpoint
must, on a successful regeneration, return a clause fragment that still
carries:

    - the ``.clause-actions`` row,
    - the ``Muuda`` button (edit URL: ``/drafter/.../step/5/edit/{idx}``),
    - the ``Genereeri uuesti`` button (regenerate URL:
      ``/drafter/.../step/5/regenerate/{idx}``),
    - and the ``AnnotationButton`` wrapper for the clause's provision.

Before the fix, the success branch emitted a stripped-down ``Div``
without the action row, so the regenerated clause became
non-interactive until the user refreshed the page. The fix extracts a
shared ``_render_clause_card`` helper used by both the initial step-5
page render and the success polling fragment.

Patterns follow ``tests/test_drafter_routes.py`` (TestClient + patched
auth provider + mocked DB / job lookups).
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime
from typing import Any
from unittest.mock import MagicMock, patch

from app.drafter.session_model import DraftingSession

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

_ORG_ID = "11111111-1111-1111-1111-111111111111"
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


def _stub_provider() -> MagicMock:
    provider = MagicMock()
    provider.get_current_user.return_value = _authed_user()
    return provider


def _make_session(*, current_step: int = 5) -> DraftingSession:
    now = datetime.now(UTC)
    return DraftingSession(
        id=_SESSION_ID,
        user_id=uuid.UUID(_USER_ID),
        org_id=uuid.UUID(_ORG_ID),
        workflow_type="full_law",
        current_step=current_step,
        intent="Test intent",
        clarifications=[],
        research_data_encrypted=None,
        proposed_structure=None,
        draft_content_encrypted=b"encrypted",
        integrated_draft_id=None,
        status="active",
        created_at=now,
        updated_at=now,
    )


def _authed_client():
    from starlette.testclient import TestClient

    client = TestClient(
        __import__("app.main", fromlist=["app"]).app,
        follow_redirects=False,
    )
    client.cookies.set("access_token", "stub-token")
    return client


def _clauses_json() -> str:
    return json.dumps(
        {
            "clauses": [
                {
                    "chapter": "1",
                    "chapter_title": "Üldsätted",
                    "paragraph": "§ 1",
                    "title": "Reguleerimisala",
                    "text": "Käesolev seadus sätestab uue korra.",
                    "citations": ["estleg:TsiviilS/par/1"],
                    "notes": "",
                }
            ]
        }
    )


# ---------------------------------------------------------------------------
# regenerate_clause_status — success branch keeps clause controls (#774)
# ---------------------------------------------------------------------------


class TestRegenerateClauseStatusSuccessFragment:
    """The success fragment MUST emit the same action row as the initial
    step-5 page so an inline HTMX swap doesn't strip ``Muuda``,
    ``Genereeri uuesti``, or the ``AnnotationButton``.
    """

    @patch("app.drafter.routes._find_latest_job")
    @patch("app.drafter.routes.decrypt_text")
    @patch("app.drafter.routes.fetch_session")
    @patch("app.auth.middleware._get_provider")
    def test_success_fragment_preserves_clause_actions(
        self,
        mock_get_provider: MagicMock,
        mock_fetch: MagicMock,
        mock_decrypt: MagicMock,
        mock_find_job: MagicMock,
    ):
        mock_get_provider.return_value = _stub_provider()
        mock_fetch.return_value = _make_session(current_step=5)
        mock_decrypt.return_value = _clauses_json()
        mock_find_job.return_value = {
            "status": "success",
            "payload": {"clause_index": 0},
        }

        client = _authed_client()
        resp = client.get(f"/drafter/{_SESSION_ID}/step/5/regenerate/0/status")

        assert resp.status_code == 200
        body = resp.text

        # The success alert appears so users see the regeneration completed.
        assert "Uuesti genereeritud." in body
        # The action row is restored — this is the regression that #774
        # specifically guards against.
        assert "clause-actions" in body
        # The clause content is rendered.
        assert "Käesolev seadus sätestab uue korra." in body

    @patch("app.drafter.routes._find_latest_job")
    @patch("app.drafter.routes.decrypt_text")
    @patch("app.drafter.routes.fetch_session")
    @patch("app.auth.middleware._get_provider")
    def test_success_fragment_carries_edit_and_regenerate_urls(
        self,
        mock_get_provider: MagicMock,
        mock_fetch: MagicMock,
        mock_decrypt: MagicMock,
        mock_find_job: MagicMock,
    ):
        """Both inline HTMX URLs (``edit`` + ``regenerate``) MUST be wired
        up in the swap, otherwise the clause becomes read-only until the
        user navigates away.
        """
        mock_get_provider.return_value = _stub_provider()
        mock_fetch.return_value = _make_session(current_step=5)
        mock_decrypt.return_value = _clauses_json()
        mock_find_job.return_value = {
            "status": "success",
            "payload": {"clause_index": 0},
        }

        client = _authed_client()
        resp = client.get(f"/drafter/{_SESSION_ID}/step/5/regenerate/0/status")

        assert resp.status_code == 200
        body = resp.text

        # Muuda HTMX target.
        assert f"/drafter/{_SESSION_ID}/step/5/edit/0" in body
        assert "Muuda" in body
        # Genereeri uuesti HTMX target.
        assert f"/drafter/{_SESSION_ID}/step/5/regenerate/0" in body
        assert "Genereeri uuesti" in body

    @patch("app.drafter.routes._find_latest_job")
    @patch("app.drafter.routes.decrypt_text")
    @patch("app.drafter.routes.fetch_session")
    @patch("app.auth.middleware._get_provider")
    def test_success_fragment_includes_annotation_button(
        self,
        mock_get_provider: MagicMock,
        mock_fetch: MagicMock,
        mock_decrypt: MagicMock,
        mock_find_job: MagicMock,
    ):
        """The clause-scoped AnnotationButton wrapper MUST appear so users
        can still add notes after a regeneration.
        """
        mock_get_provider.return_value = _stub_provider()
        mock_fetch.return_value = _make_session(current_step=5)
        mock_decrypt.return_value = _clauses_json()
        mock_find_job.return_value = {
            "status": "success",
            "payload": {"clause_index": 0},
        }

        client = _authed_client()
        resp = client.get(f"/drafter/{_SESSION_ID}/step/5/regenerate/0/status")

        assert resp.status_code == 200
        body = resp.text

        # The AnnotationButton wraps its trigger button + popover container
        # in a ``annotation-button-wrapper`` Div. The trigger button itself
        # carries the ``annotation-button`` class and HXes the annotation
        # API with both ``target_type=provision`` and the clause-scoped
        # ``target_id=<session_id>-clause-<idx>``.
        assert "annotation-button-wrapper" in body
        assert "annotation-button" in body
        assert "target_type=provision" in body
        assert f"target_id={_SESSION_ID}-clause-0" in body
