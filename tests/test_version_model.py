"""Unit tests for ``app.docs.version_model``.

All DB access is mocked via ``unittest.mock.MagicMock`` — same pattern as
``tests/test_drafter_session_model.py``.  No live database is required.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any
from unittest.mock import MagicMock

import pytest

from app.docs.version_model import (
    READING_STAGES,
    DraftVersion,
    get_draft_version,
    get_latest_version,
    list_versions_for_draft,
)

# ---------------------------------------------------------------------------
# Shared constants
# ---------------------------------------------------------------------------

_DRAFT_ID = uuid.UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
_USER_ID = uuid.UUID("bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb")
_VERSION_ID = uuid.UUID("cccccccc-cccc-cccc-cccc-cccccccccccc")


# ---------------------------------------------------------------------------
# Raw-row builder
# ---------------------------------------------------------------------------


def _make_version_row(
    *,
    version_id: uuid.UUID | None = None,
    draft_id: uuid.UUID = _DRAFT_ID,
    version_number: int = 1,
    reading_stage: str = "vtk",
    parsed_text_encrypted: bytes | None = None,
    storage_path: str = "/storage/drafts/file.docx.enc",
    graph_uri: str = "urn:draft:aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
    status: str = "ready",
    created_by: uuid.UUID = _USER_ID,
) -> tuple[Any, ...]:
    """Build a raw cursor row matching the _VERSION_COLUMNS SELECT order."""
    now = datetime.now(UTC)
    return (
        version_id or uuid.uuid4(),
        draft_id,
        version_number,
        reading_stage,
        parsed_text_encrypted,
        storage_path,
        graph_uri,
        status,
        now,
        created_by,
    )


# ---------------------------------------------------------------------------
# READING_STAGES constant
# ---------------------------------------------------------------------------


class TestReadingStages:
    def test_tuple_contains_five_stages(self):
        assert len(READING_STAGES) == 5

    def test_vtk_is_first(self):
        assert READING_STAGES[0] == "vtk"

    def test_enacted_is_last(self):
        assert READING_STAGES[-1] == "enacted"

    def test_all_expected_values_present(self):
        assert set(READING_STAGES) == {
            "vtk",
            "reading_1",
            "reading_2",
            "reading_3",
            "enacted",
        }


# ---------------------------------------------------------------------------
# get_draft_version
# ---------------------------------------------------------------------------


class TestGetDraftVersion:
    def test_returns_dataclass_on_hit(self):
        conn = MagicMock()
        row = _make_version_row(version_id=_VERSION_ID)
        conn.execute.return_value.fetchone.return_value = row

        result = get_draft_version(conn, _VERSION_ID)

        assert isinstance(result, DraftVersion)
        assert result.id == _VERSION_ID
        assert result.draft_id == _DRAFT_ID
        assert result.version_number == 1
        assert result.reading_stage == "vtk"
        assert result.parsed_text_encrypted is None
        assert result.storage_path == "/storage/drafts/file.docx.enc"
        assert result.graph_uri == "urn:draft:aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
        assert result.status == "ready"
        assert result.created_by == _USER_ID

    def test_returns_none_on_miss(self):
        conn = MagicMock()
        conn.execute.return_value.fetchone.return_value = None

        result = get_draft_version(conn, uuid.uuid4())
        assert result is None

    def test_returns_none_on_db_error(self):
        conn = MagicMock()
        conn.execute.side_effect = RuntimeError("connection lost")

        result = get_draft_version(conn, _VERSION_ID)
        assert result is None

    def test_query_passes_version_id_as_string(self):
        conn = MagicMock()
        conn.execute.return_value.fetchone.return_value = _make_version_row()

        get_draft_version(conn, _VERSION_ID)

        call_args = conn.execute.call_args
        params = call_args.args[1]
        assert str(_VERSION_ID) in params

    def test_accepts_string_id(self):
        conn = MagicMock()
        conn.execute.return_value.fetchone.return_value = _make_version_row(version_id=_VERSION_ID)

        result = get_draft_version(conn, str(_VERSION_ID))
        assert result is not None
        assert result.id == _VERSION_ID

    def test_encrypted_bytes_preserved(self):
        conn = MagicMock()
        encrypted = b"\x01\x02\x03encrypted_ciphertext"
        row = _make_version_row(version_id=_VERSION_ID, parsed_text_encrypted=encrypted)
        conn.execute.return_value.fetchone.return_value = row

        result = get_draft_version(conn, _VERSION_ID)
        assert result is not None
        assert result.parsed_text_encrypted == encrypted


# ---------------------------------------------------------------------------
# list_versions_for_draft
# ---------------------------------------------------------------------------


class TestListVersionsForDraft:
    def test_returns_list_of_dataclasses(self):
        conn = MagicMock()
        rows = [
            _make_version_row(version_number=2, reading_stage="reading_1"),
            _make_version_row(version_number=1, reading_stage="vtk"),
        ]
        conn.execute.return_value.fetchall.return_value = rows

        result = list_versions_for_draft(conn, _DRAFT_ID)

        assert len(result) == 2
        assert all(isinstance(v, DraftVersion) for v in result)
        assert result[0].version_number == 2
        assert result[1].version_number == 1

    def test_returns_empty_list_when_no_versions(self):
        conn = MagicMock()
        conn.execute.return_value.fetchall.return_value = []

        result = list_versions_for_draft(conn, _DRAFT_ID)
        assert result == []

    def test_returns_empty_list_on_db_error(self):
        conn = MagicMock()
        conn.execute.side_effect = RuntimeError("timeout")

        result = list_versions_for_draft(conn, _DRAFT_ID)
        assert result == []

    def test_query_filters_by_draft_id(self):
        conn = MagicMock()
        conn.execute.return_value.fetchall.return_value = []

        list_versions_for_draft(conn, _DRAFT_ID)

        call_args = conn.execute.call_args
        sql = call_args.args[0]
        params = call_args.args[1]
        assert "draft_id" in sql
        assert str(_DRAFT_ID) in params

    def test_query_orders_by_version_number_desc(self):
        conn = MagicMock()
        conn.execute.return_value.fetchall.return_value = []

        list_versions_for_draft(conn, _DRAFT_ID)

        sql = conn.execute.call_args.args[0]
        # ORDER BY clause must sort descending so latest version is first
        assert "version_number DESC" in sql

    def test_accepts_string_draft_id(self):
        conn = MagicMock()
        conn.execute.return_value.fetchall.return_value = []

        # Should not raise
        list_versions_for_draft(conn, str(_DRAFT_ID))
        conn.execute.assert_called_once()


# ---------------------------------------------------------------------------
# get_latest_version
# ---------------------------------------------------------------------------


class TestGetLatestVersion:
    def test_returns_highest_version(self):
        conn = MagicMock()
        # DB returns the highest version (ORDER BY version_number DESC LIMIT 1)
        row = _make_version_row(version_number=3, reading_stage="reading_2")
        conn.execute.return_value.fetchone.return_value = row

        result = get_latest_version(conn, _DRAFT_ID)

        assert result is not None
        assert result.version_number == 3
        assert result.reading_stage == "reading_2"

    def test_returns_none_when_no_versions_exist(self):
        conn = MagicMock()
        conn.execute.return_value.fetchone.return_value = None

        result = get_latest_version(conn, _DRAFT_ID)
        assert result is None

    def test_returns_none_on_db_error(self):
        conn = MagicMock()
        conn.execute.side_effect = Exception("DB gone")

        result = get_latest_version(conn, _DRAFT_ID)
        assert result is None

    def test_query_uses_limit_1(self):
        conn = MagicMock()
        conn.execute.return_value.fetchone.return_value = None

        get_latest_version(conn, _DRAFT_ID)

        sql = conn.execute.call_args.args[0]
        assert "LIMIT 1" in sql

    def test_query_orders_desc(self):
        conn = MagicMock()
        conn.execute.return_value.fetchone.return_value = None

        get_latest_version(conn, _DRAFT_ID)

        sql = conn.execute.call_args.args[0]
        assert "version_number DESC" in sql

    def test_v1_returned_when_only_one_version(self):
        conn = MagicMock()
        row = _make_version_row(version_number=1, reading_stage="vtk")
        conn.execute.return_value.fetchone.return_value = row

        result = get_latest_version(conn, _DRAFT_ID)
        assert result is not None
        assert result.version_number == 1

    def test_query_filters_by_draft_id(self):
        conn = MagicMock()
        conn.execute.return_value.fetchone.return_value = None

        get_latest_version(conn, _DRAFT_ID)

        call_args = conn.execute.call_args
        params = call_args.args[1]
        assert str(_DRAFT_ID) in params


# ---------------------------------------------------------------------------
# DraftVersion dataclass immutability
# ---------------------------------------------------------------------------


class TestDraftVersionDataclass:
    def test_is_frozen(self):
        """DraftVersion is frozen=True — mutation must raise FrozenInstanceError."""
        from dataclasses import FrozenInstanceError

        version = DraftVersion(
            id=uuid.uuid4(),
            draft_id=_DRAFT_ID,
            version_number=1,
            reading_stage="vtk",
            parsed_text_encrypted=None,
            storage_path="/enc",
            graph_uri="urn:draft:test",
            status="uploaded",
            created_at=datetime.now(UTC),
            created_by=_USER_ID,
        )
        with pytest.raises(FrozenInstanceError):
            version.version_number = 99  # type: ignore[misc]

    def test_fields_are_uuid_not_string(self):
        """id and draft_id must be uuid.UUID, not plain strings."""
        conn = MagicMock()
        row = _make_version_row(version_id=_VERSION_ID)
        conn.execute.return_value.fetchone.return_value = row

        result = get_draft_version(conn, _VERSION_ID)
        assert result is not None
        assert isinstance(result.id, uuid.UUID)
        assert isinstance(result.draft_id, uuid.UUID)
        assert isinstance(result.created_by, uuid.UUID)
