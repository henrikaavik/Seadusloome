"""Tests for the #176 @mention typeahead feature.

Covers:
    - GET /api/annotations/mentions/search — org-scoped user lookup
    - Auth gate (303 redirect when unauthenticated)
    - Empty query returns empty results
    - notify_annotation_mention fan-out + self-exclusion

Mocks the Postgres layer; same patterns as ``tests/test_annotations_routes.py``.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any
from unittest.mock import MagicMock, patch

from starlette.testclient import TestClient

from app.annotations.models import Annotation

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_ORG_ID = "11111111-1111-1111-1111-111111111111"
_OTHER_ORG_ID = "22222222-2222-2222-2222-222222222222"
_USER_ID = "33333333-3333-3333-3333-333333333333"
_OTHER_USER_ID = "44444444-4444-4444-4444-444444444444"
_MENTIONED_USER_ID = uuid.UUID("55555555-5555-5555-5555-555555555555")
_ANOTHER_MENTIONED_ID = uuid.UUID("66666666-6666-6666-6666-666666666666")


def _authed_user(
    user_id: str = _USER_ID,
    org_id: str = _ORG_ID,
) -> dict[str, Any]:
    return {
        "id": user_id,
        "email": "kasutaja@seadusloome.ee",
        "full_name": "Test Kasutaja",
        "role": "drafter",
        "org_id": org_id,
    }


def _stub_provider(user: dict[str, Any] | None = None) -> MagicMock:
    provider = MagicMock()
    provider.get_current_user.return_value = user or _authed_user()
    return provider


def _authed_client() -> TestClient:
    client = TestClient(
        __import__("app.main", fromlist=["app"]).app,
        follow_redirects=False,
    )
    client.cookies.set("access_token", "stub-token")
    return client


def _mock_db_conn(rows: list[tuple[Any, ...]] | None = None) -> MagicMock:
    """Build a mock psycopg connection whose ``execute().fetchall()`` returns *rows*."""
    cursor = MagicMock()
    cursor.fetchall.return_value = rows or []
    db = MagicMock()
    db.execute.return_value = cursor
    return db


def _mock_conn_factory(db: MagicMock) -> MagicMock:
    """Build a mock for ``_connect`` (a context-manager factory)."""
    mock_conn = MagicMock()
    mock_conn.return_value.__enter__ = MagicMock(return_value=db)
    mock_conn.return_value.__exit__ = MagicMock(return_value=False)
    return mock_conn


# ---------------------------------------------------------------------------
# GET /api/annotations/mentions/search — auth
# ---------------------------------------------------------------------------


class TestMentionsSearchAuth:
    def test_unauth_redirects_to_login(self):
        from app.main import app

        client = TestClient(app, follow_redirects=False)
        resp = client.get("/api/annotations/mentions/search?q=an")
        assert resp.status_code == 303
        assert resp.headers["location"] == "/auth/login"


# ---------------------------------------------------------------------------
# GET /api/annotations/mentions/search — happy paths
# ---------------------------------------------------------------------------


class TestMentionsSearchResults:
    @patch("app.annotations.routes._connect")
    @patch("app.auth.middleware._get_provider")
    def test_returns_matching_users(self, mock_prov, mock_conn):
        mock_prov.return_value = _stub_provider()

        db = _mock_db_conn(
            rows=[
                (str(_MENTIONED_USER_ID), "Andres Tamm", "andres@min.ee"),
                (str(_ANOTHER_MENTIONED_ID), "Anna Saar", "anna@min.ee"),
            ]
        )
        # Replace the _connect symbol's context-manager return.
        mock_conn.return_value.__enter__ = MagicMock(return_value=db)
        mock_conn.return_value.__exit__ = MagicMock(return_value=False)

        client = _authed_client()
        resp = client.get("/api/annotations/mentions/search?q=an")
        assert resp.status_code == 200
        payload = resp.json()
        assert "results" in payload
        assert len(payload["results"]) == 2
        labels = {r["label"] for r in payload["results"]}
        assert labels == {"Andres Tamm", "Anna Saar"}
        # Each result carries id + label + full_name + email.
        for r in payload["results"]:
            assert set(r.keys()) >= {"id", "label", "full_name", "email"}

        # Verify the SQL query was org-scoped.
        call_args = db.execute.call_args
        sql = call_args[0][0]
        params = call_args[0][1]
        assert "org_id = %s" in sql
        assert "is_active = TRUE" in sql
        assert params[0] == _ORG_ID  # first param is the caller's org_id

    @patch("app.annotations.routes._connect")
    @patch("app.auth.middleware._get_provider")
    def test_empty_query_returns_empty_results(self, mock_prov, mock_conn):
        """A blank/whitespace q must short-circuit before hitting the DB."""
        mock_prov.return_value = _stub_provider()

        db = _mock_db_conn(rows=[])
        mock_conn.return_value.__enter__ = MagicMock(return_value=db)
        mock_conn.return_value.__exit__ = MagicMock(return_value=False)

        client = _authed_client()

        # Missing q
        resp = client.get("/api/annotations/mentions/search")
        assert resp.status_code == 200
        assert resp.json() == {"results": []}

        # Whitespace-only q
        resp = client.get("/api/annotations/mentions/search?q=%20%20")
        assert resp.status_code == 200
        assert resp.json() == {"results": []}

        # No DB call should have happened.
        db.execute.assert_not_called()

    @patch("app.annotations.routes._connect")
    @patch("app.auth.middleware._get_provider")
    def test_no_matches_returns_empty_list(self, mock_prov, mock_conn):
        mock_prov.return_value = _stub_provider()

        db = _mock_db_conn(rows=[])
        mock_conn.return_value.__enter__ = MagicMock(return_value=db)
        mock_conn.return_value.__exit__ = MagicMock(return_value=False)

        client = _authed_client()
        resp = client.get("/api/annotations/mentions/search?q=zzzz")
        assert resp.status_code == 200
        assert resp.json() == {"results": []}

    @patch("app.annotations.routes._connect")
    @patch("app.auth.middleware._get_provider")
    def test_db_error_degrades_to_empty_list(self, mock_prov, mock_conn):
        """A DB failure must never 500 the typeahead — degrade silently."""
        mock_prov.return_value = _stub_provider()

        db = MagicMock()
        db.execute.side_effect = RuntimeError("DB exploded")
        mock_conn.return_value.__enter__ = MagicMock(return_value=db)
        mock_conn.return_value.__exit__ = MagicMock(return_value=False)

        client = _authed_client()
        resp = client.get("/api/annotations/mentions/search?q=an")
        assert resp.status_code == 200
        assert resp.json() == {"results": []}

    @patch("app.annotations.routes._connect")
    @patch("app.auth.middleware._get_provider")
    def test_cross_org_isolation_via_sql_params(self, mock_prov, mock_conn):
        """User in org1 must not see org2 users — verified via SQL param."""
        mock_prov.return_value = _stub_provider(user=_authed_user(org_id=_OTHER_ORG_ID))

        db = _mock_db_conn(rows=[])
        mock_conn.return_value.__enter__ = MagicMock(return_value=db)
        mock_conn.return_value.__exit__ = MagicMock(return_value=False)

        client = _authed_client()
        resp = client.get("/api/annotations/mentions/search?q=an")
        assert resp.status_code == 200

        # The query must have been parameterised with the caller's org_id.
        params = db.execute.call_args[0][1]
        assert params[0] == _OTHER_ORG_ID

    @patch("app.annotations.routes._connect")
    @patch("app.auth.middleware._get_provider")
    def test_missing_org_returns_empty_list(self, mock_prov, mock_conn):
        """An authed user with no org_id (edge case) gets an empty list."""
        mock_prov.return_value = _stub_provider(user=_authed_user(org_id=""))

        client = _authed_client()
        resp = client.get("/api/annotations/mentions/search?q=an")
        assert resp.status_code == 200
        assert resp.json() == {"results": []}


# ---------------------------------------------------------------------------
# notify_annotation_mention — fan-out + self-exclusion
# ---------------------------------------------------------------------------


def _make_annotation_with_mentions(
    *,
    mentions: list[uuid.UUID],
) -> Annotation:
    now = datetime.now(UTC)
    return Annotation(
        id=uuid.UUID("99999999-9999-9999-9999-999999999999"),
        user_id=uuid.UUID(_USER_ID),
        org_id=uuid.UUID(_ORG_ID),
        target_type="impact_report_item",
        target_id="conflict:abc123",
        target_metadata=None,
        content="Tere @Andres ja @Anna — vaadake palun üle.",
        resolved=False,
        resolved_by=None,
        resolved_at=None,
        created_at=now,
        updated_at=now,
        mentions=mentions,
    )


class TestNotifyAnnotationMention:
    @patch("app.notifications.wire.notify")
    def test_notifies_every_mentioned_user_except_self(self, mock_notify):
        """Each mentioned user gets one notify() call; self is skipped."""
        from app.notifications.wire import notify_annotation_mention

        # Author is _USER_ID; they appear in the mention list along with
        # two genuine targets.
        mentions = [
            uuid.UUID(_USER_ID),  # self → must be skipped
            _MENTIONED_USER_ID,
            _ANOTHER_MENTIONED_ID,
        ]
        annotation = _make_annotation_with_mentions(mentions=mentions)

        notify_annotation_mention(
            annotation=annotation,
            mentioned_user_ids=mentions,
            mentioner_user_id=uuid.UUID(_USER_ID),
        )

        # Exactly two notify() calls: one per non-self mention.
        assert mock_notify.call_count == 2
        called_user_ids = {call.kwargs["user_id"] for call in mock_notify.call_args_list}
        assert called_user_ids == {_MENTIONED_USER_ID, _ANOTHER_MENTIONED_ID}

        # Type / title checks.
        for call in mock_notify.call_args_list:
            assert call.kwargs["type"] == "annotation_mention"
            assert call.kwargs["title"] == "Sind mainiti märkuses"
            # Metadata carries annotation_id + mentioner_user_id.
            md = call.kwargs["metadata"]
            assert md["annotation_id"] == str(annotation.id)
            assert md["mentioner_user_id"] == _USER_ID

    @patch("app.notifications.wire.notify")
    def test_empty_mentions_no_notifications(self, mock_notify):
        from app.notifications.wire import notify_annotation_mention

        annotation = _make_annotation_with_mentions(mentions=[])
        notify_annotation_mention(
            annotation=annotation,
            mentioned_user_ids=[],
            mentioner_user_id=uuid.UUID(_USER_ID),
        )
        mock_notify.assert_not_called()

    @patch("app.notifications.wire.notify")
    def test_swallows_exceptions(self, mock_notify):
        """notify_annotation_mention is fire-and-forget — must not raise."""
        from app.notifications.wire import notify_annotation_mention

        mock_notify.side_effect = RuntimeError("DB exploded")

        annotation = _make_annotation_with_mentions(
            mentions=[_MENTIONED_USER_ID],
        )
        # Should not raise.
        notify_annotation_mention(
            annotation=annotation,
            mentioned_user_ids=[_MENTIONED_USER_ID],
            mentioner_user_id=uuid.UUID(_USER_ID),
        )
