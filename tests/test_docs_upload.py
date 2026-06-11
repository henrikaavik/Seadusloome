"""Unit tests for ``app.docs.upload.handle_upload``.

These tests never touch Postgres, real Fernet keys, or the filesystem
root: we patch ``store_file`` / ``delete_file`` and hand ``handle_upload``
an in-memory stub for ``UploadFile`` + ``JobQueue``. The goal is to
lock down the validation and cleanup contract — the happy path
(encrypt → insert → enqueue) plus every rejection and rollback branch.
"""

from __future__ import annotations

import io
import uuid
import zipfile
from datetime import UTC, datetime
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from app.auth.provider import UserDict
from app.docs.draft_model import Draft
from app.docs.upload import DraftUploadError, handle_upload

# ---------------------------------------------------------------------------
# Shared fixtures / helpers
# ---------------------------------------------------------------------------


def _user(org_id: str | None = "org-1") -> UserDict:
    return {
        "id": "user-1",
        "email": "koostaja@seadusloome.ee",
        "full_name": "Test Koostaja",
        "role": "drafter",
        "org_id": org_id,
        "must_change_password": False,
    }


def _docx_bytes(payload: str = "Test sisu") -> bytes:
    """Build a minimal structurally-valid .docx (OOXML ZIP) for tests.

    #858 added magic-byte sniffing + ZIP central-directory validation to
    the upload path, so test uploads must be real ZIP containers — raw
    junk bytes are now (correctly) rejected.
    """
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("[Content_Types].xml", "<Types/>")
        zf.writestr("word/document.xml", f"<w:document>{payload}</w:document>")
    return buf.getvalue()


#: Minimal bytes carrying the ``%PDF-`` magic header (#858).
_PDF_BYTES = b"%PDF-1.4\n1 0 obj\n<< >>\nendobj\ntrailer\n<< >>\n%%EOF\n"


class _StubUpload:
    """Minimal stand-in for ``starlette.datastructures.UploadFile``.

    Mirrors Starlette's incremental ``read(size)`` contract (#858) and
    records read traffic so tests can assert the handler's bounded-read
    behaviour: ``read_calls`` collects the requested window sizes and
    ``bytes_served`` the total bytes handed out.
    """

    def __init__(
        self,
        *,
        filename: str | None = "eelnou.docx",
        content_type: str
        | None = "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        size: int | None = 1024,
        contents: bytes | None = None,
    ):
        self.filename = filename
        self.content_type = content_type
        self.size = size
        self._contents = contents if contents is not None else _docx_bytes()
        self._offset = 0
        self.read_calls: list[int] = []
        self.bytes_served = 0

    async def read(self, size: int = -1) -> bytes:
        self.read_calls.append(size)
        if size < 0:
            chunk = self._contents[self._offset :]
        else:
            chunk = self._contents[self._offset : self._offset + size]
        self._offset += len(chunk)
        self.bytes_served += len(chunk)
        return chunk


def _make_draft(draft_id: uuid.UUID | None = None, **overrides: Any) -> Draft:
    """Build a ``Draft`` dataclass with sensible defaults."""
    now = datetime.now(UTC)
    base: dict[str, Any] = {
        "id": draft_id or uuid.uuid4(),
        "user_id": uuid.UUID("00000000-0000-0000-0000-000000000001"),
        "org_id": uuid.UUID("00000000-0000-0000-0000-000000000002"),
        "title": "Test eelnõu",
        "filename": "eelnou.docx",
        "content_type": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        "file_size": 1024,
        "storage_path": "/tmp/ciphertext.enc",
        "graph_uri": "https://data.riik.ee/ontology/estleg/drafts/pending-abc",
        "status": "uploaded",
        "parsed_text": None,
        "entity_count": None,
        "error_message": None,
        "created_at": now,
        "updated_at": now,
    }
    base.update(overrides)
    return Draft(**base)


class _ConnectCM:
    """Context-manager wrapper around a cursor mock.

    ``handle_upload`` enters a ``with _connect() as conn:`` block so the
    mock we inject must expose ``__enter__`` / ``__exit__`` — a plain
    MagicMock won't unwrap cleanly.
    """

    def __init__(self, conn: MagicMock):
        self.conn = conn

    def __enter__(self) -> MagicMock:
        return self.conn

    def __exit__(self, *_: Any) -> bool:
        return False


def _make_conn_factory(conn: MagicMock):
    """Return a callable that looks like ``get_connection``."""

    def factory() -> _ConnectCM:
        return _ConnectCM(conn)

    return factory


def _wire_upload_conn(
    *,
    draft_row: tuple[Any, ...],
    version_row: tuple[Any, ...],
) -> MagicMock:
    """Build a mock connection that handles BOTH inserts in handle_upload.

    Post-#618 PR-B the upload flow runs two ``INSERT ... RETURNING``
    statements (drafts then draft_versions) plus a few ``UPDATE``s.
    A single ``fetchone.return_value`` no longer works because the
    drafts INSERT expects 19 columns while the version INSERT expects
    10.  The side_effect routes each fetchone() based on the SQL
    actually being executed.
    """
    mock_conn = MagicMock()

    def _execute_side_effect(sql: str, _params: object = None):
        cursor = MagicMock()
        sql_lower = sql.lower()
        if "into drafts" in sql_lower and "returning" in sql_lower:
            cursor.fetchone.return_value = draft_row
        elif "into draft_versions" in sql_lower and "returning" in sql_lower:
            cursor.fetchone.return_value = version_row
        else:
            cursor.fetchone.return_value = None
        cursor.rowcount = 1
        return cursor

    mock_conn.execute.side_effect = _execute_side_effect
    return mock_conn


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


class TestHandleUploadHappyPath:
    def test_happy_path_returns_draft_and_enqueues_job(self):
        import asyncio

        draft_id = uuid.UUID("11111111-1111-1111-1111-111111111111")
        user_uuid = uuid.UUID("22222222-2222-2222-2222-222222222222")
        org_uuid = uuid.UUID("33333333-3333-3333-3333-333333333333")
        version_id = uuid.UUID("99999999-9999-9999-9999-999999999999")
        now = datetime.now(UTC)
        # _DRAFT_COLUMNS order (19 cols)
        draft_row = (
            draft_id,
            user_uuid,
            org_uuid,
            "Test eelnõu",
            "eelnou.docx",
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            1024,
            "/tmp/encrypted.enc",
            "https://data.riik.ee/ontology/estleg/drafts/pending-placeholder",
            "uploaded",
            None,
            None,
            None,
            now,
            now,
            now,  # last_accessed_at (#572)
            "eelnou",  # doc_type (#639)
            None,  # parent_vtk_id (#639)
            None,  # processing_completed_at (#670)
        )
        # _VERSION_COLUMNS order (10 cols) for the v1 INSERT...RETURNING.
        version_row = (
            version_id,
            draft_id,
            1,
            "vtk",
            None,
            "/tmp/encrypted.enc",
            f"https://data.riik.ee/ontology/estleg/drafts/{draft_id}",
            "uploaded",
            now,
            user_uuid,
        )

        # The handler calls execute() multiple times:
        #   1. INSERT INTO drafts ... RETURNING -> draft_row (fetchone)
        #   2. UPDATE drafts SET graph_uri ...  -> no fetch
        #   3. INSERT INTO draft_versions ... RETURNING -> version_row (fetchone)
        #
        # We can't simply set fetchone.return_value because the SAME
        # cursor mock is reused.  Instead, route fetchone via a side_effect
        # that returns the right shape based on the SQL of the latest
        # call.
        mock_conn = MagicMock()
        captured_sqls: list[str] = []

        def _execute_side_effect(sql: str, _params: object = None):
            captured_sqls.append(sql)
            cursor = MagicMock()
            sql_lower = sql.lower()
            if "into drafts" in sql_lower and "returning" in sql_lower:
                cursor.fetchone.return_value = draft_row
            elif "into draft_versions" in sql_lower and "returning" in sql_lower:
                cursor.fetchone.return_value = version_row
            else:
                cursor.fetchone.return_value = None
            cursor.rowcount = 1
            return cursor

        mock_conn.execute.side_effect = _execute_side_effect

        stored = MagicMock(
            storage_path="/tmp/encrypted.enc",
            size_bytes=9,
            filename="eelnou.docx",
        )
        mock_queue = MagicMock()
        mock_queue.enqueue.return_value = 42

        with patch("app.docs.upload.store_file", return_value=stored) as mock_store:
            draft = asyncio.run(
                handle_upload(
                    _user(),
                    "Test eelnõu",
                    _StubUpload(contents=_docx_bytes()),
                    job_queue=mock_queue,
                    conn_factory=_make_conn_factory(mock_conn),
                )
            )

        # File was encrypted + persisted.
        mock_store.assert_called_once()
        store_kwargs = mock_store.call_args.kwargs
        assert store_kwargs["filename"] == "eelnou.docx"
        assert store_kwargs["owner_id"] == "user-1"

        # Returned draft has the right shape.
        assert isinstance(draft, Draft)
        assert draft.id == draft_id
        assert draft.title == "Test eelnõu"
        assert draft.filename == "eelnou.docx"
        assert draft.status == "uploaded"
        # graph_uri was patched to the canonical form after insert.
        assert draft.graph_uri == f"https://data.riik.ee/ontology/estleg/drafts/{draft_id}"

        # DB transaction committed.
        mock_conn.commit.assert_called_once()

        # #618 PR-B: a v1 draft_versions row must have been created in the
        # SAME transaction as the drafts INSERT.
        assert any(
            "into draft_versions" in s.lower() and "returning" in s.lower() for s in captured_sqls
        ), (
            "handle_upload must INSERT into draft_versions for the v1 row of every "
            "new draft (§4.2 cutover, #618 PR-B)"
        )

        # Job was enqueued with the correct payload.
        mock_queue.enqueue.assert_called_once()
        call = mock_queue.enqueue.call_args
        assert call.args[0] == "parse_draft"
        assert call.args[1] == {"draft_id": str(draft_id)}
        assert call.kwargs.get("priority") == 0


# ---------------------------------------------------------------------------
# Validation rejections
# ---------------------------------------------------------------------------


class TestHandleUploadValidation:
    def _run(
        self,
        *,
        title: str = "Test eelnõu",
        filename: str | None = "eelnou.docx",
        content_type: str
        | None = "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        contents: bytes | None = None,
        size: int | None = None,
        user: UserDict | None = None,
    ) -> Draft:
        """Invoke handle_upload synchronously for a validation test."""
        import asyncio

        conn = MagicMock()
        conn.execute.return_value.fetchone.return_value = None
        upload = _StubUpload(
            filename=filename,
            content_type=content_type,
            size=size,
            contents=contents,
        )
        with patch("app.docs.upload.store_file") as mock_store:
            stored = MagicMock(storage_path="/tmp/x.enc", size_bytes=len(contents or b""))
            mock_store.return_value = stored
            return asyncio.run(
                handle_upload(
                    user or _user(),
                    title,
                    upload,
                    job_queue=MagicMock(),
                    conn_factory=_make_conn_factory(conn),
                )
            )

    def test_rejects_empty_title(self):
        with pytest.raises(DraftUploadError, match="Pealkiri on kohustuslik"):
            self._run(title="   ")

    def test_rejects_title_over_200_chars(self):
        with pytest.raises(DraftUploadError, match="Pealkiri on liiga pikk"):
            self._run(title="x" * 201)

    def test_accepts_title_at_exact_limit(self):
        """Title of exactly 200 characters must pass validation."""
        # This test short-circuits before the DB insert because we mock
        # fetchone to return a row — so we only need to ensure no error
        # is raised during validation.
        now = datetime.now(UTC)
        draft_id = uuid.uuid4()
        version_id = uuid.uuid4()
        user_uuid = uuid.UUID("44444444-4444-4444-4444-444444444444")
        draft_row = (
            draft_id,
            user_uuid,
            uuid.UUID("55555555-5555-5555-5555-555555555555"),
            "x" * 200,
            "eelnou.docx",
            "application/pdf",
            4,
            "/tmp/x.enc",
            "pending",
            "uploaded",
            None,
            None,
            None,
            now,
            now,
            now,  # last_accessed_at (#572)
            "eelnou",  # doc_type (#639)
            None,  # parent_vtk_id (#639)
            None,  # processing_completed_at (#670)
        )
        version_row = (
            version_id,
            draft_id,
            1,
            "vtk",
            None,
            "/tmp/x.enc",
            f"https://data.riik.ee/ontology/estleg/drafts/{draft_id}",
            "uploaded",
            now,
            user_uuid,
        )
        conn = _wire_upload_conn(draft_row=draft_row, version_row=version_row)
        import asyncio

        with patch("app.docs.upload.store_file") as mock_store:
            mock_store.return_value = MagicMock(
                storage_path="/tmp/x.enc", size_bytes=4, filename="eelnou.pdf"
            )
            draft = asyncio.run(
                handle_upload(
                    _user(),
                    "x" * 200,
                    _StubUpload(
                        filename="eelnou.pdf",
                        content_type="application/pdf",
                        contents=_PDF_BYTES,
                    ),
                    job_queue=MagicMock(),
                    conn_factory=_make_conn_factory(conn),
                )
            )
        assert draft.title == "x" * 200

    def test_rejects_invalid_extension(self):
        with pytest.raises(DraftUploadError, match="Toetamata failitüüp"):
            self._run(filename="eelnou.txt")

    def test_rejects_missing_filename(self):
        with pytest.raises(DraftUploadError, match="Faili nimi puudub"):
            self._run(filename=None)

    def test_rejects_bad_content_type(self):
        with pytest.raises(DraftUploadError, match="Toetamata failitüüp"):
            self._run(
                filename="eelnou.docx",
                content_type="text/html",
            )

    def test_rejects_empty_file(self):
        with pytest.raises(DraftUploadError, match="tühi"):
            self._run(contents=b"")

    def test_rejects_oversized_file(self, monkeypatch: pytest.MonkeyPatch):
        """With MAX_UPLOAD_SIZE_MB=1, any file over 1 MB must be rejected."""
        monkeypatch.setenv("MAX_UPLOAD_SIZE_MB", "1")
        oversize = b"x" * (2 * 1024 * 1024)
        with pytest.raises(DraftUploadError, match="Fail on liiga suur"):
            self._run(contents=oversize)

    def test_rejects_user_without_org(self):
        with pytest.raises(DraftUploadError, match="organisatsiooni"):
            self._run(user=_user(org_id=None))


# ---------------------------------------------------------------------------
# #858 — resource bounds + content sniffing
# ---------------------------------------------------------------------------


class TestUploadResourceBounds:
    """Declared-size rejection, bounded incremental read, magic bytes,
    and .docx zip-bomb caps (#858).
    """

    def _attempt(self, upload: _StubUpload, *, title: str = "Test eelnõu") -> Draft:
        import asyncio

        conn = MagicMock()
        conn.execute.return_value.fetchone.return_value = None
        with patch("app.docs.upload.store_file") as mock_store:
            mock_store.return_value = MagicMock(storage_path="/tmp/x.enc")
            return asyncio.run(
                handle_upload(
                    _user(),
                    title,
                    upload,
                    job_queue=MagicMock(),
                    conn_factory=_make_conn_factory(conn),
                )
            )

    def test_declared_size_rejected_before_any_read(self, monkeypatch: pytest.MonkeyPatch):
        """An over-limit ``upload.size`` declaration is rejected BEFORE the
        handler reads a single byte of the body."""
        monkeypatch.setenv("MAX_UPLOAD_SIZE_MB", "1")
        upload = _StubUpload(size=2 * 1024 * 1024, contents=_docx_bytes())
        with pytest.raises(DraftUploadError, match="Fail on liiga suur"):
            self._attempt(upload)
        assert upload.read_calls == [], "oversized declared size must reject pre-read"

    def test_bounded_read_never_buffers_past_cap(self, monkeypatch: pytest.MonkeyPatch):
        """A lying / absent size declaration still cannot make the handler
        slurp the full body: the incremental reader stops at limit+1 bytes
        (the no-memory-spike contract)."""
        monkeypatch.setenv("MAX_UPLOAD_SIZE_MB", "1")
        limit = 1024 * 1024
        upload = _StubUpload(size=None, contents=b"x" * (5 * limit))
        with pytest.raises(DraftUploadError, match="Fail on liiga suur"):
            self._attempt(upload)
        # Bounded-read assertions: at most limit+1 bytes ever pulled, and
        # every read used an explicit window (never a read(-1) full slurp).
        assert upload.bytes_served <= limit + 1
        assert upload.read_calls, "expected incremental reads"
        assert all(window >= 0 for window in upload.read_calls)

    def test_renamed_executable_docx_rejected(self):
        """A PE executable renamed to .docx fails magic-byte sniffing with
        an Estonian message."""
        exe = b"MZ\x90\x00" + b"\x00" * 64
        with pytest.raises(DraftUploadError, match="ei vasta .docx vormingule"):
            self._attempt(_StubUpload(contents=exe))

    def test_renamed_executable_pdf_rejected(self):
        exe = b"MZ\x90\x00" + b"\x00" * 64
        with pytest.raises(DraftUploadError, match="ei vasta .pdf vormingule"):
            self._attempt(
                _StubUpload(
                    filename="eelnou.pdf",
                    content_type="application/pdf",
                    contents=exe,
                )
            )

    def test_corrupt_docx_zip_rejected(self):
        """Correct ZIP magic but a broken central directory → Estonian
        'corrupt file' rejection (not a stack trace)."""
        junk = b"PK\x03\x04" + b"\x00" * 128
        with pytest.raises(DraftUploadError, match="vigane või rikutud"):
            self._attempt(_StubUpload(contents=junk))

    def test_docx_zip_bomb_rejected_by_absolute_cap(self, monkeypatch: pytest.MonkeyPatch):
        """A tiny upload claiming a huge uncompressed payload trips the
        absolute uncompressed-size cap (10 × upload limit)."""
        monkeypatch.setenv("MAX_UPLOAD_SIZE_MB", "1")  # absolute cap = 10 MiB
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.writestr("[Content_Types].xml", "<Types/>")
            zf.writestr("word/document.xml", b"\x00" * (16 * 1024 * 1024))
        bomb = buf.getvalue()
        assert len(bomb) < 1024 * 1024, "fixture must stay under the upload cap"
        with pytest.raises(DraftUploadError, match="pakitud sisu on lubatust"):
            self._attempt(_StubUpload(contents=bomb))

    def test_docx_zip_bomb_rejected_by_ratio_cap(self):
        """At the default 50 MB limit the absolute cap is 500 MB, so a
        12 MiB-claiming / ~12 KiB-compressed bomb must be caught by the
        expansion-ratio cap instead."""
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.writestr("[Content_Types].xml", "<Types/>")
            zf.writestr("word/document.xml", b"\x00" * (12 * 1024 * 1024))
        bomb = buf.getvalue()
        assert len(bomb) * 100 < 12 * 1024 * 1024, "fixture must exceed the ratio cap"
        with pytest.raises(DraftUploadError, match="pakitud sisu on lubatust"):
            self._attempt(_StubUpload(contents=bomb))

    def test_valid_docx_passes_content_checks(self):
        """The minimal valid .docx sails through sniffing + zip caps and
        reaches the storage layer (failure here would be a regression in
        the checks, not the fixture)."""
        upload = _StubUpload(contents=_docx_bytes())
        with patch("app.docs.upload.store_file") as mock_store:
            mock_store.side_effect = RuntimeError("stop after validation")
            import asyncio

            conn = MagicMock()
            with pytest.raises(RuntimeError, match="stop after validation"):
                asyncio.run(
                    handle_upload(
                        _user(),
                        "Test eelnõu",
                        upload,
                        job_queue=MagicMock(),
                        conn_factory=_make_conn_factory(conn),
                    )
                )
        mock_store.assert_called_once()


# ---------------------------------------------------------------------------
# Rollback on DB failure
# ---------------------------------------------------------------------------


class TestHandleUploadRollback:
    def test_db_insert_failure_deletes_encrypted_file(self):
        import asyncio

        mock_conn = MagicMock()
        # Make the INSERT raise — this simulates a unique-violation or
        # other post-file-storage failure.
        mock_conn.execute.side_effect = RuntimeError("Simulated DB failure")

        stored = MagicMock(
            storage_path="/tmp/orphaned.enc",
            size_bytes=4,
            filename="eelnou.docx",
        )

        with (
            patch("app.docs.upload.store_file", return_value=stored),
            patch("app.docs.upload.delete_file") as mock_delete,
        ):
            with pytest.raises(RuntimeError, match="Simulated DB failure"):
                asyncio.run(
                    handle_upload(
                        _user(),
                        "Test eelnõu",
                        _StubUpload(contents=_docx_bytes()),
                        job_queue=MagicMock(),
                        conn_factory=_make_conn_factory(mock_conn),
                    )
                )

        # The encrypted file must have been cleaned up.
        mock_delete.assert_called_once_with("/tmp/orphaned.enc")

    def test_job_enqueue_failure_does_not_raise(self):
        """A broken JobQueue must not take down the upload — the row is
        already committed and ops can re-enqueue from the admin dashboard.
        """
        import asyncio

        draft_id = uuid.uuid4()
        version_id = uuid.uuid4()
        user_uuid = uuid.UUID("66666666-6666-6666-6666-666666666666")
        now = datetime.now(UTC)
        draft_row = (
            draft_id,
            user_uuid,
            uuid.UUID("77777777-7777-7777-7777-777777777777"),
            "Test eelnõu",
            "eelnou.docx",
            "application/pdf",
            4,
            "/tmp/ok.enc",
            "pending",
            "uploaded",
            None,
            None,
            None,
            now,
            now,
            now,  # last_accessed_at (#572)
            "eelnou",  # doc_type (#639)
            None,  # parent_vtk_id (#639)
            None,  # processing_completed_at (#670)
        )
        version_row = (
            version_id,
            draft_id,
            1,
            "vtk",
            None,
            "/tmp/ok.enc",
            f"https://data.riik.ee/ontology/estleg/drafts/{draft_id}",
            "uploaded",
            now,
            user_uuid,
        )
        mock_conn = _wire_upload_conn(draft_row=draft_row, version_row=version_row)

        stored = MagicMock(
            storage_path="/tmp/ok.enc",
            size_bytes=4,
            filename="eelnou.docx",
        )
        mock_queue = MagicMock()
        mock_queue.enqueue.side_effect = RuntimeError("queue down")

        with patch("app.docs.upload.store_file", return_value=stored):
            draft = asyncio.run(
                handle_upload(
                    _user(),
                    "Test eelnõu",
                    _StubUpload(
                        filename="eelnou.pdf",
                        content_type="application/pdf",
                        contents=_PDF_BYTES,
                    ),
                    job_queue=mock_queue,
                    conn_factory=_make_conn_factory(mock_conn),
                )
            )

        # We still get the draft back.
        assert draft.id == draft_id
        # Enqueue attempted once, failed silently.
        mock_queue.enqueue.assert_called_once()
