"""At-rest encryption tests for ``app.chat.models`` (#570).

These tests focus on the encryption boundary itself — independent of the
generic CRUD tests in ``test_chat_models.py``. Every test uses a real
Fernet key and asserts three properties for each encrypted column:

    1. Round-trip — plaintext in via ``create_message`` emerges identical
       from a ``_row_to_message`` read.
    2. Ciphertext is opaque — the raw bytes handed to ``conn.execute`` are
       not the UTF-8 encoding of the plaintext.
    3. Fallback — a row with only plaintext columns populated (simulating
       a row that predates the #570 backfill) still decodes correctly.
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime
from typing import Any
from unittest.mock import MagicMock

import pytest
from cryptography.fernet import Fernet

from app.chat.models import Message, _row_to_message, create_message
from app.storage import decrypt_text


@pytest.fixture(autouse=True)
def _fernet_key(monkeypatch: pytest.MonkeyPatch) -> None:
    """Install a deterministic Fernet key for every test in this module."""
    monkeypatch.setenv("APP_ENV", "development")
    monkeypatch.setenv("STORAGE_ENCRYPTION_KEY", Fernet.generate_key().decode())
    import app.storage.encrypted as encrypted_module

    monkeypatch.setattr(encrypted_module, "_fernet", None)


_CONV_ID = uuid.UUID("33333333-3333-3333-3333-333333333333")


def _echo_row(conn_mock: MagicMock) -> list[Any]:
    """Capture the INSERT params and build a matching row for ``fetchone``.

    Migration 026 dropped the plaintext payload columns. Migration 036
    (#315) added ``tool_use_id`` + ``parent_message_id``. Migration 037
    (#352) added ``ontology_version``. The INSERT now binds 13
    positional params and ``_MESSAGE_COLUMNS`` is 17 wide.
    """
    params = conn_mock.execute.call_args.args[1]
    (
        _conversation_id,
        role,
        tool_name,
        tokens_input,
        tokens_output,
        model,
        content_ct,
        tool_input_ct,
        tool_output_ct,
        rag_context_ct,
        tool_use_id,
        parent_message_id,
        ontology_version,
    ) = params
    now = datetime.now(UTC)
    return [
        uuid.uuid4(),
        _CONV_ID,
        role,
        tool_name,
        tokens_input,
        tokens_output,
        model,
        now,
        content_ct,
        tool_input_ct,
        tool_output_ct,
        rag_context_ct,
        False,  # is_pinned (v017)
        False,  # is_truncated (v017)
        tool_use_id,
        parent_message_id,
        ontology_version,
    ]


class TestContentRoundTrip:
    def test_plaintext_content_is_recovered_via_decrypt(self):
        conn = MagicMock()
        plaintext = "§ 1. Seadus reguleerib SENSITIVE_DATA_12345."

        # First call is the INSERT — fetchone returns the encrypted row we
        # reconstruct from the bound params.
        def fetchone_side_effect():
            return _echo_row(conn)

        conn.execute.return_value.fetchone.side_effect = fetchone_side_effect

        result = create_message(conn, _CONV_ID, "user", plaintext)

        assert isinstance(result, Message)
        assert result.content == plaintext

    def test_ciphertext_is_not_plaintext(self):
        conn = MagicMock()
        conn.execute.return_value.fetchone.side_effect = lambda: _echo_row(conn)
        plaintext = "SENSITIVE_DATA_12345 — eelnõu §5 lõige 2"

        create_message(conn, _CONV_ID, "user", plaintext)

        params = conn.execute.call_args.args[1]
        content_ciphertext = params[6]

        assert isinstance(content_ciphertext, bytes)
        # Security: the literal plaintext must NEVER appear in the blob.
        assert b"SENSITIVE_DATA_12345" not in content_ciphertext
        assert plaintext.encode("utf-8") not in content_ciphertext
        # Sanity: the key we installed must decrypt what we wrote.
        assert decrypt_text(content_ciphertext) == plaintext


class TestJsonColumnRoundTrip:
    def test_tool_input_round_trip(self):
        conn = MagicMock()
        conn.execute.return_value.fetchone.side_effect = lambda: _echo_row(conn)
        tool_input = {"query": "SELECT ?s WHERE { ?s ?p ?o }", "limit": 10}

        result = create_message(
            conn,
            _CONV_ID,
            "tool",
            "Tool result",
            tool_name="query_ontology",
            tool_input=tool_input,
        )

        assert result.tool_input == tool_input
        params = conn.execute.call_args.args[1]
        tool_input_ct = params[7]
        assert isinstance(tool_input_ct, bytes)
        assert decrypt_text(tool_input_ct) == json.dumps(tool_input, ensure_ascii=False)

    def test_tool_output_round_trip(self):
        conn = MagicMock()
        conn.execute.return_value.fetchone.side_effect = lambda: _echo_row(conn)
        tool_output = {"results": [{"s": "x", "p": "y"}], "count": 1}

        result = create_message(
            conn,
            _CONV_ID,
            "tool",
            "Tool result",
            tool_name="query_ontology",
            tool_output=tool_output,
        )

        assert result.tool_output == tool_output
        params = conn.execute.call_args.args[1]
        tool_output_ct = params[8]
        assert isinstance(tool_output_ct, bytes)
        assert b"results" not in tool_output_ct  # opaque

    def test_rag_context_round_trip(self):
        conn = MagicMock()
        conn.execute.return_value.fetchone.side_effect = lambda: _echo_row(conn)
        rag = [{"chunk_id": "abc", "text": "Tsiviilseadustiku § 1"}]

        result = create_message(
            conn,
            _CONV_ID,
            "assistant",
            "Answer",
            rag_context=rag,
        )

        assert result.rag_context == rag
        params = conn.execute.call_args.args[1]
        rag_ct = params[9]
        assert isinstance(rag_ct, bytes)
        assert b"Tsiviilseadustiku" not in rag_ct


class TestNullJsonColumns:
    def test_none_json_columns_stay_null(self):
        conn = MagicMock()
        conn.execute.return_value.fetchone.side_effect = lambda: _echo_row(conn)

        result = create_message(conn, _CONV_ID, "user", "plain")
        params = conn.execute.call_args.args[1]
        # content is always encrypted; the three JSONB ciphertexts are NULL.
        assert params[6] is not None
        assert params[7] is None
        assert params[8] is None
        assert params[9] is None
        assert result.tool_input is None
        assert result.tool_output is None
        assert result.rag_context is None


class TestEncryptionInvariant:
    """Migration 026 dropped the plaintext fallback. ``_row_to_message``
    must refuse a row whose ``content_encrypted`` is NULL so a future
    regression cannot silently serve empty message bodies (#687)."""

    def test_null_content_encrypted_raises(self):
        from app.chat.models import _row_to_message

        now = datetime.now(UTC)
        row = (
            uuid.uuid4(),
            _CONV_ID,
            "user",
            None,  # tool_name
            None,  # tokens_input
            None,  # tokens_output
            None,  # model
            now,
            None,  # content_encrypted — NULL is the regression
            None,
            None,
            None,
            False,
            False,
        )

        with pytest.raises(ValueError, match="content_encrypted IS NULL"):
            _row_to_message(row)

    def test_encrypted_only_row_decodes_cleanly(self):
        """Positive control: a row with only encrypted columns reads back
        as the original plaintext, no plaintext column required."""
        from app.storage import encrypt_text

        now = datetime.now(UTC)
        row = (
            uuid.uuid4(),
            _CONV_ID,
            "user",
            None,
            None,
            None,
            None,
            now,
            encrypt_text("authoritative ciphertext"),
            None,
            None,
            None,
            False,
            False,
        )

        msg = _row_to_message(row)
        assert msg.content == "authoritative ciphertext"
        assert msg.tool_input is None


class TestSecurityInspection:
    """NFR §6.1: the plaintext must NEVER surface in the encrypted blob."""

    def test_sensitive_marker_absent_from_all_ciphertext(self):
        conn = MagicMock()
        conn.execute.return_value.fetchone.side_effect = lambda: _echo_row(conn)

        marker = "SENSITIVE_DATA_12345"
        tool_input = {"query": marker}
        tool_output = {"echo": marker}
        rag_context = [{"chunk_id": "x", "text": marker}]

        create_message(
            conn,
            _CONV_ID,
            "tool",
            marker,
            tool_name="query_ontology",
            tool_input=tool_input,
            tool_output=tool_output,
            rag_context=rag_context,
        )

        params = conn.execute.call_args.args[1]
        # Indices 6..9 are the four encrypted columns.
        for ct in params[6:10]:
            assert ct is not None
            assert isinstance(ct, bytes)
            assert marker.encode("utf-8") not in ct, (
                "Plaintext marker leaked into ciphertext column — Fernet not applied"
            )
