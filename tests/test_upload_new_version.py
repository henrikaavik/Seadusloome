"""Tests for the new-version upload branch in ``app.docs.upload`` (#618 PR-B).

These tests pin the §4.2 cutover behaviour for the version-upload path:
when the uploader supplies ``parent_draft_id``, the handler creates a
NEW ``draft_versions`` row tied to the existing parent draft instead of
a brand-new ``drafts`` row.  All DB access is mocked via the same
``_wire_upload_conn`` helper used by ``test_docs_upload`` so the tests
never touch Postgres.

Key invariants asserted here:

    * Permission inheritance — new version uses parent's owner_id /
      org_id, NOT the uploader's id (the audit trail of ownership stays
      with the original drafter; the acting user lives in the audit
      log only).
    * Cross-org rejection — a parent owned by a different org surfaces
      as a Estonian DraftUploadError; the same message covers
      "doesn't exist" so cross-org existence is never disclosed.
    * Status gate — only ``ready`` parents accept new versions; mid-
      pipeline parents (parsing/extracting/analyzing) are rejected.
    * Version-number allocation — new version is
      ``MAX(version_number) + 1`` for that parent.
    * Reading-stage progression — new version steps the parent's
      latest reading_stage one notch forward.
    * Graph URI scheme — §9.5 ``...drafts/{parent_id}/v{version_number}``.
    * Audit log — ``draft.version.create`` event recorded with
      uploader_id and parent_draft_id.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime
from typing import Any
from unittest.mock import MagicMock, patch

import psycopg
import pytest

from app.auth.provider import UserDict
from app.docs.draft_model import Draft
from app.docs.upload import _MAX_VERSION_ALLOC_ATTEMPTS, DraftUploadError, handle_upload

# ---------------------------------------------------------------------------
# Shared identities
# ---------------------------------------------------------------------------

_PARENT_DRAFT_ID = uuid.UUID("11111111-1111-1111-1111-111111111111")
_PARENT_USER_ID = uuid.UUID("22222222-2222-2222-2222-222222222222")
_PARENT_ORG_ID = uuid.UUID("33333333-3333-3333-3333-333333333333")
_UPLOADER_USER_ID = "44444444-4444-4444-4444-444444444444"
_OTHER_ORG_ID = uuid.UUID("99999999-9999-9999-9999-999999999999")

# Sample row layouts for the get_draft JOIN result (#618 PR-B):
# 19 columns matching _DRAFT_COLUMNS order.


def _make_parent_row(*, status: str = "ready") -> tuple[Any, ...]:
    """Build a draft row matching ``_row_to_draft``'s column order."""
    now = datetime.now(UTC)
    return (
        _PARENT_DRAFT_ID,
        _PARENT_USER_ID,
        _PARENT_ORG_ID,
        "Parent eelnõu pealkiri",
        "parent.docx",
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        2048,
        "/storage/parent.enc",
        f"https://data.riik.ee/ontology/estleg/drafts/{_PARENT_DRAFT_ID}",
        status,
        None,
        None,
        None,
        now,
        now,
        now,
        "eelnou",
        None,
        None,
    )


def _make_other_org_parent_row() -> tuple[Any, ...]:
    """Parent row owned by a DIFFERENT org from the uploader."""
    row = list(_make_parent_row(status="ready"))
    row[2] = _OTHER_ORG_ID
    return tuple(row)


def _make_version_row(
    *,
    version_id: uuid.UUID | None = None,
    version_number: int = 2,
    reading_stage: str = "reading_1",
    storage_path: str = "/storage/v2.enc",
    graph_uri: str | None = None,
) -> tuple[Any, ...]:
    """Build a draft_versions row (10 columns)."""
    now = datetime.now(UTC)
    return (
        version_id or uuid.uuid4(),
        _PARENT_DRAFT_ID,
        version_number,
        reading_stage,
        None,
        storage_path,
        graph_uri
        or f"https://data.riik.ee/ontology/estleg/drafts/{_PARENT_DRAFT_ID}/v{version_number}",
        "uploaded",
        now,
        _PARENT_USER_ID,
    )


_UNSET = object()


def _uploader(*, org_id: Any = _UNSET) -> UserDict:
    """Build an authed uploader UserDict.

    ``org_id`` defaults to the parent org so the happy-path tests can
    omit it.  Pass ``org_id=None`` explicitly to simulate an uploader
    with no org membership (the upfront validation gate).
    """
    if org_id is _UNSET:
        resolved = str(_PARENT_ORG_ID)
    else:
        resolved = org_id  # may be None
    return {
        "id": _UPLOADER_USER_ID,
        "email": "uploader@seadusloome.ee",
        "full_name": "Test uploader",
        "role": "drafter",
        "org_id": resolved,
        "must_change_password": False,
    }


class _StubUpload:
    """Minimal stand-in for Starlette's ``UploadFile``."""

    def __init__(
        self,
        *,
        filename: str = "v2.docx",
        content_type: str = (
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
        ),
        contents: bytes = b"v2 contents",
    ):
        # Annotated as ``... | None`` so pyright treats the stub as a
        # structural match for ``app.docs.upload._UploadLike`` (which
        # mirrors Starlette's ``UploadFile`` where these attributes are
        # nullable).
        self.filename: str | None = filename
        self.content_type: str | None = content_type
        self.size: int | None = len(contents)
        self._contents = contents

    async def read(self) -> bytes:
        return self._contents


class _ConnectCM:
    """Sync context-manager wrapper around a mock connection."""

    def __init__(self, conn: MagicMock):
        self.conn = conn

    def __enter__(self) -> MagicMock:
        return self.conn

    def __exit__(self, *_: Any) -> bool:
        return False


def _wire_version_conn(
    *,
    parent_row: tuple[Any, ...] | None,
    version_row: tuple[Any, ...] | None = None,
    next_version_max: int = 1,
    refreshed_row: tuple[Any, ...] | None = None,
    latest_version_row: tuple[Any, ...] | None = None,
) -> MagicMock:
    """Build a mock connection that handles every SQL the version path runs.

    The version branch hits the DB in this order:

        1. SELECT ... FROM drafts d LEFT JOIN draft_versions ...
           (initial get_draft for the parent)
        2. SELECT COALESCE(MAX(version_number), 0) FROM draft_versions ...
           (get_next_version_number)
        3. SELECT ... FROM draft_versions ... ORDER BY version_number DESC
           LIMIT 1   (get_latest_version)
        4. INSERT INTO draft_versions ... RETURNING ...
           (create_draft_version)
        5. UPDATE drafts SET status = 'uploaded', filename, ...
           (legacy mirror reset)
        6. SELECT ... FROM drafts d LEFT JOIN draft_versions ...
           (post-write get_draft to refresh)

    The fixture routes each ``execute()`` call to the right return value
    based on the SQL keywords.
    """
    mock_conn = MagicMock()
    parent_select_count = {"value": 0}

    def _execute_side_effect(sql: str, _params: object = None):
        cursor = MagicMock()
        cursor.rowcount = 1
        sql_compact = " ".join(sql.split()).lower()

        # 4. INSERT INTO draft_versions ... RETURNING ...
        if "insert into draft_versions" in sql_compact and "returning" in sql_compact:
            cursor.fetchone.return_value = version_row or _make_version_row()
            return cursor

        # 2. COALESCE(MAX(version_number), 0)
        if "coalesce(max(version_number)" in sql_compact:
            cursor.fetchone.return_value = (next_version_max,)
            return cursor

        # 3. ORDER BY version_number DESC LIMIT 1
        if (
            "from draft_versions" in sql_compact
            and "order by version_number desc" in sql_compact
            and "limit 1" in sql_compact
        ):
            cursor.fetchone.return_value = latest_version_row or _make_version_row(
                version_number=1, reading_stage="vtk"
            )
            return cursor

        # 1 + 6. JOIN-style get_draft.
        if (
            "from drafts d" in sql_compact
            and "left join draft_versions" in sql_compact
            and "where d.id" in sql_compact
        ):
            parent_select_count["value"] += 1
            # Second invocation (post-insert refresh) returns the
            # refreshed row when supplied so tests can verify the
            # JOIN surfaces the new version's status / graph_uri.
            if parent_select_count["value"] >= 2 and refreshed_row is not None:
                cursor.fetchone.return_value = refreshed_row
            else:
                cursor.fetchone.return_value = parent_row
            return cursor

        # 5. UPDATE drafts ... (legacy mirror).
        if "update drafts" in sql_compact:
            return cursor

        # Audit log inserts pass through silently.
        if "insert into audit_log" in sql_compact:
            return cursor

        cursor.fetchone.return_value = None
        return cursor

    mock_conn.execute.side_effect = _execute_side_effect
    return mock_conn


def _make_conn_factory(conn: MagicMock):
    def factory():
        return _ConnectCM(conn)

    return factory


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


class TestNewVersionHappyPath:
    def test_v2_upload_creates_version_row_with_correct_metadata(self):
        """Uploading v2 against a ``ready`` parent must:

        * Create a draft_versions row with version_number=2.
        * Step the reading_stage from the parent's latest (vtk -> reading_1).
        * Use the §9.5 graph URI scheme: ``...drafts/{parent_id}/v2``.
        * Inherit owner_id from the PARENT, not the uploader.
        """
        version_id = uuid.uuid4()
        version_row = _make_version_row(
            version_id=version_id,
            version_number=2,
            reading_stage="reading_1",
            storage_path="/storage/encrypted.enc",
            graph_uri=(f"https://data.riik.ee/ontology/estleg/drafts/{_PARENT_DRAFT_ID}/v2"),
        )
        conn = _wire_version_conn(
            parent_row=_make_parent_row(status="ready"),
            version_row=version_row,
            next_version_max=1,
            latest_version_row=_make_version_row(version_number=1, reading_stage="vtk"),
        )

        stored = MagicMock(
            storage_path="/storage/encrypted.enc",
            size_bytes=11,
            filename="v2.docx",
        )
        mock_queue = MagicMock()

        with patch("app.docs.upload.store_file", return_value=stored):
            asyncio.run(
                handle_upload(
                    _uploader(),
                    "ignored title",  # versions inherit parent title
                    _StubUpload(contents=b"v2 contents"),
                    parent_draft_id=_PARENT_DRAFT_ID,
                    job_queue=mock_queue,
                    conn_factory=_make_conn_factory(conn),
                )
            )

        # Locate the INSERT INTO draft_versions call.
        insert_call = next(
            c
            for c in conn.execute.call_args_list
            if "insert into draft_versions" in " ".join(c.args[0].split()).lower()
        )
        params = insert_call.args[1]
        # (draft_id, version_number, reading_stage, parsed_text_encrypted,
        #  storage_path, graph_uri, status, created_by)
        assert params[0] == str(_PARENT_DRAFT_ID), "version row must point at the parent draft id"
        assert params[1] == 2, "version_number must be MAX(version_number)+1 = 2"
        assert params[2] == "reading_1", (
            "reading_stage must step forward from parent's vtk -> reading_1"
        )
        assert params[4] == "/storage/encrypted.enc"
        assert params[5] == (
            f"https://data.riik.ee/ontology/estleg/drafts/{_PARENT_DRAFT_ID}/v2"
        ), "graph_uri must follow §9.5 ...drafts/{parent_id}/v{version_number}"
        assert params[6] == "uploaded", "new version starts at status='uploaded'"

        # Inherit owner from parent, NOT uploader.
        assert params[7] == str(_PARENT_USER_ID), (
            "created_by must be the PARENT's owner_id, not the uploader's id"
        )

    def test_v3_upload_steps_reading_stage_from_reading_1_to_reading_2(self):
        """Reading-stage progression chains through the legislative pipeline."""
        version_row = _make_version_row(
            version_number=3,
            reading_stage="reading_2",
            graph_uri=(f"https://data.riik.ee/ontology/estleg/drafts/{_PARENT_DRAFT_ID}/v3"),
        )
        conn = _wire_version_conn(
            parent_row=_make_parent_row(status="ready"),
            version_row=version_row,
            next_version_max=2,
            latest_version_row=_make_version_row(version_number=2, reading_stage="reading_1"),
        )

        stored = MagicMock(storage_path="/x.enc", size_bytes=1, filename="v3.docx")
        with patch("app.docs.upload.store_file", return_value=stored):
            asyncio.run(
                handle_upload(
                    _uploader(),
                    "",
                    _StubUpload(contents=b"v3"),
                    parent_draft_id=_PARENT_DRAFT_ID,
                    job_queue=MagicMock(),
                    conn_factory=_make_conn_factory(conn),
                )
            )

        insert_call = next(
            c
            for c in conn.execute.call_args_list
            if "insert into draft_versions" in " ".join(c.args[0].split()).lower()
        )
        assert insert_call.args[1][1] == 3
        assert insert_call.args[1][2] == "reading_2"

    def test_legacy_drafts_mirror_is_reset_to_uploaded(self):
        """The drafts row's status / metadata get reset for the new pipeline run.

        Without this update, the listing UI would still show the
        previous version's terminal status (e.g. ``ready``) while the
        new version is mid-pipeline.
        """
        conn = _wire_version_conn(
            parent_row=_make_parent_row(status="ready"),
            next_version_max=1,
        )
        stored = MagicMock(
            storage_path="/storage/v2.enc",
            size_bytes=4,
            filename="version-two.docx",
        )

        with patch("app.docs.upload.store_file", return_value=stored):
            asyncio.run(
                handle_upload(
                    _uploader(),
                    "",
                    _StubUpload(
                        filename="version-two.docx",
                        contents=b"v2!!",
                    ),
                    parent_draft_id=_PARENT_DRAFT_ID,
                    job_queue=MagicMock(),
                    conn_factory=_make_conn_factory(conn),
                )
            )

        update_call = next(
            c
            for c in conn.execute.call_args_list
            if "update drafts" in " ".join(c.args[0].split()).lower()
            and "set status = %s" in " ".join(c.args[0].split()).lower()
        )
        params = update_call.args[1]
        # (status, filename, content_type, file_size, storage_path,
        #  graph_uri, draft_id)
        assert params[0] == "uploaded"
        assert params[1] == "version-two.docx"
        assert params[3] == 4  # file_size
        assert params[4] == "/storage/v2.enc"
        assert params[6] == str(_PARENT_DRAFT_ID)

    def test_audit_log_records_draft_version_create(self):
        """Every new version triggers ``draft.version.create`` in the audit log
        with the uploader_id (acting user) AND the version metadata.
        """
        conn = _wire_version_conn(
            parent_row=_make_parent_row(status="ready"),
            next_version_max=1,
        )
        stored = MagicMock(storage_path="/x.enc", size_bytes=1, filename="v.docx")

        with (
            patch("app.docs.upload.store_file", return_value=stored),
            patch("app.docs.upload.log_action") as mock_log,
        ):
            asyncio.run(
                handle_upload(
                    _uploader(),
                    "",
                    _StubUpload(contents=b"v"),
                    parent_draft_id=_PARENT_DRAFT_ID,
                    job_queue=MagicMock(),
                    conn_factory=_make_conn_factory(conn),
                )
            )

        # Log helper was invoked with the new-version event.
        mock_log.assert_called_once()
        actor_id, action, detail = mock_log.call_args.args
        assert actor_id == _UPLOADER_USER_ID, (
            "audit log must record the ACTING user (the uploader), not the parent owner"
        )
        assert action == "draft.version.create"
        assert detail["draft_id"] == str(_PARENT_DRAFT_ID)
        assert detail["version_number"] == 2
        assert detail["reading_stage"] == "reading_1"
        assert detail["uploader_id"] == _UPLOADER_USER_ID

    def test_parse_job_is_enqueued_for_the_new_version(self):
        """The parse pipeline starts for the new version's bytes."""
        conn = _wire_version_conn(
            parent_row=_make_parent_row(status="ready"),
            next_version_max=1,
        )
        stored = MagicMock(storage_path="/v2.enc", size_bytes=1, filename="v.docx")
        mock_queue = MagicMock()

        with patch("app.docs.upload.store_file", return_value=stored):
            asyncio.run(
                handle_upload(
                    _uploader(),
                    "",
                    _StubUpload(contents=b"v"),
                    parent_draft_id=_PARENT_DRAFT_ID,
                    job_queue=mock_queue,
                    conn_factory=_make_conn_factory(conn),
                )
            )

        mock_queue.enqueue.assert_called_once()
        call = mock_queue.enqueue.call_args
        assert call.args[0] == "parse_draft"
        # The parse job is keyed on the parent draft_id (not the version
        # id) because the pipeline still treats the draft as the unit
        # of work; the latest version is found via the JOIN in get_draft.
        assert call.args[1] == {"draft_id": str(_PARENT_DRAFT_ID)}


# ---------------------------------------------------------------------------
# Validation rejections
# ---------------------------------------------------------------------------


class TestNewVersionRejections:
    def test_cross_org_parent_returns_estonian_not_found_message(self):
        """A parent owned by a DIFFERENT org must be invisible to the uploader.

        Same Estonian message as "doesn't exist" so cross-org existence
        is never disclosed.
        """
        conn = _wire_version_conn(
            parent_row=_make_other_org_parent_row(),
        )
        stored = MagicMock(storage_path="/x.enc", size_bytes=1, filename="x.docx")
        delete_calls: list[str] = []

        with (
            patch("app.docs.upload.store_file", return_value=stored),
            patch(
                "app.docs.upload.delete_file",
                side_effect=lambda p: delete_calls.append(p),
            ),
        ):
            with pytest.raises(DraftUploadError, match="Vanem-eelnõu ei ole kättesaadav"):
                asyncio.run(
                    handle_upload(
                        _uploader(),
                        "",
                        _StubUpload(contents=b"x"),
                        parent_draft_id=_PARENT_DRAFT_ID,
                        job_queue=MagicMock(),
                        conn_factory=_make_conn_factory(conn),
                    )
                )

        # The orphan file must be cleaned up on validation failure.
        assert delete_calls == ["/x.enc"]

    def test_missing_parent_returns_estonian_not_found_message(self):
        """A parent_draft_id that does not exist surfaces the same message."""
        conn = _wire_version_conn(parent_row=None)
        stored = MagicMock(storage_path="/x.enc", size_bytes=1, filename="x.docx")

        with (
            patch("app.docs.upload.store_file", return_value=stored),
            patch("app.docs.upload.delete_file") as mock_delete,
        ):
            with pytest.raises(DraftUploadError, match="Vanem-eelnõu ei ole kättesaadav"):
                asyncio.run(
                    handle_upload(
                        _uploader(),
                        "",
                        _StubUpload(contents=b"x"),
                        parent_draft_id=_PARENT_DRAFT_ID,
                        job_queue=MagicMock(),
                        conn_factory=_make_conn_factory(conn),
                    )
                )

        mock_delete.assert_called_once_with("/x.enc")

    @pytest.mark.parametrize(
        "parent_status",
        ["uploaded", "parsing", "extracting", "analyzing", "failed"],
    )
    def test_parent_not_ready_is_rejected(self, parent_status: str):
        """Versions can only be layered on ``ready`` parents."""
        conn = _wire_version_conn(
            parent_row=_make_parent_row(status=parent_status),
        )
        stored = MagicMock(storage_path="/x.enc", size_bytes=1, filename="x.docx")

        with (
            patch("app.docs.upload.store_file", return_value=stored),
            patch("app.docs.upload.delete_file") as mock_delete,
        ):
            with pytest.raises(DraftUploadError, match="analüüs on valmis"):
                asyncio.run(
                    handle_upload(
                        _uploader(),
                        "",
                        _StubUpload(contents=b"x"),
                        parent_draft_id=_PARENT_DRAFT_ID,
                        job_queue=MagicMock(),
                        conn_factory=_make_conn_factory(conn),
                    )
                )

        mock_delete.assert_called_once_with("/x.enc")

    def test_uploader_without_org_is_rejected(self):
        """The org_id check happens before any DB call -- no orphan file
        is created because the validation fires before ``store_file``.
        """
        with patch("app.docs.upload.store_file") as mock_store:
            with pytest.raises(DraftUploadError, match="organisatsiooni"):
                asyncio.run(
                    handle_upload(
                        _uploader(org_id=None),  # type: ignore[arg-type]
                        "",
                        _StubUpload(contents=b"x"),
                        parent_draft_id=_PARENT_DRAFT_ID,
                        job_queue=MagicMock(),
                        conn_factory=_make_conn_factory(MagicMock()),
                    )
                )
        mock_store.assert_not_called()

    def test_invalid_parent_uuid_returns_not_found_message(self):
        """Garbage in the parent_draft_id field is rejected -- the
        upfront UUID parse means we never even look the parent up.
        """
        stored = MagicMock(storage_path="/x.enc", size_bytes=1, filename="x.docx")
        with (
            patch("app.docs.upload.store_file", return_value=stored),
            patch("app.docs.upload.delete_file") as mock_delete,
        ):
            with pytest.raises(DraftUploadError, match="Vanem-eelnõu ei ole kättesaadav"):
                asyncio.run(
                    handle_upload(
                        _uploader(),
                        "",
                        _StubUpload(contents=b"x"),
                        parent_draft_id="not-a-uuid",
                        job_queue=MagicMock(),
                        conn_factory=_make_conn_factory(MagicMock()),
                    )
                )
        mock_delete.assert_called_once_with("/x.enc")


# ---------------------------------------------------------------------------
# Returned Draft reflects the new version (post-cutover JOIN)
# ---------------------------------------------------------------------------


class TestReturnedDraftReflectsNewVersion:
    def test_returned_draft_has_new_version_storage_path_and_graph_uri(self):
        """The handler re-fetches the parent through ``get_draft`` so
        the JOIN surfaces the new version's columns.  The returned
        Draft must carry the NEW version's storage_path and graph_uri,
        not the parent's pre-upload values.
        """
        # The "refreshed" row simulates what get_draft returns AFTER
        # the version row is inserted -- its COALESCE picks up the new
        # storage_path and graph_uri from draft_versions.
        new_storage_path = "/storage/v2.enc"
        new_graph_uri = f"https://data.riik.ee/ontology/estleg/drafts/{_PARENT_DRAFT_ID}/v2"
        refreshed_row = list(_make_parent_row(status="uploaded"))
        refreshed_row[7] = new_storage_path
        refreshed_row[8] = new_graph_uri
        refreshed_row[9] = "uploaded"  # status from latest version

        conn = _wire_version_conn(
            parent_row=_make_parent_row(status="ready"),
            refreshed_row=tuple(refreshed_row),
            next_version_max=1,
        )

        stored = MagicMock(
            storage_path=new_storage_path,
            size_bytes=1,
            filename="v.docx",
        )

        with patch("app.docs.upload.store_file", return_value=stored):
            result = asyncio.run(
                handle_upload(
                    _uploader(),
                    "",
                    _StubUpload(contents=b"v"),
                    parent_draft_id=_PARENT_DRAFT_ID,
                    job_queue=MagicMock(),
                    conn_factory=_make_conn_factory(conn),
                )
            )

        assert isinstance(result, Draft)
        assert result.id == _PARENT_DRAFT_ID
        assert result.storage_path == new_storage_path
        assert result.graph_uri == new_graph_uri
        assert result.status == "uploaded"


# ---------------------------------------------------------------------------
# Concurrent version-number allocation (#745)
# ---------------------------------------------------------------------------


def _wire_version_conn_with_insert_failures(
    *,
    fail_first_n: int,
    exc: BaseException,
) -> MagicMock:
    """Like :func:`_wire_version_conn` but the INSERT raises ``exc`` for the
    first *fail_first_n* calls (across the life of this mock conn), then
    succeeds.  Used to simulate a unique-violation race on the version
    number.
    """
    base = _wire_version_conn(
        parent_row=_make_parent_row(status="ready"),
        next_version_max=1,
    )
    original_side_effect = base.execute.side_effect
    insert_calls = {"n": 0}

    def _execute(sql: str, params: object = None):
        if "insert into draft_versions" in " ".join(sql.split()).lower():
            insert_calls["n"] += 1
            if insert_calls["n"] <= fail_first_n:
                raise exc
        return original_side_effect(sql, params)

    base.execute.side_effect = _execute
    return base


def _factory_sequence(conns: list[MagicMock]):
    """Return a conn-factory that hands out each conn in *conns* in turn.

    The upload retry loop calls ``factory()`` once per attempt; this lets a
    test give a distinct (or repeated) mock per attempt.
    """
    it = iter(conns)

    def factory():
        return _ConnectCM(next(it))

    return factory


class TestConcurrentVersionAllocation:
    def test_unique_violation_is_retried_then_succeeds(self):
        """A single version-number collision is transparently retried — the
        upload still returns a Draft and the parse job still fires.
        """
        # Attempt 1: INSERT raises UniqueViolation. Attempt 2: clean conn.
        attempt1 = _wire_version_conn_with_insert_failures(
            fail_first_n=1,
            exc=psycopg.errors.UniqueViolation("duplicate key (draft_id, version_number)"),
        )
        attempt2 = _wire_version_conn(
            parent_row=_make_parent_row(status="ready"),
            next_version_max=1,
        )
        stored = MagicMock(storage_path="/storage/v2.enc", size_bytes=2, filename="v2.docx")
        mock_queue = MagicMock()

        with (
            patch("app.docs.upload.store_file", return_value=stored),
            patch("app.docs.upload.delete_file") as mock_delete,
        ):
            result = asyncio.run(
                handle_upload(
                    _uploader(),
                    "",
                    _StubUpload(contents=b"v2"),
                    parent_draft_id=_PARENT_DRAFT_ID,
                    job_queue=mock_queue,
                    conn_factory=_factory_sequence([attempt1, attempt2]),
                )
            )

        assert isinstance(result, Draft)
        # The encrypted file survived the retry — only a final, exhausted
        # failure should clean it up.
        mock_delete.assert_not_called()
        # Parse pipeline still enqueued exactly once for the parent draft.
        mock_queue.enqueue.assert_called_once()
        assert mock_queue.enqueue.call_args.args[1] == {"draft_id": str(_PARENT_DRAFT_ID)}

    def test_exhausted_retries_raise_controlled_error_and_clean_up_file(self):
        """When every attempt collides, the caller gets a user-facing
        :class:`DraftUploadError` (NOT a raw 500) and the orphaned
        ciphertext is removed.
        """
        conns = [
            _wire_version_conn_with_insert_failures(
                fail_first_n=99,  # always fails
                exc=psycopg.errors.UniqueViolation("duplicate key"),
            )
            for _ in range(_MAX_VERSION_ALLOC_ATTEMPTS)
        ]
        stored = MagicMock(storage_path="/storage/orphan.enc", size_bytes=1, filename="v.docx")

        with (
            patch("app.docs.upload.store_file", return_value=stored),
            patch("app.docs.upload.delete_file") as mock_delete,
        ):
            with pytest.raises(DraftUploadError, match="samaaegse üleslaadimise"):
                asyncio.run(
                    handle_upload(
                        _uploader(),
                        "",
                        _StubUpload(contents=b"v"),
                        parent_draft_id=_PARENT_DRAFT_ID,
                        job_queue=MagicMock(),
                        conn_factory=_factory_sequence(conns),
                    )
                )

        mock_delete.assert_called_once_with("/storage/orphan.enc")

    def test_new_draft_branch_does_not_retry_on_unique_violation(self):
        """The v1/new-draft path must NOT swallow-and-retry a unique
        violation — only the version branch has the retry budget. A
        collision there (e.g. a duplicate-something race) propagates as
        before, with the file cleaned up exactly once.
        """
        mock_conn = MagicMock()
        mock_conn.execute.side_effect = psycopg.errors.UniqueViolation("dup")
        stored = MagicMock(storage_path="/tmp/orphan.enc", size_bytes=1, filename="x.docx")

        with (
            patch("app.docs.upload.store_file", return_value=stored),
            patch("app.docs.upload.delete_file") as mock_delete,
        ):
            with pytest.raises(psycopg.errors.UniqueViolation):
                asyncio.run(
                    handle_upload(
                        _uploader(),
                        "Uus eelnõu",  # new-draft branch: no parent_draft_id
                        _StubUpload(contents=b"x"),
                        job_queue=MagicMock(),
                        conn_factory=_make_conn_factory(mock_conn),
                    )
                )
        mock_delete.assert_called_once_with("/tmp/orphan.enc")
