"""Regression sweep for chat-core review findings (#861).

Covers three findings in ``app.chat.models`` / ``app.chat.handlers`` /
``app.chat.audit``:

A. A decrypt failure on ``content_encrypted`` must render a *visible*
   Estonian sentinel rather than a silently-empty body, while a NULL
   ``content_encrypted`` keeps raising (the encryption-at-rest invariant).
B. Message ordering carries an ``id`` tiebreaker and the delete /
   fork boundaries use the compound ``(created_at, id)`` tuple so they
   agree with that ordering when rows share a ``created_at`` tick.
C. The transcript-export action is audited, and the message-send audit
   helper is retained as the correct (currently unwired) primitive.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from cryptography.fernet import Fernet

from app.chat.audit import log_chat_transcript_export
from app.chat.models import (
    _UNDECRYPTABLE_CONTENT,
    _row_to_message,
    delete_messages_after,
    delete_messages_from,
    list_messages,
)

_CONV_ID = uuid.UUID("77777777-7777-7777-7777-777777777777")


@pytest.fixture(autouse=True)
def _fernet_key(monkeypatch: pytest.MonkeyPatch) -> None:
    """Install a deterministic Fernet key for the encryption-path tests."""
    monkeypatch.setenv("APP_ENV", "development")
    monkeypatch.setenv("STORAGE_ENCRYPTION_KEY", Fernet.generate_key().decode())
    import app.storage.encrypted as encrypted_module

    monkeypatch.setattr(encrypted_module, "_fernet", None)


def _message_row(content_encrypted: Any) -> tuple[Any, ...]:
    """Build a minimal v017-shaped ``messages`` row for ``_row_to_message``."""
    now = datetime.now(UTC)
    return (
        uuid.uuid4(),  # id
        _CONV_ID,  # conversation_id
        "assistant",  # role
        None,  # tool_name
        None,  # tokens_input
        None,  # tokens_output
        None,  # model
        now,  # created_at
        content_encrypted,
        None,  # tool_input_encrypted
        None,  # tool_output_encrypted
        None,  # rag_context_encrypted
        False,  # is_pinned
        False,  # is_truncated
    )


# ---------------------------------------------------------------------------
# Finding A — decryption sentinel
# ---------------------------------------------------------------------------


class TestDecryptionSentinel:
    def test_decrypt_failure_renders_visible_sentinel(self):
        """A non-NULL ciphertext that cannot be decrypted (e.g. encrypted
        under a now-rotated-away key) must surface the Estonian sentinel,
        NOT an empty string (#861)."""
        # Ciphertext from a *different* key — the installed key cannot
        # decrypt it, so ``decrypt_text`` raises ``DecryptionError``.
        foreign = Fernet(Fernet.generate_key()).encrypt(b"salajane sisu")

        msg = _row_to_message(_message_row(foreign))

        assert msg.content == _UNDECRYPTABLE_CONTENT
        assert msg.content != ""

    def test_sentinel_is_estonian_and_visible(self):
        assert _UNDECRYPTABLE_CONTENT == "[sõnumit ei õnnestunud dekrüpteerida]"

    def test_null_content_still_raises(self):
        """The NULL path keeps its hard error — that is an invariant
        violation (a regression that re-introduces a NULL write), distinct
        from an operational decrypt failure."""
        with pytest.raises(ValueError, match="content_encrypted IS NULL"):
            _row_to_message(_message_row(None))

    def test_valid_ciphertext_round_trips(self):
        """Positive control: a ciphertext under the installed key decodes
        to its plaintext, never the sentinel."""
        from app.storage import encrypt_text

        msg = _row_to_message(_message_row(encrypt_text("§ 1. Tekst")))

        assert msg.content == "§ 1. Tekst"
        assert msg.content != _UNDECRYPTABLE_CONTENT


# ---------------------------------------------------------------------------
# Finding B — ordering tiebreaker + compound boundaries
# ---------------------------------------------------------------------------


class TestOrderingTiebreaker:
    def test_list_messages_orders_by_created_at_then_id(self):
        """The read ordering must include the ``id`` tiebreaker so same-tick
        rows have a stable, total order (#861)."""
        conn = MagicMock()
        conn.execute.return_value.fetchall.return_value = []

        list_messages(conn, _CONV_ID)

        sql = conn.execute.call_args.args[0]
        assert "ORDER BY created_at ASC, id ASC" in sql

    def test_delete_after_boundary_is_compound_and_keeps_same_tick_predecessor(self):
        """``delete_messages_after`` must compare the compound tuple so a
        same-tick row that sorts *before* the boundary id is preserved."""
        conn = MagicMock()
        cursor = MagicMock()
        cursor.rowcount = 1
        conn.execute.return_value = cursor
        boundary_ts = datetime(2026, 4, 14, 10, 0, 0, tzinfo=UTC)
        boundary_id = uuid.uuid4()

        delete_messages_after(conn, _CONV_ID, boundary_ts, boundary_id)

        sql, params = conn.execute.call_args.args
        assert "(created_at, id) > (%s, %s)" in sql
        assert params == (str(_CONV_ID), boundary_ts, str(boundary_id))

    def test_delete_from_boundary_is_compound_inclusive(self):
        conn = MagicMock()
        cursor = MagicMock()
        cursor.rowcount = 2
        conn.execute.return_value = cursor
        boundary_ts = datetime(2026, 4, 14, 10, 0, 0, tzinfo=UTC)
        boundary_id = uuid.uuid4()

        delete_messages_from(conn, _CONV_ID, boundary_ts, boundary_id)

        sql, params = conn.execute.call_args.args
        assert "(created_at, id) >= (%s, %s)" in sql
        assert params == (str(_CONV_ID), boundary_ts, str(boundary_id))


# ---------------------------------------------------------------------------
# Finding C — transcript-export audit
# ---------------------------------------------------------------------------


class TestTranscriptExportAudit:
    @patch("app.chat.audit.log_action")
    def test_export_audit_payload(self, mock_log):
        user_id = uuid.uuid4()
        conv_id = uuid.uuid4()

        log_chat_transcript_export(user_id, conv_id, "md")

        mock_log.assert_called_once()
        args = mock_log.call_args[0]
        assert args[0] == str(user_id)
        assert args[1] == "chat.conversation.export"
        assert args[2]["conversation_id"] == str(conv_id)
        assert args[2]["format"] == "md"

    @patch("app.chat.audit.log_action")
    def test_export_audit_none_user(self, mock_log):
        log_chat_transcript_export(None, uuid.uuid4(), "docx")
        assert mock_log.call_args[0][0] is None
        assert mock_log.call_args[0][2]["format"] == "docx"
