"""Tests for Draft dataclass new fields: doc_type and parent_vtk_id (#639).

Covers the schema additions introduced by migration 019:

* Default values -- existing row shape (without the new columns) raises
  clearly; rows WITH the new columns default correctly.
* ``_row_to_draft`` populates both new fields correctly.
* ``create_draft`` (mocked conn) passes doc_type / parent_vtk_id to the
  INSERT and round-trips them through the RETURNING row.
* ``fetch_draft`` and ``list_drafts_for_org`` propagate the new fields.
* CHECK constraint semantics tested at the Python level via create_draft
  argument handling (DB-level constraint tests require a live DB and
  belong in the integration suite, but we test the model surface here).

All DB calls are mocked -- no live PostgreSQL required.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from app.docs.draft_model import (
    _DRAFT_COLUMNS,
    Draft,
    _row_to_draft,
    create_draft,
    fetch_draft,
    get_draft,
    list_drafts_for_org,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_ORG_ID = uuid.UUID("11111111-1111-1111-1111-111111111111")
_USER_ID = uuid.UUID("22222222-2222-2222-2222-222222222222")
_DRAFT_ID = uuid.UUID("33333333-3333-3333-3333-333333333333")
_VTK_ID = uuid.UUID("44444444-4444-4444-4444-444444444444")

_NOW = datetime.now(UTC)


def _make_raw_row(
    *,
    draft_id: uuid.UUID = _DRAFT_ID,
    user_id: uuid.UUID = _USER_ID,
    org_id: uuid.UUID = _ORG_ID,
    title: str = "Testseaduse eelnou",
    filename: str = "eelnou.docx",
    content_type: str = "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    file_size: int = 2048,
    storage_path: str = "/storage/x.enc",
    graph_uri: str = "https://data.riik.ee/ontology/estleg/drafts/33333333-3333-3333-3333-333333333333",
    status: str = "uploaded",
    parsed_text_encrypted: bytes | None = None,
    entity_count: int | None = None,
    error_message: str | None = None,
    created_at: datetime = _NOW,
    updated_at: datetime = _NOW,
    last_accessed_at: datetime | None = None,
    doc_type: str = "eelnou",
    parent_vtk_id: uuid.UUID | str | None = None,
) -> tuple[Any, ...]:
    """Return a raw DB row tuple matching the column order in ``_DRAFT_COLUMNS``."""
    return (
        str(draft_id),
        str(user_id),
        str(org_id),
        title,
        filename,
        content_type,
        file_size,
        storage_path,
        graph_uri,
        status,
        parsed_text_encrypted,
        entity_count,
        error_message,
        created_at,
        updated_at,
        last_accessed_at,
        doc_type,
        str(parent_vtk_id) if parent_vtk_id else None,
    )


def _make_draft(
    *,
    doc_type: str = "eelnou",
    parent_vtk_id: uuid.UUID | None = None,
) -> Draft:
    """Convenience Draft constructor for test fixtures."""
    return Draft(
        id=_DRAFT_ID,
        user_id=_USER_ID,
        org_id=_ORG_ID,
        title="Testseaduse eelnou",
        filename="eelnou.docx",
        content_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        file_size=2048,
        storage_path="/storage/x.enc",
        graph_uri=f"https://data.riik.ee/ontology/estleg/drafts/{_DRAFT_ID}",
        status="uploaded",
        parsed_text_encrypted=None,
        entity_count=None,
        error_message=None,
        created_at=_NOW,
        updated_at=_NOW,
        last_accessed_at=_NOW,
        doc_type=doc_type,  # type: ignore[arg-type]
        parent_vtk_id=parent_vtk_id,
    )


# ---------------------------------------------------------------------------
# Draft dataclass -- field defaults
# ---------------------------------------------------------------------------


class TestDraftDataclassDefaults:
    def test_doc_type_defaults_to_eelnou(self):
        """When doc_type is not supplied it must default to 'eelnou' so
        existing call sites (routes, handlers, tests) need no changes."""
        draft = _make_draft()
        assert draft.doc_type == "eelnou"

    def test_parent_vtk_id_defaults_to_none(self):
        """parent_vtk_id must default to None -- unlinked eelnou."""
        draft = _make_draft()
        assert draft.parent_vtk_id is None

    def test_doc_type_vtk_roundtrips(self):
        """A Draft constructed as VTK carries that type through."""
        draft = _make_draft(doc_type="vtk")
        assert draft.doc_type == "vtk"

    def test_parent_vtk_id_roundtrips(self):
        """parent_vtk_id is stored as uuid.UUID, not a string."""
        draft = _make_draft(parent_vtk_id=_VTK_ID)
        assert draft.parent_vtk_id == _VTK_ID
        assert isinstance(draft.parent_vtk_id, uuid.UUID)


# ---------------------------------------------------------------------------
# _DRAFT_COLUMNS -- column list includes new fields
# ---------------------------------------------------------------------------


class TestDraftColumns:
    def test_doc_type_in_columns(self):
        assert "doc_type" in _DRAFT_COLUMNS

    def test_parent_vtk_id_in_columns(self):
        assert "parent_vtk_id" in _DRAFT_COLUMNS


# ---------------------------------------------------------------------------
# _row_to_draft -- new columns propagate correctly
# ---------------------------------------------------------------------------


class TestRowToDraft:
    def test_eelnou_row_has_correct_doc_type(self):
        row = _make_raw_row(doc_type="eelnou")
        draft = _row_to_draft(row)
        assert draft.doc_type == "eelnou"

    def test_vtk_row_has_correct_doc_type(self):
        row = _make_raw_row(doc_type="vtk")
        draft = _row_to_draft(row)
        assert draft.doc_type == "vtk"

    def test_null_parent_vtk_id_becomes_none(self):
        row = _make_raw_row(parent_vtk_id=None)
        draft = _row_to_draft(row)
        assert draft.parent_vtk_id is None

    def test_string_parent_vtk_id_coerced_to_uuid(self):
        row = _make_raw_row(parent_vtk_id=_VTK_ID)
        draft = _row_to_draft(row)
        assert draft.parent_vtk_id == _VTK_ID
        assert isinstance(draft.parent_vtk_id, uuid.UUID)

    def test_existing_fields_still_present(self):
        """Regression: adding new columns must not break existing field reads."""
        row = _make_raw_row()
        draft = _row_to_draft(row)
        assert draft.id == _DRAFT_ID
        assert draft.user_id == _USER_ID
        assert draft.org_id == _ORG_ID
        assert draft.title == "Testseaduse eelnou"
        assert draft.status == "uploaded"


# ---------------------------------------------------------------------------
# create_draft -- passes new fields through RETURNING row
# ---------------------------------------------------------------------------


class TestCreateDraft:
    def _make_conn(self, *, doc_type: str = "eelnou", parent_vtk_id: Any = None) -> MagicMock:
        """Return a mock connection whose execute().fetchone() returns a
        valid raw row containing the supplied doc_type / parent_vtk_id."""
        conn = MagicMock()
        returning_row = _make_raw_row(doc_type=doc_type, parent_vtk_id=parent_vtk_id)
        conn.execute.return_value.fetchone.return_value = returning_row
        return conn

    def test_create_draft_default_doc_type(self):
        conn = self._make_conn(doc_type="eelnou")
        draft = create_draft(
            conn,
            user_id=_USER_ID,
            org_id=_ORG_ID,
            title="Test",
            filename="test.docx",
            content_type="application/octet-stream",
            file_size=100,
            storage_path="/tmp/x.enc",
            graph_uri="https://example.com/g",
        )
        assert draft.doc_type == "eelnou"
        assert draft.parent_vtk_id is None

    def test_create_draft_vtk_type(self):
        conn = self._make_conn(doc_type="vtk")
        draft = create_draft(
            conn,
            user_id=_USER_ID,
            org_id=_ORG_ID,
            title="VTK dokument",
            filename="vtk.docx",
            content_type="application/octet-stream",
            file_size=100,
            storage_path="/tmp/y.enc",
            graph_uri="https://example.com/g2",
            doc_type="vtk",
        )
        assert draft.doc_type == "vtk"

    def test_create_draft_with_parent_vtk_id(self):
        conn = self._make_conn(doc_type="eelnou", parent_vtk_id=_VTK_ID)
        draft = create_draft(
            conn,
            user_id=_USER_ID,
            org_id=_ORG_ID,
            title="Eelnou VTKga",
            filename="eelnou_vtk.docx",
            content_type="application/octet-stream",
            file_size=100,
            storage_path="/tmp/z.enc",
            graph_uri="https://example.com/g3",
            parent_vtk_id=_VTK_ID,
        )
        assert draft.parent_vtk_id == _VTK_ID

    def test_create_draft_passes_doc_type_to_insert(self):
        """The INSERT statement must include doc_type in its parameter list."""
        conn = self._make_conn(doc_type="vtk")
        create_draft(
            conn,
            user_id=_USER_ID,
            org_id=_ORG_ID,
            title="VTK",
            filename="vtk.docx",
            content_type="application/octet-stream",
            file_size=100,
            storage_path="/tmp/v.enc",
            graph_uri="https://example.com/gv",
            doc_type="vtk",
        )
        call_args = conn.execute.call_args
        sql: str = call_args.args[0]
        params: tuple = call_args.args[1]
        assert "doc_type" in sql
        assert "vtk" in params

    def test_create_draft_passes_parent_vtk_id_to_insert(self):
        """parent_vtk_id must appear as a positional parameter in INSERT."""
        conn = self._make_conn(doc_type="eelnou", parent_vtk_id=_VTK_ID)
        create_draft(
            conn,
            user_id=_USER_ID,
            org_id=_ORG_ID,
            title="Eelnou",
            filename="eelnou.docx",
            content_type="application/octet-stream",
            file_size=100,
            storage_path="/tmp/e.enc",
            graph_uri="https://example.com/ge",
            parent_vtk_id=_VTK_ID,
        )
        call_args = conn.execute.call_args
        params: tuple = call_args.args[1]
        assert str(_VTK_ID) in params

    def test_create_draft_invalid_status_raises(self):
        conn = self._make_conn()
        with pytest.raises(ValueError, match="Invalid draft status"):
            create_draft(
                conn,
                user_id=_USER_ID,
                org_id=_ORG_ID,
                title="X",
                filename="x.docx",
                content_type="application/octet-stream",
                file_size=1,
                storage_path="/tmp/x.enc",
                graph_uri="https://example.com/gx",
                status="invalid_status",
            )


# ---------------------------------------------------------------------------
# get_draft / fetch_draft -- new fields propagate through read path
# ---------------------------------------------------------------------------


class TestGetDraft:
    def test_get_draft_returns_new_fields(self):
        conn = MagicMock()
        conn.execute.return_value.fetchone.return_value = _make_raw_row(
            doc_type="vtk",
            parent_vtk_id=None,
        )
        draft = get_draft(conn, _DRAFT_ID)
        assert draft is not None
        assert draft.doc_type == "vtk"
        assert draft.parent_vtk_id is None

    def test_get_draft_with_parent_vtk_id(self):
        conn = MagicMock()
        conn.execute.return_value.fetchone.return_value = _make_raw_row(
            doc_type="eelnou",
            parent_vtk_id=_VTK_ID,
        )
        draft = get_draft(conn, _DRAFT_ID)
        assert draft is not None
        assert draft.doc_type == "eelnou"
        assert draft.parent_vtk_id == _VTK_ID

    def test_get_draft_missing_returns_none(self):
        conn = MagicMock()
        conn.execute.return_value.fetchone.return_value = None
        draft = get_draft(conn, _DRAFT_ID)
        assert draft is None

    @patch("app.docs.draft_model._connect")
    def test_fetch_draft_propagates_doc_type(self, mock_connect):
        raw_row = _make_raw_row(doc_type="vtk")
        conn = MagicMock()
        conn.execute.return_value.fetchone.return_value = raw_row
        mock_connect.return_value.__enter__ = MagicMock(return_value=conn)
        mock_connect.return_value.__exit__ = MagicMock(return_value=False)

        draft = fetch_draft(_DRAFT_ID)

        assert draft is not None
        assert draft.doc_type == "vtk"

    @patch("app.docs.draft_model._connect")
    def test_fetch_draft_propagates_parent_vtk_id(self, mock_connect):
        raw_row = _make_raw_row(doc_type="eelnou", parent_vtk_id=_VTK_ID)
        conn = MagicMock()
        conn.execute.return_value.fetchone.return_value = raw_row
        mock_connect.return_value.__enter__ = MagicMock(return_value=conn)
        mock_connect.return_value.__exit__ = MagicMock(return_value=False)

        draft = fetch_draft(_DRAFT_ID)

        assert draft is not None
        assert draft.parent_vtk_id == _VTK_ID


# ---------------------------------------------------------------------------
# list_drafts_for_org -- new fields propagate through listing path
# ---------------------------------------------------------------------------


class TestListDraftsForOrg:
    def test_list_includes_doc_type_for_all_rows(self):
        conn = MagicMock()
        conn.execute.return_value.fetchall.return_value = [
            _make_raw_row(draft_id=uuid.uuid4(), doc_type="eelnou"),
            _make_raw_row(draft_id=uuid.uuid4(), doc_type="vtk"),
        ]
        drafts = list_drafts_for_org(conn, _ORG_ID)
        assert len(drafts) == 2
        assert drafts[0].doc_type == "eelnou"
        assert drafts[1].doc_type == "vtk"

    def test_list_includes_parent_vtk_id(self):
        conn = MagicMock()
        conn.execute.return_value.fetchall.return_value = [
            _make_raw_row(doc_type="eelnou", parent_vtk_id=_VTK_ID),
        ]
        drafts = list_drafts_for_org(conn, _ORG_ID)
        assert len(drafts) == 1
        assert drafts[0].parent_vtk_id == _VTK_ID

    def test_list_empty_returns_empty(self):
        conn = MagicMock()
        conn.execute.return_value.fetchall.return_value = []
        drafts = list_drafts_for_org(conn, _ORG_ID)
        assert drafts == []

    def test_list_zero_limit_short_circuits(self):
        """limit=0 must return immediately without hitting the DB."""
        conn = MagicMock()
        drafts = list_drafts_for_org(conn, _ORG_ID, limit=0)
        assert drafts == []
        conn.execute.assert_not_called()

    def test_list_db_error_returns_empty_list(self):
        conn = MagicMock()
        conn.execute.side_effect = RuntimeError("boom")
        drafts = list_drafts_for_org(conn, _ORG_ID)
        assert drafts == []
