"""Unit tests for :mod:`app.docs.parse_handler`.

These tests never touch a real Postgres connection, a real Tika
container, or a real encrypted file on disk. Every external
collaborator is patched via ``unittest.mock``:

    - ``app.docs.parse_handler.get_connection``  → fake context manager
    - ``app.docs.parse_handler.get_draft``       → returns a ``Draft`` stub
    - ``app.docs.parse_handler.update_draft_status``
    - ``app.docs.parse_handler.read_file``       → returns decrypted bytes
    - ``app.docs.parse_handler.get_default_tika_client``
    - ``app.docs.parse_handler.JobQueue``        → fake enqueue()

The pattern mirrors ``tests/test_sync_orchestrator.py`` — one handler
call per test, with an assertion matrix on the mocks to prove the
correct state transitions fired.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from unittest.mock import MagicMock, patch

import pytest

from app.docs.draft_model import Draft
from app.docs.error_mapping import (
    MSG_FILE_MISSING,
    MSG_TIKA_CAPACITY,
    MSG_UNKNOWN,
)
from app.docs.parse_handler import parse_draft
from app.docs.tika_client import TikaError
from app.storage import DecryptionError

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


@dataclass
class _FakeConn:
    """Minimal psycopg-like connection that records executed SQL.

    The handler uses three styles of DB access:
      1. ``with get_connection() as conn: get_draft(conn, id)``
      2. ``with get_connection() as conn: update_draft_status(conn, id, ...); conn.commit()``
      3. ``with get_connection() as conn: conn.execute(<UPDATE ...>)``

    This stub supports all three via a MagicMock ``execute`` attribute
    so tests can assert on ``call_args_list``.
    """

    def __init__(self):
        self.execute = MagicMock()
        self.commit = MagicMock()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False


def _make_draft(draft_id: uuid.UUID | None = None) -> Draft:
    """Return a realistic ``Draft`` stub."""
    now = datetime.now(UTC)
    return Draft(
        id=draft_id or uuid.uuid4(),
        user_id=uuid.uuid4(),
        org_id=uuid.uuid4(),
        title="Tsiviilseadustiku muudatused 2026",
        filename="draft.pdf",
        content_type="application/pdf",
        file_size=1234,
        storage_path="/tmp/fake/abcd.enc",
        graph_uri="urn:draft:test",
        status="uploaded",
        parsed_text_encrypted=None,
        entity_count=None,
        error_message=None,
        created_at=now,
        updated_at=now,
    )


class _Patches:
    """Container bundling every mock used by the parse handler tests.

    Using a class keeps the per-test ``with contextlib.ExitStack`` blocks
    concise: just ``with _Patches() as p: ...``.
    """

    def __init__(self):
        self.get_connection = patch("app.docs.parse_handler.get_connection")
        self.get_draft = patch("app.docs.parse_handler.get_draft")
        self.update_draft_status = patch("app.docs.parse_handler.update_draft_status")
        self.read_file = patch("app.docs.parse_handler.read_file")
        self.tika = patch("app.docs.parse_handler.get_default_tika_client")
        self.queue_cls = patch("app.docs.parse_handler.JobQueue")

    def __enter__(self):
        self.m_get_connection = self.get_connection.start()
        self.m_get_draft = self.get_draft.start()
        self.m_update_draft_status = self.update_draft_status.start()
        self.m_read_file = self.read_file.start()
        self.m_tika = self.tika.start()
        self.m_queue_cls = self.queue_cls.start()

        # Default: connection factory returns a fresh _FakeConn per call.
        # Tests that need to inspect SQL can override this with a fixed
        # stub they can introspect.
        self.conns: list[_FakeConn] = []

        def _conn_factory(*_args, **_kwargs):
            conn = _FakeConn()
            self.conns.append(conn)
            return conn

        self.m_get_connection.side_effect = _conn_factory

        # Default Tika client: returns a non-empty text so the happy
        # path "just works" unless a test overrides the return_value.
        self.m_tika_client = MagicMock()
        self.m_tika_client.extract_text.return_value = "parsed draft body text"
        self.m_tika.return_value = self.m_tika_client

        # Default job queue
        self.m_queue = MagicMock()
        self.m_queue_cls.return_value = self.m_queue

        return self

    def __exit__(self, exc_type, exc, tb):
        self.get_connection.stop()
        self.get_draft.stop()
        self.update_draft_status.stop()
        self.read_file.stop()
        self.tika.stop()
        self.queue_cls.stop()
        return False


# ---------------------------------------------------------------------------
# Assertion helpers
# ---------------------------------------------------------------------------


def _failed_update_params(conns: list[_FakeConn]) -> tuple | None:
    """Return the params of the first ``update drafts set status='failed'`` call.

    ``_mark_draft_failed`` runs a direct SQL UPDATE so tests have to
    introspect the connection's ``execute.call_args_list`` rather than
    the ``update_draft_status`` mock.
    """
    for conn in conns:
        for call in conn.execute.call_args_list:
            sql = call.args[0].lower()
            if "update drafts" in sql and "status = 'failed'" in sql:
                return call.args[1]
    return None


def _statuses_written(conns: list[_FakeConn], update_draft_status_mock) -> list[str]:
    """Return every draft status written by the handler under test.

    Covers both:
      - ``update_draft_status(conn, id, <status>)`` (``parsing``
        transition is routed through the draft_model helper)
      - the direct SQL UPDATEs for the ``extracting`` and ``failed``
        transitions.
    """
    statuses = [c.args[2] for c in update_draft_status_mock.call_args_list]
    for conn in conns:
        for call in conn.execute.call_args_list:
            sql = call.args[0].lower()
            if "update drafts" not in sql:
                continue
            if "status = 'failed'" in sql:
                statuses.append("failed")
            elif "status = 'extracting'" in sql:
                statuses.append("extracting")
            elif "status = 'ready'" in sql:
                statuses.append("ready")
    return statuses


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


class TestParseDraftHappyPath:
    def test_happy_path_transitions_and_enqueues(self):
        draft = _make_draft()

        with _Patches() as p:
            p.m_get_draft.return_value = draft
            p.m_read_file.return_value = b"fake decrypted bytes"
            p.m_tika_client.extract_text.return_value = "extracted legal text" * 10

            result = parse_draft({"draft_id": str(draft.id)})

        # 1. Tika was called with the file bytes + content-type
        p.m_tika_client.extract_text.assert_called_once_with(
            b"fake decrypted bytes",
            "application/pdf",
        )

        # 2. update_draft_status was called once with "parsing"
        #    (the second transition, to "extracting", goes via direct
        #    conn.execute because it needs to write parsed_text too).
        status_calls = [c.args[2] for c in p.m_update_draft_status.call_args_list]
        # No "failed" should appear on the happy path.
        assert "parsing" in status_calls
        assert "failed" not in status_calls

        # 3. The parsed_text_encrypted + status='extracting' UPDATE must
        #    have run on one of the connections; the first param must be
        #    bytes (Fernet ciphertext), not a plain string.
        updated_ids = []
        for conn in p.conns:
            for call in conn.execute.call_args_list:
                sql = call.args[0]
                if "parsed_text_encrypted" in sql and "extracting" in sql:
                    params = call.args[1]
                    assert isinstance(params[0], bytes), (
                        "parsed_text_encrypted must be bytes (Fernet ciphertext), "
                        f"got {type(params[0])}"
                    )
                    updated_ids.append(params[1])
        assert str(draft.id) in updated_ids

        # 4. extract_entities was enqueued with the draft id.
        p.m_queue.enqueue.assert_called_once()
        call_args = p.m_queue.enqueue.call_args
        assert call_args.args[0] == "extract_entities"
        assert call_args.args[1] == {"draft_id": str(draft.id)}

        # 5. Return value carries the expected keys.
        assert result["draft_id"] == str(draft.id)
        assert result["next_job"] == "extract_entities"
        assert result["text_length"] == len("extracted legal text" * 10)

    def test_happy_path_strips_old_error_message(self):
        """On success, the parsed_text UPDATE must also clear error_message."""
        draft = _make_draft()

        with _Patches() as p:
            p.m_get_draft.return_value = draft
            p.m_read_file.return_value = b"data"
            p.m_tika_client.extract_text.return_value = "parsed"

            parse_draft({"draft_id": str(draft.id)})

            # At least one execute call must null out error_message.
            found = False
            for conn in p.conns:
                for call in conn.execute.call_args_list:
                    sql = call.args[0]
                    if "error_message = null" in sql:
                        found = True
                        break
            assert found, "Successful parse must clear error_message"


# ---------------------------------------------------------------------------
# Failure: draft missing
# ---------------------------------------------------------------------------


class TestMissingDraft:
    def test_missing_draft_raises_value_error(self):
        fake_id = uuid.uuid4()

        with _Patches() as p:
            p.m_get_draft.return_value = None

            with pytest.raises(ValueError, match="not found"):
                parse_draft({"draft_id": str(fake_id)})

        # No status updates, no Tika call, no enqueue.
        p.m_update_draft_status.assert_not_called()
        p.m_tika_client.extract_text.assert_not_called()
        p.m_queue.enqueue.assert_not_called()

    def test_missing_payload_draft_id_raises(self):
        with _Patches() as p:
            with pytest.raises(ValueError, match="missing"):
                parse_draft({})

        p.m_get_draft.assert_not_called()


# ---------------------------------------------------------------------------
# Failure: Tika returned empty text
# ---------------------------------------------------------------------------


class TestEmptyTikaResult:
    def test_empty_text_marks_failed_on_final_attempt(self):
        """On the final attempt the handler must flip the draft to failed."""
        draft = _make_draft()

        with _Patches() as p:
            p.m_get_draft.return_value = draft
            p.m_read_file.return_value = b"data"
            p.m_tika_client.extract_text.return_value = "   \n\t  "  # whitespace only

            with pytest.raises(ValueError, match="empty text"):
                parse_draft(
                    {"draft_id": str(draft.id)},
                    attempt=3,
                    max_attempts=3,
                )

        # parsing → failed transition (parsing via update_draft_status,
        # failed via direct SQL UPDATE in _mark_draft_failed).
        statuses = _statuses_written(p.conns, p.m_update_draft_status)
        assert "parsing" in statuses
        assert "failed" in statuses

        # #609: the stored error_message must be the mapped Estonian
        # string, and error_debug must carry the raw ValueError text.
        params = _failed_update_params(p.conns)
        assert params is not None, "expected an UPDATE ... status='failed' call"
        user_msg, debug_detail, _draft_id = params
        # "empty text" is not a currently mapped failure mode, so it
        # falls back to the generic MSG_UNKNOWN message.
        assert user_msg == MSG_UNKNOWN
        assert "empty text" in debug_detail

        # No follow-up job was enqueued.
        p.m_queue.enqueue.assert_not_called()

    def test_empty_text_does_not_mark_failed_when_retry_pending(self):
        """#448: early attempts must NOT flip the draft to failed.

        The user should not see ``Ebaõnnestus`` while the queue still
        has retry budget left — only the final attempt commits to a
        permanent failure state.
        """
        draft = _make_draft()

        with _Patches() as p:
            p.m_get_draft.return_value = draft
            p.m_read_file.return_value = b"data"
            p.m_tika_client.extract_text.return_value = "   \n\t  "

            with pytest.raises(ValueError, match="empty text"):
                parse_draft(
                    {"draft_id": str(draft.id)},
                    attempt=1,
                    max_attempts=3,
                )

        # parsing → (NOT failed) — the handler held off so the next
        # retry can pick up cleanly.
        statuses = _statuses_written(p.conns, p.m_update_draft_status)
        assert "parsing" in statuses
        assert "failed" not in statuses
        p.m_queue.enqueue.assert_not_called()


# ---------------------------------------------------------------------------
# Failure: Tika raised
# ---------------------------------------------------------------------------


class TestTikaFailure:
    def test_tika_error_marks_failed_on_final_attempt(self):
        draft = _make_draft()

        with _Patches() as p:
            p.m_get_draft.return_value = draft
            p.m_read_file.return_value = b"data"
            p.m_tika_client.extract_text.side_effect = TikaError("connect refused")

            with pytest.raises(TikaError, match="connect refused"):
                parse_draft(
                    {"draft_id": str(draft.id)},
                    attempt=3,
                    max_attempts=3,
                )

        statuses = _statuses_written(p.conns, p.m_update_draft_status)
        assert "failed" in statuses
        # #609: raw exception text goes to error_debug, user-facing
        # Estonian string goes to error_message.
        params = _failed_update_params(p.conns)
        assert params is not None
        user_msg, debug_detail, _ = params
        assert "connect refused" in debug_detail
        # "connect refused" is not one of the mapped markers, so the
        # user sees the generic fallback.
        assert user_msg == MSG_UNKNOWN
        p.m_queue.enqueue.assert_not_called()

    def test_tika_error_does_not_mark_failed_when_retry_pending(self):
        """#448: a transient TikaError on attempt 1 must not flip the draft."""
        draft = _make_draft()

        with _Patches() as p:
            p.m_get_draft.return_value = draft
            p.m_read_file.return_value = b"data"
            p.m_tika_client.extract_text.side_effect = TikaError("502 Bad Gateway")

            with pytest.raises(TikaError):
                parse_draft(
                    {"draft_id": str(draft.id)},
                    attempt=1,
                    max_attempts=3,
                )

        statuses = _statuses_written(p.conns, p.m_update_draft_status)
        assert "parsing" in statuses
        assert "failed" not in statuses

    def test_tika_user_message_is_always_under_500_chars(self):
        """#609: error_message is capped at 500 chars so routes never
        render a runaway Alert.

        The mapped Estonian strings are all short, so this mostly
        guards against future refactors that forget the slice.
        """
        draft = _make_draft()

        with _Patches() as p:
            p.m_get_draft.return_value = draft
            p.m_read_file.return_value = b"data"
            long_msg = "x" * 1500
            p.m_tika_client.extract_text.side_effect = TikaError(long_msg)

            with pytest.raises(TikaError):
                parse_draft(
                    {"draft_id": str(draft.id)},
                    attempt=3,
                    max_attempts=3,
                )

        params = _failed_update_params(p.conns)
        assert params is not None
        user_msg, _debug, _ = params
        assert len(user_msg) <= 500


# ---------------------------------------------------------------------------
# Failure: storage errors
# ---------------------------------------------------------------------------


class TestStorageFailures:
    def test_missing_file_marks_failed_on_final_attempt(self):
        draft = _make_draft()

        with _Patches() as p:
            p.m_get_draft.return_value = draft
            p.m_read_file.side_effect = FileNotFoundError(
                f"Encrypted file not found: {draft.storage_path}"
            )

            with pytest.raises(FileNotFoundError):
                parse_draft(
                    {"draft_id": str(draft.id)},
                    attempt=3,
                    max_attempts=3,
                )

        statuses = _statuses_written(p.conns, p.m_update_draft_status)
        assert "failed" in statuses
        # #609: FileNotFoundError maps to the "laadige uuesti üles"
        # Estonian message so the user knows the next action.
        params = _failed_update_params(p.conns)
        assert params is not None
        user_msg, _debug, _ = params
        assert user_msg == MSG_FILE_MISSING
        p.m_tika_client.extract_text.assert_not_called()
        p.m_queue.enqueue.assert_not_called()

    def test_decryption_error_marks_failed_on_final_attempt(self):
        draft = _make_draft()

        with _Patches() as p:
            p.m_get_draft.return_value = draft
            p.m_read_file.side_effect = DecryptionError("invalid token")

            with pytest.raises(DecryptionError, match="invalid token"):
                parse_draft(
                    {"draft_id": str(draft.id)},
                    attempt=3,
                    max_attempts=3,
                )

        statuses = _statuses_written(p.conns, p.m_update_draft_status)
        assert "failed" in statuses
        params = _failed_update_params(p.conns)
        assert params is not None
        _user_msg, debug_detail, _ = params
        # Raw error text must be preserved in error_debug for admins.
        assert "invalid token" in debug_detail


# ---------------------------------------------------------------------------
# Unexpected exceptions still mark draft failed
# ---------------------------------------------------------------------------


class TestUnexpectedException:
    def test_unknown_exception_marks_failed_on_final_attempt(self):
        draft = _make_draft()

        with _Patches() as p:
            p.m_get_draft.return_value = draft
            p.m_read_file.return_value = b"data"
            p.m_tika_client.extract_text.side_effect = RuntimeError("kaboom")

            with pytest.raises(RuntimeError, match="kaboom"):
                parse_draft(
                    {"draft_id": str(draft.id)},
                    attempt=3,
                    max_attempts=3,
                )

        statuses = _statuses_written(p.conns, p.m_update_draft_status)
        assert "failed" in statuses
        # #609: unknown -> generic Estonian fallback + raw debug text.
        params = _failed_update_params(p.conns)
        assert params is not None
        user_msg, debug_detail, _ = params
        assert user_msg == MSG_UNKNOWN
        assert "kaboom" in debug_detail

    def test_unknown_exception_does_not_mark_failed_when_retry_pending(self):
        """#448: belt-and-braces — even unknown exceptions defer."""
        draft = _make_draft()

        with _Patches() as p:
            p.m_get_draft.return_value = draft
            p.m_read_file.return_value = b"data"
            p.m_tika_client.extract_text.side_effect = RuntimeError("transient")

            with pytest.raises(RuntimeError):
                parse_draft(
                    {"draft_id": str(draft.id)},
                    attempt=1,
                    max_attempts=3,
                )

        statuses = _statuses_written(p.conns, p.m_update_draft_status)
        assert "failed" not in statuses

    def test_memory_error_maps_to_capacity_message(self):
        """#609: ``MemoryError`` during Tika parse → capacity Estonian msg."""
        draft = _make_draft()

        with _Patches() as p:
            p.m_get_draft.return_value = draft
            p.m_read_file.return_value = b"data"
            p.m_tika_client.extract_text.side_effect = MemoryError("java heap")

            with pytest.raises(MemoryError):
                parse_draft(
                    {"draft_id": str(draft.id)},
                    attempt=3,
                    max_attempts=3,
                )

        params = _failed_update_params(p.conns)
        assert params is not None
        user_msg, debug_detail, _ = params
        assert user_msg == MSG_TIKA_CAPACITY
        assert "MemoryError" in debug_detail

    def test_encrypted_pdf_maps_to_password_message(self):
        """#609: Tika "encrypted" error → actionable password message."""
        from app.docs.error_mapping import MSG_ENCRYPTED_PDF

        draft = _make_draft()

        with _Patches() as p:
            p.m_get_draft.return_value = draft
            p.m_read_file.return_value = b"data"
            p.m_tika_client.extract_text.side_effect = TikaError("PDFBox: document is encrypted")

            with pytest.raises(TikaError):
                parse_draft(
                    {"draft_id": str(draft.id)},
                    attempt=3,
                    max_attempts=3,
                )

        params = _failed_update_params(p.conns)
        assert params is not None
        user_msg, _debug, _ = params
        assert user_msg == MSG_ENCRYPTED_PDF
