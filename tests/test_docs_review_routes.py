"""Integration tests for the reviewer outcome route (issue #817).

Covers ``POST /drafts/{draft_id}/review-outcome`` and the
``_review_outcome_section`` renderer wired into the detail page. All
external dependencies (Postgres, Fernet, JobQueue) are mocked.

Pattern mirrors ``tests/test_docs_routes.py``:
    - ``patch('app.auth.middleware._get_provider')`` stubs the auth
      Beforeware so requests reach the handler with a valid ``auth``
      dict.
    - ``patch('app.docs.routes._review.<symbol>')`` intercepts the
      submodule's locally-bound dependencies (post-#704 patch-path rule).
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any
from unittest.mock import MagicMock, patch

from starlette.testclient import TestClient

from app.docs.draft_model import Draft
from app.docs.review_model import DraftReview

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_ORG_ID = "11111111-1111-1111-1111-111111111111"
_OTHER_ORG_ID = "22222222-2222-2222-2222-222222222222"
_DRAFTER_ID = "33333333-3333-3333-3333-333333333333"
_REVIEWER_ID = "44444444-4444-4444-4444-444444444444"
_DRAFT_ID = uuid.UUID("55555555-5555-5555-5555-555555555555")


def _reviewer_user(*, org_id: str = _ORG_ID) -> dict[str, Any]:
    return {
        "id": _REVIEWER_ID,
        "email": "ulevaataja@seadusloome.ee",
        "full_name": "Anne Tamm",
        "role": "reviewer",
        "org_id": org_id,
    }


def _drafter_user(*, user_id: str = _DRAFTER_ID, org_id: str = _ORG_ID) -> dict[str, Any]:
    return {
        "id": user_id,
        "email": "koostaja@seadusloome.ee",
        "full_name": "Test Koostaja",
        "role": "drafter",
        "org_id": org_id,
    }


def _make_draft(
    *,
    draft_id: uuid.UUID = _DRAFT_ID,
    org_id: str = _ORG_ID,
    user_id: str = _DRAFTER_ID,
    status: str = "ready",
) -> Draft:
    now = datetime.now(UTC)
    return Draft(
        id=draft_id,
        user_id=uuid.UUID(user_id),
        org_id=uuid.UUID(org_id),
        title="Test eelnõu",
        filename="eelnou.docx",
        content_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        file_size=2048,
        storage_path="/tmp/x.enc",
        graph_uri=f"https://data.riik.ee/ontology/estleg/drafts/{draft_id}",
        status=status,
        parsed_text_encrypted=None,
        entity_count=None,
        error_message=None,
        created_at=now,
        updated_at=now,
        last_accessed_at=now,
    )


def _stub_provider(user: dict[str, Any]) -> MagicMock:
    provider = MagicMock()
    provider.get_current_user.return_value = user
    return provider


def _authed_client(user: dict[str, Any]) -> TestClient:
    """Return a TestClient that will be authed via the patched provider.

    Caller is responsible for patching ``app.auth.middleware._get_provider``
    to return ``_stub_provider(user)``.
    """
    from app.main import app

    client = TestClient(app, follow_redirects=False)
    client.cookies.set("access_token", "stub-token")
    return client


def _stub_connect(conn: MagicMock) -> MagicMock:
    """Build a context-manager mock around *conn* for `_connect()` usage."""
    cm = MagicMock()
    cm.__enter__ = MagicMock(return_value=conn)
    cm.__exit__ = MagicMock(return_value=False)
    return cm


# ---------------------------------------------------------------------------
# Auth gating
# ---------------------------------------------------------------------------


class TestReviewOutcomeAuth:
    def test_unauthenticated_redirects_to_login(self):
        from app.main import app

        client = TestClient(app, follow_redirects=False)
        resp = client.post(
            f"/drafts/{_DRAFT_ID}/review-outcome",
            data={"outcome": "no_issue"},
        )
        assert resp.status_code == 303
        assert resp.headers["location"] == "/auth/login"

    @patch("app.docs.routes._review.fetch_draft")
    @patch("app.auth.middleware._get_provider")
    def test_drafter_role_returns_404(
        self,
        mock_get_provider: MagicMock,
        mock_fetch: MagicMock,
    ):
        """Drafters must NOT see this endpoint — same-org viewer who lacks
        the reviewer role should get 404 (not 403) so existence isn't leaked."""
        mock_get_provider.return_value = _stub_provider(_drafter_user())
        mock_fetch.return_value = _make_draft()

        client = _authed_client(_drafter_user())
        resp = client.post(
            f"/drafts/{_DRAFT_ID}/review-outcome",
            data={"outcome": "no_issue"},
        )
        assert resp.status_code == 404
        assert "Eelnõu ei leitud" in resp.text

    @patch("app.docs.routes._review.fetch_draft")
    @patch("app.auth.middleware._get_provider")
    def test_cross_org_reviewer_returns_404(
        self,
        mock_get_provider: MagicMock,
        mock_fetch: MagicMock,
    ):
        """A reviewer in another org cannot post outcomes on this org's drafts."""
        mock_get_provider.return_value = _stub_provider(_reviewer_user(org_id=_OTHER_ORG_ID))
        mock_fetch.return_value = _make_draft()

        client = _authed_client(_reviewer_user(org_id=_OTHER_ORG_ID))
        resp = client.post(
            f"/drafts/{_DRAFT_ID}/review-outcome",
            data={"outcome": "no_issue"},
        )
        assert resp.status_code == 404

    @patch("app.docs.routes._review.fetch_draft")
    @patch("app.auth.middleware._get_provider")
    def test_unknown_draft_returns_404(
        self,
        mock_get_provider: MagicMock,
        mock_fetch: MagicMock,
    ):
        mock_get_provider.return_value = _stub_provider(_reviewer_user())
        mock_fetch.return_value = None

        client = _authed_client(_reviewer_user())
        resp = client.post(
            f"/drafts/{_DRAFT_ID}/review-outcome",
            data={"outcome": "no_issue"},
        )
        assert resp.status_code == 404


# ---------------------------------------------------------------------------
# Input validation
# ---------------------------------------------------------------------------


class TestReviewOutcomeValidation:
    @patch("app.docs.routes._review.fetch_draft")
    @patch("app.auth.middleware._get_provider")
    def test_missing_outcome_returns_400(
        self,
        mock_get_provider: MagicMock,
        mock_fetch: MagicMock,
    ):
        mock_get_provider.return_value = _stub_provider(_reviewer_user())
        mock_fetch.return_value = _make_draft()

        client = _authed_client(_reviewer_user())
        resp = client.post(
            f"/drafts/{_DRAFT_ID}/review-outcome",
            data={},  # no outcome
        )
        assert resp.status_code == 400
        assert "Palun valige" in resp.text

    @patch("app.docs.routes._review.fetch_draft")
    @patch("app.auth.middleware._get_provider")
    def test_invalid_outcome_returns_400(
        self,
        mock_get_provider: MagicMock,
        mock_fetch: MagicMock,
    ):
        mock_get_provider.return_value = _stub_provider(_reviewer_user())
        mock_fetch.return_value = _make_draft()

        client = _authed_client(_reviewer_user())
        resp = client.post(
            f"/drafts/{_DRAFT_ID}/review-outcome",
            data={"outcome": "approved"},  # not in REVIEW_OUTCOMES
        )
        assert resp.status_code == 400


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


class TestReviewOutcomeHappyPath:
    @patch("app.docs.routes._review.log_review_outcome")
    @patch("app.docs.routes._review.list_reviews_for_draft")
    @patch("app.docs.routes._review.create_review")
    @patch("app.docs.routes._review._connect")
    @patch("app.docs.routes._review.fetch_draft")
    @patch("app.auth.middleware._get_provider")
    def test_reviewer_no_issue_persists_and_returns_section(
        self,
        mock_get_provider: MagicMock,
        mock_fetch: MagicMock,
        mock_connect: MagicMock,
        mock_create: MagicMock,
        mock_list: MagicMock,
        mock_log: MagicMock,
    ):
        mock_get_provider.return_value = _stub_provider(_reviewer_user())
        mock_fetch.return_value = _make_draft()
        conn = MagicMock()
        mock_connect.return_value = _stub_connect(conn)
        mock_list.return_value = [
            DraftReview(
                id=uuid.UUID("66666666-6666-6666-6666-666666666666"),
                draft_id=_DRAFT_ID,
                reviewer_id=uuid.UUID(_REVIEWER_ID),
                reviewer_name_snapshot="Anne Tamm",
                outcome="no_issue",
                comment=None,
                created_at=datetime.now(UTC),
            )
        ]

        client = _authed_client(_reviewer_user())
        resp = client.post(
            f"/drafts/{_DRAFT_ID}/review-outcome",
            data={"outcome": "no_issue"},
        )

        assert resp.status_code == 200
        mock_create.assert_called_once()
        kwargs = mock_create.call_args.kwargs
        assert kwargs["outcome"] == "no_issue"
        assert kwargs["reviewer_id"] == _REVIEWER_ID
        assert kwargs["reviewer_name"] == "Anne Tamm"
        # The section renders so HTMX can swap it in.
        assert "draft-review-section" in resp.text
        # Audit log was emitted with comment_present=False.
        mock_log.assert_called_once()
        log_kwargs = mock_log.call_args.kwargs
        assert log_kwargs["outcome"] == "no_issue"
        assert log_kwargs["comment_present"] is False

    @patch("app.docs.routes._review.log_review_outcome")
    @patch("app.docs.routes._review.list_reviews_for_draft")
    @patch("app.docs.routes._review.create_review")
    @patch("app.docs.routes._review._connect")
    @patch("app.docs.routes._review.fetch_draft")
    @patch("app.auth.middleware._get_provider")
    def test_reviewer_with_comment_logs_comment_present(
        self,
        mock_get_provider: MagicMock,
        mock_fetch: MagicMock,
        mock_connect: MagicMock,
        mock_create: MagicMock,
        mock_list: MagicMock,
        mock_log: MagicMock,
    ):
        mock_get_provider.return_value = _stub_provider(_reviewer_user())
        mock_fetch.return_value = _make_draft()
        mock_connect.return_value = _stub_connect(MagicMock())
        mock_list.return_value = []

        client = _authed_client(_reviewer_user())
        resp = client.post(
            f"/drafts/{_DRAFT_ID}/review-outcome",
            data={"outcome": "needs_discussion", "comment": "Vajab täiendavat selgitust."},
        )

        assert resp.status_code == 200
        # Comment was passed through.
        kwargs = mock_create.call_args.kwargs
        assert kwargs["comment"] == "Vajab täiendavat selgitust."
        # Audit log carries the comment_present flag without the body.
        log_kwargs = mock_log.call_args.kwargs
        assert log_kwargs["comment_present"] is True
        # CRITICAL: the audit log MUST NOT carry the comment body.
        for value in log_kwargs.values():
            assert "Vajab täiendavat selgitust" not in str(value)


# ---------------------------------------------------------------------------
# Renderer behaviour — _review_outcome_section
# ---------------------------------------------------------------------------


class TestReviewOutcomeSection:
    def test_renders_empty_div_for_drafter(self):
        """Drafters get an empty stub div so the layout stays stable
        but no UI surfaces."""
        from fasthtml.common import to_xml

        from app.docs.routes._detail import _REVIEW_SECTION_ID, _review_outcome_section

        draft = _make_draft()
        node = _review_outcome_section(draft, auth=_drafter_user())
        html = to_xml(node)
        assert f'id="{_REVIEW_SECTION_ID}"' in html
        # No outcome buttons.
        assert "Puuduvad probleemid" not in html
        assert "Leitud probleem" not in html
        assert "Vajab arutelu" not in html

    def test_renders_three_outcome_buttons_for_reviewer(self):
        from fasthtml.common import to_xml

        from app.docs.routes._detail import _review_outcome_section

        draft = _make_draft()
        node = _review_outcome_section(draft, auth=_reviewer_user(), reviews=[])
        html = to_xml(node)
        assert "Puuduvad probleemid" in html
        assert "Leitud probleem" in html
        assert "Vajab arutelu" in html
        # Optional comment field is present.
        assert "Lisa märkus" in html
        # POST target is the review-outcome route.
        assert f"/drafts/{_DRAFT_ID}/review-outcome" in html

    def test_chronological_list_renders_reviewer_name(self):
        from fasthtml.common import to_xml

        from app.docs.routes._detail import _review_outcome_section

        draft = _make_draft()
        review = DraftReview(
            id=uuid.UUID("66666666-6666-6666-6666-666666666666"),
            draft_id=_DRAFT_ID,
            reviewer_id=uuid.UUID(_REVIEWER_ID),
            reviewer_name_snapshot="Anne Tamm",
            outcome="needs_discussion",
            comment="Sätte mõju on ebaselge.",
            created_at=datetime(2026, 5, 19, 10, 30, tzinfo=UTC),
        )
        node = _review_outcome_section(draft, auth=_reviewer_user(), reviews=[review])
        html = to_xml(node)
        assert "Anne Tamm" in html
        assert "Sätte mõju on ebaselge." in html
        # Outcome badge label rendered.
        assert "Vajab arutelu" in html

    def test_deleted_reviewer_shows_placeholder(self):
        """When ``reviewer_id`` is NULL but the snapshot is present, the
        UI renders 'Original Name (kustutatud kasutaja)'."""
        from fasthtml.common import to_xml

        from app.docs.routes._detail import _review_outcome_section

        draft = _make_draft()
        review = DraftReview(
            id=uuid.UUID("66666666-6666-6666-6666-666666666666"),
            draft_id=_DRAFT_ID,
            reviewer_id=None,
            reviewer_name_snapshot="Anne Tamm",
            outcome="no_issue",
            comment=None,
            created_at=datetime.now(UTC),
        )
        node = _review_outcome_section(draft, auth=_reviewer_user(), reviews=[review])
        html = to_xml(node)
        assert "Anne Tamm" in html
        assert "kustutatud kasutaja" in html

    def test_deleted_reviewer_with_no_snapshot_uses_default_placeholder(self):
        from fasthtml.common import to_xml

        from app.docs.routes._detail import _review_outcome_section

        draft = _make_draft()
        review = DraftReview(
            id=uuid.UUID("66666666-6666-6666-6666-666666666666"),
            draft_id=_DRAFT_ID,
            reviewer_id=None,
            reviewer_name_snapshot=None,
            outcome="no_issue",
            comment=None,
            created_at=datetime.now(UTC),
        )
        node = _review_outcome_section(draft, auth=_reviewer_user(), reviews=[review])
        html = to_xml(node)
        assert "Kustutatud kasutaja" in html
