"""Unit tests for ``app.drafter.session_model``.

Tests the CRUD helpers for ``drafting_sessions``. All DB access is
mocked — same patterns as ``tests/test_docs_upload.py``.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any
from unittest.mock import MagicMock

import pytest

from app.drafter.session_model import (
    DraftingSession,
    abandon_session,
    create_session,
    create_version_snapshot,
    get_session,
    list_sessions_for_user,
    update_session,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_USER_ID = uuid.UUID("11111111-1111-1111-1111-111111111111")
_ORG_ID = uuid.UUID("22222222-2222-2222-2222-222222222222")
_OTHER_ORG_ID = uuid.UUID("33333333-3333-3333-3333-333333333333")


def _make_session_row(
    *,
    session_id: uuid.UUID | None = None,
    user_id: uuid.UUID = _USER_ID,
    org_id: uuid.UUID = _ORG_ID,
    workflow_type: str = "full_law",
    current_step: int = 1,
    intent: str | None = None,
    clarifications: str = "[]",
    research_data_encrypted: bytes | None = None,
    proposed_structure: str | None = None,
    draft_content_encrypted: bytes | None = None,
    integrated_draft_id: uuid.UUID | None = None,
    status: str = "active",
) -> tuple[Any, ...]:
    """Build a raw cursor row matching _SESSION_COLUMNS order."""
    now = datetime.now(UTC)
    return (
        session_id or uuid.uuid4(),
        user_id,
        org_id,
        workflow_type,
        current_step,
        intent,
        clarifications,
        research_data_encrypted,
        proposed_structure,
        draft_content_encrypted,
        integrated_draft_id,
        status,
        now,
        now,
    )


# ---------------------------------------------------------------------------
# create_session
# ---------------------------------------------------------------------------


class TestCreateSession:
    def test_create_session_returns_session(self):
        conn = MagicMock()
        session_id = uuid.uuid4()
        row = _make_session_row(session_id=session_id)
        conn.execute.return_value.fetchone.return_value = row

        result = create_session(conn, _USER_ID, _ORG_ID, "full_law")

        assert isinstance(result, DraftingSession)
        assert result.id == session_id
        assert result.workflow_type == "full_law"
        assert result.current_step == 1
        assert result.status == "active"
        conn.execute.assert_called_once()

    def test_create_session_rejects_invalid_workflow(self):
        conn = MagicMock()
        with pytest.raises(ValueError, match="Invalid workflow type"):
            create_session(conn, _USER_ID, _ORG_ID, "invalid")

    def test_create_session_vtk_workflow(self):
        conn = MagicMock()
        row = _make_session_row(workflow_type="vtk")
        conn.execute.return_value.fetchone.return_value = row

        result = create_session(conn, _USER_ID, _ORG_ID, "vtk")
        assert result.workflow_type == "vtk"


# ---------------------------------------------------------------------------
# get_session
# ---------------------------------------------------------------------------


class TestGetSession:
    def test_get_session_returns_none_for_missing(self):
        conn = MagicMock()
        conn.execute.return_value.fetchone.return_value = None

        result = get_session(conn, uuid.uuid4())
        assert result is None

    def test_get_session_returns_session(self):
        conn = MagicMock()
        session_id = uuid.uuid4()
        row = _make_session_row(session_id=session_id, intent="Test")
        conn.execute.return_value.fetchone.return_value = row

        result = get_session(conn, session_id)
        assert result is not None
        assert result.id == session_id
        assert result.intent == "Test"

    def test_get_session_handles_db_error(self):
        conn = MagicMock()
        conn.execute.side_effect = RuntimeError("DB error")

        result = get_session(conn, uuid.uuid4())
        assert result is None


# ---------------------------------------------------------------------------
# list_sessions_for_user (org-scoped)
# ---------------------------------------------------------------------------


class TestListSessions:
    def test_list_sessions_org_scoped(self):
        conn = MagicMock()
        row = _make_session_row(user_id=_USER_ID, org_id=_ORG_ID)
        conn.execute.return_value.fetchall.return_value = [row]

        result = list_sessions_for_user(conn, _USER_ID, _ORG_ID)
        assert len(result) == 1

        # Verify the query included org_id parameter
        call_args = conn.execute.call_args
        sql = call_args.args[0]
        params = call_args.args[1]
        assert "org_id" in sql
        assert str(_ORG_ID) in params

    def test_list_sessions_empty(self):
        conn = MagicMock()
        conn.execute.return_value.fetchall.return_value = []

        result = list_sessions_for_user(conn, _USER_ID, _ORG_ID)
        assert result == []


# ---------------------------------------------------------------------------
# update_session
# ---------------------------------------------------------------------------


class TestUpdateSession:
    def test_update_session_bumps_updated_at(self):
        conn = MagicMock()
        session_id = uuid.uuid4()

        update_session(conn, session_id, intent="Updated intent")

        conn.execute.assert_called_once()
        sql = conn.execute.call_args.args[0]
        assert "updated_at = now()" in sql
        assert "intent" in sql

    def test_update_session_ignores_unknown_fields(self):
        conn = MagicMock()
        session_id = uuid.uuid4()

        update_session(conn, session_id, nonexistent_field="val")

        # No SQL should have been executed — unknown field is ignored
        conn.execute.assert_not_called()

    def test_update_session_jsonb_fields(self):
        conn = MagicMock()
        session_id = uuid.uuid4()

        update_session(
            conn,
            session_id,
            clarifications=[{"q": "test", "a": "answer"}],
        )

        conn.execute.assert_called_once()
        sql = conn.execute.call_args.args[0]
        assert "::jsonb" in sql


# ---------------------------------------------------------------------------
# abandon_session
# ---------------------------------------------------------------------------


class TestAbandonSession:
    def test_abandon_session_sets_status(self):
        conn = MagicMock()
        session_id = uuid.uuid4()

        abandon_session(conn, session_id)

        conn.execute.assert_called_once()
        sql = conn.execute.call_args.args[0]
        assert "abandoned" in sql
        assert "updated_at" in sql


# ---------------------------------------------------------------------------
# create_version_snapshot
# ---------------------------------------------------------------------------


class TestVersionSnapshot:
    def test_create_version_snapshot(self):
        conn = MagicMock()
        session_id = uuid.uuid4()
        snapshot = b"encrypted-snapshot-data"

        create_version_snapshot(conn, session_id, 1, snapshot)

        conn.execute.assert_called_once()
        call_args = conn.execute.call_args
        sql = call_args.args[0]
        params = call_args.args[1]
        assert "drafting_session_versions" in sql
        assert str(session_id) in params
        assert 1 in params
        assert snapshot in params
