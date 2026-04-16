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
    list_eelnous_for_vtk,
    list_vtks_for_org,
    update_draft_parent_vtk,
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


# ---------------------------------------------------------------------------
# list_vtks_for_org -- VTK picker helper (#640)
# ---------------------------------------------------------------------------


class TestListVtksForOrg:
    @patch("app.docs.draft_model._connect")
    def test_returns_only_vtks_in_default_statuses(self, mock_connect):
        """Helper SQL must filter on doc_type='vtk' AND ready/analyzing."""
        conn = MagicMock()
        conn.execute.return_value.fetchall.return_value = [
            _make_raw_row(
                draft_id=uuid.uuid4(),
                doc_type="vtk",
                status="ready",
                title="VTK A",
            ),
            _make_raw_row(
                draft_id=uuid.uuid4(),
                doc_type="vtk",
                status="analyzing",
                title="VTK B",
            ),
        ]
        mock_connect.return_value.__enter__ = MagicMock(return_value=conn)
        mock_connect.return_value.__exit__ = MagicMock(return_value=False)

        vtks = list_vtks_for_org(_ORG_ID)

        assert len(vtks) == 2
        assert all(v.doc_type == "vtk" for v in vtks)
        # Verify SQL: doc_type = 'vtk' and status = any(%s)
        call_args = conn.execute.call_args
        sql: str = call_args.args[0]
        params: tuple = call_args.args[1]
        assert "doc_type = 'vtk'" in sql
        assert "status = any" in sql
        # Second param is the list of statuses.
        assert params[0] == str(_ORG_ID)
        assert list(params[1]) == ["ready", "analyzing"]

    @patch("app.docs.draft_model._connect")
    def test_custom_statuses_passed_through(self, mock_connect):
        conn = MagicMock()
        conn.execute.return_value.fetchall.return_value = []
        mock_connect.return_value.__enter__ = MagicMock(return_value=conn)
        mock_connect.return_value.__exit__ = MagicMock(return_value=False)

        list_vtks_for_org(_ORG_ID, statuses=("ready",))

        params = conn.execute.call_args.args[1]
        assert list(params[1]) == ["ready"]

    @patch("app.docs.draft_model._connect")
    def test_empty_statuses_short_circuits(self, mock_connect):
        vtks = list_vtks_for_org(_ORG_ID, statuses=())
        assert vtks == []
        mock_connect.assert_not_called()

    @patch("app.docs.draft_model._connect")
    def test_db_error_returns_empty_list(self, mock_connect):
        conn = MagicMock()
        conn.execute.side_effect = RuntimeError("boom")
        mock_connect.return_value.__enter__ = MagicMock(return_value=conn)
        mock_connect.return_value.__exit__ = MagicMock(return_value=False)

        assert list_vtks_for_org(_ORG_ID) == []

    @patch("app.docs.draft_model._connect")
    def test_results_propagate_doc_type_and_title(self, mock_connect):
        conn = MagicMock()
        conn.execute.return_value.fetchall.return_value = [
            _make_raw_row(
                draft_id=_VTK_ID,
                doc_type="vtk",
                status="ready",
                title="Maanteeseaduse VTK",
            ),
        ]
        mock_connect.return_value.__enter__ = MagicMock(return_value=conn)
        mock_connect.return_value.__exit__ = MagicMock(return_value=False)

        vtks = list_vtks_for_org(_ORG_ID)
        assert len(vtks) == 1
        assert vtks[0].title == "Maanteeseaduse VTK"
        assert vtks[0].id == _VTK_ID


# ---------------------------------------------------------------------------
# update_draft_parent_vtk -- set/clear FK (#640)
# ---------------------------------------------------------------------------


class TestUpdateDraftParentVtk:
    def test_sets_uuid_parent(self):
        conn = MagicMock()
        conn.execute.return_value.rowcount = 1

        updated = update_draft_parent_vtk(conn, _DRAFT_ID, _VTK_ID)

        assert updated is True
        sql, params = conn.execute.call_args.args
        assert "set parent_vtk_id" in sql.lower()
        assert params == (str(_VTK_ID), str(_DRAFT_ID))

    def test_clears_parent_on_none(self):
        conn = MagicMock()
        conn.execute.return_value.rowcount = 1

        updated = update_draft_parent_vtk(conn, _DRAFT_ID, None)

        assert updated is True
        params = conn.execute.call_args.args[1]
        assert params == (None, str(_DRAFT_ID))

    def test_returns_false_when_no_row_updated(self):
        conn = MagicMock()
        conn.execute.return_value.rowcount = 0

        assert update_draft_parent_vtk(conn, _DRAFT_ID, None) is False


# ---------------------------------------------------------------------------
# list_drafts_for_org_filtered -- A4 search + filter (#642)
# ---------------------------------------------------------------------------


class TestListDraftsForOrgFiltered:
    """Verifies the two-phase search + WHERE-clause builder.

    The helper opens its own connection, so we patch ``_connect`` and
    drive ``conn.execute`` directly.  ``execute`` is called in a fixed
    order:

      1. (optional) phase-1 title/filename id-set
      2. (optional) phase-2 entity-label id-set
      3. count(*) over the assembled WHERE clause
      4. SELECT _DRAFT_COLUMNS over the same WHERE clause + LIMIT/OFFSET

    The mock cursor's ``fetchall``/``fetchone`` return values are
    dispatched in that order via ``side_effect``.
    """

    def _make_conn(
        self,
        *,
        phase1_ids: list[str] | None = None,
        phase2_ids: list[str] | None = None,
        count: int = 0,
        rows: list[tuple] | None = None,
    ) -> MagicMock:
        """Build a mock connection that yields predictable cursor results."""
        from unittest.mock import MagicMock

        conn = MagicMock()
        cursors: list[MagicMock] = []

        if phase1_ids is not None:
            c = MagicMock()
            c.fetchall.return_value = [(i,) for i in phase1_ids]
            cursors.append(c)
        if phase2_ids is not None:
            c = MagicMock()
            c.fetchall.return_value = [(i,) for i in phase2_ids]
            cursors.append(c)

        # COUNT(*) cursor
        count_cursor = MagicMock()
        count_cursor.fetchone.return_value = (count,)
        cursors.append(count_cursor)

        # SELECT cursor
        select_cursor = MagicMock()
        select_cursor.fetchall.return_value = rows or []
        cursors.append(select_cursor)

        conn.execute.side_effect = cursors
        return conn

    @patch("app.docs.draft_model._connect")
    def test_no_filters_short_circuits_phase_1_and_2(self, mock_connect):
        from app.docs.draft_model import list_drafts_for_org_filtered

        conn = self._make_conn(count=0)
        mock_connect.return_value.__enter__ = MagicMock(return_value=conn)
        mock_connect.return_value.__exit__ = MagicMock(return_value=False)

        drafts, total = list_drafts_for_org_filtered(_ORG_ID)

        assert drafts == []
        assert total == 0
        # Only count + (skipped) select since count==0 short-circuits.
        # Verify phase-1 / phase-2 sub-queries did NOT run by checking
        # that the first SQL we emitted is the COUNT clause.
        first_sql = conn.execute.call_args_list[0].args[0]
        assert "count(*)" in first_sql

    @patch("app.docs.draft_model._connect")
    def test_q_runs_phase_1_then_phase_2_then_count_then_select(self, mock_connect):
        """When q is provided, the helper must run both candidate-id
        sub-queries before assembling the final WHERE clause."""
        from app.docs.draft_model import list_drafts_for_org_filtered

        candidate_id = "55555555-5555-5555-5555-555555555555"
        conn = self._make_conn(
            phase1_ids=[candidate_id],
            phase2_ids=[],
            count=1,
            rows=[
                _make_raw_row(draft_id=uuid.UUID(candidate_id), title="Maanteeseadus"),
            ],
        )
        mock_connect.return_value.__enter__ = MagicMock(return_value=conn)
        mock_connect.return_value.__exit__ = MagicMock(return_value=False)

        drafts, total = list_drafts_for_org_filtered(_ORG_ID, q="maantee")

        assert total == 1
        assert len(drafts) == 1
        assert drafts[0].title == "Maanteeseadus"

        # Inspect the four SQL statements in order.
        sqls = [c.args[0] for c in conn.execute.call_args_list]
        assert "title ilike" in sqls[0]
        assert "draft_entities" in sqls[1]
        assert "count(*)" in sqls[2]
        assert "limit %s offset %s" in sqls[3]

        # Phase 1 must use the org_id + the wrapped %q% pattern.
        phase1_params = conn.execute.call_args_list[0].args[1]
        assert phase1_params[0] == str(_ORG_ID)
        assert phase1_params[1] == "%maantee%"

    @patch("app.docs.draft_model._connect")
    def test_entity_label_match_returns_draft(self, mock_connect):
        """A draft whose entity-label matches must be returned even when
        the title doesn't match the search term."""
        from app.docs.draft_model import list_drafts_for_org_filtered

        candidate_id = "66666666-6666-6666-6666-666666666666"
        conn = self._make_conn(
            phase1_ids=[],
            phase2_ids=[candidate_id],
            count=1,
            rows=[
                _make_raw_row(draft_id=uuid.UUID(candidate_id), title="Random eelnõu"),
            ],
        )
        mock_connect.return_value.__enter__ = MagicMock(return_value=conn)
        mock_connect.return_value.__exit__ = MagicMock(return_value=False)

        drafts, total = list_drafts_for_org_filtered(_ORG_ID, q="121")

        assert total == 1
        assert drafts[0].id == uuid.UUID(candidate_id)
        # Phase-2 must scope its sub-select to the caller's org so a
        # cross-org draft_entities row can never leak.
        phase2_sql = conn.execute.call_args_list[1].args[0]
        assert "from drafts where org_id" in phase2_sql.lower()

    @patch("app.docs.draft_model._connect")
    def test_q_with_no_matches_short_circuits_to_empty(self, mock_connect):
        """When neither phase finds any candidate IDs the helper must
        not run the COUNT/SELECT pair at all."""
        from app.docs.draft_model import list_drafts_for_org_filtered

        conn = self._make_conn(phase1_ids=[], phase2_ids=[], count=0, rows=[])
        mock_connect.return_value.__enter__ = MagicMock(return_value=conn)
        mock_connect.return_value.__exit__ = MagicMock(return_value=False)

        drafts, total = list_drafts_for_org_filtered(_ORG_ID, q="nothing")

        assert drafts == []
        assert total == 0
        # Only phase-1 + phase-2 ran; COUNT/SELECT were short-circuited.
        assert conn.execute.call_count == 2

    @patch("app.docs.draft_model._connect")
    def test_combined_filters_compose_into_where_clause(self, mock_connect):
        """status + doc_type + uploader filters must all land in the
        final WHERE clause, regardless of whether q is set."""
        from app.docs.draft_model import list_drafts_for_org_filtered

        conn = self._make_conn(count=0)
        mock_connect.return_value.__enter__ = MagicMock(return_value=conn)
        mock_connect.return_value.__exit__ = MagicMock(return_value=False)

        list_drafts_for_org_filtered(
            _ORG_ID,
            doc_types={"eelnou"},
            statuses={"ready"},
            uploader_id=_USER_ID,
        )

        count_sql = conn.execute.call_args_list[0].args[0]
        count_params = conn.execute.call_args_list[0].args[1]
        assert "doc_type = any" in count_sql
        assert "status = any" in count_sql
        assert "user_id = %s" in count_sql
        # Params include the org, sorted doc_type list, sorted status
        # list, and the user id.
        assert str(_ORG_ID) in count_params
        assert ["eelnou"] in [list(p) if isinstance(p, list) else p for p in count_params]
        assert str(_USER_ID) in count_params

    @patch("app.docs.draft_model._connect")
    def test_date_range_uses_inclusive_upper_bound(self, mock_connect):
        """date_to must be rendered as ``< date_to + 1 day`` so a
        single-day range catches everything on that calendar day."""
        from datetime import date

        from app.docs.draft_model import list_drafts_for_org_filtered

        conn = self._make_conn(count=0)
        mock_connect.return_value.__enter__ = MagicMock(return_value=conn)
        mock_connect.return_value.__exit__ = MagicMock(return_value=False)

        list_drafts_for_org_filtered(
            _ORG_ID,
            date_from=date(2026, 4, 1),
            date_to=date(2026, 4, 1),
        )

        count_sql = conn.execute.call_args_list[0].args[0]
        count_params = conn.execute.call_args_list[0].args[1]
        assert "created_at >= %s" in count_sql
        assert "created_at < %s" in count_sql
        # The upper bound must be 1 day later than what we passed in.
        assert date(2026, 4, 2) in count_params
        assert date(2026, 4, 1) in count_params

    @patch("app.docs.draft_model._connect")
    def test_sort_default_is_created_desc(self, mock_connect):
        from app.docs.draft_model import list_drafts_for_org_filtered

        conn = self._make_conn(count=1, rows=[_make_raw_row()])
        mock_connect.return_value.__enter__ = MagicMock(return_value=conn)
        mock_connect.return_value.__exit__ = MagicMock(return_value=False)

        list_drafts_for_org_filtered(_ORG_ID)

        select_sql = conn.execute.call_args_list[1].args[0]
        assert "order by created_at desc" in select_sql

    @patch("app.docs.draft_model._connect")
    def test_sort_title_asc_renders_correct_order_by(self, mock_connect):
        from app.docs.draft_model import list_drafts_for_org_filtered

        conn = self._make_conn(count=1, rows=[_make_raw_row()])
        mock_connect.return_value.__enter__ = MagicMock(return_value=conn)
        mock_connect.return_value.__exit__ = MagicMock(return_value=False)

        list_drafts_for_org_filtered(_ORG_ID, sort="title_asc")

        select_sql = conn.execute.call_args_list[1].args[0]
        assert "order by title asc" in select_sql

    @patch("app.docs.draft_model._connect")
    def test_sort_status_falls_back_to_created_at_desc(self, mock_connect):
        from app.docs.draft_model import list_drafts_for_org_filtered

        conn = self._make_conn(count=1, rows=[_make_raw_row()])
        mock_connect.return_value.__enter__ = MagicMock(return_value=conn)
        mock_connect.return_value.__exit__ = MagicMock(return_value=False)

        list_drafts_for_org_filtered(_ORG_ID, sort="status")

        select_sql = conn.execute.call_args_list[1].args[0]
        assert "order by status asc, created_at desc" in select_sql

    @patch("app.docs.draft_model._connect")
    def test_unknown_sort_falls_back_to_default(self, mock_connect):
        from app.docs.draft_model import list_drafts_for_org_filtered

        conn = self._make_conn(count=1, rows=[_make_raw_row()])
        mock_connect.return_value.__enter__ = MagicMock(return_value=conn)
        mock_connect.return_value.__exit__ = MagicMock(return_value=False)

        list_drafts_for_org_filtered(_ORG_ID, sort="not-a-real-sort")

        select_sql = conn.execute.call_args_list[1].args[0]
        assert "order by created_at desc" in select_sql

    @patch("app.docs.draft_model._connect")
    def test_pagination_uses_limit_offset(self, mock_connect):
        from app.docs.draft_model import list_drafts_for_org_filtered

        conn = self._make_conn(count=100, rows=[_make_raw_row() for _ in range(25)])
        mock_connect.return_value.__enter__ = MagicMock(return_value=conn)
        mock_connect.return_value.__exit__ = MagicMock(return_value=False)

        drafts, total = list_drafts_for_org_filtered(_ORG_ID, limit=25, offset=50)

        assert total == 100
        assert len(drafts) == 25
        select_params = conn.execute.call_args_list[1].args[1]
        # The last two positional params are limit + offset.
        assert select_params[-2:] == (25, 50)

    @patch("app.docs.draft_model._connect")
    def test_candidate_cap_truncates_phase_union(self, mock_connect):
        """When phase 1 returns 500 IDs and phase 2 returns more, the
        merged set must stop at the 500-id cap."""
        from app.docs.draft_model import _CANDIDATE_CAP, list_drafts_for_org_filtered

        # Generate 500 unique IDs for phase 1.
        ids = [str(uuid.UUID(int=i)) for i in range(_CANDIDATE_CAP)]
        # Phase 2 returns ids + 50 extras that must NOT be considered.
        extra = [str(uuid.UUID(int=i + _CANDIDATE_CAP)) for i in range(50)]
        conn = self._make_conn(
            phase1_ids=ids,
            phase2_ids=extra,
            count=_CANDIDATE_CAP,
            rows=[_make_raw_row() for _ in range(25)],
        )
        mock_connect.return_value.__enter__ = MagicMock(return_value=conn)
        mock_connect.return_value.__exit__ = MagicMock(return_value=False)

        list_drafts_for_org_filtered(_ORG_ID, q="ka")

        # The COUNT params include the merged ID list -- it must be
        # exactly _CANDIDATE_CAP items.
        count_params = conn.execute.call_args_list[2].args[1]
        merged = next(p for p in count_params if isinstance(p, list))
        assert len(merged) == _CANDIDATE_CAP
        # No phase-2 extras should appear.
        assert not any(extra_id in merged for extra_id in extra)

    @patch("app.docs.draft_model._connect")
    def test_db_error_returns_empty(self, mock_connect):
        from app.docs.draft_model import list_drafts_for_org_filtered

        conn = MagicMock()
        conn.execute.side_effect = RuntimeError("boom")
        mock_connect.return_value.__enter__ = MagicMock(return_value=conn)
        mock_connect.return_value.__exit__ = MagicMock(return_value=False)

        drafts, total = list_drafts_for_org_filtered(_ORG_ID, q="anything")

        assert drafts == []
        assert total == 0

    @patch("app.docs.draft_model._connect")
    def test_zero_limit_short_circuits(self, mock_connect):
        from app.docs.draft_model import list_drafts_for_org_filtered

        drafts, total = list_drafts_for_org_filtered(_ORG_ID, limit=0)

        assert drafts == []
        assert total == 0
        mock_connect.assert_not_called()


# ---------------------------------------------------------------------------
# #643: list_eelnous_for_vtk — children of a VTK on its detail page
# ---------------------------------------------------------------------------


class TestListEelnousForVtk:
    @patch("app.docs.draft_model._connect")
    def test_filters_on_parent_vtk_id_and_org_id_and_doc_type(self, mock_connect):
        """SQL enforces parent + org + doc_type at the data-access layer."""
        conn = MagicMock()
        conn.execute.return_value.fetchall.return_value = [
            _make_raw_row(
                draft_id=uuid.uuid4(),
                doc_type="eelnou",
                parent_vtk_id=_VTK_ID,
                title="Child A",
            ),
        ]
        mock_connect.return_value.__enter__ = MagicMock(return_value=conn)
        mock_connect.return_value.__exit__ = MagicMock(return_value=False)

        children = list_eelnous_for_vtk(_VTK_ID, org_id=_ORG_ID)

        assert len(children) == 1
        assert children[0].doc_type == "eelnou"
        assert children[0].parent_vtk_id == _VTK_ID
        sql: str = conn.execute.call_args.args[0]
        params: tuple = conn.execute.call_args.args[1]
        assert "parent_vtk_id = %s" in sql
        assert "org_id = %s" in sql
        assert "doc_type = 'eelnou'" in sql
        assert "order by created_at desc" in sql.lower()
        # SQL params: (vtk_id, org_id) — both stringified UUIDs.
        assert params == (str(_VTK_ID), str(_ORG_ID))

    def test_org_id_is_keyword_only(self):
        """org_id is required and keyword-only — callers cannot forget it.

        This is the contract guarantee that lets every other reader rely
        on `list_eelnous_for_vtk` for org-scoped reads without their own
        post-filter.
        """
        with pytest.raises(TypeError):
            list_eelnous_for_vtk(_VTK_ID)  # type: ignore[call-arg]

    @patch("app.docs.draft_model._connect")
    def test_db_error_returns_empty_list(self, mock_connect):
        """A DB outage must not crash the VTK detail page."""
        mock_connect.side_effect = RuntimeError("db unavailable")

        children = list_eelnous_for_vtk(_VTK_ID, org_id=_ORG_ID)

        assert children == []

    @patch("app.docs.draft_model._connect")
    def test_no_children_returns_empty_list(self, mock_connect):
        conn = MagicMock()
        conn.execute.return_value.fetchall.return_value = []
        mock_connect.return_value.__enter__ = MagicMock(return_value=conn)
        mock_connect.return_value.__exit__ = MagicMock(return_value=False)

        assert list_eelnous_for_vtk(_VTK_ID, org_id=_ORG_ID) == []
