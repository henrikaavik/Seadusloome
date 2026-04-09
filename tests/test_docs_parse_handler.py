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
        parsed_text=None,
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

        # 3. The parsed_text + status='extracting' UPDATE must have run
        #    on one of the connections.
        updated_ids = []
        for conn in p.conns:
            for call in conn.execute.call_args_list:
                sql = call.args[0]
                if "parsed_text" in sql and "extracting" in sql:
                    updated_ids.append(call.args[1][1])
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

        # parsing → failed transition
        status_calls = [c.args[2] for c in p.m_update_draft_status.call_args_list]
        assert "parsing" in status_calls
        assert "failed" in status_calls

        # error_message must mention "empty text"
        failed_call = next(
            c for c in p.m_update_draft_status.call_args_list if c.args[2] == "failed"
        )
        assert "empty text" in failed_call.kwargs["error_message"]

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
        status_calls = [c.args[2] for c in p.m_update_draft_status.call_args_list]
        assert "parsing" in status_calls
        assert "failed" not in status_calls
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

        status_calls = [c.args[2] for c in p.m_update_draft_status.call_args_list]
        assert "failed" in status_calls
        failed_call = next(
            c for c in p.m_update_draft_status.call_args_list if c.args[2] == "failed"
        )
        assert "connect refused" in failed_call.kwargs["error_message"]
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

        status_calls = [c.args[2] for c in p.m_update_draft_status.call_args_list]
        assert "parsing" in status_calls
        assert "failed" not in status_calls

    def test_tika_timeout_error_message_is_truncated_to_500(self):
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

        failed_call = next(
            c for c in p.m_update_draft_status.call_args_list if c.args[2] == "failed"
        )
        assert len(failed_call.kwargs["error_message"]) == 500


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

        status_calls = [c.args[2] for c in p.m_update_draft_status.call_args_list]
        assert "failed" in status_calls
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

        status_calls = [c.args[2] for c in p.m_update_draft_status.call_args_list]
        assert "failed" in status_calls
        failed_call = next(
            c for c in p.m_update_draft_status.call_args_list if c.args[2] == "failed"
        )
        assert "invalid token" in failed_call.kwargs["error_message"]


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

        status_calls = [c.args[2] for c in p.m_update_draft_status.call_args_list]
        assert "failed" in status_calls

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

        status_calls = [c.args[2] for c in p.m_update_draft_status.call_args_list]
        assert "failed" not in status_calls
